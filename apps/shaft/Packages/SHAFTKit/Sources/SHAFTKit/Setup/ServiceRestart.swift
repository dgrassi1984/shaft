#if os(macOS)
import Foundation

/// The services still running on the settings a setup step just replaced (`not_live` of `POST /setup/provider`)
/// restarted, so they pick the saved ones up: a running API reads the model settings when it starts and runs the
/// conversations itself, so until it restarts it refuses the new provider's models and the welcome fails. Each restart
/// is a local process of the installed server (`Installer.shaft`, never the API: the web service restarts under it),
/// under the server's restart barrier: refused while an instance is replying. After the web service restarted, the app
/// waits ("Reconnecting…") until it answers again. A failure throws the server's own words (InstallError).
@MainActor
public enum ServiceRestart {
    public typealias Run = @MainActor (_ args: [String]) async throws -> ProcessResult

    /// The verb that restarts a service in its current mode, and whether it answers one JSON line. Web last: the app
    /// talks through it.
    static let verbs: [(service: String, args: [String], json: Bool)] = [
        ("api", ["api", "restart", "--json"], true),
        ("telegram", ["telegram", "restart"], false),
        ("scheduler", ["autonomy", "restart"], false),        // drains: restarts once its runs end, cutting none
        ("web", ["web", "restart", "--json"], true),
    ]
    /// How long (in 1 s tries) the app waits for the restarted web service to answer.
    public static let reconnectTries = 90

    /// Restart `services` (names from `not_live`), then wait for the web service when it was one of them. `progress`
    /// says what is being done now.
    public static func restart(_ services: [String], run: Run, reachable: @MainActor () async -> Bool,
                               sleep: @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) },
                               progress: @MainActor (String) -> Void = { _ in }) async throws {
        let wanted = Set(services)
        if let unknown = wanted.subtracting(verbs.map(\.service)).sorted().first {
            throw InstallError("SHAFT's \(unknown) service still runs on the old settings and this app cannot restart "
                               + "it: restart your Mac, then Try again.")
        }
        for verb in verbs where wanted.contains(verb.service) {
            progress("Restarting SHAFT's \(verb.service) service…")
            let r = try await run(verb.args)
            try Task.checkCancellation()
            if verb.json {
                let line = String(decoding: r.stdout, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
                let object = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
                if let object, object["ok"] as? Bool == false {
                    throw InstallError(object["error"] as? String ?? "The server refused without saying why.")
                }
                guard r.succeeded, object?["ok"] as? Bool == true else {
                    throw InstallError("Restarting SHAFT's \(verb.service) service failed (exit \(r.status))"
                                       + (r.said.isEmpty ? "." : ": \(r.said)"))
                }
            } else if !r.succeeded {
                throw InstallError("Restarting SHAFT's \(verb.service) service failed (exit \(r.status))"
                                   + (r.said.isEmpty ? "." : ": \(r.said)"))
            }
        }
        guard wanted.contains("web") else { return }
        progress("Reconnecting…")
        for _ in 0..<reconnectTries {
            if await reachable() { return }
            try await sleep(.seconds(1))
        }
        throw InstallError("The SHAFT server has not answered since it restarted (\(reconnectTries) s). Try again; if it "
                           + "keeps failing, restart your Mac.")
    }
}
#endif
