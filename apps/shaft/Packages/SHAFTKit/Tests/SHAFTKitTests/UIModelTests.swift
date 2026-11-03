import Foundation
import Testing
import UserNotifications
@testable import SHAFTKit

/// What the UI lanes draw from: the rows of a feed with their day separators, the text quotes become, the operator's
/// own text, the documents list and anchors, and the notification policy.
struct FeedRowsTests {
    func feed(_ name: String) throws -> Feed {
        try SHAFTJSON.decoder().decode(Feed.self, from: try DecodingTests.fixture(name))
    }

    @Test func dayRowsGoBeforeTheFirstItemOfEachDayInTheDevicesZone() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Europe/Rome")!
        var feed = try feed("contract-idle")
        var content = try #require(feed.content)
        // Move the first item to the day before: two days, two separators.
        if case .user(var first) = content.items[0] {
            first.ts = first.ts.addingTimeInterval(-86_400)
            content.items[0] = .user(first)
        }
        feed.content = content
        let rows = FeedRows.rows(feed, calendar: calendar)
        let days = rows.compactMap { row -> Date? in if case .day(let d) = row { d } else { nil } }
        #expect(days.count == 2)
        #expect(rows.first?.id.hasPrefix("day-") == true)
        #expect(rows.dropFirst().first?.id == "u0")
        #expect(Set(rows.map(\.id)).count == rows.count)          // ids are unique: a list diffs on them
        #expect(rows.last == .end)                                  // can_end
    }

    @Test func theReplyQueuedMessagesAndNotSentOnesFollowTheItems() throws {
        let feed = try feed("contract-live")
        let content = try #require(feed.content)
        let held = OutboxEntry(instance: "thelos", mid: content.mids[0], text: "arrived", mode: .now, quotes: [],
                               retryOf: nil, files: [], created: Date(), state: .sending, reason: nil)
        let lost = OutboxEntry(instance: "thelos", mid: "lost-1", text: "lost", mode: .now, quotes: [], retryOf: nil,
                               files: [], created: Date(), state: .notSent, reason: "offline")
        let ids = FeedRows.rows(feed, unsent: [held, lost]).map(\.id)
        let live = try #require(ids.firstIndex(of: "live"))
        #expect(ids[live + 1].hasPrefix("queue-"))
        #expect(ids.contains("unsent-lost-1"))
        #expect(!ids.contains("unsent-\(content.mids[0])"))          // the feed holds its mid: it arrived
        #expect(!ids.contains("acts"))                                // replying: no End
        #expect(FeedRows.newestKey(feed) == "live")
    }

    @Test func anArchivedChatShowsNothingThatActs() throws {
        var feed = try feed("contract-live")
        feed.content?.archived = true
        feed.content?.canEnd = true
        let ids = FeedRows.rows(feed).map(\.id)
        #expect(!ids.contains { $0.hasPrefix("queue-") || $0 == "acts" })
    }

    @Test func arrivalsAreTheInstancesNewAnswersNotTheFirstLoadNorTheOperatorsOwn() throws {
        let idle = try feed("contract-idle")
        #expect(FeedRows.arrivals(from: nil, to: idle).isEmpty)
        var older = idle
        older.content?.items.removeLast()                           // a3, the failed answer
        older.content?.items.removeLast()                           // u2
        let new = FeedRows.arrivals(from: older, to: idle)
        #expect(new.map(\.key) == ["a3"])
    }
}

struct QuoteTextTests {
    // The server's own cases (supervisor/tests/test_app_api.py, test_quotes_become_the_text_as_the_web_composer_writes_it).
    @Test func quotesBecomeTheTextAsTheServerWritesIt() {
        #expect(QuoteText.compose("Rerun it", quotes: []) == "Rerun it")
        #expect(QuoteText.compose(" and also ", quotes: [Quote(quote: " one\r\ntwo  \n")]) == "> one\n> two\n\nand also")
        #expect(QuoteText.compose("first", quotes: [Quote(quote: "a\n\nb", answer: " yes ", doc: "plan.md"),
                                                     Quote(quote: "c")])
                == "first\n\n> (from notes/docs/plan.md)\n> a\n>\n> b\n\nyes\n\n> c")
        #expect(QuoteText.compose("", quotes: [Quote(quote: "only")]) == "> only")
        #expect(QuoteText.compose("Rerun it", quotes: [Quote(quote: "Keep the lines?", answer: "Yes")])
                == "Rerun it\n\n> Keep the lines?\n\nYes")
    }

    @Test func theOperatorsTextIsQuotesCodeAndPlainText() {
        let blocks = OperatorText.blocks("> Ship it?\n>\n> today\n\nYes:\n```\nmake test\n```\ndone")
        #expect(blocks == [.quote("Ship it?\n\ntoday"), .text("Yes:"), .code("make test"), .text("done")])
        #expect(OperatorText.blocks("   > three spaces quote") == [.quote("three spaces quote")])
        #expect(OperatorText.blocks("    > four is text") == [.text("    > four is text")])
        #expect(OperatorText.blocks("") == [])
    }
}

