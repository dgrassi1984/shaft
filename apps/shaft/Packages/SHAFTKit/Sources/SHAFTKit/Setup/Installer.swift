#if os(macOS)
import CryptoKit
import Foundation
import Observation

/// The install checklist's rows, in the order they run: the Command Line Tools (git), the server cloned into
/// `~/SHAFT` (a clone needs an empty target, so it comes before anything is written there), uv and Python 3.12 in
/// `~/SHAFT/.runtime`, `shaft install` (git identity, setup, api, web, scheduler), then pairing this app locally.
public enum InstallStep: String, Codable, CaseIterable, Sendable {
    case git, server, runtime, configure, pair
}

public enum StepState: Equatable, Sendable {
    case pending, running, done
    /// The real error: the command's stderr or the server's `error`.
    case failed(String)
}

/// One `shaft install --json` line: a sub-row of the configure step.
public struct InstallSubRow: Equatable, Sendable, Identifiable {
    public var step: String
    public var ok: Bool
    public var detail: String
    public var error: String?
    public var id: String { step }
}

/// The uv release the app installs, pinned with each architecture's SHA-256: the download must match the pin (a
/// checksum fetched from the same release would prove nothing). A uv bump is an app update.
public struct UVPin: Sendable, Equatable {
    public var version: String
    /// Keyed by uv's target triple: `aarch64-apple-darwin`, `x86_64-apple-darwin`.
    public var sha256: [String: String]

    public init(version: String, sha256: [String: String]) {
        self.version = version
        self.sha256 = sha256
    }

    /// uv 0.13.7 (2026-11-03). The hashes are the release's published `uv-<arch>.tar.gz.sha256` files, checked against
    /// GitHub's asset digests and the downloaded tarballs.
    public static let current = UVPin(version: "0.13.7", sha256: [
        "aarch64-apple-darwin": "81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731",
        "x86_64-apple-darwin": "20c47e4c1f630b8abaff8c291fa9b9aa300fef92c4fbff95adf198b55287cda7",
    ])

    public static var hostArch: String {
        #if arch(arm64)
        "aarch64-apple-darwin"
        #else
        "x86_64-apple-darwin"
        #endif
    }

    public func url(arch: String) -> URL {
        URL(string: "https://github.com/astral-sh/uv/releases/download/\(version)/uv-\(arch).tar.gz")!
    }
}

/// Fetches a URL into a file. The real one is `URLSessionDownloader`; tests write fixed bytes.
public protocol Downloading: Sendable {
    func download(_ url: URL, to destination: URL) async throws
}

public struct URLSessionDownloader: Downloading {
    let session: URLSession

    public init(session: URLSession = .shared) {
        self.session = session
    }

    public func download(_ url: URL, to destination: URL) async throws {
        let (file, response) = try await session.download(from: url)
        defer { try? FileManager.default.removeItem(at: file) }
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw InstallError("Downloading \(url.absoluteString) failed: HTTP \(http.statusCode).")
        }
        try? FileManager.default.removeItem(at: destination)
        try FileManager.default.moveItem(at: file, to: destination)
    }
}

/// A step's failure, worded for the checklist.
public struct InstallError: Error, Sendable, Equatable, LocalizedError {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

/// The local install engine behind the setup wizard's Install step. Each step checks whether its result already holds
/// and does nothing then, so reopening the app, `run()` again or Retry never redoes finished work: rerunning a
/// completed step is a no-op success. A failure stops the run at that step with the real error; `retry(_:)` reruns
/// that step and those after it.
@MainActor
@Observable
public final class Installer {
    public struct Configuration: Sendable {
        /// The user's home: the server goes to `<home>/SHAFT`.
        public var home: URL
        public var repository: URL
        public var uv: UVPin
        public var arch: String
        /// How often the git step looks for the Command Line Tools while the system dialog installs them, and for how
        /// long.
        public var pollInterval: Duration
        public var toolsTimeout: Duration
        /// The environment every command starts from (the app's own).
        public var environment: [String: String]

        public init(home: URL = FileManager.default.homeDirectoryForCurrentUser,
                    repository: URL = URL(string: "https://github.com/dgrassi1984/shaft")!,
                    uv: UVPin = .current, arch: String = UVPin.hostArch,
                    pollInterval: Duration = .seconds(5), toolsTimeout: Duration = .seconds(30 * 60),
                    environment: [String: String] = ProcessInfo.processInfo.environment) {
            self.home = home
            self.repository = repository
            self.uv = uv
            self.arch = arch
            self.pollInterval = pollInterval
            self.toolsTimeout = toolsTimeout
            self.environment = environment
        }

