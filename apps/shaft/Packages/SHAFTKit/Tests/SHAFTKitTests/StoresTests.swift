import Foundation
import Testing
@testable import SHAFTKit

@MainActor
struct StoresTests {
    func makeStore(_ server: StubServer, folder: URL) throws -> InstancesStore {
        InstancesStore(client: server.client, outbox: try Outbox(directory: folder.appending(path: "outbox")),
                       drafts: DraftStore(directory: folder.appending(path: "drafts")))
    }

    @Test func seenPostsTheVersionShownAndAStaleOneIsNotAnError() async throws {
        let folder = try temporaryFolder("stores")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            switch request.path {
            case "/app/v1/instances":
                return .json(200, fixtureText(try DecodingTests.fixture("contract-instances")))
            default:
                return request.json?["version"] as? String == "9f2c4e1ab07d3c55"
                    ? .json(200, #"{"unread": false, "badge": 0}"#)
                    : .json(409, #"{"error": {"code": "stale_version", "message": "the chat moved", "version": "41d07be2c95a8f13"}}"#)
            }
        }
        let store = try makeStore(server, folder: folder)
        await store.refresh()
        #expect(store.badge == 1)
        #expect(store.row("thelos")?.unread == true)

        #expect(await store.markSeen("thelos", version: "old") == false)
        #expect(store.error == nil)
        #expect(store.row("thelos")?.unread == true)

        #expect(await store.markSeen("thelos", version: "9f2c4e1ab07d3c55"))
        #expect(store.badge == 0)
        #expect(store.row("thelos")?.unread == false)
        #expect(server.requests.last?.path == "/app/v1/chat/thelos/seen")
    }

    @Test func aSendClearsTheDraftAndAFailureStaysInTheChatAsNotSent() async throws {
        let folder = try temporaryFolder("stores")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let store = try makeStore(server, folder: folder)
        let chat = store.chat("thelos")
        let file = try store.drafts.attach(data: Data("x".utf8), name: "note.txt", for: "thelos")
        chat.draft = Draft(text: "Rerun it", quotes: [Quote(quote: "Ship it?", answer: "Yes")], attachments: [file])
        #expect(try store.drafts.load("thelos").text == "Rerun it")

        let result = await chat.send(mode: .queue)
        guard case .failure = result else { Issue.record("the send did not fail"); return }
        #expect(chat.draft.isEmpty)
        #expect(try store.drafts.load("thelos").isEmpty)
        let unsent = try #require(chat.unsent.first)
        #expect(unsent.text == "Rerun it")
        #expect(unsent.mode == .queue)
        #expect(unsent.quotes == [Quote(quote: "Ship it?", answer: "Yes")])
        #expect(unsent.files.map(\.name) == ["note.txt"])
        #expect(unsent.state == .notSent)
    }

    @Test func theUnpairedAnswerTellsTheApp() async throws {
        let folder = try temporaryFolder("stores")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in .json(401, #"{"error": {"code": "unpaired", "message": "Pair this device again."}}"#) }
        let store = try makeStore(server, folder: folder)
        var told = 0
        store.onUnpaired = { told += 1 }
        await store.refresh()
        #expect(told == 1)
        #expect(store.error?.isUnpaired == true)
    }
}
