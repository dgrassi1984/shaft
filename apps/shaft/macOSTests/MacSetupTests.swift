import Foundation
import SHAFTKit
import Testing

/// MacSetup's own lifecycle (macOS/Setup/MacSetup.swift) with a scripted runner over a temporary home: nothing here
/// touches the real ~/SHAFT, the Keychain or launchd.
@MainActor
struct MacSetupTests {
    /// Fix round 1: closing the setup window while Continue's pairing is suspended opens no session, keeps no
    /// credential and leaves the saved step where it was, even when the command answers afterwards.
    @Test func closingTheSetupWhilePairingIsSuspendedOpensNoSession() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "mac-setup-\(UUID().uuidString)", directoryHint: .isDirectory)
        defer { try? FileManager.default.removeItem(at: root) }
        let home = root.appending(path: "home", directoryHint: .isDirectory)
        // An install already there: the checkout, uv and uv's Python under ~/SHAFT/.runtime.
        let server = home.appending(path: "SHAFT", directoryHint: .isDirectory)
        let python = server.appending(path: ".runtime/python/cpython-3.12-macos/bin/python3.12")
        for file in [server.appending(path: ".runtime/bin/uv"), server.appending(path: "bin/shaft"), python] {
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data("x".utf8).write(to: file)
        }
        try FileManager.default.createDirectory(at: server.appending(path: ".git"), withIntermediateDirectories: true)

        let suite = "mac-setup-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let states = SetupStateStore(defaults: defaults)
        var saved = SetupState()
        saved.step = .install
        saved.operatorName = "Ada"
        try states.save(saved)

        let runner = PairHeldRunner(python: python.path)
        var configuration = Installer.Configuration(home: home)
        configuration.environment = ["PATH": "/usr/bin:/bin"]
        let setup = MacSetup(states: states, configure: { configuration }, runner: runner)
        let credentials = MemoryCredentialStore()
        let session = SHAFTSession(pairing: PairingStore(store: credentials),
                                   outbox: try Outbox(directory: root.appending(path: "outbox")),
                                   drafts: DraftStore(directory: root.appending(path: "drafts")))
        setup.update(session)
        #expect(setup.route == .wizard)

        let task = try #require(setup.continueInstall(session))
        for _ in 0..<400 where !runner.holding { try await Task.sleep(for: .milliseconds(5)) }
        #expect(runner.holding)

        setup.stopInstall()                    // the window closed while `shaft web pair` was still running
        runner.release()                       // it answers anyway
        await task.value

        #expect(session.store == nil)
        #expect(session.credential == nil)
        #expect(try credentials.load() == nil)
        #expect(try states.load().step == .install)
        #expect(setup.state.step == .install)
        #expect(setup.route == .wizard)
        #expect(!setup.installing)
    }
}

/// Answers uv's Python lookup, and holds `shaft web pair` open until released, ignoring cancellation.
private final class PairHeldRunner: ProcessRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var waiting: CheckedContinuation<Void, Never>?
    private var released = false
    let python: String

    init(python: String) { self.python = python }

    var holding: Bool { lock.withLock { waiting != nil } }

    func release() {
        let continuation: CheckedContinuation<Void, Never>? = lock.withLock {
            released = true
            defer { waiting = nil }
            return waiting
        }
        continuation?.resume()
    }

    func run(_ exe: URL, _ args: [String], env: [String: String], stdin: Data?,
             onLine: (@Sendable (String) -> Void)?) async throws -> ProcessResult {
        if exe.lastPathComponent == "uv", args.starts(with: ["python", "find"]) {
            return ProcessResult(status: 0, stdout: Data((python + "\n").utf8), stderr: Data())
        }
        if exe.lastPathComponent == "shaft", args.starts(with: ["web", "pair"]) {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                let now = lock.withLock { () -> Bool in
                    if released { return true }
                    waiting = continuation
                    return false
                }
                if now { continuation.resume() }
            }
            let answer = #"{"ok": true, "base_url": "http://127.0.0.1:8090", "device": "d-1", "secret": "s", "name": "mac-app", "login": "local@mac"}"#
            return ProcessResult(status: 0, stdout: Data((answer + "\n").utf8), stderr: Data())
        }
        return ProcessResult(status: 1, stdout: Data(), stderr: Data("no answer for \(exe.lastPathComponent) \(args)".utf8))
    }
}