        public var server: URL { home.appending(path: "SHAFT", directoryHint: .isDirectory) }
        public var runtime: URL { server.appending(path: ".runtime", directoryHint: .isDirectory) }
        var runtimeBin: URL { runtime.appending(path: "bin", directoryHint: .isDirectory) }
        var runtimePython: URL { runtime.appending(path: "python", directoryHint: .isDirectory) }
        var uvBinary: URL { runtimeBin.appending(path: "uv") }
        var shaft: URL { server.appending(path: "bin/shaft") }
    }

    public private(set) var steps: [InstallStep: StepState]
    /// What each finished step found or did, for the row's second line.
    public private(set) var details: [InstallStep: String] = [:]
    /// The configure step's lines from `shaft install --json`, as they arrive.
    public private(set) var subRows: [InstallSubRow] = []
    /// The server release cloned (or found), `vX.Y.Z`.
    public private(set) var version: String?
    /// The Python 3.12 uv installed under `~/SHAFT/.runtime/python`: `SHAFT_PYTHON` for every `shaft` command.
    public private(set) var python: URL?
    /// The local pairing: saved in the credential store by the pair step (or found there).
    public private(set) var credential: ServerCredential?
    public private(set) var isRunning = false

    /// `shaft install --operator`: required by the configure step.
    public var operatorName: String
    /// `shaft install --email`, when given.
    public var email: String

    public let configuration: Configuration
    @ObservationIgnored private let runner: any ProcessRunning
    @ObservationIgnored private let downloader: any Downloading
    @ObservationIgnored private let store: any CredentialStore
    @ObservationIgnored private let sleep: @Sendable (Duration) async throws -> Void

    /// The `--name` the app pairs with.
    public static let deviceName = "mac-app"

    public init(operatorName: String = "", email: String = "", runner: any ProcessRunning = ProcessRunner(),
                downloader: any Downloading = URLSessionDownloader(), store: any CredentialStore,
                configuration: Configuration = Configuration(),
                sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.operatorName = operatorName
        self.email = email
        self.runner = runner
        self.downloader = downloader
        self.store = store
        self.configuration = configuration
        self.sleep = sleep
        steps = Dictionary(uniqueKeysWithValues: InstallStep.allCases.map { ($0, StepState.pending) })
    }

    public var isComplete: Bool { InstallStep.allCases.allSatisfy { steps[$0] == .done } }

    /// Run every step not yet done, in order; stop at the first failure.
    public func run() async {
        guard !isRunning else { return }
        isRunning = true
        defer { isRunning = false }
        for step in InstallStep.allCases where steps[step] != .done {
            steps[step] = .running
            do {
                try await perform(step)
                steps[step] = .done
            } catch {
                if error is CancellationError || Task.isCancelled {   // the window closed: not a failure, rerun later
                    steps[step] = .pending
                    return
                }
                steps[step] = .failed(Self.message(error))
                return
            }
        }
    }

    /// Rerun `step` and every step after it; the steps before it stay as they are.
    public func retry(_ step: InstallStep) async {
        guard !isRunning, let from = InstallStep.allCases.firstIndex(of: step) else { return }
        for later in InstallStep.allCases[from...] {
            steps[later] = .pending
            details[later] = nil
        }
        await run()
    }

    /// Run a `shaft` verb of the installed server (`~/SHAFT/bin/shaft`) with the app's Python: what the wizard runs
    /// before the web service is up. A secret goes in `stdin`, never in `args`.
    public func shaft(_ args: [String], stdin: Data? = nil,
                      onLine: (@Sendable (String) -> Void)? = nil) async throws -> ProcessResult {
        let python = try await interpreter()
        return try await runner.run(configuration.shaft, args, env: shaftEnvironment(python), stdin: stdin, onLine: onLine)
    }

    // MARK: - The steps

    private func perform(_ step: InstallStep) async throws {
        switch step {
        case .git: try await commandLineTools()
        case .server: try await server()
        case .runtime: try await runtime()
        case .configure: try await configure()
        case .pair: try await pair()
        }
    }

    private static let xcodeSelect = URL(fileURLWithPath: "/usr/bin/xcode-select")
    private static let git = URL(fileURLWithPath: "/usr/bin/git")
    private static let tar = URL(fileURLWithPath: "/usr/bin/tar")
    private static let xattr = URL(fileURLWithPath: "/usr/bin/xattr")

