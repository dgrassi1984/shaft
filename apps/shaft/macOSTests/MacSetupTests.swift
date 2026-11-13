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

extension MacSetupTests {
    /// Fix round 2: Continue then the window closing before Continue's task first runs (no yield between), over a
    /// finished install whose credential is stored: nothing opens and nothing moves.
    @Test func continueStoppedBeforeItsTaskRunsOpensNoSession() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "mac-setup-\(UUID().uuidString)", directoryHint: .isDirectory)
        defer { try? FileManager.default.removeItem(at: root) }
        let home = root.appending(path: "home", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: home.appending(path: "SHAFT/.git"), withIntermediateDirectories: true)
        let suite = "mac-setup-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let states = SetupStateStore(defaults: defaults)
        var saved = SetupState()
        saved.step = .install
        try states.save(saved)

        let credentials = MemoryCredentialStore()
        let session = SHAFTSession(pairing: PairingStore(store: credentials),
                                   outbox: try Outbox(directory: root.appending(path: "outbox")),
                                   drafts: DraftStore(directory: root.appending(path: "drafts")))
        // The install's pair step finished: its credential is stored, the session not opened on it yet.
        let local = ServerCredential(baseURL: URL(string: "http://127.0.0.1:8090")!, device: "d-1", secret: "s",
                                     name: "mac-app", login: "local@mac")
        try credentials.save(local)
        let runner = PairHeldRunner(python: "/nowhere")
        var configuration = Installer.Configuration(home: home)
        configuration.environment = ["PATH": "/usr/bin:/bin"]
        let setup = MacSetup(states: states, configure: { configuration }, runner: runner)
        setup.update(session)
        let installer = try #require(setup.installer(for: session))
        await installer.run(only: .pair)                  // finds the stored credential: no command runs
        #expect(installer.credential == local)
        #expect(session.store == nil)

        let task = try #require(setup.continueInstall(session))
        setup.stopInstall()                               // before the task has run at all
        await task.value

        #expect(session.store == nil)
        #expect(session.credential == nil)
        #expect(try states.load().step == .install)
        #expect(setup.state.step == .install)
        #expect(setup.route == .wizard)
        #expect(!setup.wizardContinues)
        #expect(!setup.installing)
    }
}

extension MacSetupTests {
    /// Update server (docs/app-api.md §19) updates this Mac's own install: only for a loopback pairing with ~/SHAFT
    /// there, one updater per session, deploying in ~/SHAFT, and only once the server said it runs from there.
    @Test func updateServerIsOnlyForThisMacsOwnInstall() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "mac-setup-\(UUID().uuidString)", directoryHint: .isDirectory)
        defer { try? FileManager.default.removeItem(at: root) }
        let home = root.appending(path: "home", directoryHint: .isDirectory)
        let server = home.appending(path: "SHAFT", directoryHint: .isDirectory)
        let python = server.appending(path: ".runtime/python/cpython-3.12-macos/bin/python3.12")
        let suite = "mac-setup-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        var configuration = Installer.Configuration(home: home)
        configuration.environment = ["PATH": "/usr/bin:/bin"]
        let runner = DeployCheckRunner(python: python.path)
        let setup = MacSetup(states: SetupStateStore(defaults: defaults), configure: { configuration }, runner: runner)

        func session(_ base: String) throws -> SHAFTSession {
            let credentials = MemoryCredentialStore()
            try credentials.save(ServerCredential(baseURL: URL(string: base)!, device: "d-1", secret: "s",
                                                  name: "mac-app", login: "local@mac"))
            let folder = root.appending(path: UUID().uuidString)
            return SHAFTSession(pairing: PairingStore(store: credentials),
                                outbox: try Outbox(directory: folder.appending(path: "outbox")),
                                drafts: DraftStore(directory: folder.appending(path: "drafts")))
        }
        let local = try session("http://127.0.0.1:9")            // a port nothing listens on: no real server is asked
        #expect(local.credential != nil)
        #expect(setup.updater(for: local) == nil)                 // no ~/SHAFT: nothing of this Mac's to update
        for file in [server.appending(path: ".runtime/bin/uv"), server.appending(path: "bin/shaft"), python] {
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data("x".utf8).write(to: file)
        }
        try FileManager.default.createDirectory(at: server.appending(path: ".git"), withIntermediateDirectories: true)
        #expect(setup.updater(for: try session("https://other-mac.example.ts.net:8444")) == nil)   // another Mac's server
        let updater = try #require(setup.updater(for: local))
        #expect(setup.updater(for: local) === updater)            // one per session: its run outlives the Settings window
        #expect(updater.checkout == server)                       // `shaft deploy` runs in ~/SHAFT, nowhere else
        // Nothing answers at 127.0.0.1:9: the server cannot say where it runs from, so nothing is offered and
        // no `shaft deploy` runs (the comparison itself: SHAFTKit's ServerUpdaterTests).
        updater.checkForUpdate()
        await updater.finished()
        guard case .failed = updater.check else {
            Issue.record("expected the unreachable server's failure, got \(updater.check)")
            return
        }
        #expect(runner.shaftCalls.isEmpty)
        // Use SHAFT from iPhone/iPad: one per session, and it ends with the session (closed window, unpairing).
        let phone = try #require(setup.phoneAccess(for: local))
        #expect(setup.phoneAccess(for: local) === phone)
        setup.update(nil)
        #expect(setup.phoneAccess(for: local) !== phone)
        let other = try #require(setup.phoneAccess(for: local))
        #expect(setup.phoneAccess(for: try session("https://other-mac.example.ts.net:8444")) == nil)   // not this Mac's
        #expect(setup.phoneAccess(for: local) !== other)       // and leaving the session dropped the old one
    }
}

/// Answers uv's Python lookup; records each `shaft` call (none is expected).
private final class DeployCheckRunner: ProcessRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var calls: [[String]] = []
    let python: String

    init(python: String) { self.python = python }

    var shaftCalls: [[String]] { lock.withLock { calls } }

    func run(_ exe: URL, _ args: [String], env: [String: String], stdin: Data?,
             onLine: (@Sendable (String) -> Void)?) async throws -> ProcessResult {
        if exe.lastPathComponent == "uv", args.starts(with: ["python", "find"]) {
            return ProcessResult(status: 0, stdout: Data((python + "\n").utf8), stderr: Data())
        }
        if exe.lastPathComponent == "shaft" { lock.withLock { calls.append(args) } }
        return ProcessResult(status: 1, stdout: Data(), stderr: Data("no answer for \(exe.lastPathComponent) \(args)".utf8))
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
