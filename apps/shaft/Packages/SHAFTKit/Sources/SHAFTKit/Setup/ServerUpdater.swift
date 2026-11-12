#if os(macOS)
import Foundation
import Observation

/// One line of `shaft deploy TAG --json`: `{"step", "ok", "detail"[, "error"]}`, `"waiting": true` on a services step
/// that held back for an open conversation, `"restarted": [str]` on one that restarted services.
public struct DeployRow: Equatable, Sendable, Identifiable {
    public var step: String
    public var ok: Bool
    public var detail: String
    public var error: String?
    public var waiting: Bool
    public var restarted: [String]
    public var id: String { step }

    public init(step: String, ok: Bool, detail: String, error: String? = nil, waiting: Bool = false,
                restarted: [String] = []) {
        self.step = step
        self.ok = ok
        self.detail = detail
        self.error = error
        self.waiting = waiting
        self.restarted = restarted
    }

    static func parse(_ line: String) -> DeployRow? {
        guard let object = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
              let step = object["step"] as? String, let ok = object["ok"] as? Bool else { return nil }
        return DeployRow(step: step, ok: ok, detail: object["detail"] as? String ?? "", error: object["error"] as? String,
                         waiting: object["waiting"] as? Bool ?? false, restarted: object["restarted"] as? [String] ?? [])
    }
}

/// Settings → Services → Update server (Mac): `shaft deploy --check --json` says whether a newer release is out, and
/// `shaft deploy <tag> --json` moves this Mac's server onto it (docs/deploy.md), each step a row as it arrives. Both run
/// as local processes with the installed server's Python (`Installer.shaft`), not through the API: the web service
/// restarts under them. The updater owns its work, so closing Settings never cuts a deploy halfway; `stop()` (the
/// session going away) does.
@MainActor
@Observable
public final class ServerUpdater {
    public enum Check: Equatable, Sendable {
        case idle
        case checking
        case upToDate(current: String)
        case available(latest: String, current: String)
        case failed(String)
    }

    public enum Phase: Equatable, Sendable {
        case idle
        case running
        /// The services step held back: who still has a conversation open. Retry runs `--services-only`.
        case waiting(String)
        case done
        /// The step's own error, or why the command failed.
        case failed(String)
    }

    public private(set) var check: Check = .idle
    public private(set) var phase: Phase = .idle
    public private(set) var rows: [DeployRow] = []
    /// The release being deployed, or the last one.
    public private(set) var tag: String?
    /// The web service is restarting under the app: its calls fail until it answers again.
    public private(set) var reconnecting = false
    /// After a deploy that ended well: the session reads the server again (its version, its services).
    @ObservationIgnored public var onFinished: (@MainActor () async -> Void)?

    public typealias Run = @MainActor (_ args: [String], _ onLine: (@Sendable (String) -> Void)?) async throws
        -> ProcessResult

    public let repository: URL
    @ObservationIgnored private let run: Run
    @ObservationIgnored private let reachable: @MainActor () async -> Bool
    @ObservationIgnored private let sleep: @Sendable (Duration) async throws -> Void
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var task: Task<Void, Never>?
    /// How long (in 1 s tries) the app waits for the restarted web service to answer.
    public static let reconnectTries = 90

    public init(repository: URL, run: @escaping Run, reachable: @escaping @MainActor () async -> Bool,
                sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.repository = repository
        self.run = run
        self.reachable = reachable
        self.sleep = sleep
    }

    /// The installed server's `shaft`, through the Installer (its Python, its environment).
    public convenience init(installer: Installer, reachable: @escaping @MainActor () async -> Bool) {
        self.init(repository: installer.configuration.repository,
                  run: { [weak installer] args, onLine in
                      guard let installer else { throw CancellationError() }
                      return try await installer.shaft(args, onLine: onLine)
                  },
                  reachable: reachable)
    }

    public var isBusy: Bool { check == .checking || phase == .running }

    /// The release's page on GitHub: what it changes.
    public func releasePage(_ tag: String) -> URL {
        repository.appending(path: "releases/tag/\(tag)")
    }

    /// The work started last, for a caller (a test) that waits for it.
    public func finished() async { await task?.value }

    /// Stop what runs (the session went away): its command is ended and its answer dropped.
    public func stop() {
        generation &+= 1
        task?.cancel()
        task = nil
        if check == .checking { check = .idle }
        if phase == .running { phase = .idle }
        reconnecting = false
    }

    /// `shaft deploy --check --json`. Not while a deploy runs.
    public func checkForUpdate() {
        guard !isBusy else { return }
        let generation = self.generation
        check = .checking
        task = Task { [weak self] in
            guard let self else { return }
            let answer: Check
            do {
                let r = try await self.run(["deploy", "--check", "--json"], nil)
                answer = Self.checked(r)
            } catch {
                guard generation == self.generation else { return }
                self.check = Self.isCancellation(error) ? .idle : .failed(Self.message(error))
                return
            }
            guard generation == self.generation else { return }
            self.check = answer
        }
    }

