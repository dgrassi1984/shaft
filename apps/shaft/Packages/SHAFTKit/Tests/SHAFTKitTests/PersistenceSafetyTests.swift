import Foundation
import Testing
@testable import SHAFTKit

@MainActor
struct PersistenceSafetyTests {
    @Test func leavingTheSandboxKeepsTheDraftsAndOutboxOfTheContainer() throws {
        let base = FileManager.default.temporaryDirectory.appending(path: "shaft-adopt-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: base) }
        let container = base.appending(path: "container/SHAFT")
        let root = base.appending(path: "real/SHAFT")
        try LocalFiles.write(Data("draft".utf8), to: container.appending(path: "drafts/main.json"))
        try LocalFiles.write(Data("outbox".utf8), to: container.appending(path: "servers/abc/outbox/outbox.json"))
        // Something the unsandboxed folder already holds (the server's own backup folder) is left alone.
        try LocalFiles.write(Data("backup".utf8), to: root.appending(path: "battery-backup/b.json"))

        try LocalFiles.adoptSandboxContainer(container, into: root)
        #expect(try Data(contentsOf: root.appending(path: "drafts/main.json")) == Data("draft".utf8))
        #expect(try Data(contentsOf: root.appending(path: "servers/abc/outbox/outbox.json")) == Data("outbox".utf8))
        #expect(try Data(contentsOf: root.appending(path: "battery-backup/b.json")) == Data("backup".utf8))
        #expect(!FileManager.default.fileExists(atPath: container.appending(path: "drafts").path))

        // A second call never overwrites what the app has written since.
        try LocalFiles.write(Data("new".utf8), to: container.appending(path: "drafts/other.json"))
        try LocalFiles.adoptSandboxContainer(container, into: root)
        #expect(!FileManager.default.fileExists(atPath: root.appending(path: "drafts/other.json").path))
        // No container at all is a no-op.
        try LocalFiles.adoptSandboxContainer(base.appending(path: "none"), into: root)
    }

    @Test func reinstallDoesNotReuseThePairingLeftInKeychain() throws {
        let suite = "shaft-reinstall-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let credentials = MemoryCredentialStore(server.credential)
        let pairing = PairingStore(store: credentials)

        try pairing.forgetOnFirstLaunch(defaults: defaults)
        #expect(try pairing.current() == nil)

        // Pairing survives ordinary launches, including a new defaults object for the same container.
        try credentials.save(server.credential)
        let nextLaunch = try #require(UserDefaults(suiteName: suite))
        try pairing.forgetOnFirstLaunch(defaults: nextLaunch)
        #expect(try pairing.current() == server.credential)

        // Uninstall removes the container's defaults but leaves the Keychain item behind.
        defaults.removePersistentDomain(forName: suite)
        try pairing.forgetOnFirstLaunch(defaults: defaults)
        #expect(try pairing.current() == nil)
    }

    @Test func failedFirstLaunchDeletionIsRetriedBeforeKeepingANewPairing() throws {
        let suite = "shaft-install-failure-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let blocked = PairingStore(store: BlockedCredentialStore())
        #expect(throws: KeychainError.self) { try blocked.forgetOnFirstLaunch(defaults: defaults) }

        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let credentials = MemoryCredentialStore(server.credential)
        let pairing = PairingStore(store: credentials)
        try pairing.forgetOnFirstLaunch(defaults: defaults)
        #expect(try pairing.current() == nil)
        try credentials.save(server.credential)
        try pairing.forgetOnFirstLaunch(defaults: defaults)
        #expect(try pairing.current() == server.credential)
    }

    private struct BlockedCredentialStore: CredentialStore {
        func load() throws -> ServerCredential? { nil }
        func save(_ credential: ServerCredential) throws {}
        func delete() throws { throw KeychainError(status: -25308, what: "removing the pairing") }
    }

    @Test func enqueueWriteFailureKeepsTheDraftAndDoesNotSend() async throws {
        let folder = try temporaryFolder("enqueue-failure")
        defer { try? FileManager.default.removeItem(at: folder) }
        let blocked = folder.appending(path: "outbox")
        try Data().write(to: blocked)
        let server = StubServer { _ in .json(200, ThreadsTests.threadsJSON) }
        let outbox = try Outbox(directory: blocked)
        let drafts = DraftStore(directory: folder.appending(path: "drafts"))
        let store = InstancesStore(client: server.client, outbox: outbox, drafts: drafts)
        await store.refreshThreads("same")
        let chat = store.chat("same", .from)
        chat.draft = Draft(text: "Keep this text")
        let result = await chat.send()
        guard case .failure(let error) = result else { Issue.record("enqueue succeeded"); return }
        #expect(error.message.contains("Could not save the outbox"))
        #expect(chat.actionError == error)
        #expect(chat.draft.text == "Keep this text")
        #expect(try drafts.load("same", cid: "from").text == "Keep this text")
        #expect(outbox.entries.isEmpty)
        #expect(server.requests.allSatisfy { $0.path == "/app/v1/threads/same" })
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
