#if os(macOS)
import Foundation
import Observation

/// Settings → Services → "Use SHAFT from iPhone/iPad" (Mac): adds Tailscale to a local-only install so a phone can pair
/// over the tailnet, and makes the link the phone pairs with. Three steps, each a local `shaft` process of the installed
/// server run through `Installer.shaft` (never the API: the web service restarts under the second one, and a launchd
/// job could not reach Tailscale's CLI):
///   1. Look for Tailscale in /Applications and ~/Applications. If it is missing, the row says where to get it.
///   2. `api on --json`, then `web on --json` (tailnet mode): they add the tailnet login next to the Mac's own and keep
///      the Mac's loopback pairing as it is. A refusal shows the server's real message (Tailscale signed out…);
///      (the web service restarts under the app: it waits, "Reconnecting…", until the service answers again);
///   3. `web pair --name iphone --json`: the link, its expiry, and a new one on request.
/// The work is owned, so closing Settings never cuts it halfway; `stop()` does.
@MainActor
@Observable
public final class PhoneAccess {
    public enum State: Equatable, Sendable {
        case idle
        /// Tailscale's app is not on this Mac.
        case missingApp
        /// What is being done now ("Connecting this Mac to your tailnet…", "Making the link…").
        case working(String)
        /// The server's own message (or why the command failed).
        case failed(String)
        case link(String, expiresMinutes: Int)
    }

    public private(set) var state: State = .idle
    /// After the tailnet is set up: the session reads the server again (its services).
    @ObservationIgnored public var onConnected: (@MainActor () async -> Void)?

    public static var tailscaleApp: URL {
        let files = FileManager.default
        return resolveTailscaleApp(
            systemApp: URL(fileURLWithPath: "/Applications/Tailscale.app"),
            homeApp: files.homeDirectoryForCurrentUser.appendingPathComponent("Applications/Tailscale.app"),
            fileExists: { files.fileExists(atPath: $0.path) }
        )
    }

    static func resolveTailscaleApp(systemApp: URL, homeApp: URL, fileExists: (URL) -> Bool) -> URL {
        [systemApp, homeApp].first(where: fileExists) ?? systemApp
    }

    public static let tailscaleDownload = URL(string: "https://tailscale.com/download")!
    /// What the phone is called in `shaft web status`.
    public static let deviceName = "iphone"

    public typealias Run = @MainActor (_ args: [String]) async throws -> ProcessResult

    @ObservationIgnored private let run: Run
    @ObservationIgnored private let hasTailscale: @MainActor () -> Bool
    @ObservationIgnored private let reachable: @MainActor () async -> Bool
    @ObservationIgnored private let sleep: @Sendable (Duration) async throws -> Void
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var task: Task<Void, Never>?
    /// Set once the tailnet steps went through: a new link then runs the pairing step alone.
    @ObservationIgnored private var connected = false
    /// `web on` went through but the restarted web service has not answered yet: Try again waits for it, it does not
    /// restart anything a second time.
    @ObservationIgnored private var awaitingService = false
    /// How long (in 1 s tries) the app waits for the restarted web service to answer.
    public static let reconnectTries = 90

    public init(run: @escaping Run,
                reachable: @escaping @MainActor () async -> Bool,
                hasTailscale: @escaping @MainActor () -> Bool = {
                    FileManager.default.fileExists(atPath: PhoneAccess.tailscaleApp.path)
                },
                sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.run = run
        self.reachable = reachable
        self.hasTailscale = hasTailscale
        self.sleep = sleep
    }

    /// The installed server's `shaft`, through the Installer (its Python, its environment).
    public convenience init(installer: Installer, reachable: @escaping @MainActor () async -> Bool) {
        self.init(run: { [weak installer] args in
            guard let installer else { throw CancellationError() }
            return try await installer.shaft(args)
        }, reachable: reachable)
    }

    public var isBusy: Bool {
        if case .working = state { return true }
        return false
    }

    /// The work started last, for a caller (a test) that waits for it.
    public func finished() async { await task?.value }

    /// Stop what runs (the session went away): its command is ended and its answer dropped.
    public func stop() {
        generation &+= 1
        task?.cancel()
        task = nil
        if isBusy { state = .idle }
    }

    /// Steps 1 to 3. Also Try again, and Check again once Tailscale is installed.
    public func start() {
        guard !isBusy else { return }
        guard hasTailscale() else {
            state = .missingApp
            return
        }
        begin(awaitingService ? "Reconnecting…" : "Connecting this Mac to your tailnet…") { [weak self] run in
            guard let self else { return }
            if !self.awaitingService { try await self.tailnet(run: run) }
            try await self.reconnect(run: run)
            try await self.makeLink(run: run)
        }
    }

    /// Step 3 alone: a new link (the old one stays valid until it expires or is used).
    public func newLink() {
        guard !isBusy else { return }
        guard connected else { return start() }
        begin("Making the link…") { [weak self] run in try await self?.makeLink(run: run) }
    }

    // MARK: -

    private struct Refused: Error { let message: String }

    private func begin(_ what: String, _ work: @escaping @MainActor (Int) async throws -> Void) {
        generation &+= 1
        let run = generation
        state = .working(what)
        task = Task { [weak self] in
            do {
                try await work(run)
            } catch let refused as Refused {
                guard let self, self.generation == run else { return }
                self.state = .failed(refused.message)
            } catch {
                guard let self, self.generation == run else { return }
                self.state = (error is CancellationError || Task.isCancelled) ? .idle
                    : .failed((error as? InstallError)?.message ?? error.localizedDescription)
            }
        }
    }

    private func tailnet(run: Int) async throws {
        for verb in ["api", "web"] {
            _ = try await answer(["\(verb)", "on", "--json"], run: run)
        }
        connected = true
        awaitingService = true
    }

    /// Until the restarted web service answers again; only then is the link asked for.
    private func reconnect(run: Int) async throws {
        state = .working("Reconnecting…")
        for _ in 0..<Self.reconnectTries {
            if await reachable() {
                try Task.checkCancellation()
                guard generation == run else { throw CancellationError() }
                awaitingService = false
                await onConnected?()
                return
            }
            try await sleep(.seconds(1))
            guard generation == run else { throw CancellationError() }
        }
        throw Refused(message: "The SHAFT server has not answered since it was set up for Tailscale "
                      + "(\(Self.reconnectTries) s). Try again; if it keeps failing, restart SHAFT.")
    }

    private func makeLink(run: Int) async throws {
        state = .working("Making the link…")
        let object = try await answer(["web", "pair", "--name", Self.deviceName, "--json"], run: run)
        guard let link = object["link"] as? String, !link.isEmpty else {
            throw Refused(message: "The server made no pairing link.")
        }
        state = .link(link, expiresMinutes: object["expires_minutes"] as? Int ?? 10)
    }

    /// Run one verb and return its JSON line; a refusal, a nonzero exit or no JSON at all is the failure shown.
    private func answer(_ args: [String], run: Int) async throws -> [String: Any] {
        let r = try await self.run(args)
        try Task.checkCancellation()
        guard generation == run else { throw CancellationError() }
        let line = String(decoding: r.stdout, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
        let object = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
        if let object, object["ok"] as? Bool == false {
            throw Refused(message: object["error"] as? String ?? "The server refused without saying why.")
        }
        guard r.succeeded, let object, object["ok"] as? Bool == true else {
            let said = r.said
            throw Refused(message: "Setting up Tailscale failed (exit \(r.status))"
                          + (said.isEmpty ? "." : ": \(said)"))
        }
        return object
    }
}
#endif
