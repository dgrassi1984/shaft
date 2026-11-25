import AVFoundation
import Foundation
import Observation
import SHAFTKit

/// Record a meeting on this Mac and hand it to the server (docs/meetings.md): `MeetingCapture` writes
/// `~/Library/Application Support/SHAFT/Meetings/<stamp>.m4a` and its sidecar `<stamp>.json`; on Stop,
/// `MeetingUploader` sends it in chunks to the paired server, which transcribes it. A recording whose upload did not
/// finish (no server, the app quit) is sent again at the next launch or the next Stop; a finished one is kept 7 days.
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
    /// Said by the capture while recording (the system audio silent: the permission is the likely cause).
    private(set) var warning: String?
    /// The upload under way, for its progress.
    private(set) var uploader: MeetingUploader?

    @ObservationIgnored private var capture: MeetingCapture?
    @ObservationIgnored private var file: URL?
    @ObservationIgnored private let client: @MainActor () -> APIClient?
    @ObservationIgnored let folder: URL

    var isRecording: Bool { if case .recording = phase { true } else { false } }
    /// Set before the permission prompt's await, so a second Record click cannot start a second capture over the first
    /// (review 2026-11-25).
    @ObservationIgnored private var starting = false

    init(client: @escaping @MainActor () -> APIClient?,
         folder: URL = URL.applicationSupportDirectory.appending(path: "SHAFT/Meetings", directoryHint: .isDirectory)) {
        self.client = client
        self.folder = folder
    }

    /// Start recording: online (the microphone and every other app's sound) or in person (the microphone).
    func start(online: Bool) async {
        guard !isRecording, !starting else { return }
        starting = true
        defer { starting = false }
        warning = nil
        guard await AVCaptureDevice.requestAccess(for: .audio) else {
            phase = .failed("SHAFT may not use the microphone: allow it in System Settings → Privacy & Security → Microphone.")
            return
        }
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            // A new name every time (a time stamp and a random suffix): never an earlier recording's, whose upload
            // record would then describe another file (review 2026-11-25).
            let stamp = Self.stamp.string(from: Date()) + "-" + UUID().uuidString.prefix(8).lowercased()
            let url = folder.appending(path: "\(stamp).m4a")
            let capture = MeetingCapture(online: online, url: url, stallSeconds: 20)
            capture.onWarning = { [weak self] message in Task { @MainActor in self?.warning = message } }
            try capture.start()
            self.capture = capture
            file = url
            phase = .recording(started: Date(), online: online)
        } catch {
            phase = .failed("Could not start recording: \(error.localizedDescription)")
        }
    }

    /// Stop, finalise the file, and send it.
    func stop() async {
        guard case .recording = phase, let capture, let file else { return }
        let sidecar = capture.stop()            // also writes <stamp>.json
        self.capture = nil
        self.file = nil
        if let problems = sidecar["problems"] as? [String], !problems.isEmpty {
            warning = "The recording is incomplete: " + problems.joined(separator: "; ") + "."
        }
        await send(file)
    }

    /// Send every recording here whose upload has not finished (the server confirms a finished one at once).
    func sendPending() async {
        guard !isRecording, uploader == nil else { return }
        let files = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? []
        for file in files.filter({ $0.pathExtension == "m4a" }).sorted(by: { $0.path < $1.path }) {
            await send(file)
        }
        try? MeetingUploader.pruneUploaded(in: folder)
    }

    private func send(_ file: URL) async {
        guard let client = client() else {
            phase = .failed("Recorded, not sent: pair this Mac with your SHAFT server. It is sent at the next launch.")
            return
        }
        let side = file.deletingPathExtension().appendingPathExtension("json")
        let sidecar = (try? Data(contentsOf: side)) ?? Data("{}".utf8)
        let fields = (try? JSONSerialization.jsonObject(with: sidecar)) as? [String: Any] ?? [:]
        let iso = ISO8601DateFormatter()
        let started = (fields["started"] as? String).flatMap(iso.date(from:)) ?? Date()
        let ended = (fields["ended"] as? String).flatMap(iso.date(from:))
        let title = "Meeting " + started.formatted(date: .abbreviated, time: .shortened)
        let uploader = MeetingUploader(client: client, audioURL: file)
        self.uploader = uploader
        phase = .uploading
        let aliases = try? await client.meetingAccountAliases()
        let account = aliases?.count == 1 ? aliases?.first : nil
        let create = MeetingCreate(title: title, mode: (fields["mode"] as? String) ?? "online", started: started,
                                   ended: ended, device: Host.current().localizedName ?? "Mac", ext: "m4a",
                                   account: account)
        await uploader.upload(create, sidecarJSON: sidecar)
        self.uploader = nil
        switch uploader.state {
        case .done(let id): phase = .sent(id)
        case .failed(let message): phase = .failed("Recorded, not sent yet: \(message). It is sent again at the next launch.")
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
