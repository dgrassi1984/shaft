import SHAFTKit
import Testing

@MainActor
struct ServerKeysTests {
    private func makeKeys(_ server: SetupStub) -> ServerKeys {
        let client = APIClient(credential: server.credential, session: server.session)
        return ServerKeys { client }
    }

    @Test func loadMarksTheSetNames() async {
        let server = SetupStub()
        server.answer { _ in .init(200, #"{"names":["SCRAPEBADGER_API_KEY","HF_TOKEN"]}"#) }
        let keys = makeKeys(server)

        await keys.load()

        #expect(keys.set == ["SCRAPEBADGER_API_KEY", "HF_TOKEN"])
        #expect(keys.loadError == nil)
    }

    @Test func saveTrimsTheKeyAndReloadsTheSetNames() async throws {
        let server = SetupStub()
        server.answer { request in
            request.method == "POST"
                ? .init(200, #"{"said":"HF_TOKEN saved"}"#)
                : .init(200, #"{"names":["OPENROUTER_API_KEY","HF_TOKEN"]}"#)
        }
        let keys = makeKeys(server)

        await keys.save(name: "HF_TOKEN", value: "  hf_x \n")

        #expect(server.requests.map(\.method) == ["POST", "GET"])
        #expect(server.requests.map(\.path) == ["/app/v1/setup/secret", "/app/v1/setup/secrets"])
        let request = try #require(server.requests.first)
        #expect(request.json as? [String: String] == ["name": "HF_TOKEN", "value": "hf_x"])
        #expect(keys.set == ["OPENROUTER_API_KEY", "HF_TOKEN"])
        #expect(keys.outcome["HF_TOKEN"] == .saved)
        #expect(keys.saving == nil)
    }

    @Test func anEmptyValueIsRefusedWithoutCallingTheServer() async {
        let server = SetupStub()
        let keys = makeKeys(server)

        await keys.save(name: "HF_TOKEN", value: " \n ")

        #expect(server.requests.isEmpty)
        #expect(keys.outcome["HF_TOKEN"] == .failed("Paste the key before saving."))
    }

    @Test(arguments: [400, 409])
    func aRefusedSaveShowsTheServersMessage(status: Int) async {
        let server = SetupStub()
        server.answer { _ in
            .init(status, #"{"error":{"code":"refused","message":"The server could not save this key."}}"#)
        }
        let keys = makeKeys(server)

        await keys.save(name: "HF_TOKEN", value: "hf_x")

        #expect(keys.outcome["HF_TOKEN"] == .failed("The server could not save this key."))
        #expect(keys.set.isEmpty)
        #expect(server.requests.map(\.method) == ["POST"])
    }
}

@MainActor
struct ServerKeysOldServerTests {
    /// A server released before `GET /setup/secrets` answers 404: the section says it needs a newer server instead of
    /// showing "not found" (the live server keeps the old version until the operator updates it).
    @Test func aServerWithoutTheRouteSaysItNeedsANewerServer() async {
        let server = SetupStub()
        server.answer { _ in .init(404, #"{"error":{"code":"not_found","message":"No such route."}}"#) }
        let keys = ServerKeys { APIClient(credential: server.credential, session: server.session) }

        await keys.load()

        #expect(keys.loadError == "This needs a newer SHAFT server.")
        #expect(keys.set.isEmpty)
    }
}