    /// git comes with the Command Line Tools. Missing: `xcode-select --install` opens the system dialog, and the step
    /// waits for the install to finish.
    private func commandLineTools() async throws {
        if try await run(Self.xcodeSelect, ["-p"]).succeeded {
            details[.git] = "Command Line Tools installed"
            return
        }
        let asked = try await run(Self.xcodeSelect, ["--install"])
        details[.git] = "Waiting for the Command Line Tools: finish the install in the system dialog"
        let attempts = max(1, Int((configuration.toolsTimeout / configuration.pollInterval).rounded(.up)))
        for _ in 0..<attempts {
            try await sleep(configuration.pollInterval)
            if try await run(Self.xcodeSelect, ["-p"]).succeeded {
                details[.git] = "Command Line Tools installed"
                return
            }
        }
        let said = asked.succeeded ? "" : " (xcode-select --install: \(asked.said))"
        throw InstallError("The Command Line Tools are still not installed\(said). Finish the install in the system dialog "
                           + "(or run xcode-select --install in Terminal), then Try again.")
    }

    /// `~/SHAFT`: cloned at the newest release that has its template tag, with the `template` branch at that tag. An
    /// existing folder that is not a SHAFT checkout is never deleted or overwritten.
    private func server() async throws {
        let path = configuration.server.path
        let fm = FileManager.default
        var isDirectory: ObjCBool = false
        if fm.fileExists(atPath: path, isDirectory: &isDirectory) {
            guard isDirectory.boolValue else {
                throw InstallError("\(path) already exists and is not a folder. Move it away, then Try again.")
            }
            if fm.fileExists(atPath: configuration.server.appending(path: ".git").path) {
                try await existingCheckout()
                return
            }
            if !(try fm.contentsOfDirectory(atPath: path)).isEmpty {
                throw InstallError("\(path) already exists and is not a SHAFT checkout. SHAFT installs there: move that "
                                   + "folder away (it is never deleted or overwritten), then Try again.")
            }
        }
        let tag = try await latestRelease()
        try await git(["clone", "--branch", tag, configuration.repository.absoluteString, path], "Cloning SHAFT \(tag)")
        try await templateBranch(tag)
        version = tag
        details[.server] = "SHAFT \(tag) in \(path)"
    }

