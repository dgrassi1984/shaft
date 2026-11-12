#if os(macOS)
import CryptoKit
import Foundation
import Testing
@testable import SHAFTKit

// The setup engine against a scripted machine: a fake runner answers every command the Installer runs and makes the
// files a real command would (the clone's .git, uv in .runtime/bin), all under a temporary home. Nothing here touches
// the real ~/SHAFT, the Keychain or launchd; the one test of the real ProcessRunner runs /bin/sh only.

/// One command the Installer ran.
struct RanCommand: Sendable {
    let exe: URL
    let args: [String]
    let env: [String: String]
    let stdin: Data?

    var line: String { ([exe.lastPathComponent] + args).joined(separator: " ") }
}

/// A runner whose answers come from a script; it records every call.
final class FakeRunner: ProcessRunning, @unchecked Sendable {
    typealias Script = @Sendable (RanCommand) throws -> ProcessResult
    private let lock = NSLock()
    private var log: [RanCommand] = []
    private let script: Script

    init(_ script: @escaping Script) { self.script = script }

    var calls: [RanCommand] { lock.withLock { log } }
    func reset() { lock.withLock { log = [] } }

    func run(_ exe: URL, _ args: [String], env: [String: String], stdin: Data?,
             onLine: (@Sendable (String) -> Void)?) async throws -> ProcessResult {
        let call = RanCommand(exe: exe, args: args, env: env, stdin: stdin)
        lock.withLock { log.append(call) }
        let result = try script(call)
        if let onLine {
            for line in String(decoding: result.stdout, as: UTF8.self).split(separator: "\n") { onLine(String(line)) }
        }
        return result
    }
}

/// A downloader that writes fixed bytes and counts its downloads.
final class FakeDownloader: Downloading, @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    private var urls: [URL] = []
    let bytes: Data

    init(_ bytes: Data) { self.bytes = bytes }

    var downloads: Int { lock.withLock { count } }
    var requested: [URL] { lock.withLock { urls } }

    func download(_ url: URL, to destination: URL) async throws {
        lock.withLock { count += 1; urls.append(url) }
        try bytes.write(to: destination)
    }
}

func ok(_ out: String = "", _ err: String = "") -> ProcessResult {
    ProcessResult(status: 0, stdout: Data(out.utf8), stderr: Data(err.utf8))
}

func failed(_ status: Int32, _ err: String, _ out: String = "") -> ProcessResult {
    ProcessResult(status: status, stdout: Data(out.utf8), stderr: Data(err.utf8))
}

/// A healthy Mac as the Installer sees it, with knobs for the failures the tests need.
final class Machine: @unchecked Sendable {
    let home: URL
    let lock = NSLock()
    var toolsInstalled = true
    var toolsArriveAfterPolls = 0
    var tags = ["v0.55.0", "v0.56.0", "v0.57.0", "template-v0.55.0", "template-v0.56.0"]
    var installLines = [
        #"{"step": "identity", "ok": true, "detail": "set Ada <ada@example.com> for this repository"}"#,
        #"{"step": "setup", "ok": true, "detail": "done"}"#,
        #"{"step": "api", "ok": true, "detail": "running"}"#,
        #"{"step": "web", "ok": true, "detail": "running"}"#,
        #"{"step": "scheduler", "ok": true, "detail": "running"}"#,
    ]
    var installStatus: Int32 = 0
    var pairJSON = #"{"ok": true, "base_url": "http://127.0.0.1:8090", "device": "d-123", "secret": "s3cr3t-bearer", "name": "mac-app", "login": "local@mac"}"#
    var templateBranch = false
    var pythonInstalled = false
    var origin = "https://github.com/dgrassi1984/shaft.git"
    /// What `uv --version` fails with once uv is unpacked (nil: it answers the pinned version).
    var uvVersionError: String?
    /// What `uv python find` fails with other than "not found" (nil: found or not found).
    var pythonFindError: String?
    private var polls = 0

    init(home: URL) { self.home = home }

    var server: URL { home.appending(path: "SHAFT") }
    var python: String { server.appending(path: ".runtime/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12").path }

