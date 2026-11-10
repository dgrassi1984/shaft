#if os(macOS)
import Foundation
import XCTest
@testable import SHAFTKit

// Voice M2's end-to-end check: a whole voice turn through VoiceSession against the fixture server
// (apps/shaft/DevServer/fixture_server.py: the real /app/v1, web_chat relay and stream over fake instances), with a
// recorded-speech file standing in for the microphone (FileTranscriber), real speech rendered on a silent engine and
// the generated working sound. Nothing is heard, no microphone is used, nothing takes focus. Test speech is made
// with `/usr/bin/say -o` into a temporary file.
//
// What it does not prove: echo cancellation (the file goes straight to the transcriber, and the output is silent),
// the live end-of-turn delay with a real voice, or the audio on the iPhone — M3, with the microphone and speakers.

/// The fixture server, started for one test on a free port over a temporary folder.
private final class FixtureServer {
    let process = Process()
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("shaft-fx-\(UUID().uuidString)")
    let port = Int.random(in: 20_000..<40_000)
    private(set) var link = ""

    init() throws {
        let python = URL(fileURLWithPath: "/opt/homebrew/opt/python@3.12/bin/python3.12")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw XCTSkip("the fixture server needs python3.12 at \(python.path)")
        }
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()                    // Tests/SHAFTKitTests/<file> → the repository
        let script = repo.appendingPathComponent("apps/shaft/DevServer/fixture_server.py")
        process.executableURL = python
        process.arguments = [script.path, "serve", "--dir", dir.path, "--port", String(port)]
        var env = ProcessInfo.processInfo.environment
        env.removeValue(forKey: "VIRTUAL_ENV")
        env["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin"
        process.environment = env
        let out = Pipe()
        process.standardOutput = out
        process.standardError = out
        try process.run()
        var text = ""
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            let data = out.fileHandleForReading.availableData
            if data.isEmpty, !process.isRunning { break }
            text += String(decoding: data, as: UTF8.self)
            if let line = text.split(separator: "\n").first(where: { $0.hasPrefix("pair: ") }) {
                link = String(line.dropFirst(6))
                return
            }
        }
        stop()
        throw NotStarted(output: String(text.suffix(600)))      // a failure, never a skip: it hides a broken setup
    }

    struct NotStarted: Error { let output: String }

    func stop() {
        if process.isRunning { process.terminate(); process.waitUntilExit() }
        try? FileManager.default.removeItem(at: dir)
    }
}

/// What happened, with VoiceClock times, from every thread.
private final class Log: @unchecked Sendable {
    private let lock = NSLock()
    private var entries: [(at: TimeInterval, what: String)] = []

    func add(_ what: String) { lock.withLock { entries.append((VoiceClock.now(), what)) } }
    var all: [(at: TimeInterval, what: String)] { lock.withLock { entries } }
    func times(_ what: String) -> [TimeInterval] { all.filter { $0.what == what }.map(\.at) }
    func first(_ prefix: String) -> TimeInterval? { all.first { $0.what.hasPrefix(prefix) }?.at }
}

/// The real speech output, its calls and callbacks logged.
private final class RecordingSpeech: SpeechOutput, @unchecked Sendable {
    let inner: EngineSpeechOutput
    let log: Log
    private let lock = NSLock()
    private var texts: [Int: String] = [:]

    init(_ inner: EngineSpeechOutput, _ log: Log) { self.inner = inner; self.log = log }

    func useVoice(identifier: String?, name: String?, language: VoiceLanguage) -> String? {
        inner.useVoice(identifier: identifier, name: name, language: language)
    }

    func speak(id: Int, text: String, onStart: @escaping @Sendable (Int) -> Void,
               onFinish: @escaping @Sendable (Int) -> Void) {
        lock.withLock { texts[id] = text }
        log.add("queue \(text)")
        inner.speak(id: id, text: text,
                    onStart: { [log, self] in log.add("start \(self.text($0))"); onStart($0) },
                    onFinish: { [log, self] in log.add("finish \(self.text($0))"); onFinish($0) })
    }

