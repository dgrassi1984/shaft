import Foundation
import Observation
import SHAFTKit

/// Settings → Server keys (docs/app-api.md §18.3): which provider and tool keys the server's `.env` holds
/// (`GET /setup/secrets`, names only) and a new value for one (`POST /setup/secret`, on the verb's stdin). The typed
/// value lives only in the row's field and is cleared once saved; nothing here keeps it.
@MainActor
@Observable
final class ServerKeys {
    enum Outcome: Equatable {
        case saved
        case failed(String)
    }

    static let rows: [(name: String, label: String, help: String, link: URL?)] = [
        ("OPENROUTER_API_KEY", "OpenRouter", "Models named openrouter/…", nil),
        ("ANTHROPIC_API_KEY", "Anthropic", "Models named claude-…", nil),
        ("SHAFT_CUSTOM_API_KEY", "Custom endpoint", "The bearer for your custom/ endpoint", nil),
        ("SCRAPEBADGER_API_KEY", "Web search", "ScrapeBadger key for the web_search tool", nil),
        ("HF_TOKEN", "Hugging Face", "A read token for gated models. Accept each model's terms on its page first.",
         URL(string: "https://huggingface.co/settings/tokens")),
    ]

    private(set) var set: Set<String> = []
    private(set) var loadError: String?
    private(set) var saving: String?
    private(set) var outcome: [String: Outcome] = [:]

    @ObservationIgnored private let client: @MainActor () throws -> APIClient
    @ObservationIgnored private var loadGeneration = 0

    init(client: @escaping @MainActor () throws -> APIClient) {
        self.client = client
    }

    func load() async {
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
            _ = try await client().setupSecret(name: name, value: trimmed)
            set.insert(name)
            outcome[name] = .saved
            await load()
        } catch {
            outcome[name] = .failed(APIError.from(error)?.message ?? error.localizedDescription)
        }
    }
}