struct DocsTreeTests {
    func entry(_ path: String, bytes: Int, minutes: Double, inChat: Bool = false, comments: Int = 0) -> DocEntry {
        DocEntry(path: path, bytes: bytes, mtime: Date(timeIntervalSince1970: minutes * 60), sha: "x", tooBig: false,
                 openComments: comments, inChat: inChat)
    }

    @Test func aLevelListsItsFoldersWithTheirTotalsThenItsDocuments() {
        let docs = [entry("ledger.md", bytes: 100, minutes: 5), entry("plans/q4.md", bytes: 50, minutes: 9, inChat: true),
                    entry("plans/deep/x.md", bytes: 10, minutes: 1, comments: 2), entry("zeta.md", bytes: 900, minutes: 2)]
        let top = DocsTree.level(docs, in: "")
        #expect(top.folders.map(\.name) == ["plans"])
        let plans = top.folders[0]
        #expect((plans.count, plans.bytes, plans.inChat, plans.openComments) == (2, 60, true, 2))
        #expect(top.docs.map(\.path) == ["ledger.md", "zeta.md"])                 // newest first by default
        #expect(DocsTree.level(docs, in: "", sort: .name, ascending: true).docs.map(\.path) == ["ledger.md", "zeta.md"])
        #expect(DocsTree.level(docs, in: "", sort: .size).docs.map(\.path) == ["zeta.md", "ledger.md"])
        let inner = DocsTree.level(docs, in: "plans")
        #expect(inner.folders.map(\.path) == ["plans/deep"])
        #expect(inner.docs.map(\.path) == ["plans/q4.md"])
        #expect(DocsTree.size(812) == "812 B")
        #expect(DocsTree.size(1536) == "1.5 KB")
    }

    @Test func anAnchorKeepsItsContextAndIsFoundAgainOrNot() throws {
        let text = "Net daily totals are rounded once, at the end. Totals are rounded once more."
        let range = (text as NSString).range(of: "rounded once")
        let anchor = try #require(Anchoring.anchor(in: text, range: range))
        #expect(anchor.exact == "rounded once")
        #expect(anchor.prefix == "Net daily totals are ")
        #expect(anchor.start == range.location)
        #expect(anchor.suffix.count == 32)
        // The second "rounded once" has another prefix: the anchor finds its own.
        #expect(Anchoring.locate(anchor, in: text) == range)
        #expect(Anchoring.locate(anchor, in: "Inserted. " + text)?.location == range.location + 10)
        #expect(Anchoring.locate(anchor, in: "Nothing of it here.") == nil)
        #expect(Anchoring.anchor(in: text, range: NSRange(location: 3, length: 0)) == nil)
    }
}

