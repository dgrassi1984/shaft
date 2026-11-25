import Foundation
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

/// Bug 2026-11-25: a Mac paired over the tailnet with the server on that same Mac never showed Server keys (its routes
/// answer only loopback, and the section was offered only to a loopback pairing). Now the tailnet pairing finds the
/// loopback path `/version` names, checks who listens there without its bearer (`/app/v1/local` answers the same
/// `local_id`), and only then uses it with the same bearer; another Mac's server never gets the section or the bearer.
@MainActor
struct ServerKeysOverTheTailnetTests {
    private let bearer = "Bearer d-1.s"

    private func probe(_ tailnet: SetupStub, _ loopback: SetupStub) async -> APIClient? {
        await MacSetup.loopback(credential: tailnet.credential,
                                client: APIClient(credential: tailnet.credential, session: tailnet.session),
                                host: loopback.host, transport: loopback.session)
    }

    /// SetupStub answers /version itself (`version`) and does not log it.
    private func sameMac() -> (tailnet: SetupStub, loopback: SetupStub) {
        let tailnet = SetupStub(), loopback = SetupStub()
        tailnet.version = .init(200, #"{"server":"v0.69.0","api":1,"local_port":8785,"local_id":"abc"}"#)
        tailnet.answer { _ in .init(200, #"{"names":["OPENROUTER_API_KEY"]}"#) }
        loopback.answer { request in
            switch (request.method, request.path) {
            case ("GET", "/app/v1/local"): .init(200, #"{"local_id":"abc"}"#)
            case ("POST", _): .init(200, #"{"said":"HF_TOKEN saved"}"#)
            default: .init(200, #"{"names":["HF_TOKEN"]}"#)
            }
        }
        return (tailnet, loopback)
    }

    @Test func theSameMacsServerIsCheckedWithoutTheBearerThenReachedWithIt() async throws {
        let (tailnet, loopback) = sameMac()
        let found = try #require(await probe(tailnet, loopback))
        let keys = ServerKeys(locate: { found })
        #expect(keys.reach == .unknown)
        await keys.resolve()
        await keys.save(name: "HF_TOKEN", value: "hf_x")

        #expect(keys.reach == .here)
        #expect(keys.set == ["HF_TOKEN"])
        #expect(loopback.requests.map(\.path) == ["/app/v1/local", "/app/v1/setup/secret", "/app/v1/setup/secrets"])
        #expect(loopback.requests.map(\.authorization) == [nil, bearer, bearer])
        #expect(loopback.requests.allSatisfy { $0.port == 8785 })
        #expect(tailnet.requests.isEmpty)                                   // the keys never go over the tailnet
    }

    @Test func aServerWithoutALoopbackPathIsElsewhere() async {
        let (tailnet, loopback) = sameMac()
        tailnet.version = .init(200, #"{"server":"v0.69.0","api":1}"#)   // SHAFT_WEB_LOCAL off, or before it
        #expect(await probe(tailnet, loopback) == nil)
        #expect(loopback.requests.isEmpty)
    }

    @Test func anotherListenerOnThePortNeverGetsTheBearer() async {
        // The server is on another Mac: whatever listens on its port here answers another id, or refuses
        let (tailnet, loopback) = sameMac()
        for answer in [SetupStub.Answer(200, #"{"local_id":"not-abc"}"#),
                       SetupStub.Answer(404, #"{"error":{"code":"not_found","message":"no"}}"#),
                       SetupStub.Answer(200, "not json")] {
            loopback.answer { _ in answer }
            #expect(await probe(tailnet, loopback) == nil)
        }
        #expect(loopback.requests.count == 3)
        #expect(loopback.requests.allSatisfy { $0.authorization == nil })
    }

    @Test func everyOpeningAsksAgainAndALaterNoClearsTheClient() async {
        let server = SetupStub()
        server.answer { _ in .init(200, #"{"names":[]}"#) }
        var answers: [APIClient?] = [nil, APIClient(credential: server.credential, session: server.session), nil]
        let keys = ServerKeys(locate: { answers.removeFirst() })

        await keys.save(name: "HF_TOKEN", value: "hf_x")                    // before it is found: nothing is sent
        #expect(server.requests.isEmpty)
        await keys.resolve()
        #expect(keys.reach == .elsewhere)
        await keys.resolve()
        #expect(keys.reach == .here)
        await keys.resolve()                                                // the loopback path turned off since
        #expect(keys.reach == .elsewhere)
        await keys.load()
        #expect(server.requests.isEmpty)
    }

    @Test func aSlowEarlierAskCannotUndoALaterOne() async {
        let server = SetupStub()
        var calls = 0
        let keys = ServerKeys(locate: {
            calls += 1
            if calls == 1 {                                                 // the first opening's ask, still running
                try? await Task.sleep(for: .milliseconds(100))
                return APIClient(credential: server.credential, session: server.session)
            }
            return nil
        })
        async let first: Void = keys.resolve()
        try? await Task.sleep(for: .milliseconds(10))
        await keys.resolve()
        await first
        #expect(keys.reach == .elsewhere)
    }
}
