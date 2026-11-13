import Foundation
import Testing
@testable import SHAFTKit

@MainActor
struct PersistenceSafetyTests {
    private func adoptionFolders() -> (base: URL, container: URL, root: URL) {
        let base = FileManager.default.temporaryDirectory.appending(path: "shaft-adopt-\(UUID().uuidString)")
        return (base, base.appending(path: "container/SHAFT"), base.appending(path: "real/SHAFT"))
    }

    private func text(_ url: URL) throws -> String { String(decoding: try Data(contentsOf: url), as: UTF8.self) }

    @Test func leavingTheSandboxKeepsTheDraftsAndOutboxOfTheContainer() throws {
        let (base, container, root) = adoptionFolders()
        defer { try? FileManager.default.removeItem(at: base) }
        try LocalFiles.write(Data("draft".utf8), to: container.appending(path: "drafts/main.json"))
        try LocalFiles.write(Data("outbox".utf8), to: container.appending(path: "servers/abc/outbox/outbox.json"))
        try LocalFiles.write(Data("backup".utf8), to: root.appending(path: "battery-backup/b.json"))

        try LocalFiles.adoptSandboxContainer(container, into: root)
        #expect(try text(root.appending(path: "drafts/main.json")) == "draft")
        #expect(try text(root.appending(path: "servers/abc/outbox/outbox.json")) == "outbox")
        #expect(try text(root.appending(path: "battery-backup/b.json")) == "backup")
        #expect(!FileManager.default.fileExists(atPath: container.appending(path: "drafts").path))
        // No container at all is a no-op.
        try LocalFiles.adoptSandboxContainer(base.appending(path: "none"), into: root)
    }

    @Test func aPartialEarlierMoveIsFinishedFileByFile() throws {
        let (base, container, root) = adoptionFolders()
        defer { try? FileManager.default.removeItem(at: base) }
        // The destination already holds a draft; the container still holds that one's sibling and an attachment.
        try LocalFiles.write(Data("a".utf8), to: root.appending(path: "drafts/a.json"))
        try LocalFiles.write(Data("b".utf8), to: container.appending(path: "drafts/b.json"))
        try LocalFiles.write(Data("pdf".utf8), to: container.appending(path: "drafts/files/a/1/x.pdf"))

        try LocalFiles.adoptSandboxContainer(container, into: root)
        #expect(try text(root.appending(path: "drafts/a.json")) == "a")
        #expect(try text(root.appending(path: "drafts/b.json")) == "b")
        #expect(try text(root.appending(path: "drafts/files/a/1/x.pdf")) == "pdf")
        #expect(!FileManager.default.fileExists(atPath: container.appending(path: "drafts").path))
    }

    @Test func anIdenticalDuplicateIsDroppedAndAConflictingOneKeptBeside() throws {
        let (base, container, root) = adoptionFolders()
        defer { try? FileManager.default.removeItem(at: base) }
        try LocalFiles.write(Data("same".utf8), to: root.appending(path: "drafts/s.json"))
        try LocalFiles.write(Data("same".utf8), to: container.appending(path: "drafts/s.json"))
        try LocalFiles.write(Data("new".utf8), to: root.appending(path: "drafts/c.json"))
        try LocalFiles.write(Data("old".utf8), to: container.appending(path: "drafts/c.json"))

        try LocalFiles.adoptSandboxContainer(container, into: root)
        #expect(try text(root.appending(path: "drafts/s.json")) == "same")
        #expect(try text(root.appending(path: "drafts/c.json")) == "new")
        #expect(try text(root.appending(path: "drafts/c.from-sandbox.json")) == "old")
        #expect(!FileManager.default.fileExists(atPath: root.appending(path: "drafts/s.from-sandbox.json").path))
        #expect(!FileManager.default.fileExists(atPath: container.appending(path: "drafts").path))
        // The stores open by key and a key has no dot: the kept copy can never be read as a draft.
        let drafts = DraftStore(directory: root.appending(path: "drafts"))
        #expect(throws: (any Error).self) { try drafts.load("c.from-sandbox") }
    }

