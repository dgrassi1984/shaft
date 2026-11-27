import AVFoundation
import Foundation
import Observation
import SHAFTKit
import UserNotifications

@MainActor
protocol IOSMeetingActivity: AnyObject {
    func setInterrupted(_ interrupted: Bool)
    func end(at date: Date, problem: String?)
}

@MainActor
@Observable
final class IOSMeetingRecorder {
    private struct Sidecar: Codable {
        struct Gap: Codable {
            let start: TimeInterval
            let end: TimeInterval
            let reason: String
            /// How long the gap lasted on the clock: paused time is absent from the file, so `start` and `end` can be
            /// the same audio position (review 2026-11-27: a ten-minute call read as a gap of no length).
            var wallSeconds: TimeInterval? = nil

            enum CodingKeys: String, CodingKey {
                case start, end, reason
                case wallSeconds = "wall_seconds"
            }
        }

        let started: Date
        var ended: Date?
        let mode: String
        var inputDevice: String
        var duration: TimeInterval?
        var gaps: [Gap] = []
        var problems: [String] = []

        enum CodingKeys: String, CodingKey {
            case started, ended, mode, duration, gaps, problems
            case inputDevice = "input_device"
        }
    }

    private struct Recording {
        let file: URL
        var sidecar: Sidecar
        var interruption: (start: TimeInterval, deadline: ContinuousClock.Instant, began: Date)?

        mutating func endGap(at audioTime: TimeInterval, now: Date) {
            guard let interruption else { return }
            // Paused time is absent from the file, so both ends may mark the same audio position: the clock time
            // says how long it lasted.
            sidecar.gaps.append(.init(start: interruption.start, end: max(interruption.start, audioTime),
                                      reason: "Audio session interruption",
                                      wallSeconds: max(0, now.timeIntervalSince(interruption.began))))
            self.interruption = nil
        }
    }

    private(set) var starting = false
    private(set) var problem: String?
    /// A recording stopped early and the operator has not opened Meetings since: the chat keeps a banner for it.
    private(set) var problemUnseen = false
    private var recording: Recording?
    var started: Date? { recording?.sidecar.started }
    var interrupted: Bool { recording?.interruption != nil }

    private(set) var uploader: MeetingUploader?
    private(set) var uploadProblem: String?
    private(set) var lastSent: String?
    private(set) var damagedRecordings: [URL] = []

    /// Recordings are IMA4 in CAF, which survives the app being ended mid-recording; .m4a files from older builds
    /// are still sent.
    static let recordingExtension = "caf"
    static let audioExtensions: Set<String> = ["caf", "m4a"]
    static let recordingSettings: [String: Any] = [
        AVFormatIDKey: kAudioFormatAppleIMA4, AVSampleRateKey: 24_000, AVNumberOfChannelsKey: 1
    ]

    @ObservationIgnored private let audio: any IOSMeetingAudio
    @ObservationIgnored private let audioUse: IOSAudioUse
    @ObservationIgnored private let folder: URL
    @ObservationIgnored private let now: @MainActor () -> Date
    @ObservationIgnored private let device: @MainActor () -> String
    @ObservationIgnored private let interruptionLimit: Duration
    @ObservationIgnored private let retryInterval: Duration
    @ObservationIgnored private let notify: @MainActor (String) -> Void
    @ObservationIgnored private let startActivity: @MainActor (Date) -> (any IOSMeetingActivity)?
    @ObservationIgnored private var activity: (any IOSMeetingActivity)?
    @ObservationIgnored private var interruptionTask: Task<Void, Never>?
    @ObservationIgnored private var client: APIClient?
    @ObservationIgnored private var foreground = false
    @ObservationIgnored private var scanRequested = false
    @ObservationIgnored private(set) var uploadTask: Task<Void, Never>?

    init(audio: any IOSMeetingAudio, audioUse: IOSAudioUse,
         folder: URL = URL.applicationSupportDirectory.appending(path: "SHAFT/Meetings", directoryHint: .isDirectory),
         now: @escaping @MainActor () -> Date = { Date() }, device: @escaping @MainActor () -> String,
         interruptionLimit: Duration = .seconds(3 * 3600), retryInterval: Duration = .seconds(1),
         notify: @escaping @MainActor (String) -> Void = IOSMeetingRecorder.postNotification,
         startActivity: @escaping @MainActor (Date) -> (any IOSMeetingActivity)? = { _ in nil }) {
        self.notify = notify
        self.startActivity = startActivity
        self.audio = audio
        self.audioUse = audioUse
        self.folder = folder
        self.now = now
        self.device = device
        self.interruptionLimit = interruptionLimit
        self.retryInterval = retryInterval
        audio.onEvent = { [weak self] in self?.handle($0) }
    }

