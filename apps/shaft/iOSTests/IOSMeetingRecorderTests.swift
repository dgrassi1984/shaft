import Foundation
import Testing
import SHAFTKit

@MainActor
struct IOSMeetingRecorderTests {
    private final class Audio: IOSMeetingAudio {
        var onEvent: ((IOSMeetingAudioEvent) -> Void)?
        var canResume = true
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
        lazy var recorder = IOSMeetingRecorder(audio: audio, audioUse: audioUse, folder: folder,
                                               now: { [unowned self] in self.time }, device: { "Example device" })

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
        fixture.audio.onEvent?(.interrupted)
        #expect(recorder.interrupted)
        fixture.time.addTimeInterval(10)
        fixture.audio.onEvent?(.interruptionEnded(shouldResume: true))
        #expect(!recorder.interrupted)
        fixture.time.addTimeInterval(4)
        fixture.audio.onEvent?(.interrupted)
        fixture.time.addTimeInterval(2)
        fixture.audio.onEvent?(.interruptionEnded(shouldResume: true))
        fixture.time.addTimeInterval(1)
        recorder.stop()

        #expect(try String(contentsOf: fixture.file("m4a"), encoding: .utf8) == "beforeafterafter")
        let sidecar = try fixture.sidecar()
        #expect(sidecar["started"] as? String == "1970-01-01T00:00:00Z")
        #expect(sidecar["ended"] as? String == "1970-01-01T00:00:22Z")
        #expect(sidecar["mode"] as? String == "inperson")
        #expect(sidecar["input_device"] as? String == "Example microphone")
        let gaps = try #require(sidecar["gaps"] as? [[String: Any]])
        #expect(gaps.compactMap { $0["start"] as? Double } == [5, 9])
        #expect(gaps.compactMap { $0["end"] as? Double } == [15, 11])
        #expect(gaps.compactMap { $0["reason"] as? String } == Array(repeating: "Audio session interruption", count: 2))
    }

    @Test(arguments: [true, false])
    func aFailedResumeKeepsThePartialFileAndReportsTheProblem(shouldResume: Bool) async throws {
        let fixture = Fixture()
        let recorder = fixture.recorder
        fixture.audio.canResume = false
        await recorder.start()
        fixture.time.addTimeInterval(5)
        fixture.audio.onEvent?(.interrupted)
        fixture.time.addTimeInterval(10)
        fixture.audio.onEvent?(.interruptionEnded(shouldResume: shouldResume))

        let reason = shouldResume ? "The microphone could not resume recording." : "iOS did not allow the recording to resume."
        #expect(recorder.problem == "Recording stopped early. \(reason)")
        #expect(try fixture.sidecar()["problems"] as? [String] == [reason])
        #expect(try String(contentsOf: fixture.file("m4a"), encoding: .utf8) == "before")
        #expect(fixture.audioUse.claim(.voice))
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
        #expect(create["ext"] as? String == "m4a")
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
