import Foundation
import Observation
import SHAFTKit

/// Settings → Server keys (docs/app-api.md §18.3): which provider and tool keys the server's `.env` holds
/// (`GET /setup/secrets`, names only) and a new value for one (`POST /setup/secret`, on the verb's stdin). The typed
/// value lives only in the row's field and is cleared once saved; nothing here keeps it.
///
/// Those routes answer only loopback. A loopback pairing reaches them at once (`init(client:)`); a pairing over the
/// tailnet reaches them only when its server runs on this Mac, at the loopback port `/version` names, with the same
/// bearer (`init(locate:)`, `resolve()`): the section shows once that is found (`reach == .here`).
@MainActor
@Observable
final class ServerKeys {
    enum Outcome: Equatable {
        case saved
        case failed(String)
    }

    /// Whether this Mac reaches the server's loopback-only routes: not asked yet, yes, or no (another Mac's server, or
    /// its loopback path off).
    enum Reach: Equatable {
        case unknown
        case here
        case elsewhere
    }

    static let rows: [(name: String, label: String, help: String, link: URL?)] = [
        ("OPENROUTER_API_KEY", "OpenRouter", "Models named openrouter/…", nil),
        ("ANTHROPIC_API_KEY", "Anthropic", "Models named claude-…", nil),
        ("SHAFT_CUSTOM_API_KEY", "Custom endpoint", "The bearer for your custom/ endpoint", nil),
        ("SCRAPEBADGER_API_KEY", "Web search", "ScrapeBadger key for the web_search tool", nil),
        ("HF_TOKEN", "Hugging Face", "A read token for gated models. Accept each model's terms on its page first.",
         URL(string: "https://huggingface.co/settings/tokens")),
    ]

    private(set) var reach: Reach
    private(set) var set: Set<String> = []
    private(set) var loadError: String?
    private(set) var saving: String?
    private(set) var outcome: [String: Outcome] = [:]

    @ObservationIgnored private var client: (@MainActor () throws -> APIClient)?
    @ObservationIgnored private let locate: (@MainActor () async -> APIClient?)?
    @ObservationIgnored private var loadGeneration = 0
    @ObservationIgnored private var resolveGeneration = 0

    /// A loopback pairing: the session's own client reaches the routes.
    init(client: @escaping @MainActor () throws -> APIClient) {
        self.client = client
        locate = nil
        reach = .here
    }

    /// A pairing over the tailnet: `locate` finds a loopback client for the same server with the same bearer, or nil.
    init(locate: @escaping @MainActor () async -> APIClient?) {
        client = nil
        self.locate = locate
        reach = .unknown
    }

    /// Ask where the routes are, on every Settings opening: the loopback path may have been turned on or off, or its
    /// port changed, since. Only the latest ask is kept, so a slow earlier one cannot undo it.
    func resolve() async {
        guard let locate else { return }
        resolveGeneration += 1
        let generation = resolveGeneration
        let found = await locate()
        guard generation == resolveGeneration else { return }
        if let found {
            client = { found }
            reach = .here
        } else {
            client = nil
            reach = .elsewhere
        }
    }

    func load() async {
        guard let client else { return }
        loadGeneration += 1
        let generation = loadGeneration
        do {
            let answer = try await client().setupSecrets()
            guard generation == loadGeneration else { return }
            set = Set(answer.names)
            loadError = nil
        } catch {
            guard generation == loadGeneration, !APIError.isCancellation(error) else { return }
            // A server from before this route answers 404: say so rather than "not found".
            loadError = APIError.from(error)?.status == 404 ? "This needs a newer SHAFT server."
                : APIError.from(error)?.message ?? error.localizedDescription
        }
    }

    func save(name: String, value: String) async {
        guard saving == nil else { return }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            outcome[name] = .failed("Paste the key before saving.")
            return
        }
        saving = name
        outcome[name] = nil
        defer { saving = nil }
        do {
            guard let client else { throw APIError.notPaired }
            _ = try await client().setupSecret(name: name, value: trimmed)
            set.insert(name)
            outcome[name] = .saved
            await load()
        } catch {
            outcome[name] = .failed(APIError.from(error)?.message ?? error.localizedDescription)
        }
    }
}
