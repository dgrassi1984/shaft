import Foundation
import Testing
import UserNotifications
@testable import SHAFTKit

/// Topic threads (docs/app-api.md §3.1, §6, §7, §11): several open chats per instance and a From feed. The JSON here is
/// written by hand from the contract, until the server's own fixtures carry it.
@MainActor
struct ThreadsTests {
    nonisolated static let threadA = "web-aaaa000000000001"
    nonisolated static let threadB = "web-bbbb000000000002"

    /// Two open threads, B the most recently active and replying behind A's reply; the From feed unread.
    nonisolated static let threadsJSON = """
    {"threads": [
      {"id": "web-aaaa000000000001", "title": "Ledger fix", "created": "2026-11-10T09:00:00+01:00",
       "last": "2026-11-10T10:00:00+01:00", "line": "you: ship it?", "state": "idle", "waiting": null,
       "unread": false},
      {"id": "web-bbbb000000000002", "title": "October close", "created": "2026-11-10T09:30:00+01:00",
       "last": "2026-11-10T11:00:00+01:00", "line": "Reconciled 214 accounts.", "state": "replying",
       "waiting": "waiting: answering in «Ledger fix»", "unread": true}
     ],
     "from": {"line": "Nightly report sent.", "last": "2026-11-10T07:00:00+01:00", "unread": true}}
    """

    nonisolated static let notFound = #"{"error": {"code": "not_found", "message": "Nothing at /app/v1/threads/wisp."}}"#

    func makeStore(_ server: StubServer, folder: URL) throws -> InstancesStore {
        InstancesStore(client: server.client, outbox: try Outbox(directory: folder.appending(path: "outbox")),
                       drafts: DraftStore(directory: folder.appending(path: "drafts")))
    }