    func answer(_ call: RanCommand) throws -> ProcessResult {
        try lock.withLock { try answerLocked(call) }
    }

    private func answerLocked(_ call: RanCommand) throws -> ProcessResult {
        let a = call.args
        switch call.exe.lastPathComponent {
        case "xcode-select":
            if a == ["-p"] {
                if toolsInstalled { return ok("/Library/Developer/CommandLineTools\n") }
                polls += 1
                if toolsArriveAfterPolls > 0, polls > toolsArriveAfterPolls { toolsInstalled = true; return ok("/Library/Developer/CommandLineTools\n") }
                return failed(2, "xcode-select: error: unable to get active developer directory")
            }
            if a == ["--install"] { return ok("", "xcode-select: note: install requested for command line developer tools\n") }
        case "git":
            if a.first == "ls-remote" {
                return ok(tags.map { "abc123\trefs/tags/\($0)" }.joined(separator: "\n") + "\n")
            }
            if a.first == "clone" {
                let target = URL(fileURLWithPath: a.last!)
                try FileManager.default.createDirectory(at: target.appending(path: ".git"), withIntermediateDirectories: true)
                try FileManager.default.createDirectory(at: target.appending(path: "bin"), withIntermediateDirectories: true)
                try Data("#!/bin/sh\n".utf8).write(to: target.appending(path: "bin/shaft"))
                return ok("", "Cloning into '\(target.path)'...\n")
            }
            if a.count >= 3, a[0] == "-C" {
                let rest = Array(a.dropFirst(2))
                switch rest.first {
                case "remote": return ok(origin + "\n")
                case "rev-parse": return templateBranch ? ok("abc123\n") : failed(1, "")
                case "describe": return ok("v0.56.0\n")
                case "fetch": return ok()
                case "branch": templateBranch = true; return ok()
                default: break
                }
            }
        case "tar":
            // -xzf <tarball> -C <bin> --strip-components 1
            let bin = URL(fileURLWithPath: a[a.firstIndex(of: "-C")! + 1])
            for name in ["uv", "uvx"] { try Data("binary".utf8).write(to: bin.appending(path: name)) }
            return ok()
        case "xattr":
            return failed(1, "xattr: \(a.last!): No such xattr: com.apple.quarantine\n")
        case "uv":
            if a == ["--version"], let uvVersionError { return failed(1, uvVersionError) }
            if a == ["--version"] { return ok("uv \(UVPin.current.version) (abc 2026-11-03 aarch64-apple-darwin)\n") }
            if a.starts(with: ["python", "find"]), let pythonFindError { return failed(2, pythonFindError) }
            if a.starts(with: ["python", "find"]) {
                return pythonInstalled ? ok(python + "\n") : failed(2, "error: No interpreter found for Python 3.12 in managed installations")
            }
            if a.starts(with: ["python", "install"]) {
                // uv makes the interpreter: a real file, so the containment check can resolve it.
                let file = URL(fileURLWithPath: python)
                try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
                try Data("python".utf8).write(to: file)
                pythonInstalled = true
                return ok("", "Installed Python 3.12.12\n")
            }
        case "shaft":
            if a.first == "install" {
                return ProcessResult(status: installStatus, stdout: Data((installLines.joined(separator: "\n") + "\n").utf8), stderr: Data())
            }
            if a.starts(with: ["web", "pair"]) {
                return ok(pairJSON + "\n")
            }
            if a.starts(with: ["google", "client"]) { return ok(#"{"ok": true}"# + "\n") }
        default: break
        }
        throw InstallerTestError("the fake machine has no answer for: \(call.line)")
    }
}

struct InstallerTestError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

let uvBytes = Data("pretend uv tarball".utf8)

func hex(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }

@MainActor
func makeInstaller(_ machine: Machine, runner: any ProcessRunning, downloader: FakeDownloader, store: any CredentialStore,
                   pinnedHash: String? = nil, source: ServerSource? = nil) -> Installer {
    let pin = UVPin(version: UVPin.current.version, sha256: ["aarch64-apple-darwin": pinnedHash ?? hex(uvBytes)])
    let config = Installer.Configuration(home: machine.home, uv: pin, arch: "aarch64-apple-darwin",
                                         pollInterval: .milliseconds(1), toolsTimeout: .milliseconds(5), serverSource: source,
                                         environment: ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/nowhere",
                                                       "VIRTUAL_ENV": "/somewhere/venv"])
    return Installer(operatorName: "Ada", email: "ada@example.com", runner: runner, downloader: downloader, store: store,
                     configuration: config, sleep: { _ in })
}

@MainActor
struct SetupTests {
    @Test func aFreshMacRunsEveryStepInOrder() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        let runner = FakeRunner(machine.answer)
        let store = MemoryCredentialStore()
        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: store)

