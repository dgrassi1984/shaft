import Foundation
import Testing
@testable import SHAFTKit

@MainActor
struct PersistenceSafetyTests {
    @Test func enqueueWriteFailureKeepsTheDraftAndDoesNotSend() async throws {
        let folder = try temporaryFolder("enqueue-failure")
        defer { try? FileManager.default.removeItem(at: folder) }
        let blocked = folder.appending(path: "outbox")
        try Data().write(to: blocked)
        let server = StubServer { _ in .json(200, "{}") }
        let outbox = try Outbox(directory: blocked)
        let drafts = DraftStore(directory: folder.appending(path: "drafts"))
        let store = InstancesStore(client: server.client, outbox: outbox, drafts: drafts)
        let chat = store.chat("same")
        chat.draft = Draft(text: "Keep this text")
        let result = await chat.send()
        guard case .failure(let error) = result else { Issue.record("enqueue succeeded"); return }
        #expect(error.message.contains("Could not save the outbox"))
        #expect(chat.actionError == error)
        #expect(chat.draft.text == "Keep this text")
        #expect(try drafts.load("same").text == "Keep this text")
        #expect(outbox.entries.isEmpty)
        #expect(server.requests.isEmpty)
    }

    @Test func rePairingDoesNotExposeAnotherServersDraftOrOutbox() async throws {
        let folder = try temporaryFolder("server-isolation")
        defer { try? FileManager.default.removeItem(at: folder) }
        let first = StubServer { _ in throw StubServer.NetworkDown() }
        let second = StubServer { _ in .json(200, #"{"device":"new","secret":"new","name":"test","login":"test"}"#) }
        let credentials = MemoryCredentialStore(first.credential)
        let drafts = DraftStore(directory: folder.appending(path: "drafts"))
        let file = try drafts.attach(data: Data("private".utf8), name: "note.txt", for: "same")
        try drafts.save(Draft(text: "first server draft", attachments: [file]), for: "same")
        let outbox = try Outbox(directory: folder.appending(path: "outbox"))
        try outbox.enqueue(OutgoingMessage(text: "first server outbox"), to: "same")
        let session = SHAFTSession(pairing: PairingStore(store: credentials, session: second.session),
                                   outbox: outbox, drafts: drafts, session: first.session)
        let firstStore = try #require(session.store)
        #expect(firstStore.chat("same").draft.text == "first server draft")
        #expect(firstStore.outbox.entries.first?.text == "first server outbox")
        #expect(try Data(contentsOf: firstStore.drafts.fileURL(of: file, for: "same")) == Data("private".utf8))
        session.forget()
        #expect(await session.pair(pasted: second.baseURL.absoluteString + "/pair?t=test"))
        let secondStore = try #require(session.store)
        #expect(secondStore.chat("same").draft.isEmpty)
        #expect(secondStore.outbox.entries.isEmpty)
        #expect(await secondStore.outbox.retry(outbox.entries[0].mid) == nil)
        #expect(second.requests.map(\.path) == ["/app/v1/pair"])
        try credentials.save(first.credential)
        let reopened = SHAFTSession(pairing: PairingStore(store: credentials), outbox: outbox, drafts: drafts)
        #expect(reopened.store?.chat("same").draft.text == "first server draft")
        #expect(reopened.store?.outbox.entries.first?.text == "first server outbox")
    }

    @Test func namespaceUsesSchemeHostAndEffectivePort() throws {
        let folder = try temporaryFolder("origin")
        defer { try? FileManager.default.removeItem(at: folder) }
        let directory = folder.appending(path: "drafts")
        let first = try LocalFiles.namespaced(directory, origin: URL(string: "https://EXAMPLE.com/path")!)
        #expect(first == (try LocalFiles.namespaced(directory, origin: URL(string: "https://example.com:443/other")!)))
        #expect(first != (try LocalFiles.namespaced(directory, origin: URL(string: "http://example.com:443")!)))
        #expect(first != (try LocalFiles.namespaced(directory, origin: URL(string: "https://example.com:8444")!)))
    }
}
