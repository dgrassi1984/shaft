import Foundation
import Observation
import SHAFTKit

@MainActor
@Observable
final class IOSMeetingRecorder {
    private struct Sidecar: Codable {
        struct Gap: Codable {
            let start: TimeInterval
            let end: TimeInterval
            let reason: String
        }

        let started: Date
        var ended: Date?
        let mode: String
        let inputDevice: String
        var gaps: [Gap] = []
        var problems: [String] = []

        enum CodingKeys: String, CodingKey {
            case started, ended, mode, gaps, problems
            case inputDevice = "input_device"
        }
    }

    private struct Recording {
        let file: URL
        var sidecar: Sidecar
        var interruptedAt: Date?

        mutating func endGap(at date: Date) {
            guard let interruptedAt else { return }
            // The AAC file omits time spent paused. Anchor each gap to the audio clock.
            let previousPauses = sidecar.gaps.reduce(0) { $0 + $1.end - $1.start }
            let start = max(0, interruptedAt.timeIntervalSince(sidecar.started) - previousPauses)
            let duration = max(0, date.timeIntervalSince(interruptedAt))
            sidecar.gaps.append(.init(start: start, end: start + duration, reason: "Audio session interruption"))
            self.interruptedAt = nil
        }
    }

    private(set) var starting = false
    private(set) var problem: String?
    private var recording: Recording?
    var started: Date? { recording?.sidecar.started }
    var interrupted: Bool { recording?.interruptedAt != nil }

    private(set) var uploader: MeetingUploader?
    private(set) var uploadProblem: String?
    private(set) var lastSent: String?

    @ObservationIgnored private let audio: any IOSMeetingAudio
    @ObservationIgnored private let audioUse: IOSAudioUse
    @ObservationIgnored private let folder: URL
    @ObservationIgnored private let now: @MainActor () -> Date
    @ObservationIgnored private let device: @MainActor () -> String
    @ObservationIgnored private var client: APIClient?
    @ObservationIgnored private var foreground = false
    @ObservationIgnored private var scanRequested = false
    @ObservationIgnored private(set) var uploadTask: Task<Void, Never>?

    init(audio: any IOSMeetingAudio, audioUse: IOSAudioUse,
         folder: URL = URL.applicationSupportDirectory.appending(path: "SHAFT/Meetings", directoryHint: .isDirectory),
         now: @escaping @MainActor () -> Date = { Date() }, device: @escaping @MainActor () -> String) {
        self.audio = audio
        self.audioUse = audioUse
        self.folder = folder
        self.now = now
        self.device = device
        audio.onEvent = { [weak self] in self?.handle($0) }
    }

    func start() async {
        guard recording == nil, !starting else { return }
        guard audioUse.claim(.meeting) else {
            problem = "End voice mode before recording a meeting."
            return
        }
        starting = true
        problem = nil
        defer {
            starting = false
            if recording == nil { audioUse.release(.meeting) }
        }
        guard await audio.requestPermission() else {
            problem = "Microphone access is off. Allow SHAFT to use the microphone in Settings."
            return
        }
        let file = folder.appendingPathComponent("\(Self.stamp.string(from: now()))-\(UUID().uuidString.prefix(8).lowercased()).m4a")
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let input = try audio.start(at: file)
            recording = Recording(file: file, sidecar: Sidecar(started: now(), mode: "inperson", inputDevice: input))
        } catch {
            audio.stop()
            try? FileManager.default.removeItem(at: file)
            problem = "Could not start recording. \(error.localizedDescription)"
        }
    }

    func stop() {
        finish()
    }

    private func handle(_ event: IOSMeetingAudioEvent) {
        guard var recording else { return }
        switch event {
        case .interrupted:
            guard !interrupted else { return }
            recording.interruptedAt = now()
            self.recording = recording
            audio.pause()
        case .interruptionEnded(let shouldResume):
            guard interrupted else { return }
            guard shouldResume else {
                finish(problem: "iOS did not allow the recording to resume.")
                return
            }
            do {
                guard try audio.resume() else {
                    finish(problem: "The microphone could not resume recording.")
                    return
                }
            } catch {
                finish(problem: "The microphone could not resume recording. \(error.localizedDescription)")
                return
            }
            recording.endGap(at: now())
            self.recording = recording
        case .failed(let problem):
            finish(problem: problem)
        }
    }

    private func finish(problem failure: String? = nil) {
        guard var recording else { return }
        let ended = now()
        recording.endGap(at: ended)
        audio.stop()
        audioUse.release(.meeting)
        self.recording = nil
        recording.sidecar.ended = ended
        if let failure { recording.sidecar.problems.append(failure) }
        problem = failure.map { "Recording stopped early. \($0)" }

        do {
            let encoder = JSONEncoder()
            encoder.dateEncodingStrategy = .iso8601
            try encoder.encode(recording.sidecar).write(to: Self.sidecarURL(recording.file), options: .atomic)
        } catch {
            problem = [problem, "Could not save recording details. \(error.localizedDescription)"]
                .compactMap { $0 }.joined(separator: " ")
        }
        if client == nil { uploadProblem = "Pair with SHAFT to send this recording." }
        requestUpload()
    }

    func updateUploadContext(foreground: Bool, client: APIClient?) {
        if !foreground || self.client !== client {
            scanRequested = false
            uploadTask?.cancel()
        }
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
        guard manager.fileExists(atPath: folder.path) else { return }
        do {
            let files = try manager.contentsOfDirectory(at: folder, includingPropertiesForKeys: [.creationDateKey])
                .filter { $0.pathExtension == "m4a" }
                .sorted { $0.lastPathComponent < $1.lastPathComponent }
            try? MeetingUploader.pruneUploaded(in: folder)
            if client != nil { uploadProblem = nil }
            for file in files {
                try Task.checkCancellation()
                guard file != recording?.file else { continue }
                guard manager.fileExists(atPath: file.path) else {
                    // pruneUploaded owns the audio and upload record. The app owns the sidecar.
                    try? manager.removeItem(at: Self.sidecarURL(file))
                    continue
                }
                if let client { await send(file, client: client) }
            }
        } catch where !Task.isCancelled {
            uploadProblem = "Could not read saved recordings. \(error.localizedDescription)"
        } catch {
            // The next foreground pass resumes from the uploader's saved chunk indices.
        }
    }

    private func send(_ file: URL, client: APIClient) async {
        let data = try? Data(contentsOf: Self.sidecarURL(file))
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let sidecar = data.flatMap { try? decoder.decode(Sidecar.self, from: $0) }
        let started = sidecar?.started ?? (try? file.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? now()
        let uploader = MeetingUploader(client: client, audioURL: file)
        self.uploader = uploader
        defer { self.uploader = nil }
        let aliases = try? await client.meetingAccountAliases()
        guard !Task.isCancelled else { return }
        let account = aliases?.count == 1 ? aliases?.first : nil
        let title = "Meeting " + started.formatted(date: .abbreviated, time: .shortened)
        let create = MeetingCreate(title: title, mode: "inperson", started: started, ended: sidecar?.ended,
                                   device: device(), ext: "m4a", account: account)
        await uploader.upload(create, sidecarJSON: data)
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
