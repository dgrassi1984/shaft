import Foundation
import Testing
@testable import SHAFTKit

@MainActor
struct NavigationStoreRegressionTests {
    func makeStore(_ server: StubServer, folder: URL) throws -> InstancesStore {
        try NavigationTests().makeStore(server, folder: folder)
    }

    nonisolated static func answer(_ request: StubServer.Request) -> StubServer.Response {
        if request.path == "/app/v1/instances" { return .json(200, NavigationTests.instances) }
        if request.path.hasPrefix("/app/v1/notes/") { return .json(404, NavigationTests.absent) }
        return .json(200, NavigationTests.threads)
    }

    @Test func unknownMainAndThreadSaveAndQueueButUnsupportedCannotWrite() async throws {
        // Regression: offline launch lost Main drafts and refused sends as not paired.
        let folder = try temporaryFolder("unknown-targets")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let store = try makeStore(server, folder: folder)
        #expect(store.threadsState("example") == .unknown)
        for ref in [ChatRef.from, .thread("web-a")] {
            let chat = store.chat("example", ref)
            chat.draft.text = "Offline message"
            #expect(try store.drafts.load("example", cid: ref.draftKey).text == "Offline message")
            let result = await chat.send()
            guard case .failure(let error) = result else {
                Issue.record("Sent while offline")
                return
            }
            #expect(error != .notPaired)
            let entry = try #require(store.outbox.entries.first { $0.cid == ref.draftKey })
            #expect(entry.text == "Offline message")
            #expect(entry.state == .notSent)
        }
        #expect(store.outbox.entries.count == 2)

        server.setAnswer { request in
            request.path == "/app/v1/instances"
                ? .json(200, NavigationTests.instances) : .json(404, NavigationTests.absent)
        }
        await store.refresh()
        await store.refreshThreads("example")
        #expect(store.threadsState("example") == .unsupported)
        let entries = store.outbox.entries
        let requestCount = server.requests.count
        for ref in [ChatRef.from, .thread("web-a")] {
            let chat = store.chat("example", ref)
            chat.draft.text = "Still deciding the destination"
            #expect(try store.drafts.load("example", cid: ref.draftKey).isEmpty)
            #expect(!chat.attach(data: Data("x".utf8), name: "note.txt"))
            #expect(await chat.send() == .failure(.notPaired))
        }
        #expect(store.outbox.entries == entries)
        #expect(server.requests.count == requestCount)
    }