    func stopAll() {
        log.add("stopAll")
        inner.stopAll()
    }

    private func text(_ id: Int) -> String { lock.withLock { texts[id] ?? "?" } }
}

/// The real generated sounds, the working sound's switches logged.
private final class RecordingSounds: VoiceSounds, @unchecked Sendable {
    let inner: GeneratedVoiceSounds
    let log: Log

    init(_ inner: GeneratedVoiceSounds, _ log: Log) { self.inner = inner; self.log = log }

    func working(_ on: Bool) { log.add(on ? "working on" : "working off"); inner.working(on) }
    func duckWorking(_ ducked: Bool) { inner.duckWorking(ducked) }
    func setWorkingVolume(_ volume: Float) { inner.setWorkingVolume(volume) }
    func earcon(_ state: VoiceState) { log.add("state \(state.rawValue)"); inner.earcon(state) }
}

/// English test speech in a temporary AIFF, made with `say` (written to the file, never played).
private func spoken(_ text: String) throws -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("shaft-e2e-\(UUID().uuidString).aiff")
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/say")
    process.arguments = ["-v", "Samantha", "-o", url.path, text]
    try process.run()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else { throw XCTSkip("/usr/bin/say could not write test speech") }
    return url
}

@MainActor
final class VoiceSessionEndToEndTests: XCTestCase {
    private var server: FixtureServer?

    override func tearDown() async throws {
        server?.stop()
    }

    /// A session over the fixture server, its speech and sounds logged, hearing `clips`.
    private func session(_ clips: [(URL, at: TimeInterval)], log: Log) async throws -> (VoiceSession, APIClient) {
        let server = try FixtureServer()
        self.server = server
        let credential = try await APIClient.pair(link: PairingLink(server.link))
        let client = APIClient(credential: credential)
        let graph = VoiceAudioGraph(silent: true)
        let session = VoiceSession(client: client, instance: "tundra", instanceName: "Tundra", language: .en,
                                   transcriber: FileTranscriber(clips),
                                   speech: RecordingSpeech(EngineSpeechOutput(graph: graph), log),
                                   sounds: RecordingSounds(GeneratedVoiceSounds(graph: graph), log), graph: graph)
        do {
            try await session.start()
        } catch let error as VoiceAudioError {
            if case .transcriberModelMissing = error { throw XCTSkip("\(error.localizedDescription)") }
            throw error
        }
        return (session, client)
    }

    /// Wait until `done`, or fail after `seconds`.
    private func wait(_ seconds: TimeInterval, _ what: String, _ done: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(seconds)
        while !done() {
            if Date() > deadline { XCTFail("timed out waiting for \(what)"); return }
            try await Task.sleep(for: .milliseconds(50))
        }
    }

    private func spokenTurns(_ client: APIClient) async throws -> [UserItem] {
        guard case .value(let feed, _) = try await client.feed("tundra") else { return [] }
        return (feed.content?.items ?? []).compactMap { if case .user(let u) = $0, u.via == "voice" { return u } else { return nil } }
    }