        await installer.run()

        #expect(InstallStep.allCases == [.git, .server, .runtime, .configure, .pair])
        for step in InstallStep.allCases { #expect(installer.steps[step] == .done, "\(step): \(String(describing: installer.steps[step]))") }
        #expect(installer.version == "v0.56.0")   // v0.57.0 has no template tag yet
        let lines = runner.calls.map(\.line)
        let server = machine.server.path
        #expect(lines.contains("git clone --branch v0.56.0 https://github.com/dgrassi1984/shaft \(server)"))
        #expect(lines.contains("git -C \(server) fetch origin tag template-v0.56.0"))
        #expect(lines.contains("git -C \(server) branch -f template template-v0.56.0"))
        #expect(lines.contains("shaft install --operator Ada --email ada@example.com --json"))
        #expect(lines.contains("shaft web pair --local --name mac-app --json"))
        // The interpreter is uv's own, under ~/SHAFT/.runtime, never ~/.cache/uv or a venv.
        #expect(installer.python?.path == machine.python)
        let install = try #require(runner.calls.first { $0.args.first == "install" })
        #expect(install.env["SHAFT_PYTHON"] == machine.python)
        #expect(install.env["PATH"] == machine.server.appending(path: ".runtime/bin").path + ":/usr/bin:/bin:/usr/sbin:/sbin")
        #expect(install.env["HOME"] == home.path)
        #expect(install.env["VIRTUAL_ENV"] == nil)
        let pythonInstall = try #require(runner.calls.first { $0.args.starts(with: ["python", "install"]) })
        #expect(pythonInstall.env["UV_PYTHON_INSTALL_DIR"] == machine.server.appending(path: ".runtime/python").path)
        #expect(pythonInstall.args.contains("--no-bin"))
        // The configure step's lines are its sub-rows, in order.
        #expect(installer.subRows.map(\.step) == ["identity", "setup", "api", "web", "scheduler"])
        #expect(installer.subRows.allSatisfy { $0.ok })
    }

    @Test func everyStepIsIdempotentOnRerun() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        let runner = FakeRunner(machine.answer)
        let downloader = FakeDownloader(uvBytes)
        let store = MemoryCredentialStore()
        let first = makeInstaller(machine, runner: runner, downloader: downloader, store: store)
        await first.run()
        let paired = try #require(try store.load())

        // The same Installer again: every step is done, so nothing runs at all.
        runner.reset()
        await first.run()
        #expect(runner.calls.isEmpty, "\(runner.calls.map(\.line))")

        // Reopening the app (a new Installer) over the finished install: every step is a no-op success.
        let second = makeInstaller(machine, runner: runner, downloader: downloader, store: store)
        await second.run()
        for step in InstallStep.allCases { #expect(second.steps[step] == .done, "\(step): \(String(describing: second.steps[step]))") }
        let lines = runner.calls.map(\.line)
        #expect(downloader.downloads == 1)                                  // uv is not downloaded again
        #expect(!lines.contains { $0.hasPrefix("git clone") })              // the clone is kept
        #expect(!lines.contains { $0.contains("fetch origin tag") })
        #expect(!lines.contains { $0.hasPrefix("tar ") })
        #expect(!lines.contains { $0.hasPrefix("uv python install") })
        #expect(!lines.contains { $0.hasPrefix("xcode-select --install") })
        #expect(!lines.contains { $0.contains("web pair") })                // no second device paired
        #expect(try store.load() == paired)
        #expect(second.credential == paired)
        #expect(second.python?.path == machine.python)
        // `shaft install` is the server's own idempotent check (a rerun changes nothing): it is the one thing rerun.
        #expect(lines.filter { $0.hasPrefix("shaft install") }.count == 1)
    }