    @Test func legacyDetectionMovesMainDraftAndOutboxToCurrent() async throws {
        let folder = try temporaryFolder("legacy-main")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            request.path == "/app/v1/instances"
                ? .json(200, NavigationTests.instances) : .json(404, NavigationTests.absent)
        }
        let store = try makeStore(server, folder: folder)
        let file = try store.drafts.attach(data: Data("main file".utf8), name: "main.txt", for: "example", cid: "from")
        try store.drafts.save(Draft(text: "Main", attachments: [file]), for: "example", cid: "from")
        try store.drafts.save(Draft(text: "Legacy"), for: "example")
        let current = store.chat("example", .current)
        let main = store.chat("example", .from)
        main.draft.text = "Main plus typing"
        store.outbox.sender = RecordingSender(down: true)
        let message = OutgoingMessage(text: "Not sent", cid: "from")
        _ = await store.outbox.send(message, to: "example")
        await store.refresh()
        await store.refreshThreads("example")
        #expect(store.resolve("example", .from) == .current)
        #expect(current.draft.text == "Legacy\n\nMain plus typing")
        #expect(main.draft.isEmpty)
        #expect(try store.drafts.load("example", cid: "from").isEmpty)
        let kept = try #require(current.draft.attachments.first)
        #expect(try Data(contentsOf: store.drafts.fileURL(of: kept, for: "example")) == Data("main file".utf8))
        #expect(current.unsent.map(\.mid) == [message.mid])
        #expect(main.unsent.isEmpty)
        let reopened = try Outbox(directory: store.outbox.directory)
        #expect(reopened.entries.first?.cid == nil)
    }

    @Test func createdThreadWaitsForSuccessfulListAndEarlierPromotesWhenFound() async throws {
        let folder = try temporaryFolder("thread-settle")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { Self.answer($0) }
        let store = try makeStore(server, folder: folder)
        await store.refreshThreads("example")
        server.setAnswer { request in
            if request.path.hasSuffix("/new") {
                return .json(200, #"{"chat":{"id":"web-new","title":"New"},"said":"created"}"#)
            }
            throw StubServer.NetworkDown()
        }
        let created = try #require(await store.newThread("example"))
        #expect(store.resolve("example", created) == .thread("web-new"))
        #expect(store.resolve("example", .thread("web-a")) == .thread("web-a"))
        server.setAnswer { Self.answer($0) }
        await store.refreshThreads("example")
        #expect(store.resolve("example", created) == .earlier("web-new"))
        #expect(store.resolve("example", .earlier("web-a")) == .thread("web-a"))
    }

    @Test func staleInstancesResponseCannotChangeETagOrThreadReadRevision() async throws {
        let folder = try temporaryFolder("stale-instances")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            if request.path == "/app/v1/instances" {
                let body = NavigationTests.instances.replacingOccurrences(of: #""instances_rev":1"#, with: #""instances_rev":2"#)
                return .json(200, body, headers: ["ETag": "accepted"])
            }
            return Self.answer(request)
        }
        let store = try makeStore(server, folder: folder)
        await store.refresh()
        await store.refreshMovedThreads()
        let reads = server.requests.filter { $0.path == "/app/v1/threads/example" }.count
        server.setAnswer { request in
            if request.path == "/app/v1/instances" {
                return .json(200, NavigationTests.instances, headers: ["ETag": "stale"])
            }
            return Self.answer(request)
        }
        await store.refresh()
        await store.refreshMovedThreads()
        #expect(server.requests.filter { $0.path == "/app/v1/threads/example" }.count == reads)
        server.setAnswer { _ in .notModified }
        await store.refresh()
        #expect(server.requests.last?.header("If-None-Match") == "accepted")
    }

    @Test func transientNotFoundKeepsLoadedThreadsAndSelection() async throws {
        let folder = try temporaryFolder("transient-threads")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { Self.answer($0) }
        let store = try makeStore(server, folder: folder)
        await store.refresh()
        await store.refreshThreads("example")
        server.setAnswer { _ in .json(404, NavigationTests.absent) }
        await store.refreshThreads("example")
        #expect(store.threads("example")?.threads.map(\.id) == ["web-a", "web-b"])
        #expect(store.resolve("example", .thread("web-a")) == .thread("web-a"))
        #expect(store.threadsError["example"]?.status == 404)
        server.setAnswer { Self.answer($0) }
        await store.refreshMovedThreads()
        #expect(store.threadsError["example"] == nil)
        #expect(store.resolve("example", .thread("web-a")) == .thread("web-a"))
    }

    @Test func historicalDraftMergesWithoutLosingEitherDraftOrFiles() async throws {
        let folder = try temporaryFolder("historical-merge")
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try makeStore(StubServer { Self.answer($0) }, folder: folder)
        let oldFile = try store.drafts.attach(data: Data("old".utf8), name: "old.txt", for: "example")
        let newFile = try store.drafts.attach(data: Data("new".utf8), name: "new.txt", for: "example", cid: "web-b")
        try store.drafts.save(Draft(text: "Old", quotes: [Quote(quote: "old quote")], attachments: [oldFile]), for: "example")
        try store.drafts.save(Draft(text: "New", attachments: [newFile]), for: "example", cid: "web-b")
        let destination = store.chat("example", .thread("web-b"))
        await store.refreshThreads("example")
        #expect(destination.draft.text == "New\n\nOld")
        #expect(destination.draft.quotes == [Quote(quote: "old quote")])
        #expect(destination.draft.attachments.count == 2)
        let bytes = try destination.draft.attachments.map {
            try Data(contentsOf: store.drafts.fileURL(of: $0, for: "example", cid: "web-b"))
        }
        #expect(bytes == [Data("new".utf8), Data("old".utf8)])
        #expect(try store.drafts.load("example").isEmpty)
        await store.refreshThreads("example")
        #expect(destination.draft.text == "New\n\nOld")
    }

    @Test func moveResponseOrdersKnownRowsWithoutDroppingUnlistedRows() async throws {
        let folder = try temporaryFolder("move-partial")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { Self.answer($0) }
        let store = try makeStore(server, folder: folder)
        await store.refreshThreads("example")
        server.setAnswer { request in
            if request.path.hasSuffix("/move") {
                return .json(200, #"{"rev":2,"ids":["web-b","web-unseen"]}"#)
            }
            throw StubServer.NetworkDown()
        }
        await store.moveThread("example", id: "web-b", after: nil)
        #expect(store.threads("example")?.threads.map(\.id) == ["web-b", "web-a"])
        #expect(store.resolve("example", .thread("web-a")) == .thread("web-a"))
    }
    @Test func aThreadCreatedDuringAMoveDoesNotSettleAgainstTheOldList() async throws {
        let folder = try temporaryFolder("create-during-move")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { Self.answer($0) }
        let store = try makeStore(server, folder: folder)
        await store.refreshThreads("example")
        let began = AsyncStream<Void>.makeStream()
        let release = AsyncStream<Void>.makeStream()
        defer { began.continuation.finish(); release.continuation.finish() }
        var held = StubServer.Response.json(200, #"{"rev":2,"ids":["web-b","web-a"]}"#)
        held.beforeReply = {
            began.continuation.yield(())
            for await _ in release.stream { break }
        }
        let response = held
        server.setAnswer { request in
            if request.path.hasSuffix("/move") { return response }
            if request.path.hasSuffix("/new") {
                return .json(200, #"{"chat":{"id":"web-new","title":"New"},"said":"created"}"#)
            }
            throw StubServer.NetworkDown()
        }
        let moving = Task { await store.moveThread("example", id: "web-b", after: nil) }
        for await _ in began.stream { break }
        let created = try #require(await store.newThread("example"))
        #expect(store.resolve("example", created) == .thread("web-new"))
        release.continuation.finish()
        await moving.value
        #expect(store.resolve("example", created) == .thread("web-new"))
    }

    @Test func historicalMigrationRetriesAfterASkippedMove() async throws {
        let folder = try temporaryFolder("migration-retry")
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try makeStore(StubServer { Self.answer($0) }, folder: folder)
        try store.drafts.save(Draft(text: "Old"), for: "example")
        let blocked = store.drafts.directory.appending(path: "example@web-b.json")
        try Data("unreadable draft".utf8).write(to: blocked)
        await store.refreshThreads("example")
        #expect(try store.drafts.load("example").text == "Old")
        try FileManager.default.removeItem(at: blocked)
        await store.refreshThreads("example")
        #expect(try store.drafts.load("example").isEmpty)
        #expect(try store.drafts.load("example", cid: "web-b").text == "Old")
    }

}
