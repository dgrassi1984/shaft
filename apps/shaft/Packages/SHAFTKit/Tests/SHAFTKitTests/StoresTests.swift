import Foundation
import Testing
@testable import SHAFTKit

@MainActor
struct StoresTests {
    func makeStore(_ server: StubServer, folder: URL) throws -> InstancesStore {
        InstancesStore(client: server.client, outbox: try Outbox(directory: folder.appending(path: "outbox")),
                       drafts: DraftStore(directory: folder.appending(path: "drafts")))
    }

    /// Bug: a push left badge 1 on the icon while the app already had 0 unread.
    @Test func aStalePushBadgeIsClearedByEveryAnswerEvenWhenZeroIsUnchanged() async throws {
        let folder = try temporaryFolder("badge")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in .notModified }
        let store = try makeStore(server, folder: folder)
        var reported: [Int] = []
        store.onBadge = { reported.append($0) }

        // A 304 before any count is known cannot confirm the initial zero.
        await store.refresh()
        #expect(reported.isEmpty)

        server.setAnswer { _ in
            .json(200, #"{"instances": [], "badge": 0}"#, headers: ["ETag": "\"zero\""])
        }
        await store.refresh()
        #expect(store.badge == 0)
        #expect(reported == [0])
        await store.refresh()
        #expect(reported == [0, 0])

        server.setAnswer { _ in .notModified }
        await store.refresh()
        #expect(server.requests.last?.header("If-None-Match") == "\"zero\"")
        #expect(reported == [0, 0, 0])

        server.setAnswer { _ in .json(200, #"{"unread": false, "badge": 0}"#) }
        #expect(await store.markSeen("wren", version: "shown"))
        #expect(store.badge == 0)
        #expect(reported == [0, 0, 0, 0])
    }

    @Test func seenPostsTheVersionShownAndAStaleOneIsNotAnError() async throws {
        let folder = try temporaryFolder("stores")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            switch request.path {
            case "/app/v1/instances":
                return .json(200, fixtureText(try DecodingTests.fixture("instances")))
            default:
                return request.json?["version"] as? String == "9f2c4e1ab07d3c55"
                    ? .json(200, #"{"unread": false, "badge": 0}"#)
                    : .json(409, #"{"error": {"code": "stale_version", "message": "the chat moved", "version": "41d07be2c95a8f13"}}"#)
            }
        }
        let store = try makeStore(server, folder: folder)
        await store.refresh()
        #expect(store.badge == 1)
        #expect(store.row("wren")?.unread == true)

        #expect(await store.markSeen("wren", version: "old") == false)
        #expect(store.error == nil)
        #expect(store.row("wren")?.unread == true)

        #expect(await store.markSeen("wren", version: "9f2c4e1ab07d3c55"))
        #expect(store.badge == 0)
        #expect(store.row("wren")?.unread == false)
        #expect(server.requests.last?.path == "/app/v1/chat/wren/seen")
    }

    @Test func aListPollThatBeganBeforeSeenLandedDoesNotBringTheDotBack() async throws {
        let folder = try temporaryFolder("seen-poll")
        defer { try? FileManager.default.removeItem(at: folder) }
        let unread = fixtureText(try DecodingTests.fixture("instances"))
        let server = StubServer { _ in .json(200, unread, headers: ["ETag": "\"initial\""]) }
        let store = try makeStore(server, folder: folder)
        var badges: [Int] = []
        store.onBadge = { badges.append($0) }
        await store.refresh()
        #expect(store.row("wren")?.unread == true)

        let began = AsyncStream<Void>.makeStream()
        let release = AsyncStream<Void>.makeStream()
        defer { began.continuation.finish(); release.continuation.finish() }
        var response = StubServer.Response.json(200, unread, headers: ["ETag": "\"dropped\""])
        response.beforeReply = {
            began.continuation.yield(())
            for await _ in release.stream { break }
        }
        let held = response
        server.setAnswer { request in
            request.method == "GET" ? held : .json(200, #"{"unread": false, "badge": 0}"#)
        }
        let poll = Task { await store.refresh() }
        for await _ in began.stream { break }
        #expect(await store.markSeen("wren", version: "shown"))
        release.continuation.yield(())
        await poll.value

        #expect(store.row("wren")?.unread == false)
        #expect(store.badge == 0)
        #expect(badges == [1, 0])
        server.setAnswer { _ in .notModified }
        await store.refresh()
        #expect(server.requests.last?.header("If-None-Match") == "\"initial\"")
        #expect(badges == [1, 0, 0])
    }

    @Test func aSeenThatFailedIsTriedAgainUntilItLands() async throws {
        let folder = try temporaryFolder("seen-retry")
        defer { try? FileManager.default.removeItem(at: folder) }
        let unread = fixtureText(try DecodingTests.fixture("instances"))
        let steps = Steps()
        let server = StubServer { request in
            if request.method == "GET" { return .json(200, unread) }
            if steps.next() == 1 { throw StubServer.NetworkDown() }
            return .json(200, #"{"unread": false, "badge": 0}"#)
        }
        let store = InstancesStore(client: server.client,
                                   outbox: try Outbox(directory: folder.appending(path: "outbox")),
                                   drafts: DraftStore(directory: folder.appending(path: "drafts")),
                                   sleep: { steps.sleep($0) })
        await store.refresh()
        let chat = store.chat("wren")
        chat.apply(.feed(try SHAFTJSON.decoder().decode(Feed.self, from: APIClientTests.idle)))

        #expect(await chat.markSeenUntilDone())
        #expect(steps.sleeps == [.seconds(2)])
        #expect(store.row("wren")?.unread == false)
        #expect(store.badge == 0)
        let posts = server.requests.filter { $0.method == "POST" }
        #expect(posts.count == 2)
        #expect(posts.allSatisfy { $0.path == "/app/v1/chat/wren/seen" })
        #expect(posts.allSatisfy { $0.json?["version"] as? String == "109fd56053bf7c4d" })
    }

    @Test func markingSeenStopsWhenCancelled() async throws {
        let folder = try temporaryFolder("seen-cancel")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let steps = Steps()
        let sleeping = AsyncStream<Void>.makeStream()
        defer { sleeping.continuation.finish() }
        let store = InstancesStore(client: server.client,
                                   outbox: try Outbox(directory: folder.appending(path: "outbox")),
                                   drafts: DraftStore(directory: folder.appending(path: "drafts")),
                                   sleep: { duration in
                                       steps.sleep(duration)
                                       sleeping.continuation.yield(())
                                       try await Task.sleep(for: duration)
                                   })
        let chat = store.chat("wren")
        chat.apply(.feed(try SHAFTJSON.decoder().decode(Feed.self, from: APIClientTests.idle)))
        let task = Task { await chat.markSeenUntilDone() }
        for await _ in sleeping.stream { break }
        task.cancel()

        #expect(await task.value == false)
        #expect(steps.sleeps == [.seconds(2)])
        #expect(server.requests.count == 1)
        #expect(server.requests.first?.path == "/app/v1/chat/wren/seen")
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