    private func existingCheckout() async throws {
        let path = configuration.server.path
        let origin = try await run(Self.git, ["-C", path, "remote", "get-url", "origin"], env: gitEnvironment())
        let url = String(decoding: origin.stdout, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        guard origin.succeeded, Self.sameRepository(url, configuration.repository.absoluteString) else {
            let found = origin.succeeded ? "a checkout of \(url)" : "a git checkout without the SHAFT origin (\(origin.said))"
            throw InstallError("\(path) is \(found), not SHAFT. Move that folder away (it is never deleted or "
                               + "overwritten), then Try again.")
        }
        let tag = try await describe()
        let hasTemplate = try await run(Self.git, ["-C", path, "rev-parse", "--verify", "--quiet", "refs/heads/template"],
                                        env: gitEnvironment()).succeeded
        if !hasTemplate {
            // A clone whose template fetch failed: finish it from the release it is at.
            guard let tag else {
                throw InstallError("\(path) has no template branch and is not at a release tag: run "
                                   + "git -C \(path) fetch origin tag template-<release> and branch -f template from it.")
            }
            try await templateBranch(tag)
        }
        version = tag
        details[.server] = "SHAFT \(tag ?? "checkout") in \(path)"
    }

    /// The release tag the checkout is at, nil when it is not at one (a development checkout).
    private func describe() async throws -> String? {
        let r = try await run(Self.git, ["-C", configuration.server.path, "describe", "--tags", "--exact-match", "HEAD"],
                              env: gitEnvironment())
        let tag = String(decoding: r.stdout, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return r.succeeded && Self.semver(tag) != nil ? tag : nil
    }

    private func templateBranch(_ tag: String) async throws {
        let path = configuration.server.path
        try await git(["-C", path, "fetch", "origin", "tag", "template-\(tag)"], "Fetching template-\(tag)")
        try await git(["-C", path, "branch", "-f", "template", "template-\(tag)"], "Setting the template branch")
    }

    /// The highest `vX.Y.Z` that also has `template-vX.Y.Z`.
    private func latestRelease() async throws -> String {
        let listed = try await git(["ls-remote", "--tags", "--refs", configuration.repository.absoluteString,
                                    "v*", "template-v*"], "Listing SHAFT's releases")
        var releases: [[Int]: String] = [:]
        var templates: Set<[Int]> = []
        for line in String(decoding: listed.stdout, as: UTF8.self).split(separator: "\n") {
            guard let ref = line.split(separator: "\t").last, ref.hasPrefix("refs/tags/") else { continue }
            let name = String(ref.dropFirst("refs/tags/".count))
            if name.hasPrefix("template-"), let v = Self.semver(String(name.dropFirst("template-".count))) {
                templates.insert(v)
            } else if let v = Self.semver(name) {
                releases[v] = name
            }
        }
        guard let newest = releases.keys.filter(templates.contains).max(by: { $0.lexicographicallyPrecedes($1) }),
              let tag = releases[newest] else {
            throw InstallError("no release with a template tag yet (template-vX.Y.Z)")
        }
        return tag
    }

    /// uv (pinned, checksum-verified before unpacking) in `~/SHAFT/.runtime/bin`, then Python 3.12 under
    /// `~/SHAFT/.runtime/python`: never `~/.cache/uv`, where sandbox-exec refuses an interpreter.
    private func runtime() async throws {
        if try await !uvIsPinned() {
            try await installUV()
        }
        if let found = try await findPython() {
            python = found
        } else {
            let r = try await run(configuration.uvBinary, ["python", "install", "3.12", "--no-bin"], env: uvEnvironment())
            guard r.succeeded else { throw Self.failure("uv python install 3.12", r) }
            guard let found = try await findPython() else {
                throw InstallError("uv installed Python 3.12 but uv python find 3.12 does not see it under "
                                   + "\(configuration.runtimePython.path).")
            }
            python = found
        }
        details[.runtime] = "uv \(configuration.uv.version), Python at \(python?.path ?? "")"
    }

    private func uvIsPinned() async throws -> Bool {
        guard FileManager.default.fileExists(atPath: configuration.uvBinary.path) else { return false }
        // A uv that cannot start (damaged, another architecture) is replaced, not an error.
        let r: ProcessResult
        do {
            r = try await run(configuration.uvBinary, ["--version"], env: uvEnvironment())
        } catch is InstallError {
            return false
        }
        guard r.succeeded else { return false }
        let words = String(decoding: r.stdout, as: UTF8.self).split(separator: " ")
        return words.count >= 2 && words[0] == "uv" && words[1].trimmingCharacters(in: .whitespacesAndNewlines)
            == configuration.uv.version
    }

    private func installUV() async throws {
        let pin = configuration.uv, arch = configuration.arch
        guard let expected = pin.sha256[arch]?.lowercased() else {
            throw InstallError("No pinned uv \(pin.version) for \(arch): this app cannot install SHAFT on this Mac.")
        }
        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appending(path: "shaft-uv-\(UUID().uuidString)", directoryHint: .isDirectory)
        try fm.createDirectory(at: scratch, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: scratch) }
        let tarball = scratch.appending(path: "uv-\(arch).tar.gz")
        let url = pin.url(arch: arch)
        do {
            try await downloader.download(url, to: tarball)
        } catch let error as InstallError {
            throw error
        } catch {
            try Task.checkCancellation()
            throw InstallError("Downloading uv \(pin.version) from \(url.absoluteString) failed: \(error.localizedDescription)")
        }
        let got = SHA256.hash(data: try Data(contentsOf: tarball)).map { String(format: "%02x", $0) }.joined()
        guard got == expected else {
            throw InstallError("The uv download does not match its pinned checksum (expected \(expected), got \(got)), so "
                               + "it was not installed. Try again; if it happens again, something is altering the download.")
        }
        let bin = configuration.runtimeBin
        try fm.createDirectory(at: bin, withIntermediateDirectories: true)
        let unpacked = try await run(Self.tar, ["-xzf", tarball.path, "-C", bin.path, "--strip-components", "1"])
        guard unpacked.succeeded else { throw Self.failure("Unpacking uv", unpacked) }
        for name in ["uv", "uvx"] {
            let file = bin.appending(path: name).path
            guard fm.fileExists(atPath: file) else {
                throw InstallError("The uv \(pin.version) archive did not contain \(name).")
            }
            let r = try await run(Self.xattr, ["-d", "com.apple.quarantine", file])
            if !r.succeeded, !r.said.contains("No such xattr") { throw Self.failure("Clearing quarantine on \(name)", r) }
        }
        guard try await uvIsPinned() else {
            throw InstallError("uv was unpacked into \(bin.path) but does not report version \(pin.version).")
        }
    }

    /// uv's own Python 3.12 under `.runtime/python`, nil when it is not installed there.
    private func findPython() async throws -> URL? {
        let r = try await run(configuration.uvBinary, ["python", "find", "--managed-python", "3.12"], env: uvEnvironment())
        guard r.succeeded else { return nil }
        let path = String(decoding: r.stdout, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        let found = URL(fileURLWithPath: path)
        guard Self.isInside(found, configuration.runtimePython) else {
            throw InstallError("uv python find 3.12 answered \(path), outside \(configuration.runtimePython.path).")
        }
        return found
    }

    /// `shaft install --json`: its lines become the sub-rows as they arrive; a line with `ok: false` fails the step with
    /// its `error`.
    private func configure() async throws {
        let name = operatorName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { throw InstallError("Enter your name first: SHAFT signs its commits with it.") }
        guard FileManager.default.fileExists(atPath: configuration.shaft.path) else {
            throw InstallError("\(configuration.shaft.path) is missing: the SHAFT checkout is incomplete.")
        }
        var args = ["install", "--operator", name]
        let address = email.trimmingCharacters(in: .whitespacesAndNewlines)
        if !address.isEmpty { args += ["--email", address] }
        args.append("--json")

        subRows = []
        let (lines, sink) = AsyncStream.makeStream(of: String.self)
        let reader = Task { @MainActor [weak self] in
            var stray: [String] = []
            for await line in lines {
                if let row = Self.subRow(line) { self?.subRows.append(row) } else if !line.isEmpty { stray.append(line) }
            }
            return stray
        }
        let result: ProcessResult
        do {
            result = try await shaft(args, onLine: { sink.yield($0) })
        } catch {
            sink.finish()
            _ = await reader.value
            throw error
        }
        sink.finish()
        let stray = await reader.value
        if let refused = subRows.first(where: { !$0.ok }) {
            throw InstallError(refused.error ?? (refused.detail.isEmpty ? "shaft install: \(refused.step) failed" : refused.detail))
        }
        guard result.succeeded else { throw Self.failure("shaft install", result, extra: stray) }
        guard !subRows.isEmpty else {
            throw InstallError("shaft install reported no steps" + (stray.isEmpty ? "." : ": " + stray.joined(separator: "\n")))
        }
        details[.configure] = subRows.map { "\($0.step): \($0.detail)" }.joined(separator: "\n")
    }

    /// `shaft web pair --local --json`: the credential goes to the credential store. A local pairing already stored is
    /// kept (pairing again would add a second device).
    private func pair() async throws {
        if let existing = try store.load(), Self.isLoopback(existing.baseURL) {
            credential = existing
            details[.pair] = "Paired as \(existing.name)"
            return
        }
        let r = try await shaft(["web", "pair", "--local", "--name", Self.deviceName, "--json"])
        // stdout holds the bearer: it is parsed, never shown.
        let line = String(decoding: r.stdout, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
        let answer = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
        if let answer, answer["ok"] as? Bool == false {
            throw InstallError(answer["error"] as? String ?? "shaft web pair refused without saying why (exit \(r.status)).")
        }
        guard r.succeeded else {
            let err = String(decoding: r.stderr, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            throw InstallError("shaft web pair failed (exit \(r.status))" + (err.isEmpty ? "." : ": \(err)"))
        }
        guard let answer, answer["ok"] as? Bool == true,
              let base = answer["base_url"] as? String, let baseURL = URL(string: base), baseURL.host() != nil,
              let device = answer["device"] as? String, !device.isEmpty,
              let secret = answer["secret"] as? String, !secret.isEmpty,
              let name = answer["name"] as? String, let login = answer["login"] as? String else {
            throw InstallError("shaft web pair answered something the app cannot read (expected ok, base_url, device, "
                               + "secret, name, login).")
        }
        let paired = ServerCredential(baseURL: baseURL, device: device, secret: secret, name: name, login: login)
        do {
            try store.save(paired)
        } catch {
            throw InstallError("Paired, but keeping the pairing failed: \(error.localizedDescription)")
        }
        credential = paired
        details[.pair] = "Paired as \(name)"
    }

    // MARK: - Commands and their environments

    private func interpreter() async throws -> URL {
        if let python { return python }
        guard FileManager.default.fileExists(atPath: configuration.uvBinary.path), let found = try await findPython() else {
            throw InstallError("Python 3.12 is not installed in \(configuration.runtime.path) yet: run the install first.")
        }
        python = found
        return found
    }

    private func run(_ exe: URL, _ args: [String], env: [String: String]? = nil) async throws -> ProcessResult {
        do {
            return try await runner.run(exe, args, env: env ?? baseEnvironment(), stdin: nil, onLine: nil)
        } catch let error as LaunchError {
            throw InstallError(error.localizedDescription)
        }
    }

    @discardableResult
    private func git(_ args: [String], _ what: String) async throws -> ProcessResult {
        let r = try await run(Self.git, args, env: gitEnvironment())
        guard r.succeeded else { throw Self.failure(what + " (git \(args.first == "-C" ? args[2] : args[0]))", r) }
        return r
    }

    /// The app's environment with `HOME` set to the install's home and nothing that would pick another Python.
    private func baseEnvironment() -> [String: String] {
        var env = configuration.environment
        env["HOME"] = configuration.home.path
        for key in ["VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME", "PYTHONPATH"] { env[key] = nil }
        if env["PATH", default: ""].isEmpty { env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin" }
        return env
    }

    /// git never prompts: the app has no terminal to answer in.
    private func gitEnvironment() -> [String: String] {
        var env = baseEnvironment()
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env
    }

    /// uv keeps its Pythons and its cache inside `.runtime` and ignores any uv config of the user's.
    private func uvEnvironment() -> [String: String] {
        var env = baseEnvironment()
        env["UV_PYTHON_INSTALL_DIR"] = configuration.runtimePython.path
        env["UV_CACHE_DIR"] = configuration.runtime.appending(path: "cache").path
        env["UV_NO_CONFIG"] = "1"
        return env
    }

    private func shaftEnvironment(_ python: URL) -> [String: String] {
        var env = baseEnvironment()
        env["SHAFT_PYTHON"] = python.path
        env["PATH"] = configuration.runtimeBin.path + ":" + env["PATH", default: ""]
        return env
    }

    // MARK: - Helpers

    private static func failure(_ what: String, _ r: ProcessResult, extra: [String] = []) -> InstallError {
        let said = ([r.said] + extra).filter { !$0.isEmpty }.joined(separator: "\n")
        return InstallError("\(what) failed (exit \(r.status))" + (said.isEmpty ? "." : ": \(said)"))
    }

    private static func message(_ error: any Error) -> String {
        (error as? InstallError)?.message ?? error.localizedDescription
    }

    static func subRow(_ line: String) -> InstallSubRow? {
        guard let object = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
              let step = object["step"] as? String, let ok = object["ok"] as? Bool else { return nil }
        return InstallSubRow(step: step, ok: ok, detail: object["detail"] as? String ?? "", error: object["error"] as? String)
    }

    /// `X.Y.Z` of `vX.Y.Z`, nil for anything else.
    static func semver(_ tag: String) -> [Int]? {
        guard tag.hasPrefix("v") else { return nil }
        let parts = tag.dropFirst().split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 3 else { return nil }
        let numbers = parts.compactMap { $0.allSatisfy(\.isASCII) && $0.allSatisfy(\.isNumber) ? Int($0) : nil }
        return numbers.count == 3 ? numbers : nil
    }

    /// The same GitHub repository whether written as https or ssh, with or without `.git`.
    static func sameRepository(_ a: String, _ b: String) -> Bool {
        func normal(_ url: String) -> String {
            var s = url.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            for prefix in ["git@github.com:", "ssh://git@github.com/", "git://github.com/", "http://github.com/"]
            where s.hasPrefix(prefix) {
                s = "https://github.com/" + s.dropFirst(prefix.count)
            }
            while s.hasSuffix("/") { s.removeLast() }
            if s.hasSuffix(".git") { s.removeLast(4) }
            return s
        }
        return normal(a) == normal(b)
    }

    static func isLoopback(_ url: URL) -> Bool {
        ["127.0.0.1", "localhost", "::1"].contains(url.host() ?? "")
    }

    static func isInside(_ file: URL, _ folder: URL) -> Bool {
        func paths(_ url: URL) -> [String] {
            [url.standardizedFileURL.path, url.resolvingSymlinksInPath().path]
        }
        return paths(file).contains { f in paths(folder).contains { f.hasPrefix($0.hasSuffix("/") ? $0 : $0 + "/") } }
    }
}
#endif