    @Test func aFailedStepShowsStderrAndStops() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        let offline = OfflineFlag(true)
        let runner = FakeRunner { call in
            if call.args.first == "clone", offline.value {
                return failed(128, "fatal: unable to access 'https://github.com/dgrassi1984/shaft/': Could not resolve host: github.com\n")
            }
            return try machine.answer(call)
        }
        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore())
        await installer.run()

        #expect(installer.steps[.git] == .done)
        guard case .failed(let message) = installer.steps[.server] else {
            Issue.record("the server step did not fail: \(String(describing: installer.steps[.server]))"); return
        }
        #expect(message.contains("Could not resolve host: github.com"))
        #expect(message.contains("exit 128"))
        // It stops there: nothing after the failed step ran.
        for step in [InstallStep.runtime, .configure, .pair] { #expect(installer.steps[step] == .pending) }
        #expect(!runner.calls.contains { $0.exe.lastPathComponent == "tar" || $0.exe.lastPathComponent == "uv" })

        // With the network back, Retry re-runs the failed step and those after it; the done step before it is not rerun.
        offline.value = false
        runner.reset()
        await installer.retry(.server)
        for step in InstallStep.allCases { #expect(installer.steps[step] == .done, "\(step)") }
        #expect(!runner.calls.contains { $0.exe.lastPathComponent == "xcode-select" })
        #expect(runner.calls.first?.args.first == "ls-remote")
    }

    @Test func retryRerunsOnlyTheFailedStepAndThoseAfter() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        machine.installStatus = 1
        machine.installLines = [
            #"{"step": "identity", "ok": true, "detail": "kept"}"#,
            #"{"step": "setup", "ok": false, "detail": "", "error": "cannot create the template worktree: disk full"}"#,
        ]
        let runner = FakeRunner(machine.answer)
        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore())
        await installer.run()
        #expect(installer.steps[.configure] == .failed("cannot create the template worktree: disk full"))
        #expect(installer.steps[.pair] == .pending)
        #expect(installer.subRows.map(\.ok) == [true, false])

        machine.installStatus = 0
        machine.installLines = [#"{"step": "identity", "ok": true, "detail": "kept"}"#,
                                #"{"step": "setup", "ok": true, "detail": "done"}"#]
        runner.reset()
        await installer.retry(.configure)
        #expect(installer.steps[.configure] == .done)
        #expect(installer.steps[.pair] == .done)
        let lines = runner.calls.map(\.line)
        #expect(lines.first?.hasPrefix("shaft install") == true, "\(lines)")
        #expect(!lines.contains { $0.hasPrefix("xcode-select") || $0.hasPrefix("git ") || $0.hasPrefix("uv ") })
        #expect(installer.subRows.map(\.ok) == [true, true])   // the failed run's rows are replaced, not appended
    }

    @Test func uvChecksumMismatchFailsTheRuntimeStep() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        let runner = FakeRunner(machine.answer)
        let downloader = FakeDownloader(Data("a tampered tarball".utf8))
        let installer = makeInstaller(machine, runner: runner, downloader: downloader, store: MemoryCredentialStore())
        await installer.run()

        guard case .failed(let message) = installer.steps[.runtime] else {
            Issue.record("the runtime step did not fail: \(String(describing: installer.steps[.runtime]))"); return
        }
        #expect(message.contains("checksum"))
        #expect(message.contains(hex(uvBytes)))                          // the pinned hash
        #expect(message.contains(hex(Data("a tampered tarball".utf8))))   // what came down
        #expect(downloader.requested == [URL(string: "https://github.com/astral-sh/uv/releases/download/\(UVPin.current.version)/uv-aarch64-apple-darwin.tar.gz")!])
        // Checked before unpacking: tar never ran and nothing landed in .runtime/bin.
        #expect(!runner.calls.contains { $0.exe.lastPathComponent == "tar" })
        #expect(!FileManager.default.fileExists(atPath: machine.server.appending(path: ".runtime/bin/uv").path))
        #expect(installer.steps[.configure] == .pending)
    }

    @Test func thePinnedUVIsARealReleaseForBothArchitectures() {
        let pin = UVPin.current
        #expect(pin.version.split(separator: ".").count == 3)
        for arch in ["aarch64-apple-darwin", "x86_64-apple-darwin"] {
            let hash = pin.sha256[arch]
            #expect(hash?.count == 64 && hash?.allSatisfy(\.isHexDigit) == true, "\(arch): \(String(describing: hash))")
        }
        #expect(UVPin.hostArch == "aarch64-apple-darwin" || UVPin.hostArch == "x86_64-apple-darwin")
    }

    @Test func pairStoresTheCredentialFromJSON() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        let runner = FakeRunner(machine.answer)
        let store = MemoryCredentialStore()
        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: store)
        await installer.run()

        let expected = ServerCredential(baseURL: URL(string: "http://127.0.0.1:8090")!, device: "d-123",
                                        secret: "s3cr3t-bearer", name: "mac-app", login: "local@mac")
        #expect(try store.load() == expected)
        #expect(installer.credential == expected)
        #expect(installer.steps[.pair] == .done)
    }

    @Test func aRefusedPairShowsTheServersErrorAndNeverItsOutput() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        machine.pairJSON = #"{"ok": false, "error": "local pairing is off (SHAFT_WEB_LOCAL): run shaft web on --local first"}"#
        let store = MemoryCredentialStore()
        let installer = makeInstaller(machine, runner: FakeRunner(machine.answer), downloader: FakeDownloader(uvBytes), store: store)
        await installer.run()
        #expect(installer.steps[.pair] == .failed("local pairing is off (SHAFT_WEB_LOCAL): run shaft web on --local first"))
        #expect(try store.load() == nil)

        // An answer the app cannot read never puts the bearer on the screen.
        machine.pairJSON = #"{"ok": true, "base_url": "", "device": "d-1", "secret": "s3cr3t-bearer", "name": "mac-app", "login": "x"}"#
        await installer.retry(.pair)
        guard case .failed(let message) = installer.steps[.pair] else { Issue.record("the pair step did not fail"); return }
        #expect(!message.contains("s3cr3t-bearer"))
    }

    @Test func secretsGoOnStdinNeverArgs() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        let runner = FakeRunner(machine.answer)
        let suite = "shaft-setup-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore())
        await installer.run()

        // A wizard verb with a secret: the secret is on stdin, in no argument and no environment value.
        let secret = #"{"installed": {"client_id": "id-1", "client_secret": "GOCSPX-very-secret"}}"#
        _ = try await installer.shaft(["google", "client", "home", "--json"], stdin: Data(secret.utf8))
        let call = try #require(runner.calls.last)
        #expect(call.stdin == Data(secret.utf8))
        #expect(call.env["SHAFT_PYTHON"] == machine.python)
        // No command the setup ran carries a secret in argv or its environment: not the client secret, not the bearer
        // `web pair` printed.
        for ran in runner.calls {
            for text in ran.args + Array(ran.env.values) {
                #expect(!text.contains("GOCSPX-very-secret"), "\(ran.line)")
                #expect(!text.contains("s3cr3t-bearer"), "\(ran.line)")
            }
        }
        // And the wizard's saved state never holds one.
        let states = SetupStateStore(defaults: defaults)
        var state = SetupState()
        state.step = .model
        state.operatorName = "Ada"
        try states.save(state)
        let stored = try #require(defaults.data(forKey: SetupStateStore.key))
        #expect(!String(decoding: stored, as: UTF8.self).contains("s3cr3t-bearer"))
        // Only these fields exist: a new field is a deliberate decision that it is not a secret.
        let keys = try #require(try JSONSerialization.jsonObject(with: stored) as? [String: Any]).keys
        #expect(Set(keys).isSubset(of: ["step", "operatorName", "email", "provider", "model", "instanceName",
                                        "createdInstance", "startedInstance", "drive", "driveText", "autonomy"]),
                "\(keys)")
    }

    @Test func resumesAtThePersistedStep() throws {
        let suite = "shaft-setup-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }

        let store = SetupStateStore(defaults: defaults)
        #expect(try store.load() == SetupState())          // nothing saved: the wizard starts at Welcome
        #expect(try store.load().step == .welcome)

        var state = SetupState()
        state.step = .instance
        state.operatorName = "Ada"
        state.email = "ada@example.com"
        state.provider = "openrouter"
        state.model = "openrouter/x/model"
        state.instanceName = "tundra"
        state.createdInstance = "tundra"
        state.startedInstance = "tundra"
        state.drive = "research"
        state.driveText = "Read widely and report back."
        state.autonomy = "talk"
        try store.save(state)

        // Reopening the app: a new store over the same defaults resumes there, with every input.
        let reopened = SetupStateStore(defaults: try #require(UserDefaults(suiteName: suite)))
        #expect(try reopened.load() == state)

        // A state saved by an older app (fewer fields) still loads, at its step.
        defaults.set(Data(#"{"step": "model", "operatorName": "Ada"}"#.utf8), forKey: SetupStateStore.key)
        let older = try reopened.load()
        #expect(older.step == .model)
        #expect(older.operatorName == "Ada")
        #expect(older.autonomy == "own")

        // A corrupt value is an error, not a silent restart.
        defaults.set(Data(#"{"step": "nowhere"}"#.utf8), forKey: SetupStateStore.key)
        #expect(throws: (any Error).self) { try reopened.load() }

        reopened.clear()
        #expect(try reopened.load() == SetupState())
    }

    @Test func missingCommandLineToolsAskTheSystemAndWait() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        machine.toolsInstalled = false
        machine.toolsArriveAfterPolls = 3
        let runner = FakeRunner(machine.answer)
        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore())
        await installer.run()
        #expect(installer.steps[.git] == .done)
        #expect(runner.calls.filter { $0.line == "xcode-select --install" }.count == 1)

        // Never installed: the step gives up after the timeout and says what to do.
        let other = Machine(home: home)
        other.toolsInstalled = false
        let waiting = makeInstaller(other, runner: FakeRunner(other.answer), downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore())
        await waiting.run()
        guard case .failed(let message) = waiting.steps[.git] else { Issue.record("the git step did not fail"); return }
        #expect(message.contains("Command Line Tools"))
        #expect(waiting.steps[.server] == .pending)
    }

    @Test func noTemplateTagFailsTheServerStep() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        machine.tags = ["v0.56.0", "v0.57.0"]
        let installer = makeInstaller(machine, runner: FakeRunner(machine.answer), downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore())
        await installer.run()
        #expect(installer.steps[.server] == .failed("no release with a template tag yet (template-vX.Y.Z)"))
    }

    @Test func aFolderThatIsNotSHAFTIsNeverTouched() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        let mine = machine.server.appending(path: "notes.txt")
        try FileManager.default.createDirectory(at: machine.server, withIntermediateDirectories: true)
        try Data("mine".utf8).write(to: mine)
        let runner = FakeRunner(machine.answer)
        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore())
        await installer.run()
        guard case .failed(let message) = installer.steps[.server] else { Issue.record("the server step did not fail"); return }
        #expect(message.contains(machine.server.path))
        #expect(try Data(contentsOf: mine) == Data("mine".utf8))
        #expect(!runner.calls.contains { $0.args.first == "clone" })

        // A checkout of another repository is refused the same way.
        try FileManager.default.removeItem(at: mine)
        try FileManager.default.createDirectory(at: machine.server.appending(path: ".git"), withIntermediateDirectories: true)
        let other = FakeRunner { call in
            if call.args.count > 2, call.args[2] == "remote" { return ok("https://github.com/someone/else.git\n") }
            return try machine.answer(call)
        }
        let second = makeInstaller(machine, runner: other, downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore())
        await second.run()
        guard case .failed(let refused) = second.steps[.server] else { Issue.record("another checkout was accepted"); return }
        #expect(refused.contains(machine.server.path))
        #expect(refused.contains("someone/else"))
    }

    @Test func aUVThatFailsAfterUnpackingFailsWithItsStderr() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        machine.uvVersionError = "dyld[4242]: Library not loaded: /usr/lib/libmissing.dylib\n  Reason: tried: '/usr/lib/libmissing.dylib' (no such file)\n"
        let runner = FakeRunner(machine.answer)
        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore())
        await installer.run()
        guard case .failed(let message) = installer.steps[.runtime] else {
            Issue.record("the runtime step did not fail: \(String(describing: installer.steps[.runtime]))"); return
        }
        #expect(message.contains("dyld[4242]: Library not loaded: /usr/lib/libmissing.dylib"), "\(message)")
        #expect(!runner.calls.contains { $0.args.starts(with: ["python"]) })
    }

    @Test func aFailingPythonFindFailsWithItsStderrAndInstallsNothing() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        machine.pythonFindError = "error: Failed to discover managed Python installations\n  cause: failed to read directory `/x/.runtime/python`: Permission denied (os error 13)\n"
        let runner = FakeRunner(machine.answer)
        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: MemoryCredentialStore())
        await installer.run()
        guard case .failed(let message) = installer.steps[.runtime] else {
            Issue.record("the runtime step did not fail: \(String(describing: installer.steps[.runtime]))"); return
        }
        #expect(message.contains("Permission denied (os error 13)"), "\(message)")
        // An operational failure is not "Python is absent": nothing is installed over it.
        #expect(!runner.calls.contains { $0.args.starts(with: ["python", "install"]) })
    }

    @Test func anInterpreterThatResolvesOutsideTheRuntimeIsRefused() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let elsewhere = try temporaryFolder("uv-cache")
        defer { try? FileManager.default.removeItem(at: elsewhere) }
        let machine = Machine(home: home)
        // Lexically under .runtime/python, really in another folder (as if ~/.cache/uv).
        let outside = elsewhere.appending(path: "python3.12")
        try Data("python".utf8).write(to: outside)
        let link = URL(fileURLWithPath: machine.python)
        try FileManager.default.createDirectory(at: link.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: outside)
        machine.pythonInstalled = true
        // ~/SHAFT is a finished checkout, so the run reaches the runtime step.
        try FileManager.default.createDirectory(at: machine.server.appending(path: ".git"), withIntermediateDirectories: true)
        machine.templateBranch = true
        let installer = makeInstaller(machine, runner: FakeRunner(machine.answer), downloader: FakeDownloader(uvBytes),
                                      store: MemoryCredentialStore())
        await installer.run()
        guard case .failed(let message) = installer.steps[.runtime] else {
            Issue.record("accepted: \(String(describing: installer.python)), steps \(String(describing: installer.steps))"); return
        }
        #expect(message.contains("outside"), "\(message)")
        #expect(installer.python == nil)
        #expect(installer.steps[.configure] == .pending)
    }

    @Test func anInjectedServerSourceClonesItWithoutTheReleaseLookup() async throws {
        let home = try temporaryFolder("setup")
        defer { try? FileManager.default.removeItem(at: home) }
        let machine = Machine(home: home)
        let source = ServerSource(url: "/Users/dev/src/shaft", ref: "feature/onboarding", templateRef: "template")
        machine.origin = source.url
        let runner = FakeRunner(machine.answer)
        let store = MemoryCredentialStore()
        let installer = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: store, source: source)
        await installer.run()
        for step in InstallStep.allCases { #expect(installer.steps[step] == .done, "\(step)") }
        let lines = runner.calls.map(\.line)
        let server = machine.server.path
        #expect(!lines.contains { $0.hasPrefix("git ls-remote") })
        #expect(lines.contains("git clone --branch feature/onboarding /Users/dev/src/shaft \(server)"))
        #expect(lines.contains("git -C \(server) fetch origin template"))
        #expect(lines.contains("git -C \(server) branch -f template FETCH_HEAD"))
        #expect(installer.version == "feature/onboarding")

        // Rerun over it: the checkout of that source is kept.
        runner.reset()
        let again = makeInstaller(machine, runner: runner, downloader: FakeDownloader(uvBytes), store: store, source: source)
        await again.run()
        #expect(again.steps[.server] == .done)
        #expect(!runner.calls.contains { $0.args.first == "clone" || $0.args.first == "ls-remote" })
    }

    @Test func aStoredValueOfTheWrongTypeIsAnError() throws {
        let suite = "shaft-setup-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set("welcome", forKey: SetupStateStore.key)
        #expect(throws: (any Error).self) { try SetupStateStore(defaults: defaults).load() }
        // Kept progress, readable or not, is a wizard started and not finished; clear() ends it.
        #expect(SetupStateStore(defaults: defaults).hasSaved)
        SetupStateStore(defaults: defaults).clear()
        #expect(!SetupStateStore(defaults: defaults).hasSaved)
    }

    @Test func cancellingReturnsWhileADescendantHoldsThePipes() async throws {
        let runner = ProcessRunner()
        let started = ContinuousClock.now
        // The shell exits at once; the background sleep keeps stdout and stderr open for 20 s.
        let task = Task { try await runner.run(URL(fileURLWithPath: "/bin/sh"), ["-c", "sleep 20 & exit 0"],
                                               env: ["PATH": "/bin"], stdin: nil) }
        try await Task.sleep(for: .milliseconds(500))
        task.cancel()
        await #expect(throws: CancellationError.self) { _ = try await task.value }
        #expect(ContinuousClock.now - started < .seconds(10))
    }

    @Test func theRealRunnerCapturesBothStreamsFeedsStdinAndCancels() async throws {
        let runner = ProcessRunner()
        let lines = LineLog()
        let result = try await runner.run(URL(fileURLWithPath: "/bin/sh"),
                                          ["-c", "cat; echo second; echo oops >&2; exit 3"],
                                          env: ["PATH": "/usr/bin:/bin"], stdin: Data("first\n".utf8),
                                          onLine: { lines.append($0) })
        #expect(result.status == 3)
        #expect(String(decoding: result.stdout, as: UTF8.self) == "first\nsecond\n")
        #expect(String(decoding: result.stderr, as: UTF8.self) == "oops\n")
        #expect(lines.all == ["first", "second"])

        let echoed = try await runner.run(URL(fileURLWithPath: "/bin/echo"), ["hello"], env: [:], stdin: nil)
        #expect(String(decoding: echoed.stdout, as: UTF8.self) == "hello\n")

        // A program that cannot start is an error that names it.
        await #expect(throws: (any Error).self) {
            _ = try await runner.run(URL(fileURLWithPath: "/nonexistent/tool"), [], env: [:], stdin: nil)
        }

        // Cancelling stops the process instead of waiting for it.
        let started = ContinuousClock.now
        let task = Task { try await runner.run(URL(fileURLWithPath: "/bin/sh"), ["-c", "exec sleep 30"], env: ["PATH": "/bin"], stdin: nil) }
        try await Task.sleep(for: .milliseconds(300))
        task.cancel()
        await #expect(throws: CancellationError.self) { _ = try await task.value }
        #expect(ContinuousClock.now - started < .seconds(15))
    }
}

final class OfflineFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var on: Bool
    init(_ on: Bool) { self.on = on }
    var value: Bool {
        get { lock.withLock { on } }
        set { lock.withLock { on = newValue } }
    }
}

final class LineLog: @unchecked Sendable {
    private let lock = NSLock()
    private var lines: [String] = []
    func append(_ line: String) { lock.withLock { lines.append(line) } }
    var all: [String] { lock.withLock { lines } }
}
#endif
