#if os(macOS)
import Foundation
import Testing
@testable import SHAFTKit

// The Mac app pairs with the server on this Mac without a pasted link: the Installer's pair step makes the credential
// (`shaft web pair --local`) and the session opens it directly. Nothing here touches the Keychain, ~/SHAFT or launchd.

@MainActor
struct LocalPairingTests {
    private func session(_ store: MemoryCredentialStore, _ server: StubServer, folder: URL) throws -> SHAFTSession {
        SHAFTSession(pairing: PairingStore(store: store, session: server.session),
                     outbox: try Outbox(directory: folder.appending(path: "outbox")),
                     drafts: DraftStore(directory: folder.appending(path: "drafts")), session: server.session)
    }

    @Test func pairWithACredentialOpensTheStoreWithoutALink() async throws {
        let folder = try temporaryFolder("local-pair")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in .json(200, #"{"instances": []}"#) }
        let store = MemoryCredentialStore()
        let session = try session(store, server, folder: folder)
        #expect(session.store == nil)

        #expect(await session.pair(credential: server.credential))

        #expect(session.store != nil)
        #expect(session.credential == server.credential)
        #expect(try store.load() == server.credential)      // kept through the PairingStore
        #expect(session.problem == nil)
        #expect(server.requests.isEmpty)                    // no POST /app/v1/pair: the credential is already made
    }

    @Test func aCredentialThatCannotBeKeptIsTheProblemAndOpensNothing() async throws {
        let folder = try temporaryFolder("local-pair")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in .json(200, "{}") }
        let session = SHAFTSession(pairing: PairingStore(store: RefusingStore(), session: server.session),
                                   outbox: try Outbox(directory: folder.appending(path: "outbox")),
                                   drafts: DraftStore(directory: folder.appending(path: "drafts")), session: server.session)

        #expect(await !session.pair(credential: server.credential))

        #expect(session.store == nil)
        #expect(try #require(session.problem).contains("keeping the pairing"))
    }

    @Test func aLostLocalPairingNamesItsServerAndAnOperatorUnpairDoesNot() async throws {
        let folder = try temporaryFolder("local-pair")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in .json(401, #"{"error": {"code": "unpaired", "message": "Pair this device again."}}"#) }
        let store = MemoryCredentialStore()
        let session = try session(store, server, folder: folder)
        #expect(await session.pair(credential: server.credential))
        #expect(session.loss == nil)

        // 401 unpaired on any request: the credential is deleted and the session says which server dropped it.
        await session.store?.refresh()
        #expect(session.store == nil)
        #expect(try store.load() == nil)
        #expect(session.loss == .server(server.baseURL))

        // Pairing again clears it; the operator's own Forget is a loss of another kind.
        #expect(await session.pair(credential: server.credential))
        #expect(session.loss == nil)
        session.forget()
        #expect(session.loss == .operatorChose)
    }

    @Test func runOnlyPairRunsJustThePairStepOnAnExistingInstall() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        let runner = FakeRunner(machine.answer)
        let store = MemoryCredentialStore()
        // An install made earlier: the checkout, uv and its Python are there.
        await makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore()).run()
        runner.reset()

        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: store)
        installer.operatorName = ""                      // the root gate knows no name: configure must not run
        await installer.run(only: .pair)

        #expect(installer.steps[.pair] == .done)
        for step in [InstallStep.git, .server, .runtime, .configure] { #expect(installer.steps[step] == .pending) }
        let lines = runner.calls.map(\.line)
        #expect(lines.allSatisfy { $0.hasPrefix("uv python find") || $0.hasPrefix("shaft web pair") }, "\(lines)")
        #expect(lines.contains("shaft web pair --local --name mac-app --json"))
        #expect(try store.load() == installer.credential)
        #expect(installer.credential?.baseURL == URL(string: "http://127.0.0.1:8090"))

        // A failure is the step's real error, and Try again (run(only:) again) reruns it.
        machine.pairJSON = #"{"ok": false, "error": "the web service is not running"}"#
        try store.delete()
        await installer.run(only: .pair)
        #expect(installer.steps[.pair] == .failed("the web service is not running"))
    }

    @Test func linesArrivingAfterACancelNeverReachTheSubRows() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        let late = LateLines()
        let runner = FakeRunner { call in
            if call.args.first == "install", late.cancelNext {
                late.cancelNext = false
                throw CancellationError()
            }
            return try machine.answer(call)
        }
        let installer = makeInstaller(machine, runner: LineKeepingRunner(runner, keep: late), downloader: FakeDownloader(uvBytes),
                                      store: MemoryCredentialStore())
        late.cancelNext = true
        await installer.run()
        #expect(installer.steps[.configure] == .pending)    // cancelled: not a failure
        #expect(installer.subRows.isEmpty)

        // The cancelled command's reader thread hands a line over after the cancel: nothing changes.
        late.send(#"{"step": "stale", "ok": false, "error": "from the cancelled run"}"#)
        await Task.yield()
        #expect(installer.subRows.isEmpty)

        await installer.run()
        #expect(installer.subRows.map(\.step) == ["identity", "setup", "api", "web", "scheduler"])
        late.send(#"{"step": "stale", "ok": false, "error": "from the cancelled run"}"#)
        await Task.yield()
        #expect(installer.subRows.map(\.step) == ["identity", "setup", "api", "web", "scheduler"])
        #expect(installer.steps[.configure] == .done)
    }
}

private struct RefusingStore: CredentialStore {
    func load() throws -> ServerCredential? { nil }
    func save(_ credential: ServerCredential) throws { throw KeychainError(status: -25308, what: "keeping the pairing") }
    func delete() throws {}
}

/// Keeps the first `onLine` the configure step hands over, so a test can call it after the command was cancelled.
private final class LateLines: @unchecked Sendable {
    private let lock = NSLock()
    private var kept: (@Sendable (String) -> Void)?
    private var cancel = false

    var cancelNext: Bool {
        get { lock.withLock { cancel } }
        set { lock.withLock { cancel = newValue } }
    }

    func keep(_ onLine: @escaping @Sendable (String) -> Void) { lock.withLock { if kept == nil { kept = onLine } } }
    func send(_ line: String) { lock.withLock { kept }?(line) }
}

private struct LineKeepingRunner: ProcessRunning {
    let inner: FakeRunner
    let keep: LateLines

    init(_ inner: FakeRunner, keep: LateLines) {
        self.inner = inner
        self.keep = keep
    }

    func run(_ exe: URL, _ args: [String], env: [String: String], stdin: Data?,
             onLine: (@Sendable (String) -> Void)?) async throws -> ProcessResult {
        if let onLine { keep.keep(onLine) }
        return try await inner.run(exe, args, env: env, stdin: stdin, onLine: onLine)
    }
}
#endif
