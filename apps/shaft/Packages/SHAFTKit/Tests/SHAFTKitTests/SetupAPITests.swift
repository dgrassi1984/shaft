import Foundation
import Testing
@testable import SHAFTKit

/// The setup routes (docs/app-api.md §18) as the Mac app's wizard calls them: the method, the path, the body the server
/// checks field by field, and the answers decoded from the server's own fixtures.
struct SetupAPITests {
    static func fixture(_ name: String) -> String {
        String(decoding: try! DecodingTests.fixture(name), as: UTF8.self)
    }

    @Test func drivesAreTheFourPresetsInOrder() async throws {
        let server = StubServer { _ in .json(200, Self.fixture("setup-drives")) }
        let drives = try await server.client.setupDrives()
        #expect(drives.drives.map(\.id) == ["assistant", "research", "builder", "improve"])
        let request = try #require(server.requests.first)
        #expect((request.method, request.path) == ("GET", "/app/v1/setup/drives"))
    }

    @Test func aSecretGoesInTheBodyAloneAndNeverComesBack() async throws {
        let server = StubServer { _ in .json(200, #"{"said": "OPENROUTER_API_KEY saved"}"#) }
        let said = try await server.client.setupSecret(name: "OPENROUTER_API_KEY", value: "sk-or-secret")
        #expect(said.said == "OPENROUTER_API_KEY saved")
        let request = try #require(server.requests.first)
        #expect((request.method, request.path) == ("POST", "/app/v1/setup/secret"))
        #expect(request.json as? [String: String] == ["name": "OPENROUTER_API_KEY", "value": "sk-or-secret"])
        #expect(!request.url.absoluteString.contains("sk-or-secret"))
    }

    @Test func aProviderRequestSendsOnlyItsFieldsInTheServersNames() async throws {
        let server = StubServer { _ in .json(200, Self.fixture("setup-provider")) }
        let answer = try await server.client.setupProvider(ProviderRequest(provider: "openrouter"))
        #expect(answer.model == "openrouter/a")
        #expect(answer.off == [.init(function: "coding", why: "no openrouter coder is configured")])
        #expect(try #require(server.requests.first).json as? [String: String] == ["provider": "openrouter"])

        _ = try await server.client.setupProvider(ProviderRequest(
            provider: "custom", model: "big", judgeModel: "small", baseUrl: "https://host/v1", priceIn: 1.5,
            priceOut: 4, judgePriceIn: 0, judgePriceOut: 0.25, vision: true))
        let body = try #require(server.requests.last?.json)
        #expect(Set(body.keys) == ["provider", "model", "judge_model", "base_url", "price_in", "price_out",
                                   "judge_price_in", "judge_price_out", "vision"])
        #expect(body["base_url"] as? String == "https://host/v1")
        #expect(body["price_in"] as? Double == 1.5)
        #expect(body["judge_price_in"] as? Double == 0)
        #expect(body["vision"] as? Bool == true)
        #expect(try #require(server.requests.last).timeout >= 120)
    }

    @Test func aCheckKeepsEveryModelsRow() async throws {
        let server = StubServer { _ in .json(200, Self.fixture("setup-check")) }
        let check = try await server.client.checkProvider("openrouter")
        #expect(!check.ok)
        #expect(check.models.map(\.error) == [nil, "OpenRouter HTTP 401: no key"])
        let request = try #require(server.requests.first)
        #expect((request.method, request.path) == ("POST", "/app/v1/setup/provider/check"))
        #expect(request.json as? [String: String] == ["provider": "openrouter"])
        #expect(request.timeout >= 180)
    }

    @Test func creatingSendsTheFiveFieldsAndARefusalKeepsWhatWasDone() async throws {
        let errors = try #require(try JSONSerialization.jsonObject(with: DecodingTests.fixture("setup-errors")) as? [String: Any])
        let refused = try JSONSerialization.data(withJSONObject: try #require((errors["refused"] as? [String: Any])?["body"]))
        let server = StubServer { request in
            request.json?["name"] as? String == "other"
                ? .json(409, String(decoding: refused, as: UTF8.self))
                : .json(200, Self.fixture("setup-instance"))
        }
        let made = try await server.client.setupInstance(name: "ledger", operator: "Ada", model: "openrouter/x",
                                                         drive: "Keep the ledgers.", autonomy: "own")
        #expect(made == SetupInstanceResponse(instance: "ledger", done: ["new", "config", "drive"], paused: true))
        let request = try #require(server.requests.first)
        #expect(request.path == "/app/v1/setup/instance")
        #expect(request.json as? [String: String] == ["name": "ledger", "operator": "Ada", "model": "openrouter/x",
                                                      "drive": "Keep the ledgers.", "autonomy": "own"])
        #expect(request.timeout >= 200)
        let error = await #expect(throws: APIError.self) {
            _ = try await server.client.setupInstance(name: "other", operator: "Ada", model: "m", drive: "d", autonomy: "own")
        }
        guard case .server(409, let body) = error else { Issue.record("not a refusal: \(String(describing: error))"); return }
        #expect(body.done == ["new"])
        #expect(error?.message == "LLM_MODEL: no such model.")
    }

    @Test func autonomyAndHelloNameTheInstance() async throws {
        let server = StubServer { request in
            request.path.hasPrefix("/app/v1/setup/autonomy/")
                ? .json(200, Self.fixture("setup-autonomy")) : .json(200, Self.fixture("setup-hello"))
        }
        let chosen = try await server.client.setupAutonomy("wisp", then: "stay")
        #expect(chosen == SetupAutonomyResponse(instance: "wisp", then: "stay"))
        let hello = try await server.client.setupHello("wisp")
        #expect(hello.chat.id == "web-00000000000000a1")
        let requests = server.requests
        #expect(requests.map(\.path) == ["/app/v1/setup/autonomy/wisp", "/app/v1/setup/hello/wisp"])
        #expect(requests[0].json as? [String: String] == ["then": "stay"])
        #expect(requests.allSatisfy { $0.method == "POST" })
    }
}