    /// Deploy the release the check offered.
    public func update() {
        guard !isBusy, case .available(let latest, _) = check else { return }
        start(latest, servicesOnly: false)
    }

    /// The services step again, after it waited for a conversation (`--services-only`).
    public func retryServices() {
        guard !isBusy, case .waiting = phase, let tag else { return }
        start(tag, servicesOnly: true)
    }

    // MARK: -

    private func start(_ tag: String, servicesOnly: Bool) {
        generation &+= 1
        let run = generation
        self.tag = tag
        rows = servicesOnly ? rows.filter { $0.step != "services" && $0.step != "verify" } : []
        phase = .running
        reconnecting = false
        task = Task { [weak self] in await self?.deploy(tag, servicesOnly: servicesOnly, run: run) }
    }

    private func deploy(_ tag: String, servicesOnly: Bool, run: Int) async {
        let (lines, sink) = AsyncStream.makeStream(of: String.self)
        let reader = Task { @MainActor [weak self] in
            var stray: [String] = []
            for await line in lines {
                // A stopped run's lines (still buffered, or handed over late by a reader thread) change nothing.
                guard let self, self.generation == run else { continue }
                if let row = DeployRow.parse(line) { self.apply(row) } else if !line.isEmpty { stray.append(line) }
            }
            return stray
        }
        let result: ProcessResult
        do {
            result = try await self.run(["deploy", tag, "--json"] + (servicesOnly ? ["--services-only"] : []),
                                        { sink.yield($0) })
        } catch {
            sink.finish()
            _ = await reader.value
            guard generation == run else { return }
            reconnecting = false
            phase = Self.isCancellation(error) ? .idle : .failed(Self.message(error))
            return
        }
        sink.finish()
        let stray = await reader.value
        guard generation == run else { return }
        if let refused = rows.first(where: { !$0.ok }) {
            reconnecting = false
            let said = refused.detail.isEmpty ? "shaft deploy: \(refused.step) failed" : refused.detail
            phase = .failed(refused.error ?? said)
            return
        }
        guard result.succeeded else {
            reconnecting = false
            let said = ([result.said] + stray).filter { !$0.isEmpty }.joined(separator: "\n")
            phase = .failed("shaft deploy failed (exit \(result.status))" + (said.isEmpty ? "." : ": \(said)"))
            return
        }
        if let services = rows.last, services.step == "services", services.waiting {
            phase = .waiting(services.detail)
            return
        }
        guard rows.last?.step == "verify" else {
            reconnecting = false
            phase = .failed("shaft deploy ended without its verify step"
                            + (stray.isEmpty ? "." : ": " + stray.joined(separator: "\n")))
            return
        }
        if reconnecting {
            guard await reconnect(run: run) else { return }
        }
        phase = .done
        check = .upToDate(current: tag)
        await onFinished?()
    }

    private func apply(_ row: DeployRow) {
        if let at = rows.firstIndex(where: { $0.step == row.step }) { rows[at] = row } else { rows.append(row) }
        if !row.restarted.isEmpty { reconnecting = true }
    }

    /// Until the restarted web service answers again. False: it did not (said), or the run was stopped.
    private func reconnect(run: Int) async -> Bool {
        for _ in 0..<Self.reconnectTries {
            if await reachable() {
                guard generation == run else { return false }
                reconnecting = false
                return true
            }
            guard generation == run else { return false }
            do { try await sleep(.seconds(1)) } catch { return false }
            guard generation == run else { return false }
        }
        reconnecting = false
        phase = .failed("The server was updated, but it has not answered since its restart (\(Self.reconnectTries) s). "
                        + "Its log (~/SHAFT/.supervisor/com.shaft.web.log) says why.")
        return false
    }

    static func checked(_ r: ProcessResult) -> Check {
        let line = String(decoding: r.stdout, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
        let object = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
        if let object, object["ok"] as? Bool == false {
            return .failed(object["error"] as? String ?? "shaft deploy --check refused without saying why.")
        }
        guard r.succeeded, let object, object["ok"] as? Bool == true, let current = object["current"] as? String else {
            let said = r.said
            return .failed("shaft deploy --check failed (exit \(r.status))" + (said.isEmpty ? "." : ": \(said)"))
        }
        if let latest = object["latest"] as? String { return .available(latest: latest, current: current) }
        return .upToDate(current: current)
    }

    private static func isCancellation(_ error: any Error) -> Bool {
        error is CancellationError || Task.isCancelled
    }

    private static func message(_ error: any Error) -> String {
        (error as? InstallError)?.message ?? error.localizedDescription
    }
}
#endif
