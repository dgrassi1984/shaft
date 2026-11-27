import AVFoundation
import Foundation
import Testing
import SHAFTKit

@MainActor
struct IOSMeetingRecorderTests {
    private final class Audio: IOSMeetingAudio {
        var onEvent: ((IOSMeetingAudioEvent) -> Void)?
        var canResume = true
        var currentTime: TimeInterval = 0
        private var file: URL?
        private var paused = false

        func requestPermission() async -> Bool { true }

        func start(at url: URL) throws -> String {
            try Data("before".utf8).write(to: url)
            file = url
            return "Example microphone"
        }

        func pause() { paused = true }

        func resume() throws -> Bool {
            guard canResume, paused, let file else { return false }
            let handle = try FileHandle(forWritingTo: file)
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: Data("after".utf8))
            paused = false
            return true
        }

        func stop() { file = nil }
    }

    @MainActor
    private final class Fixture {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let audio = Audio()
        let audioUse = IOSAudioUse()
        var time = Date(timeIntervalSince1970: 0)
        var interruptionLimit: Duration = .seconds(30)
        var retryInterval: Duration = .seconds(1)
        var notified: [String] = []
        lazy var recorder = IOSMeetingRecorder(audio: audio, audioUse: audioUse, folder: folder,
                                               now: { [unowned self] in self.time }, device: { "Example device" },
                                               interruptionLimit: interruptionLimit, retryInterval: retryInterval,
                                               notify: { [unowned self] in self.notified.append($0) })

        deinit { try? FileManager.default.removeItem(at: folder) }

        func file(_ ext: String) throws -> URL {
            let files = try FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)
            let matching = files.filter { $0.pathExtension == ext }
            #expect(matching.count == 1)
            return try #require(matching.first)
        }

        func sidecar() throws -> [String: Any] {
            let json = try JSONSerialization.jsonObject(with: Data(contentsOf: file("json")))
            return try #require(json as? [String: Any])
        }
    }

    @Test func interruptionsResumeIntoTheSameFileAndMarkTheAudioTimeline() async throws {
        let fixture = Fixture()
        let recorder = fixture.recorder
        await recorder.start()
        fixture.time.addTimeInterval(5)
        fixture.audio.currentTime = 3
        fixture.audio.onEvent?(.interrupted)
        #expect(recorder.interrupted)
        fixture.time.addTimeInterval(10)
        fixture.audio.onEvent?(.interruptionEnded(shouldResume: true))
        #expect(!recorder.interrupted)
        fixture.time.addTimeInterval(4)
        fixture.audio.currentTime = 6
        fixture.audio.onEvent?(.interrupted)
        fixture.time.addTimeInterval(2)
        fixture.audio.onEvent?(.interruptionEnded(shouldResume: true))
        fixture.time.addTimeInterval(1)
        recorder.stop()

        #expect(try String(contentsOf: fixture.file("caf"), encoding: .utf8) == "beforeafterafter")
        let sidecar = try fixture.sidecar()
        #expect(sidecar["started"] as? String == "1970-01-01T00:00:00Z")
        #expect(sidecar["ended"] as? String == "1970-01-01T00:00:22Z")
        #expect(sidecar["mode"] as? String == "inperson")
        #expect(sidecar["input_device"] as? String == "Example microphone")
        let gaps = try #require(sidecar["gaps"] as? [[String: Any]])
        #expect(gaps.compactMap { $0["start"] as? Double } == [3, 6])
        #expect(gaps.compactMap { $0["end"] as? Double } == [3, 6])
        #expect(gaps.compactMap { $0["reason"] as? String } == Array(repeating: "Audio session interruption", count: 2))
        // Review 2026-11-27: both ends are one audio position, so the gap read as no time at all; the clock says
        // the first lasted 10 s and the second 2 s.
        #expect(gaps.compactMap { $0["wall_seconds"] as? Double } == [10, 2])
    }

    /// Review 2026-11-27: a phone call longer than 30 s ended the meeting and the rest was not recorded. The default
    /// waits for the call: a recorder made without a limit is still interrupted, not finished, after a minute.
    @Test func aLongCallDoesNotEndTheMeetingByDefault() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let audio = Audio()
        audio.canResume = false
        var time = Date(timeIntervalSince1970: 0)
        let recorder = IOSMeetingRecorder(audio: audio, audioUse: IOSAudioUse(), folder: folder, now: { time },
                                          device: { "Example device" }, retryInterval: .milliseconds(5),
                                          notify: { _ in })
        await recorder.start()
        audio.onEvent?(.interrupted)
        time.addTimeInterval(60)
        try await Task.sleep(for: .milliseconds(50))
        #expect(recorder.interrupted)
        #expect(recorder.started != nil)
        recorder.stop()
    }

    @Test func foregroundResumesEvenWithoutAnInterruptionEndedEvent() async throws {
        let fixture = Fixture()
        let recorder = fixture.recorder
        await recorder.start()
        fixture.audio.onEvent?(.interrupted)
        recorder.updateUploadContext(foreground: true, client: nil)

        #expect(!recorder.interrupted)
        recorder.stop()
        #expect(try String(contentsOf: fixture.file("caf"), encoding: .utf8) == "beforeafter")
    }

    @Test func timerFinalisesAnInterruptionThatCannotResume() async throws {
        let fixture = Fixture()
        fixture.interruptionLimit = .milliseconds(20)
        fixture.retryInterval = .milliseconds(5)
        let recorder = fixture.recorder
        fixture.audio.canResume = false
        await recorder.start()
        fixture.audio.onEvent?(.interrupted)
        try await Task.sleep(for: .milliseconds(150))

        let reason = "The microphone could not resume recording after an audio interruption."
        #expect(recorder.started == nil)
        #expect(recorder.problem == "Recording stopped early. \(reason)")
        #expect(recorder.problemUnseen)
        #expect(fixture.notified == ["Recording stopped early. \(reason)"])   // in the background: said on the lock screen
        recorder.markProblemSeen()
        #expect(!recorder.problemUnseen)
        #expect(try fixture.sidecar()["problems"] as? [String] == [reason])
        #expect(try String(contentsOf: fixture.file("caf"), encoding: .utf8) == "before")
        #expect(fixture.audioUse.claim(.voice))
    }

    @Test(arguments: [true, false])
    func inputLossKeepsRecordingOnlyWhenTheMicrophoneIsStillRecording(stillRecording: Bool) async throws {
        let fixture = Fixture()
        let recorder = fixture.recorder
        await recorder.start()
        fixture.audio.currentTime = 4
        fixture.audio.onEvent?(.routeChanged(input: "Built-in Microphone", recording: stillRecording))

        #expect((recorder.started != nil) == stillRecording)
        if stillRecording { recorder.stop() }
        let sidecar = try fixture.sidecar()
        #expect(sidecar["input_device"] as? String == "Built-in Microphone")
        let problems = try #require(sidecar["problems"] as? [String])
        #expect(problems.first == "Audio input disconnected. Current input: Built-in Microphone.")
        if !stillRecording { #expect(problems.last == "The microphone stopped recording after the input changed.") }
        let gaps = try #require(sidecar["gaps"] as? [[String: Any]])
        #expect(gaps.first?["start"] as? Double == 4)
        #expect(gaps.first?["end"] as? Double == 4)
    }

    @Test func anUnexpectedStopFinalisesAndRecordsTheProblem() async throws {
        let fixture = Fixture()
        let recorder = fixture.recorder
        await recorder.start()
        fixture.audio.onEvent?(.failed("The microphone stopped recording unexpectedly."))

        #expect(recorder.started == nil)
        #expect(try fixture.sidecar()["problems"] as? [String] == ["The microphone stopped recording unexpectedly."])
        #expect(try String(contentsOf: fixture.file("caf"), encoding: .utf8) == "before")
    }

    @Test func readAloudAndMeetingTakeTurnsOwningAudio() async {
        let fixture = Fixture()
        let recorder = fixture.recorder
        #expect(fixture.audioUse.claim(.readAloud))
        await recorder.start()
        #expect(recorder.problem == "Read-aloud is still stopping. Tap Record again in a moment.")
        #expect(recorder.started == nil)
        fixture.audioUse.release(.readAloud)
        await recorder.start()
        #expect(recorder.started == Date(timeIntervalSince1970: 0))
        #expect(!fixture.audioUse.claim(.readAloud))
        recorder.stop()
        #expect(fixture.audioUse.claim(.readAloud))
    }

    @Test func stoppingTheSynthKeepsItsClaimUntilTheSessionQueueDrains() async throws {
        let synth = SystemReadAloudSynth()
        let audioUse = IOSAudioUse.shared
        let meetingClaimed = audioUse.claim(.meeting)
        try #require(meetingClaimed)
        var refused = false
        synth.speak(["Example"], voice: nil, onProgress: {}, onFinish: { refused = true })
        #expect(refused)
        audioUse.release(.meeting)

        synth.speak(["Example"], voice: nil, onProgress: {}, onFinish: {})
        synth.stop()
        #expect(audioUse.owner == .readAloud)
        #expect(!audioUse.claim(.meeting))

        let deadline = ContinuousClock.now + .seconds(5)
        while audioUse.owner == .readAloud, ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(audioUse.claim(.meeting))
        audioUse.release(.meeting)
    }

    /// 2026-11-26: a recording on the iPhone came back "Recording damaged": iOS ended the app mid-recording and AAC in
    /// .m4a cannot be read without the index written on close. The recorder's own format, read as the file stood
    /// before close, must still be playable, so the next launch sends it.
    @Test func recordingFormatStaysReadableWhenCutShort() async throws {
        let fixture = Fixture()
        try FileManager.default.createDirectory(at: fixture.folder, withIntermediateDirectories: true)
        let live = fixture.folder.appending(path: "live.\(IOSMeetingRecorder.recordingExtension)")
        let cut = fixture.folder.appending(path: "cut.\(IOSMeetingRecorder.recordingExtension)")
        let audio = try AVAudioFile(forWriting: live, settings: IOSMeetingRecorder.recordingSettings)
        let buffer = try #require(AVAudioPCMBuffer(pcmFormat: audio.processingFormat, frameCapacity: 24_000))
        buffer.frameLength = 24_000
        for i in 0..<24_000 { buffer.floatChannelData?[0][i] = Float(sin(Double(i) * 0.1) * 0.3) }
        for _ in 0..<3 { try audio.write(from: buffer) }
        try FileManager.default.copyItem(at: live, to: cut)  // the bytes on disk as an ended app leaves them
        _ = audio
        // An ended app can leave half a packet at the end, which the server's decoder (ffmpeg -xerror) refuses.
        let handle = try FileHandle(forWritingTo: cut)
        try handle.seekToEnd()
        try handle.write(contentsOf: Data(repeating: 7, count: 17))
        try handle.close()
        let before = try #require(try FileManager.default.attributesOfItem(atPath: cut.path)[.size] as? Int)

        // The launch scan's own recovery: trimmed to whole packets, playable, sent and marked as cut short.
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/app/v1/meetings"):
                return .json(201, #"{"id":"m-example","chunk_size":1048576,"received":[]}"#)
            case ("PUT", "/app/v1/meetings/m-example/chunk/0"):
                return .json(200, #"{"received":[0]}"#)
            default:
                return Self.answer(request, retry: true)
            }
        }
        try FileManager.default.removeItem(at: live)
        fixture.recorder.updateUploadContext(foreground: true, client: server.client)
        await fixture.recorder.uploadTask?.value
        #expect(fixture.recorder.damagedRecordings.isEmpty)
        let after = try #require(try FileManager.default.attributesOfItem(atPath: cut.path)[.size] as? Int)
        #expect(before - after == 17)
        let read = try AVAudioFile(forReading: cut)
        let seconds = Double(read.length) / read.processingFormat.sampleRate
        #expect(seconds > 2.5, "a cut-short recording kept only \(seconds) s")
        let create = try #require(server.requests.first { $0.method == "POST" && $0.path == "/app/v1/meetings" }?.json)
        #expect(create["ext"] as? String == "caf")
        #expect((create["sidecar"] as? [String: Any])?["problems"] as? [String] == ["The app stopped before the recording ended."])
    }

    @Test func launchSendsPlayableOrphansAndKeepsDamagedAudio() async throws {
        let fixture = Fixture()
        let recorder = fixture.recorder
        try FileManager.default.createDirectory(at: fixture.folder, withIntermediateDirectories: true)
        let damaged = fixture.folder.appending(path: "damaged.m4a")
        let playable = fixture.folder.appending(path: "playable.m4a")
        try Data("broken".utf8).write(to: damaged)
        do {
            let audio = try AVAudioFile(forWriting: playable, settings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1
            ])
            let buffer = try #require(AVAudioPCMBuffer(pcmFormat: audio.processingFormat, frameCapacity: 12_000))
            buffer.frameLength = 12_000
            buffer.floatChannelData?[0].update(repeating: 0, count: 12_000)
            try audio.write(from: buffer)
        }
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/app/v1/meetings"):
                return .json(201, #"{"id":"m-example","chunk_size":1048576,"received":[]}"#)
            case ("PUT", "/app/v1/meetings/m-example/chunk/0"):
                return .json(200, #"{"received":[0]}"#)
            default:
                return Self.answer(request, retry: true)
            }
        }
        recorder.updateUploadContext(foreground: true, client: server.client)
        await recorder.uploadTask?.value

        #expect(recorder.damagedRecordings.map(\.lastPathComponent) == ["damaged.m4a"])
        #expect(try Data(contentsOf: damaged) == Data("broken".utf8))
        let creates = server.requests.filter { $0.method == "POST" && $0.path == "/app/v1/meetings" }
        #expect(creates.count == 1)
        let create = try #require(creates.first)
        let sidecar = try #require(create.json?["sidecar"] as? [String: Any])
        #expect(sidecar["problems"] as? [String] == ["The app stopped before the recording ended."])
        #expect((sidecar["duration"] as? Double ?? 0) > 0)
        #expect(recorder.lastSent == "m-example")
    }

    @Test func voiceAndMeetingTakeTurnsOwningAudio() async {
        let fixture = Fixture()
        let recorder = fixture.recorder
        #expect(fixture.audioUse.claim(.voice))
        await recorder.start()
        #expect(recorder.problem == "End voice mode before recording a meeting.")
        fixture.audioUse.release(.voice)
        await recorder.start()
        #expect(recorder.started == Date(timeIntervalSince1970: 0))
        #expect(!fixture.audioUse.claim(.voice))
        recorder.stop()
        #expect(fixture.audioUse.claim(.voice))
    }

    @Test func returningToForegroundSendsOnlyTheMissingChunk() async throws {
        let fixture = Fixture()
        let recorder = fixture.recorder
        let server = StubServer { Self.answer($0, retry: false) }
        let client = server.client
        recorder.updateUploadContext(foreground: true, client: client)
        await recorder.uploadTask?.value
        await recorder.start()
        fixture.time.addTimeInterval(10)
        recorder.stop()
        await recorder.uploadTask?.value
        #expect(recorder.uploadProblem == "Recording saved. Upload failed. Example upload failed.")

        recorder.updateUploadContext(foreground: false, client: client)
        let requestCount = server.requests.count
        server.setAnswer { Self.answer($0, retry: true) }
        recorder.updateUploadContext(foreground: true, client: client)
        await recorder.uploadTask?.value

        let create = try #require(server.requests.first { $0.method == "POST" && $0.path == "/app/v1/meetings" }?.json)
        #expect(create["mode"] as? String == "inperson")
        #expect(create["device"] as? String == "Example device")
        #expect(create["ext"] as? String == "caf")
        #expect(create["account"] as? String == "work")
        #expect((create["sidecar"] as? [String: Any])?["input_device"] as? String == "Example microphone")
        let resumed = server.requests.dropFirst(requestCount)
        #expect(resumed.filter { $0.method == "PUT" }.map(\.path) == ["/app/v1/meetings/m-example/chunk/1"])
        #expect(resumed.filter { $0.method == "PUT" }.map { String(decoding: $0.body, as: UTF8.self) } == ["ore"])
        #expect(recorder.lastSent == "m-example")
    }

    private nonisolated static func answer(_ request: StubServer.Request, retry: Bool) -> StubServer.Response {
        let detail = "/app/v1/meetings/m-example"
        switch (request.method, request.path) {
        case ("GET", "/app/v1/instances"):
            return .json(200, #"{"instances":[],"paused":[{"name":"example","since":null}],"badge":0}"#)
        case ("GET", "/app/v1/access/example"):
            return .json(200, """
                {"instance":"example","browser":{"installed":null,"run":true},"web_search":false,"telegram":false,
                 "mail":[],"calendar":[],"drive":[],"chat":[],"google_accounts":[{"alias":"work","address":"","scopes":[]}]}
                """)
        case ("POST", "/app/v1/meetings") where !retry:
            return .json(201, #"{"id":"m-example","chunk_size":3,"received":[]}"#)
        case ("GET", "\(detail)/upload") where retry:
            return .json(200, #"{"chunk_size":3,"received":[0]}"#)
        case ("PUT", "\(detail)/chunk/0") where !retry:
            return .json(200, #"{"received":[0]}"#)
        case ("PUT", "\(detail)/chunk/1") where retry:
            return .json(200, #"{"received":[0,1]}"#)
        case ("PUT", "\(detail)/chunk/1"):
            return .json(400, #"{"error":{"code":"bad_request","message":"Example upload failed."}}"#)
        case ("POST", "\(detail)/finish") where retry:
            return .json(200, """
                {"id":"m-example","title":"Meeting","created":"2026-11-25T14:02:31Z","started":"2026-11-25T14:02:31Z",
                 "mode":"inperson","device":"Example device","ext":"m4a","state":"queued","chunk_size":3,
                 "chunks_received":[0,1]}
                """)
        default:
            return .json(400, #"{"error":{"code":"bad_request","message":"Unexpected request."}}"#)
        }
    }
}
