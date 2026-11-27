import AVFoundation
import Foundation
import IOKit.pwr_mgt
import Observation
import SHAFTKit

/// Record a meeting on this Mac and hand it to the server (docs/meetings.md): `MeetingCapture` writes
/// `~/Library/Application Support/SHAFT/Meetings/<stamp>.caf` and its sidecar `<stamp>.json`; on Stop,
/// `MeetingUploader` sends it in chunks to the paired server, which transcribes it. A recording whose upload did not
/// finish (no server, the app quit) is sent again at the next launch or the next Stop; a finished one is kept 7 days.
/// Quitting while recording finishes the file first; a recording cut short by a crash is recovered at the next launch
/// (its CAF trimmed to whole packets) and sent saying so, and one that holds no audio is kept, not sent, and listed in
/// `damaged`. While recording, the Mac does not go to sleep on its own.
@MainActor
@Observable
final class MeetingRecorder {
    enum Phase: Equatable {
        case idle
        case recording(started: Date, online: Bool)
        case uploading
        case sent(String)
        case failed(String)
    }

    private(set) var phase: Phase = .idle
    /// Said by the capture while recording (the system audio silent: the permission is the likely cause), and what
    /// the stopped recording could not keep: shown until the next recording starts, also after it was sent.
    var warning: String? {
        let lines = liveWarnings.sorted { $0.key < $1.key }.map(\.value) + (stopWarning.map { [$0] } ?? [])
        return lines.isEmpty ? nil : lines.joined(separator: " ")
    }
    /// The capture's standing problems by cause, cleared when the cause clears; and what the stopped recording could
    /// not keep.
    private var liveWarnings: [String: String] = [:]
    private var stopWarning: String?
    /// Recordings here that hold no readable audio: kept for the operator (Show in Finder), never sent.
    private(set) var damaged: [URL] = []
    /// The upload under way, for its progress.
    private(set) var uploader: MeetingUploader?
    /// The inputs connected now, and the system default's name, kept current as devices come and go.
    private(set) var microphones: [MeetingMicrophone] = []
    private(set) var defaultMicrophoneName: String?
    /// The microphone the next recording uses, by UID; nil is the system default input. Kept across launches.
    private(set) var microphoneUID: String?
    @ObservationIgnored private(set) var microphoneName: String?
    /// The input the current recording is from, and why it is not the chosen one (kept apart from `warning`, which
    /// the capture may overwrite while recording).
    private(set) var recordingFrom: String?
    private(set) var inputNotice: String?

    @ObservationIgnored private var capture: MeetingCapture?
    @ObservationIgnored private var sendTask: Task<Void, Never>?
    /// Recordings waiting for the send task, in order: a Stop during another upload joins it (review 2026-11-27: it
    /// was dropped, and the menu said the earlier one's "Sent").
    @ObservationIgnored private var sendQueue: [URL] = []
    @ObservationIgnored private var sleepAssertion: IOPMAssertionID = 0
    @ObservationIgnored private var file: URL?
    @ObservationIgnored private let client: @MainActor () -> APIClient?
    @ObservationIgnored let folder: URL

    var isRecording: Bool { if case .recording = phase { true } else { false } }
    /// Set before the permission prompt's await, so a second Record click cannot start a second capture over the first
    /// (review 2026-11-25).
    @ObservationIgnored private var starting = false

    @ObservationIgnored private let defaults: UserDefaults

    /// Where recordings wait until the server has them.
    static let defaultFolder = URL.applicationSupportDirectory.appending(path: "SHAFT/Meetings", directoryHint: .isDirectory)

    init(client: @escaping @MainActor () -> APIClient?,
         folder: URL = MeetingRecorder.defaultFolder,
         defaults: UserDefaults = .standard) {
        self.client = client
        self.folder = folder
        self.defaults = defaults
        microphoneUID = defaults.string(forKey: Self.microphoneUIDKey)
        microphoneName = defaults.string(forKey: Self.microphoneNameKey)
        refreshMicrophones()
        MeetingMicrophones.observe { [weak self] in Task { @MainActor in self?.refreshMicrophones() } }
    }