    @Test func aPreservedCopyIsNeverDeleted() throws {
        let (base, container, root) = adoptionFolders()
        defer { try? FileManager.default.removeItem(at: base) }
        try LocalFiles.write(Data("A".utf8), to: root.appending(path: "drafts/c.json"))
        try LocalFiles.write(Data("B".utf8), to: root.appending(path: "drafts/c.from-sandbox.json"))
        try LocalFiles.write(Data("C".utf8), to: container.appending(path: "drafts/c.json"))

        try LocalFiles.adoptSandboxContainer(container, into: root)
        #expect(try text(root.appending(path: "drafts/c.json")) == "A")
        #expect(try text(root.appending(path: "drafts/c.from-sandbox.json")) == "B")
        #expect(try text(root.appending(path: "drafts/c.from-sandbox-2.json")) == "C")

        // The same conflict again is absorbed by the copy already kept.
        try LocalFiles.write(Data("C".utf8), to: container.appending(path: "drafts/c.json"))
        try LocalFiles.adoptSandboxContainer(container, into: root)
        #expect(!FileManager.default.fileExists(atPath: root.appending(path: "drafts/c.from-sandbox-3.json").path))
        #expect(!FileManager.default.fileExists(atPath: container.appending(path: "drafts/c.json").path))
    }

    @MainActor
    @Test func aContainerBehindADeniedFolderIsAStartupErrorNotAnEmptyStart() throws {
        let (base, _, root) = adoptionFolders()
        let locked = base.appending(path: "locked")
        let container = locked.appending(path: "inner/SHAFT")
        try FileManager.default.createDirectory(at: container, withIntermediateDirectories: true)
        try LocalFiles.write(Data("b".utf8), to: container.appending(path: "drafts/b.json"))
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: locked.path)
        defer {
            try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: locked.path)
            try? FileManager.default.removeItem(at: base)
        }
        var started = 0
        let model = AppModel(start: {
            try LocalFiles.adoptSandboxContainer(container, into: root)
            started += 1
            return SHAFTSession(pairing: PairingStore(store: MemoryCredentialStore()),
                                outbox: try Outbox(directory: root.appending(path: "outbox")),
                                drafts: DraftStore(directory: root.appending(path: "drafts")))
        })
        #expect(model.session == nil)
        #expect(started == 0)
        #expect(try #require(model.startupError).contains(container.path))
        #expect(!FileManager.default.fileExists(atPath: root.path))
    }

    @MainActor
    @Test func aFailedMoveIsAStartupErrorAndTryAgainMigratesAndStarts() throws {
        let (base, container, root) = adoptionFolders()
        defer {
            try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: root.appending(path: "drafts").path)
            try? FileManager.default.removeItem(at: base)
        }
        try LocalFiles.write(Data("b".utf8), to: container.appending(path: "drafts/b.json"))
        // A destination folder that refuses new files makes the move fail.
        try FileManager.default.createDirectory(at: root.appending(path: "drafts"), withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o555], ofItemAtPath: root.appending(path: "drafts").path)

        var started = 0
        let model = AppModel(start: {
            try LocalFiles.adoptSandboxContainer(container, into: root)
            started += 1
            return SHAFTSession(pairing: PairingStore(store: MemoryCredentialStore()),
                                outbox: try Outbox(directory: root.appending(path: "outbox")),
                                drafts: DraftStore(directory: root.appending(path: "drafts")))
        })
        #expect(model.session == nil)
        let message = try #require(model.startupError)
        #expect(message.contains(container.path))
        #expect(message.contains("Could not move"))
        #expect(started == 0)
        #expect(FileManager.default.fileExists(atPath: container.appending(path: "drafts/b.json").path))

        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: root.appending(path: "drafts").path)
        model.retryStart()
        #expect(model.startupError == nil)
        #expect(model.session != nil)
        #expect(started == 1)
        #expect(try text(root.appending(path: "drafts/b.json")) == "b")
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