    /// A spoken question that needs tools: it is sent with voice=1 and recorded as spoken; the narration and the
    /// answer are spoken in order; the working sound plays from the first tool_start through both tools with no gap
    /// (one on, one off) and stops before the answer is spoken; the reply's words never reach the transcript.
    func testASpokenTurnWithToolsIsSentSpokenAndTheWorkingSoundBridgesBothTools() async throws {
        let log = Log()
        let (session, client) = try await session([(try spoken("Please use a tool to check the garden."), at: 0.5)],
                                                  log: log)
        defer { Task { await session.end() } }
        try await wait(40, "the reply to be spoken") { log.first("start The lantern is beside the door.") != nil }
        try await wait(15, "listening again") { session.state == .listening && log.first("finish The lantern") != nil }

        let turns = try await spokenTurns(client)
        XCTAssertEqual(turns.count, 1, "one spoken turn: \(turns.map(\.text))")
        let turn = try XCTUnwrap(turns.first)
        XCTAssertEqual(turn.via, "voice")
        XCTAssertTrue(turn.text.lowercased().contains("tool"), turn.text)
        for word in ["gate", "lantern", "notes"] {
            XCTAssertFalse(turn.text.lowercased().contains(word), "the reply leaked into the transcript: \(turn.text)")
        }

        let queued = log.all.map(\.what).filter { $0.hasPrefix("queue ") }
        XCTAssertEqual(queued, ["queue I will check the garden notes.", "queue The garden gate is open.",
                                "queue The lantern is beside the door."])
        let on = log.times("working on"), off = log.times("working off")
        XCTAssertEqual(on.count, 1, "the working sound must not stop between back-to-back tools: \(log.all.map(\.what))")
        XCTAssertEqual(off.count, 1)
        if let start = on.first, let stop = off.first {
            XCTAssertGreaterThan(stop - start, 3.0, "two tools of 1.5 s with 0.3 s between: it covers both")
            let answer = try XCTUnwrap(log.first("queue The garden gate"))
            XCTAssertLessThanOrEqual(stop, answer + 0.05, "the working sound stops when the answer starts")
        }
        let latency = try XCTUnwrap(session.latency)
        XCTAssertGreaterThan(latency, 0)
        XCTAssertLessThan(latency, 8)
        print("e2e tool turn: latency \(String(format: "%.2f", latency)) s;",
              log.all.map { String(format: "%.2f %@", $0.at - log.all[0].at, $0.what) })
    }

    /// Barge-in: while a long answer is spoken, you talk; its speech stops at once, your words go in as a spoken
    /// interjection, and the instance's answer to it — a reply of its own after the long one — is followed and spoken.
    func testABargeInStopsTheSpeechSendsAnInterjectionAndItsAnswerIsSpoken() async throws {
        let log = Log()
        let (session, client) = try await session([
            (try spoken("Tell me the long story now."), at: 0.5),
            (try spoken("Wait, what about the lantern by the door?"), at: 10),
        ], log: log)
        defer { Task { await session.end() } }
        try await wait(60, "the answer to the interjection to be spoken") {
            log.first("finish The lantern is beside the door.") != nil && session.state == .listening
        }

        let turns = try await spokenTurns(client)
        XCTAssertEqual(turns.count, 2, "\(turns.map(\.text))")
        XCTAssertEqual(turns.map(\.via), ["voice", "voice"])
        XCTAssertTrue(turns.first?.text.lowercased().contains("long") ?? false)
        // Whole, nothing lost or added at its start (M3: "to. Wait, …" when the transcriber was asked to finalize early).
        XCTAssertTrue(turns.last?.text.lowercased().hasPrefix("wait") ?? false, "\(turns.last?.text ?? "")")

        let stop = try XCTUnwrap(log.first("stopAll"), "the speech was never stopped")
        XCTAssertGreaterThan(stop - log.all[0].at, 10, "not before you spoke")
        let started = log.all.filter { $0.what.hasPrefix("start Lantern number") }
        XCTAssertGreaterThan(started.count, 0, "the long answer was being spoken")
        XCTAssertLessThan(started.count, 10, "barge-in cut the long answer short")
        XCTAssertTrue(started.allSatisfy { $0.at < stop }, "nothing of the long answer starts after the barge-in")
        let answer = try XCTUnwrap(log.first("queue The garden gate is open."), "the interjection's answer was not spoken")
        XCTAssertGreaterThan(answer, stop)
        print("e2e barge-in:", log.all.map { String(format: "%.2f %@", $0.at - log.all[0].at, $0.what) })
    }
}
#endif