@MainActor
struct DocumentsStoreTests {
    @Test func aStaleSaveComesBackWithTheDiffAndSavingAgainWithItsShaReplacesIt() async throws {
        let server = StubServer { request in
            switch (request.path, request.json?["sha"] as? String) {
            case ("/app/v1/docs/thelos/save", "old"):
                return .json(409, #"{"error": {"code": "stale", "message": "It changed since you opened it.", "sha": "new", "diff": "-yours\n+theirs"}}"#)
            case ("/app/v1/docs/thelos/save", "new"):
                return .json(200, #"{"said": "saved · thelos gets the change with your next message", "path": "ledger.md", "sha": "newer"}"#)
            case ("/app/v1/docs/thelos/doc", _):
                return .json(200, fixtureText(try DecodingTests.fixture("contract-doc-comments")))
            case ("/app/v1/docs/thelos", _):
                return .json(200, #"{"dir": "notes/docs", "max_bytes": 262144, "docs": []}"#)
            default:
                return .json(404, #"{"error": {"code": "not_found", "message": "no"}}"#)
            }
        }
        let store = DocumentsStore(client: server.client, instance: "thelos")
        let stale = await store.save(path: "ledger.md", text: "mine", sha: "old")
        guard case .stale(let conflict) = stale else { Issue.record("not stale: \(stale)"); return }
        #expect(conflict.sha == "new")
        #expect(conflict.diff == "-yours\n+theirs")
        #expect(conflict.text == "mine")
        #expect(store.error == nil)                                      // a conflict is a choice, not an error

        let saved = await store.save(path: "ledger.md", text: conflict.text, sha: try #require(conflict.sha))
        guard case .saved(let answer) = saved else { Issue.record("not saved: \(saved)"); return }
        #expect(answer.sha == "newer")
        #expect(store.open?.path == "ledger.md")                         // read again after the save
        #expect(store.said?.hasPrefix("saved") == true)
    }

    @Test func aCommentReadsTheDocumentAgainAndARefusalIsSaid() async throws {
        let server = StubServer { request in
            switch request.path {
            case "/app/v1/docs/thelos/doc":
                return .json(200, fixtureText(try DecodingTests.fixture("contract-doc-comments")))
            case "/app/v1/docs/thelos/comment":
                return request.json?["text"] as? String == ""
                    ? .json(409, #"{"error": {"code": "refused", "message": "a comment needs text"}}"#)
                    : .json(200, #"{"said": "kept, not sent", "thread": "c4"}"#)
            default:
                return .json(200, #"{"dir": "notes/docs", "max_bytes": 262144, "docs": []}"#)
            }
        }
        let store = DocumentsStore(client: server.client, instance: "thelos")
        await store.load("ledger.md")
        let doc = try #require(store.open)
        #expect(await store.comment(anchor: nil, text: "Whole thing?", send: .keep))
        let posted = try #require(server.requests.first { $0.path == "/app/v1/docs/thelos/comment" }?.json)
        #expect(posted["sha"] as? String == doc.sha)
        #expect(posted["send"] as? String == "")
        #expect(posted.keys.contains("anchor") && posted["anchor"] is NSNull)
        #expect(server.requests.filter { $0.path == "/app/v1/docs/thelos/doc" }.count == 2)
        #expect(await store.comment(anchor: nil, text: "", send: .now) == false)
        #expect(store.error?.message == "a comment needs text")
    }
}

@MainActor
struct ChatStoreUITests {
    func makeStore(_ server: StubServer, folder: URL) throws -> InstancesStore {
        InstancesStore(client: server.client, outbox: try Outbox(directory: folder.appending(path: "outbox")),
                       drafts: DraftStore(directory: folder.appending(path: "drafts")))
    }

    @Test func ratingsPostWhatTheContractAsks() async throws {
        let folder = try temporaryFolder("ui")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in .json(200, #"{"said": "Feedback kept.", "id": "f-1"}"#) }
        let store = try makeStore(server, folder: folder)
        let chat = store.archivedChat("thelos", cid: "web-3f9a1c0d7e2b4a65")
        #expect(await chat.rate(k: 3, thumb: "down", reason: "wrong month") == "f-1")
        #expect(await chat.rate(file: "m.md", thumb: "up") == "f-1")
        await chat.unpend("f-1")
        await chat.sendRating("f-1", reason: "better")
        let bodies = server.requests.compactMap(\.json)
        #expect(bodies[0]["kind"] as? String == "chat")
        #expect(bodies[0]["cid"] as? String == "web-3f9a1c0d7e2b4a65")
        #expect(bodies[0]["k"] as? Int == 3)
        #expect(bodies[1]["kind"] as? String == "message")
        #expect(bodies[1]["file"] as? String == "m.md")
        #expect(bodies[2]["op"] as? String == "unpend")
        #expect(bodies[3]["reason"] as? String == "better")
        #expect(chat.said == "Feedback kept.")
        #expect(server.requests.allSatisfy { $0.path == "/app/v1/feedback/thelos" })
    }

    @Test func aNotSentMessageGoesBackToTheComposerWithItsFilesAndLeavesTheOutbox() async throws {
        let folder = try temporaryFolder("ui")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let store = try makeStore(server, folder: folder)
        let chat = store.chat("thelos")
        #expect(chat.attach(data: Data("x".utf8), name: "note.txt"))
        chat.draft.text = "Rerun it"
        chat.addQuote("Ship it?")
        _ = await chat.send()
        let lost = try #require(chat.unsent.first)
        chat.draft.text = "also"
        chat.editUnsent(lost.mid)
        #expect(chat.unsent.isEmpty)
        #expect(chat.draft.text == "also\n\nRerun it")
        #expect(chat.draft.quotes == [Quote(quote: "Ship it?")])
        let file = try #require(chat.draft.attachments.first)
        #expect(try Data(contentsOf: try #require(chat.fileURL(of: file))) == Data("x".utf8))
        chat.removeAttachment(file.id)
        chat.removeQuote(at: 0)
        #expect(try store.drafts.load("thelos") == Draft(text: "also\n\nRerun it"))
    }

    @Test func coveringTheChatAsksAgainAtOnceWithWatch0() async throws {
        let folder = try temporaryFolder("ui")
        defer { try? FileManager.default.removeItem(at: folder) }
        let idle = fixtureText(try DecodingTests.fixture("contract-idle"))
        let server = StubServer { request in
            // The first answers at once; a long-poll is held (here: answered 304 after a moment).
            if request.query("since") != nil { Thread.sleep(forTimeInterval: 0.2); return .notModified }
            return .json(200, idle)
        }
        let store = try makeStore(server, folder: folder)
        let chat = store.chat("thelos")
        chat.start()
        try await waitUntil { chat.feed != nil }
        chat.setWatching(false)
        try await waitUntil { server.requests.contains { $0.query("watch") == "0" } }
        #expect(chat.isPolling)
        chat.stop()
        #expect(!chat.isPolling)
    }
}

struct NotificationPolicyTests {
    @Test func theChatOnScreenGetsNoBanner() {
        #expect(AppModel.presentation(for: "thelos", visibleChat: "thelos") == [.badge])
        #expect(AppModel.presentation(for: "thelos", visibleChat: "wren").contains(.banner))
        #expect(AppModel.presentation(for: "thelos", visibleChat: nil).contains(.banner))
        #expect(AppModel.presentation(for: nil, visibleChat: "thelos").contains(.banner))
    }
}

@MainActor
func waitUntil(timeout: Duration = .seconds(5), _ condition: @MainActor () -> Bool) async throws {
    let clock = ContinuousClock()
    let deadline = clock.now + timeout
    while !condition() {
        if clock.now > deadline { throw CancellationError() }
        try await Task.sleep(for: .milliseconds(20))
    }
}

struct AppLinksTests {
    @Test func aChatLinkNamesItsInstanceAndNothingElseDoes() throws {
        #expect(AppLinks.instance(in: try #require(AppLinks.chat("thelos"))) == "thelos")
        #expect(AppLinks.instance(in: URL(string: "shaft://chat/wren-2")!) == "wren-2")
        #expect(AppLinks.instance(in: URL(string: "shaft://chat/")!) == nil)
        #expect(AppLinks.instance(in: URL(string: "shaft://chat/../etc")!) == nil)
        #expect(AppLinks.instance(in: URL(string: "https://chat/thelos")!) == nil)
        #expect(AppLinks.instance(in: URL(string: "shaft://docs/thelos")!) == nil)
    }
}

@MainActor
struct ReconnectTests {
    @Test func aSendThatGetsThroughEndsTheOfflineWaitAtOnce() async throws {
        let folder = try temporaryFolder("ui")
        defer { try? FileManager.default.removeItem(at: folder) }
        let idle = fixtureText(try DecodingTests.fixture("contract-idle"))
        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let store = InstancesStore(client: server.client, outbox: try Outbox(directory: folder.appending(path: "o")),
                                   drafts: DraftStore(directory: folder.appending(path: "d")))
        let chat = store.chat("thelos")
        chat.start()
        // Long enough into the backoff that the next try is seconds away.
        try await waitUntil(timeout: .seconds(20)) {
            if case .retrying(_, let at) = chat.connection { at.timeIntervalSinceNow > 2.5 } else { false }
        }
        // The server is back: the send gets through, and the feed is asked for now, not after the backoff.
        server.setAnswer { request in
            request.path.hasSuffix("/send") ? .json(200, #"{"said": "sent to thelos", "chat": "web-1", "file": null}"#)
                                            : .json(200, idle)
        }
        chat.draft.text = "back online"
        _ = await chat.send()
        try await waitUntil(timeout: .seconds(1)) { chat.connection == .live }
        chat.stop()
    }
}