    /// Choose the microphone for the next recording (nil: the system default).
    func chooseMicrophone(_ uid: String?) {
        microphoneUID = uid
        if let uid, let mic = microphones.first(where: { $0.uid == uid }) { microphoneName = mic.name }
        if uid == nil { microphoneName = nil }
        defaults.set(microphoneUID, forKey: Self.microphoneUIDKey)
        defaults.set(microphoneName, forKey: Self.microphoneNameKey)
    }

    /// The Meeting menu's microphone entries after System Default.
    var microphoneEntries: [MeetingMicrophone] {
        MeetingMicrophones.menuEntries(saved: microphoneUID, savedName: microphoneName, available: microphones)
    }

    func refreshMicrophones() {
        microphones = MeetingMicrophones.connected()
        defaultMicrophoneName = MeetingMicrophones.defaultName()
        // A renamed input keeps its UID: keep the saved name current for when it is unplugged.
        if let uid = microphoneUID, let mic = microphones.first(where: { $0.uid == uid }), mic.name != microphoneName {
            microphoneName = mic.name
            defaults.set(mic.name, forKey: Self.microphoneNameKey)
        }
    }

    static let microphoneUIDKey = "meetings.microphoneUID"
    static let microphoneNameKey = "meetings.microphoneName"

    /// Start recording: online (the microphone and every other app's sound) or in person (the microphone).
    func start(online: Bool) async {
        guard !isRecording, !starting else { return }
        starting = true
        defer { starting = false }
        liveWarnings = [:]
        stopWarning = nil
        guard await AVCaptureDevice.requestAccess(for: .audio) else {
            phase = .failed("SHAFT may not use the microphone: allow it in System Settings → Privacy & Security → Microphone.")
            return
        }
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            // A new name every time (a time stamp and a random suffix): never an earlier recording's, whose upload
            // record would then describe another file (review 2026-11-25).
            let stamp = Self.stamp.string(from: Date()) + "-" + UUID().uuidString.prefix(8).lowercased()
            let url = folder.appending(path: "\(stamp).\(Self.recordingExtension)")
            refreshMicrophones()
            let input = MeetingMicrophones.resolve(saved: microphoneUID, savedName: microphoneName, available: microphones)
            let capture = MeetingCapture(online: online, url: url, stallSeconds: 20, inputUID: input.uid)
            capture.onWarning = { [weak self] cause, message in
                Task { @MainActor in self?.liveWarnings[cause] = message }
            }
            try capture.start()
            self.capture = capture
            file = url
            holdSleep(true)
            recordingFrom = capture.inputName
            inputNotice = input.warning ?? capture.inputFallback
            phase = .recording(started: Date(), online: online)
        } catch {
            phase = .failed("Could not start recording: \(error.localizedDescription)")
        }
    }

    /// Stop, finalise the file, and send it.
    func stop() async {
        guard let file = finishCapture() else { return }
        startSending([file])
    }

    /// The app is quitting: finish the file so it stays readable; it is sent at the next launch.
    func finishForQuit() {
        _ = finishCapture()
    }

    /// Stop the capture and write the sidecar; the file, or nil when nothing was recording.
    private func finishCapture() -> URL? {
        guard case .recording = phase, let capture, let file else { return nil }
        let sidecar = capture.stop()            // also writes <stamp>.json
        self.capture = nil
        self.file = nil
        holdSleep(false)
        recordingFrom = nil
        inputNotice = nil
        phase = .idle
        liveWarnings = [:]
        if let problems = sidecar["problems"] as? [String], !problems.isEmpty {
            stopWarning = "The recording is incomplete: " + problems.joined(separator: "; ") + "."
        }
        return file
    }

    /// Send every recording here whose upload has not finished (the server confirms a finished one at once).
    func sendPending() async {
        guard !isRecording, sendTask == nil else { return }
        startSending(pendingFiles())
        await sendTask?.value
        try? MeetingUploader.pruneUploaded(in: folder)
    }

    /// Stop the upload under way; the recording stays here and is sent again at the next launch or Send Again.
    func cancelUpload() {
        sendTask?.cancel()
    }

    private func startSending(_ files: [URL]) {
        sendQueue += files.filter { !sendQueue.contains($0) }
        guard sendTask == nil, !sendQueue.isEmpty else { return }
        sendTask = Task { [weak self] in
            while let self, !Task.isCancelled, !self.sendQueue.isEmpty {
                await self.send(self.sendQueue.removeFirst())
            }
            guard let self else { return }
            self.sendTask = nil
            self.uploader = nil
            if Task.isCancelled {
                self.sendQueue = []                 // still here: sent at the next launch or Send Again
                if !self.isRecording { self.phase = .failed("Sending stopped. It is sent again at the next launch.") }
            }
        }
    }

    /// The recordings left to send, oldest first: a finished one as it is, one a crash cut short recovered first, and
    /// one that holds no audio kept out (in `damaged`).
    func pendingFiles() -> [URL] {
        let files = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: [.creationDateKey])) ?? []
        var ready: [URL] = []
        var bad: [URL] = []
        for file in files.filter({ Self.audioExtensions.contains($0.pathExtension) }).sorted(by: { $0.path < $1.path }) {
            guard file != self.file else { continue }
            if MeetingCapture.recover(file) { ready.append(file) } else { bad.append(file) }
        }
        damaged = bad
        return ready
    }

    /// New recordings are IMA4 CAF; .m4a ones from older builds are still sent.
    static let recordingExtension = "caf"
    static let audioExtensions: Set<String> = ["caf", "m4a"]

    /// No idle sleep while recording (review 2026-11-27): closing the lid still sleeps, and the time asleep becomes a
    /// gap in the recording.
    private func holdSleep(_ hold: Bool) {
        if hold, sleepAssertion == 0 {
            IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleSystemSleep as CFString,
                                        IOPMAssertionLevel(kIOPMAssertionLevelOn), "SHAFT is recording a meeting" as CFString,
                                        &sleepAssertion)
        } else if !hold, sleepAssertion != 0 {
            IOPMAssertionRelease(sleepAssertion)
            sleepAssertion = 0
        }
    }

    private func send(_ file: URL) async {
        guard let client = client() else {
            if !isRecording {
                phase = .failed("Recorded, not sent: pair this Mac with your SHAFT server. It is sent at the next launch.")
            }
            return
        }
        let side = file.deletingPathExtension().appendingPathExtension("json")
        let sidecar = (try? Data(contentsOf: side)) ?? Data("{}".utf8)
        let fields = (try? JSONSerialization.jsonObject(with: sidecar)) as? [String: Any] ?? [:]
        let iso = ISO8601DateFormatter()
        let started = (fields["started"] as? String).flatMap(iso.date(from:)) ?? Date()
        let ended = (fields["ended"] as? String).flatMap(iso.date(from:))
        let uploader = MeetingUploader(client: client, audioURL: file)
        self.uploader = uploader
        // A recording started meanwhile keeps its phase: the upload goes on beside it (review 2026-11-27: Record
        // stayed disabled for as long as an upload kept retrying).
        if !isRecording { phase = .uploading }
        let aliases = try? await client.meetingAccountAliases()
        let account = aliases?.count == 1 ? aliases?.first : nil
        let create = MeetingCreate(title: "",  // the server names it from the calendar, else by date
                                   mode: (fields["mode"] as? String) ?? "online", started: started,
                                   ended: ended, device: Host.current().localizedName ?? "Mac", ext: file.pathExtension,
                                   account: account)
        await uploader.upload(create, sidecarJSON: sidecar)
        self.uploader = nil
        guard !isRecording else { return }
        switch uploader.state {
        case .done(let id): phase = .sent(id)
        case .failed(let message): phase = .failed("Recorded, not sent yet: \(message). It is sent again at the next launch.")
        case .idle: break                        // cancelled: startSending says so
        default: phase = .failed("Recorded, not sent yet. It is sent again at the next launch.")
        }
    }

    private static let stamp: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH-mm-ss"
        return f
    }()
}