    func start() async {
        guard recording == nil, !starting else { return }
        guard audioUse.claim(.meeting) else {
            problem = audioUse.owner == .readAloud
                ? "Read-aloud is still stopping. Tap Record again in a moment."
                : "End voice mode before recording a meeting."
            return
        }
        starting = true
        problem = nil
        problemUnseen = false
        defer {
            starting = false
            if recording == nil { audioUse.release(.meeting) }
        }
        guard await audio.requestPermission() else {
            problem = "Microphone access is off. Allow SHAFT to use the microphone in Settings."
            return
        }
        let file = folder.appendingPathComponent("\(Self.stamp.string(from: now()))-\(UUID().uuidString.prefix(8).lowercased()).\(Self.recordingExtension)")
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let input = try audio.start(at: file)
            let started = now()
            recording = Recording(file: file, sidecar: Sidecar(started: started, mode: "inperson", inputDevice: input))
            activity = startActivity(started)
        } catch {
            audio.stop()
            try? FileManager.default.removeItem(at: file)
            problem = "Could not start recording. \(error.localizedDescription)"
        }
    }

    func stop() {
        finish()
    }

    var uploadsInForeground: Bool { foreground }

    private func handle(_ event: IOSMeetingAudioEvent) {
        guard var recording else { return }
        switch event {
        case .interrupted:
            guard !interrupted else { return }
            // A phone call holds the microphone for as long as it lasts: wait for it (review 2026-11-27: after 30 s
            // the rest of the meeting was not recorded), up to the limit.
            recording.interruption = (audio.currentTime, ContinuousClock.now + interruptionLimit, now())
            self.recording = recording
            audio.pause()
            activity?.setInterrupted(true)
            interruptionTask = Task { [weak self, retryInterval] in
                while !Task.isCancelled {
                    do { try await Task.sleep(for: retryInterval) } catch { return }
                    guard let self else { return }
                    self.tryResume()
                }
            }
        case .interruptionEnded(let shouldResume):
            if shouldResume { tryResume() }
        case .routeChanged(let input, let stillRecording):
            let time = max(audio.currentTime, recording.interruption?.start ?? 0)
            let note = "Audio input disconnected. Current input: \(input)."
            recording.sidecar.inputDevice = input
            recording.sidecar.gaps.append(.init(start: time, end: time, reason: note))
            recording.sidecar.problems.append(note)
            self.recording = recording
            if stillRecording { problem = note }
            else { finish(problem: "The microphone stopped recording after the input changed.") }
        case .failed(let problem):
            finish(problem: problem)
        }
    }

    private func tryResume() {
        guard var recording, let interruption = recording.interruption else { return }
        // Past the limit the meeting is over, even if the microphone is free again (review 2026-11-27: an app
        // suspended for hours resumed the old meeting when brought back).
        guard ContinuousClock.now < interruption.deadline else {
            finish(problem: "The microphone could not resume recording after an audio interruption.")
            return
        }
        guard (try? audio.resume()) == true else { return }
        recording.endGap(at: audio.currentTime, now: now())
        self.recording = recording
        activity?.setInterrupted(false)
        interruptionTask?.cancel()
        interruptionTask = nil
    }

    private func finish(problem failure: String? = nil) {
        guard var recording else { return }
        let ended = now()
        recording.endGap(at: audio.currentTime, now: ended)
        interruptionTask?.cancel()
        interruptionTask = nil
        audio.stop()
        audioUse.release(.meeting)
        self.recording = nil
        recording.sidecar.ended = ended
        if let failure { recording.sidecar.problems.append(failure) }
        problem = failure.map { "Recording stopped early. \($0)" }
        activity?.end(at: ended, problem: problem)
        activity = nil
        if let problem {
            problemUnseen = true
            if !foreground { notify(problem) }
        }

        do {
            try Self.save(recording.sidecar, for: recording.file)
        } catch {
            problem = [problem, "Could not save recording details. \(error.localizedDescription)"]
                .compactMap { $0 }.joined(separator: " ")
        }
        if client == nil { uploadProblem = "Pair with SHAFT to send this recording." }
        requestUpload()
    }

    /// Meetings was opened: the chat's banner about a recording that stopped early goes.
    func markProblemSeen() {
        problemUnseen = false
    }

    /// Said on the lock screen when the recording stopped early with the app in the background.
    static func postNotification(_ problem: String) {
        let content = UNMutableNotificationContent()
        content.title = "Meeting recording stopped"
        content.body = problem
        content.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "meeting-stopped-" + UUID().uuidString,
                                                                     content: content, trigger: nil))
    }

    func updateUploadContext(foreground: Bool, client: APIClient?) {
        if !foreground || self.client !== client {
            scanRequested = false
            uploadTask?.cancel()
        }
        if foreground, !self.foreground { tryResume() }
        self.foreground = foreground
        self.client = client
        requestUpload()
    }

    private func requestUpload() {
        guard foreground else { return }
        scanRequested = true
        guard uploadTask == nil else { return }
        uploadTask = Task {
            defer {
                uploadTask = nil
                if scanRequested { requestUpload() }
            }
            while scanRequested, !Task.isCancelled {
                scanRequested = false
                await sendPending(client)
            }
        }
    }

    private func sendPending(_ client: APIClient?) async {
        let manager = FileManager.default
        guard manager.fileExists(atPath: folder.path) else {
            damagedRecordings = []
            return
        }
        do {
            let files = try manager.contentsOfDirectory(at: folder, includingPropertiesForKeys: [.creationDateKey])
                .filter { Self.audioExtensions.contains($0.pathExtension) }
                .sorted { $0.lastPathComponent < $1.lastPathComponent }
            try? MeetingUploader.pruneUploaded(in: folder)
            if client != nil { uploadProblem = nil }
            var damaged: [URL] = []
            var pending: [(URL, Sidecar)] = []
            for file in files {
                try Task.checkCancellation()
                guard file != recording?.file else { continue }
                guard manager.fileExists(atPath: file.path) else {
                    // pruneUploaded owns the audio and upload record. The app owns the sidecar.
                    try? manager.removeItem(at: Self.sidecarURL(file))
                    continue
                }
                guard let sidecar = try finishedSidecar(for: file) else {
                    damaged.append(file)
                    continue
                }
                pending.append((file, sidecar))
            }
            damagedRecordings = damaged
            if let client {
                for (file, sidecar) in pending {
                    try Task.checkCancellation()
                    await send(file, sidecar: sidecar, client: client)
                }
            }
        } catch where !Task.isCancelled {
            uploadProblem = "Could not read saved recordings. \(error.localizedDescription)"
        } catch {
            // The next foreground pass resumes from the uploader's saved chunk indices.
        }
    }

    private func finishedSidecar(for file: URL) throws -> Sidecar? {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let data = try? Data(contentsOf: Self.sidecarURL(file))
        let existing = data.flatMap { try? decoder.decode(Sidecar.self, from: $0) }
        if let existing, existing.ended != nil { return existing }
        try? MeetingFiles.trimPartialPacket(of: file)
        guard let duration = try? Self.playableDuration(of: file) else { return nil }
        let dates = try? file.resourceValues(forKeys: [.creationDateKey])
        let started = existing?.started ?? dates?.creationDate ?? now()
        var recovered = existing ?? Sidecar(started: started, mode: "inperson", inputDevice: "Microphone")
        recovered.ended = started.addingTimeInterval(duration)
        recovered.duration = duration
        recovered.problems.append("The app stopped before the recording ended.")
        try Self.save(recovered, for: file)
        return recovered
    }

    private static func playableDuration(of file: URL) throws -> TimeInterval? {
        let audio = try AVAudioFile(forReading: file)
        let duration = Double(audio.length) / audio.processingFormat.sampleRate
        guard duration.isFinite, duration > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: audio.processingFormat, frameCapacity: 1024) else { return nil }
        try audio.read(into: buffer)
        guard buffer.frameLength > 0 else { return nil }
        audio.framePosition = max(0, audio.length - 1024)
        try audio.read(into: buffer)
        return buffer.frameLength > 0 ? duration : nil
    }

    private static func save(_ sidecar: Sidecar, for file: URL) throws {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        try encoder.encode(sidecar).write(to: sidecarURL(file), options: .atomic)
    }

    private func send(_ file: URL, sidecar: Sidecar, client: APIClient) async {
        let started = sidecar.started
        let uploader = MeetingUploader(client: client, audioURL: file)
        self.uploader = uploader
        defer { self.uploader = nil }
        let aliases = try? await client.meetingAccountAliases()
        guard !Task.isCancelled else { return }
        let account = aliases?.count == 1 ? aliases?.first : nil
        let create = MeetingCreate(title: "",  // the server names it from the calendar, else by date
                                   mode: "inperson", started: started, ended: sidecar.ended,
                                   device: device(), ext: file.pathExtension, account: account)
        do {
            try await uploader.upload(create, sidecarJSON: Data(contentsOf: Self.sidecarURL(file)))
        } catch {
            uploadProblem = "Could not read recording details. \(error.localizedDescription)"
            return
        }
        switch uploader.state {
        case .done(let id): lastSent = id
        case .failed(let message): uploadProblem = "Recording saved. Upload failed. \(message)"
        default: break
        }
    }

    private static func sidecarURL(_ file: URL) -> URL {
        file.deletingPathExtension().appendingPathExtension("json")
    }

    private static let stamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd'T'HH-mm-ss"
        return formatter
    }()
}