    /// A server with threads for `wisp`, answering the rail, the threads and anything else with `{"said"}`.
    func threadsServer(threads: String = ThreadsTests.threadsJSON) throws -> StubServer {
        let instances = fixtureText(try DecodingTests.fixture("instances"))
        return StubServer { request in
            switch request.path {
            case "/app/v1/instances": return .json(200, instances)
            case "/app/v1/threads/wisp": return .json(200, threads)
            case "/app/v1/chat/wisp/seen": return .json(200, #"{"unread": false, "badge": 1}"#)
            case let path where path.hasPrefix("/app/v1/chat/wisp/web-") || path == "/app/v1/from/wisp":
                return .json(200, fixtureText(APIClientTests.idle))
            default: return .json(200, #"{"said": "ok"}"#)
            }
        }
    }

    // MARK: Decoding

    @Test func theThreadsListDecodesAsTheContractWritesIt() throws {
        let answer = try SHAFTJSON.decoder().decode(ThreadsResponse.self, from: Data(Self.threadsJSON.utf8))
        #expect(answer.threads.map(\.id) == [Self.threadA, Self.threadB])
        #expect(answer.threads[0].waiting == nil)
        #expect(answer.threads[1].replying)
        #expect(answer.threads[1].waiting == "waiting: answering in «Ledger fix»")
        #expect(answer.threads[1].unread)
        #expect(answer.newest?.id == Self.threadB)
        #expect(answer.from.unread)
        #expect(answer.from.line == "Nightly report sent.")
        // The fixture check keeps every field the server sends.
        let json = try JSONSerialization.jsonObject(with: Data(Self.threadsJSON.utf8))
        #expect(try FixtureShape.check(json, name: "threads") == 1)

        // A thread nothing was said in yet: no `last`; it is newer by its creation.
        let fresh = """
        {"threads": [{"id": "web-c", "title": "New thread", "created": "2026-11-10T12:00:00+01:00", "last": null,
                      "line": "", "state": "idle", "waiting": null, "unread": false},
                     {"id": "web-d", "title": "Older", "created": "2026-11-10T08:00:00+01:00",
                      "last": "2026-11-10T11:59:00+01:00", "line": "x", "state": "idle", "unread": false}],
         "from": {"line": "", "last": null, "unread": false}}
        """
        let decoded = try SHAFTJSON.decoder().decode(ThreadsResponse.self, from: Data(fresh.utf8))
        #expect(decoded.threads[0].last == nil)
        #expect(decoded.threads[1].waiting == nil)
        #expect(decoded.newest?.id == "web-c")
        #expect(decoded.from.last == nil)
    }

    @Test func theNewFieldsAreOptionalForAServerBeforeThreads() throws {
        let old = #"{"id": "web-1", "state": "idle", "created": "2026-11-03T13:58:40+01:00", "ended": null}"#
        #expect(try SHAFTJSON.decoder().decode(ChatInfo.self, from: Data(old.utf8)).title == nil)
        let titled = #"{"id": "web-1", "title": "Ledger fix", "state": "idle", "created": "2026-11-03T13:58:40+01:00", "ended": null}"#
        #expect(try SHAFTJSON.decoder().decode(ChatInfo.self, from: Data(titled.utf8)).title == "Ledger fix")

        let oldEvent = #"{"title": "t replied", "body": "b", "url": "/i/t/chat", "tag": "t", "instance": "t"}"#
        #expect(try SHAFTJSON.decoder().decode(PushEvent.self, from: Data(oldEvent.utf8)).cid == nil)
        let event = #"{"title": "t replied", "body": "b", "url": "/i/t/chat?id=web-a", "tag": "t", "instance": "t", "cid": "web-a"}"#
        #expect(try SHAFTJSON.decoder().decode(PushEvent.self, from: Data(event.utf8)).cid == "web-a")

        // The server's fixture is a server with threads; one before them sends no `threads` on a row.
        var object = try #require(JSONSerialization.jsonObject(with: DecodingTests.fixture("instances")) as? [String: Any])
        var list = try #require(object["instances"] as? [[String: Any]])
        list[0].removeValue(forKey: "threads")
        object["instances"] = list
        #expect(try SHAFTJSON.decoder().decode(InstancesResponse.self,
                                               from: JSONSerialization.data(withJSONObject: object)).instances[0].threads == nil)
        list[0]["threads"] = 2
        object["instances"] = list
        let withThreads = try SHAFTJSON.decoder().decode(InstancesResponse.self,
                                                         from: JSONSerialization.data(withJSONObject: object))
        #expect(withThreads.instances[0].threads == 2)

        let made = try SHAFTJSON.decoder().decode(
            NewThreadResponse.self, from: Data(#"{"chat": {"id": "web-n", "title": "New thread"}, "said": "thread created"}"#.utf8))
        #expect(made.chat.id == "web-n")
        #expect(try FixtureShape.check(JSONSerialization.jsonObject(with: SHAFTJSON.encoder().encode(made)),
                                       name: "new") == 1)
    }

    // MARK: The client

    @Test func everyThreadRouteGoesWhereTheContractSays() async throws {
        let server = StubServer { request in
            switch request.path {
            case "/app/v1/threads/tundra": return .json(200, Self.threadsJSON)
            case "/app/v1/chat/tundra/new":
                return .json(200, #"{"chat": {"id": "web-n", "title": "Plans"}, "said": "thread created"}"#)
            case "/app/v1/chat/tundra/seen": return .json(200, #"{"unread": false, "badge": 0}"#)
            case "/app/v1/chat/tundra/away": return .json(200, #"{"ok": true}"#)
            case "/app/v1/chat/tundra/stream":
                return .json(409, #"{"error": {"code": "not_replying", "message": "no reply is running"}}"#)
            case "/app/v1/chat/tundra/web-a", "/app/v1/from/tundra", "/app/v1/chat/tundra":
                return .json(200, fixtureText(APIClientTests.idle))
            default: return .json(200, #"{"said": "ok"}"#)
            }
        }
        let client = server.client
        #expect(try await client.threads("tundra").threads.count == 2)
        #expect(try await client.newThread("tundra").chat.id == "web-n")
        _ = try await client.newThread("tundra", title: "Plans")
        _ = try await client.rename("tundra", cid: "web-a", title: "Ledger")
        _ = try await client.seen("tundra", version: "v1", cid: "web-a")
        _ = try await client.seen("tundra", version: "v2")
        _ = try await client.away("tundra", at: Date(timeIntervalSince1970: 1), cid: "from")
        _ = try await client.away("tundra", at: Date(timeIntervalSince1970: 2))
        _ = try await client.unqueue("tundra", mid: "m1", cid: "web-a")
        _ = try await client.unqueue("tundra", mid: "m2")
        _ = try await client.feed("tundra", ref: .thread("web-a"))
        _ = try await client.feed("tundra", ref: .from)
        _ = try await client.feed("tundra", ref: .current)
        for try await _ in client.replyStream("tundra", cid: "web-a") {}
        for try await _ in client.replyStream("tundra") {}

        let requests = server.requests
        #expect(requests.map(\.path) == [
            "/app/v1/threads/tundra", "/app/v1/chat/tundra/new", "/app/v1/chat/tundra/new",
            "/app/v1/chat/tundra/rename", "/app/v1/chat/tundra/seen", "/app/v1/chat/tundra/seen",
            "/app/v1/chat/tundra/away", "/app/v1/chat/tundra/away", "/app/v1/chat/tundra/unqueue",
            "/app/v1/chat/tundra/unqueue", "/app/v1/chat/tundra/web-a", "/app/v1/from/tundra", "/app/v1/chat/tundra",
            "/app/v1/chat/tundra/stream", "/app/v1/chat/tundra/stream",
        ])
        #expect(requests[0].method == "GET")
        #expect(requests[1].json?.isEmpty == true)                      // no title: the first message names it
        #expect(requests[2].json?["title"] as? String == "Plans")
        #expect(requests[3].json?["cid"] as? String == "web-a")
        #expect(requests[3].json?["title"] as? String == "Ledger")
        #expect(requests[4].json?["cid"] as? String == "web-a")
        #expect(requests[4].json?["version"] as? String == "v1")
        #expect(requests[5].json?.keys.sorted() == ["version"])          // a build before threads sent just this
        #expect(requests[6].json?["cid"] as? String == "from")
        #expect(requests[6].json?["at"] as? Int == 1000)
        #expect(requests[7].json?.keys.sorted() == ["at"])
        #expect(requests[8].json?["cid"] as? String == "web-a")
        #expect(requests[9].json?.keys.sorted() == ["mid"])
        #expect(requests[13].query("cid") == "web-a")
        #expect(requests[14].query("cid") == nil)
    }

    /// A message goes to the thread it was written in, in every mode: `cid` with `now` and `queue`, and with the next
    /// cycle too, so the note shows in that conversation and the cycle's answer comes there. None
    /// for the current chat.
    /// Regression: a Next-cycle note went without `cid`, so it and its answer landed in From, away from the thread.
    @Test func aSendCarriesItsConversationInEveryMode() async throws {
        let server = StubServer { _ in .json(200, #"{"said": "sent", "chat": "web-a", "file": null}"#) }
        let cases: [(OutgoingMessage, String?)] = [
            (OutgoingMessage(text: "a", mode: .now, cid: "web-a"), "web-a"),
            (OutgoingMessage(text: "b", mode: .queue, cid: "web-a"), "web-a"),
            (OutgoingMessage(text: "c", mode: .background, cid: "web-a"), "web-a"),
            (OutgoingMessage(text: "d", mode: .background, cid: "from"), "from"),
            (OutgoingMessage(text: "e", mode: .now), nil),
            (OutgoingMessage(text: "f", mode: .background), nil),
            (OutgoingMessage(text: "g", mode: .background, cid: ""), nil),
        ]
        for (message, _) in cases { _ = try await server.client.send("tundra", message) }
        let forms = try server.requests.map {
            try ParsedMultipart(body: $0.body, contentType: #require($0.header("Content-Type")))
        }
        #expect(forms.map { $0.field("cid") } == cases.map(\.1))
        #expect(forms.map { $0.field("mode") } == ["now", "queue", "background", "background", "now", "background",
                                                   "background"])
        #expect(forms.map { $0.field("text") } == ["a", "b", "c", "d", "e", "f", "g"])

        // The thread survives the outbox's storage; a message stored before threads has none.
        let restored = try SHAFTJSON.decoder().decode(OutgoingMessage.self,
                                                      from: SHAFTJSON.encoder().encode(cases[0].0))
        #expect(restored.cid == "web-a")
        let old = #"{"mid": "m", "text": "t", "mode": "now", "quotes": [], "files": []}"#
        #expect(try SHAFTJSON.decoder().decode(OutgoingMessage.self, from: Data(old.utf8)).cid == nil)
    }

    @Test func fromConversationAndCancellation() async throws {
        let server = StubServer { _ in .json(200, #"{"said":"cancelled"}"#) }
        _ = try await server.client.send("wisp", OutgoingMessage(text: "Answer here", mode: .now, cid: "from"))
        let form = try ParsedMultipart(body: server.requests[0].body,
                                       contentType: #require(server.requests[0].header("Content-Type")))
        #expect(form.field("cid") == "from")
        #expect(form.field("mode") == "now")
        _ = try await server.client.uncycle("wisp", mid: "m1", file: "ignored.md")
        _ = try await server.client.uncycle("wisp", file: "note.md")
        #expect(server.requests[1].path == "/app/v1/chat/wisp/uncycle")
        #expect(server.requests[1].json as? [String: String] == ["mid": "m1"])
        #expect(server.requests[2].json as? [String: String] == ["file": "note.md"])

        var json = try #require(JSONSerialization.jsonObject(with: APIClientTests.idle) as? [String: Any])
        json["chat"] = ["id": "web-from", "title": "From wisp", "state": "idle",
                        "created": "2026-11-10T09:00:00+01:00", "ended": NSNull()]
        let feed = try SHAFTJSON.decoder().decode(Feed.self, from: JSONSerialization.data(withJSONObject: json))
        #expect(feed.content?.chat?.title == "From wisp")
        #expect(feed.content?.items.contains { if case .user = $0 { true } else { false } } == true)
        #expect(feed.content?.items.contains { if case .assistant = $0 { true } else { false } } == true)

        let later = #"{"key":"bnote","file":"note","ts":"2026-11-10T09:00:00Z","text":"later","mid":null,"read":false,"attachments":[],"comments":[]}"#
        let old = try SHAFTJSON.decoder().decode(LaterItem.self, from: Data(later.utf8))
        #expect(old.cancellable == nil && old.cancelled == nil)
        var note = try #require(JSONSerialization.jsonObject(with: Data(later.utf8)) as? [String: Any])
        note["cancellable"] = true
        note["cancelled"] = NSNull()
        let active = try SHAFTJSON.decoder().decode(LaterItem.self, from: JSONSerialization.data(withJSONObject: note))
        #expect(active.cancellable == true && active.cancelled == nil)
        note["cancellable"] = false
        note["cancelled"] = "2026-11-10T10:00:00Z"
        let cancelled = try SHAFTJSON.decoder().decode(LaterItem.self, from: JSONSerialization.data(withJSONObject: note))
        #expect(cancelled.cancellable == false && cancelled.cancelled != nil)

        let fromJSON = #"{"line":"you: hello","last":null,"unread":false,"state":"waiting","waiting":"answering elsewhere"}"#
        let row = try SHAFTJSON.decoder().decode(FromRow.self, from: Data(fromJSON.utf8))
        #expect(row.state == "waiting" && row.waiting == "answering elsewhere")
        let oldRow = try SHAFTJSON.decoder().decode(FromRow.self, from: Data(#"{"line":"","last":null,"unread":false}"#.utf8))
        #expect(oldRow.state == nil && oldRow.waiting == nil)
    }

    @Test func fromActionsUseFromAndCancellationRefusalsReachTheScreen() async throws {
        let folder = try temporaryFolder("from-actions")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            if request.path.hasSuffix("/uncycle") {
                return .json(409, #"{"error":{"code":"refused","message":"already read in a cycle"}}"#)
            }
            return .json(200, #"{"said":"ok"}"#)
        }
        let store = try makeStore(server, folder: folder)
        let from = store.chat("wisp", .from)
        await from.stopReply()
        await from.unqueue("queued-mid")
        #expect(server.requests[0].json?["cid"] as? String == "from")
        #expect(server.requests[1].json?["cid"] as? String == "from")
        let note = try SHAFTJSON.decoder().decode(LaterItem.self, from: Data(#"{"key":"bnote","file":"note","ts":"2026-11-10T09:00:00Z","text":"later","mid":"m1","read":false,"attachments":[],"comments":[]}"#.utf8))
        await from.uncycle(note)
        #expect(from.actionError?.message == "already read in a cycle")
    }

    // MARK: Drafts and the outbox never cross threads

    /// Regression: drafts were per instance, so with threads a draft typed in thread A showed in thread B, and B's save
    /// pruned A's attached files from the instance's one folder.
    @Test func aDraftTypedInThreadAIsNotInThreadB() throws {
        let folder = try temporaryFolder("thread-drafts")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = try threadsServer()
        let store = try makeStore(server, folder: folder)
        let a = store.chat("wisp", .thread(Self.threadA))
        let b = store.chat("wisp", .thread(Self.threadB))
        #expect(a !== b)
        #expect(store.chat("wisp", .thread(Self.threadA)) === a)

        #expect(a.attach(data: Data("ledger".utf8), name: "ledger.csv"))
        a.draft.text = "About the ledger"
        #expect(b.draft.isEmpty)
        #expect(store.chat("wisp").draft.isEmpty)
        #expect(store.chat("wisp", .from).draft.isEmpty)

        // B's own draft and file: saving it leaves A's file where it is.
        #expect(b.attach(data: Data("close".utf8), name: "close.csv"))
        b.draft.text = "About the close"
        b.removeAttachment(b.draft.attachments[0].id)
        let aFile = try #require(a.fileURL(of: a.draft.attachments[0]))
        #expect(FileManager.default.fileExists(atPath: aFile.path))

        // On disk too, and after a quit.
        #expect(try store.drafts.load("wisp", cid: Self.threadA).text == "About the ledger")
        #expect(try store.drafts.load("wisp", cid: Self.threadB).text == "About the close")
        #expect(try store.drafts.load("wisp").isEmpty)
        let reopened = try makeStore(server, folder: folder)
        #expect(reopened.chat("wisp", .thread(Self.threadA)).draft.text == "About the ledger")
        #expect(reopened.chat("wisp", .thread(Self.threadA)).draft.attachments.map(\.name) == ["ledger.csv"])
        #expect(reopened.chat("wisp", .thread(Self.threadB)).draft.text == "About the close")
        // An earlier chat has no draft at all.
        #expect(reopened.archivedChat("wisp", cid: Self.threadA).draft.isEmpty)
        #expect(throws: (any Error).self) { try store.drafts.load("wisp", cid: "../x") }
    }

    /// Regression: the outbox was per instance, so a message that failed in thread A showed as not sent in every thread,
    /// and its retry went without `cid` to whichever thread was most recently active.
    @Test func anOutboxMessageForAIsSentWithAsCidAndShowsOnlyInA() async throws {
        let folder = try temporaryFolder("thread-outbox")
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try makeStore(try threadsServer(), folder: folder)
        let sender = RecordingSender(down: true)
        store.outbox.sender = sender
        let a = store.chat("wisp", .thread(Self.threadA))
        let b = store.chat("wisp", .thread(Self.threadB))

        a.draft.text = "Ship the ledger fix"
        guard case .failure = await a.send(mode: .queue) else { Issue.record("the send did not fail"); return }
        #expect(a.unsent.map(\.text) == ["Ship the ledger fix"])
        #expect(a.unsent.first?.cid == Self.threadA)
        #expect(b.unsent.isEmpty)
        #expect(store.chat("wisp").unsent.isEmpty)
        #expect(store.chat("wisp", .from).unsent.isEmpty)

        sender.down = false
        let mid = try #require(a.unsent.first?.mid)
        guard case .success = await a.retry(mid) else { Issue.record("the retry did not go"); return }
        #expect(sender.sent.map(\.message.cid) == [Self.threadA, Self.threadA])
        #expect(sender.sent.map(\.message.mode) == [.queue, .queue])
        #expect(a.unsent.isEmpty)

        // The outbox's own file keeps the thread across a quit.
        sender.down = true
        b.draft.text = "Close October"
        _ = await b.send()
        let reopened = try Outbox(directory: folder.appending(path: "outbox"))
        #expect(reopened.entries(for: "wisp", cid: Self.threadB).map(\.text) == ["Close October"])
        #expect(reopened.entries(for: "wisp", cid: Self.threadA).isEmpty)
        #expect(reopened.entries(for: "wisp").isEmpty)
    }

    /// From sends now with its own cid; its feed can reconcile a send whose response was lost.
    @Test func theFromFeedSendsNowInItsOwnConversation() async throws {
        let folder = try temporaryFolder("thread-from")
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try makeStore(try threadsServer(), folder: folder)
        let sender = RecordingSender(down: true)
        store.outbox.sender = sender
        let from = store.chat("wisp", .from)
        from.draft.text = "Check the nightly totals"
        #expect(try store.drafts.load("wisp", cid: "from").text == "Check the nightly totals")
        _ = await from.send(mode: .now)
        #expect(sender.sent.first?.message.mode == .now)
        #expect(sender.sent.first?.message.cid == "from")
        #expect(sender.sent.first?.message.sentCid == "from")
        try store.outbox.enqueue(OutgoingMessage(text: "Still sending", cid: "from"), to: "wisp")
        #expect(from.unsent.contains { $0.state == .sending })
        var arrived = try SHAFTJSON.decoder().decode(Feed.self, from: APIClientTests.idle)
        arrived.content?.mids = from.unsent.map(\.mid)
        from.apply(.feed(arrived))
        #expect(from.unsent.isEmpty)
        #expect(store.chat("wisp", .thread(Self.threadA)).unsent.isEmpty)
    }

    /// Next cycle from a thread goes `background` with that thread's id: the note shows in the thread, and the cycle's
    /// answer comes there. It stays the thread's while it is not sent.
    @Test func aNextCycleNoteFromAThreadCarriesItsThread() async throws {
        let folder = try temporaryFolder("thread-later")
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try makeStore(try threadsServer(), folder: folder)
        let sender = RecordingSender(down: true)
        store.outbox.sender = sender
        let a = store.chat("wisp", .thread(Self.threadA))
        a.draft.text = "Check March when you can"
        _ = await a.send(mode: .background)
        #expect(sender.sent.first?.message.mode == .background)
        #expect(sender.sent.first?.message.sentCid == Self.threadA)
        #expect(a.unsent.map(\.text) == ["Check March when you can"])
        #expect(store.chat("wisp", .from).unsent.isEmpty)
    }

    // MARK: Threads in the store

    /// An older server answers `404` for `/threads`: the instance keeps one chat, as before threads, and is not asked
    /// again on every poll.
    @Test func aServerBeforeThreadsFallsBackToOneChat() async throws {
        let folder = try temporaryFolder("thread-fallback")
        defer { try? FileManager.default.removeItem(at: folder) }
        let instances = fixtureText(try DecodingTests.fixture("instances"))
        let server = StubServer { request in
            switch request.path {
            case "/app/v1/instances": return .json(200, instances)
            case "/app/v1/chat/wisp": return .json(200, fixtureText(APIClientTests.idle))
            default: return .json(404, Self.notFound)
            }
        }
        let store = try makeStore(server, folder: folder)
        await store.refresh()
        await store.refreshMovedThreads()
        #expect(store.threadsState("wisp") == .unsupported)
        #expect(store.threads("wisp") == nil)
        #expect(store.error == nil)
        #expect(store.defaultRef("wisp") == .current)
        #expect(store.resolve("wisp", nil) == .current)
        #expect(store.resolve("wisp", .thread("web-x")) == .current)
        #expect(store.resolve("wisp", .from) == .current)
        #expect(store.resolve("wisp", .earlier("web-x")) == .earlier("web-x"))

        let asked = server.requests.filter { $0.path == "/app/v1/threads/wisp" }.count
        #expect(asked == 1)
        await store.refresh()
        await store.refreshMovedThreads()
        #expect(server.requests.filter { $0.path == "/app/v1/threads/wisp" }.count == asked)

        // The current chat reads `/chat/<n>`, as before threads.
        let chat = store.chat("wisp", store.defaultRef("wisp"))
        chat.start()
        try await waitUntil { chat.feed != nil }
        chat.stop()
        #expect(server.requests.contains { $0.path == "/app/v1/chat/wisp" })
        #expect(!server.requests.contains { $0.path.hasPrefix("/app/v1/chat/wisp/web-") })

        // A 404 for an instance the rail does not list is not a server before threads.
        await store.refreshThreads("gone")
        #expect(store.threadsState("gone") == .unknown)
        #expect(store.threadsError["gone"]?.code == "not_found")
    }

    @Test func loadedThreadsPickTheNewestAndAnEndedThreadGivesWay() async throws {
        let folder = try temporaryFolder("thread-load")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = try threadsServer()
        let store = try makeStore(server, folder: folder)
        await store.refresh()
        await store.refreshMovedThreads()
        #expect(store.threads("wisp")?.threads.count == 2)
        #expect(store.defaultRef("wisp") == .thread(Self.threadB))
        #expect(store.resolve("wisp", nil) == .thread(Self.threadB))
        #expect(store.resolve("wisp", .thread(Self.threadA)) == .thread(Self.threadA))
        #expect(store.resolve("wisp", .from) == .from)
        #expect(store.resolve("wisp", .thread("web-ended")) == .thread(Self.threadB))

        // Nothing moved: not asked again.
        let asked = server.requests.count
        await store.refreshMovedThreads()
        #expect(server.requests.count == asked)

        // A thread's feed reads `/chat/<n>/<cid>`.
        let chat = store.chat("wisp", .thread(Self.threadA))
        chat.start()
        try await waitUntil { chat.feed != nil }
        chat.stop()
        #expect(server.requests.contains { $0.path == "/app/v1/chat/wisp/\(Self.threadA)" })
    }

    /// Seen marks one thread: the instance stays unread while another thread, or the From feed, is.
    @Test func seenMarksOneThreadAndTheRowStaysUnreadForTheOthers() async throws {
        let folder = try temporaryFolder("thread-seen")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = try threadsServer()
        let store = try makeStore(server, folder: folder)
        await store.refresh()
        await store.refreshThreads("wisp")
        #expect(store.row("wisp")?.unread == true)

        #expect(await store.markSeen("wisp", version: "v", cid: Self.threadB))
        #expect(server.requests.last?.json?["cid"] as? String == Self.threadB)
        #expect(store.threads("wisp")?.threads.first { $0.id == Self.threadB }?.unread == false)
        #expect(store.threads("wisp")?.from.unread == true)
        #expect(store.row("wisp")?.unread == true)

        #expect(await store.markSeen("wisp", version: "v", cid: ChatRef.fromID))
        #expect(store.threads("wisp")?.from.unread == false)
        #expect(store.row("wisp")?.unread == false)
        #expect(store.badge == 1)
    }

    /// A draft and a not-sent message kept for the current chat (by a build before threads) move to the thread a send
    /// without `cid` reaches, the most recently active one, once threads are read: never out of sight, never in two.
    @Test func theCurrentChatsDraftAndUnsentMoveToTheNewestThread() async throws {
        let folder = try temporaryFolder("thread-adopt")
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try makeStore(try threadsServer(), folder: folder)
        let sender = RecordingSender(down: true)
        store.outbox.sender = sender
        let file = try store.drafts.attach(data: Data("x".utf8), name: "note.txt", for: "wisp")
        try store.drafts.save(Draft(text: "half-written", attachments: [file]), for: "wisp")
        _ = await store.outbox.send(OutgoingMessage(text: "lost on the way"), to: "wisp")
        #expect(store.chat("wisp").draft.text == "half-written")

        await store.refresh()
        await store.refreshThreads("wisp")
        let newest = store.chat("wisp", .thread(Self.threadB))
        #expect(newest.draft.text == "half-written")
        let moved = try #require(newest.fileURL(of: newest.draft.attachments[0]))
        #expect(try String(contentsOf: moved, encoding: .utf8) == "x")
        #expect(newest.unsent.map(\.text) == ["lost on the way"])
        #expect(try store.drafts.load("wisp").isEmpty)
        #expect(store.chat("wisp").draft.isEmpty)                // the current chat's store let go of it
        #expect(store.chat("wisp").unsent.isEmpty)
        #expect(store.chat("wisp", .thread(Self.threadA)).draft.isEmpty)
        #expect(store.chat("wisp", .thread(Self.threadA)).unsent.isEmpty)

        // A thread's own draft is never overwritten.
        try store.drafts.save(Draft(text: "another"), for: "wisp")
        await store.refreshThreads("wisp")
        #expect(try store.drafts.load("wisp", cid: Self.threadB).text == "half-written")
        #expect(try store.drafts.load("wisp").text == "another")
    }

    @Test func newThreadAndRenameReadTheListAgain() async throws {
        let folder = try temporaryFolder("thread-new")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = try threadsServer()
        server.setAnswer { request in
            switch request.path {
            case "/app/v1/instances": return .json(200, fixtureText(try DecodingTests.fixture("instances")))
            case "/app/v1/threads/wisp": return .json(200, Self.threadsJSON)
            case "/app/v1/chat/wisp/new":
                return .json(200, #"{"chat": {"id": "web-bbbb000000000002", "title": "New thread"}, "said": "thread created"}"#)
            case "/app/v1/chat/wisp/rename":
                return request.json?["title"] as? String == ""
                    ? .json(409, #"{"error": {"code": "refused", "message": "a title cannot be blank"}}"#)
                    : .json(200, #"{"said": "thread renamed"}"#)
            default: return .json(200, #"{"said": "ok"}"#)
            }
        }
        let store = try makeStore(server, folder: folder)
        await store.refresh()
        #expect(await store.newThread("wisp") == .thread(Self.threadB))
        #expect(server.requests.last?.path == "/app/v1/threads/wisp")
        let chat = store.chat("wisp", .thread(Self.threadB))
        #expect(chat.title == "October close")

        #expect(await chat.rename("Q4 close"))
        #expect(server.requests.last?.path == "/app/v1/threads/wisp")
        #expect(await chat.rename("") == false)
        #expect(chat.actionError?.message == "a title cannot be blank")
        // Only a thread has a title to change.
        #expect(await store.chat("wisp").rename("x") == false)
    }

    // MARK: Notifications

    @Test func aNotificationShowsAndOpensItsThread() {
        let latest = PushLatest(events: [
            PushEvent(title: "tundra replied", body: "In A.", url: "/i/tundra/chat?id=web-a", tag: "tundra",
                      instance: "tundra", cid: "web-a"),
            PushEvent(title: "tundra replied", body: "In B.", url: "/i/tundra/chat?id=web-b", tag: "tundra",
                      instance: "tundra", cid: "web-b"),
            PushEvent(title: "wisp says", body: "Elsewhere.", url: "/i/wisp/from", tag: "wisp", instance: "wisp",
                      cid: "from"),
        ], badge: 3)
        #expect(NotificationContent.choose(latest, instance: "tundra", cid: "web-a")?.body == "In A.")
        #expect(NotificationContent.choose(latest, instance: "tundra")?.body == "In B.")
        #expect(NotificationContent.choose(latest, instance: "tundra", cid: "web-gone")?.body == "In B.")
        #expect(NotificationContent.choose(latest, instance: "orrin", cid: "web-a") == nil)

        let filled = NotificationContent.choose(latest, instance: "wisp", cid: "from")
        #expect(filled?.cid == "from")
        let content = UNMutableNotificationContent()
        filled?.apply(to: content)
        #expect(NotificationContent.instance(from: content.userInfo) == "wisp")
        #expect(NotificationContent.cid(from: content.userInfo) == "from")
        #expect(ChatRef(cid: NotificationContent.cid(from: content.userInfo)) == .from)
        #expect(ChatRef(cid: "web-a") == .thread("web-a"))
        #expect(ChatRef(cid: nil) == nil)
        #expect(NotificationContent.cid(from: ["instance": "wisp"]) == nil)

        let model = AppModel(session: SHAFTSession(pairing: PairingStore(store: MemoryCredentialStore()),
                                                   outbox: try! Outbox(directory: FileManager.default.temporaryDirectory
                                                       .appending(path: "shaft-tap-\(UUID().uuidString)")),
                                                   drafts: DraftStore(directory: FileManager.default.temporaryDirectory)))
        model.notificationTapped(instance: "tundra", cid: "web-a")
        #expect(model.openChat == ChatTarget(instance: "tundra", cid: "web-a"))
        #expect(model.openChat?.ref == .thread("web-a"))
        model.notificationTapped(instance: "tundra")
        #expect(model.openChat?.ref == nil)
    }
}
