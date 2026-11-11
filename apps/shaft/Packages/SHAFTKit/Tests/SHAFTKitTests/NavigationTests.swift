import Foundation
import Testing
@testable import SHAFTKit

@MainActor
struct NavigationTests {
    nonisolated static let instances = """
    {"instances":[{"name":"example","state":"idle","since":"","model":"","usd_today":0,
    "line":"","last":null,"replying":false,"unread":true,"next_cycle":"","voice_mode":"off",
    "threads":2,"threads_rev":"content-1","unread_main":false,"unread_threads":1}],
    "instances_rev":1,"badge":1}
    """
    nonisolated static let threads = """
    {"threads":[
    {"id":"web-a","title":"A","created":"2026-11-10T09:00:00Z","line":"","state":"idle","unread":false,"failed":true},
    {"id":"web-b","title":"B","created":"2026-11-10T10:00:00Z","line":"","state":"idle","unread":true}],
    "from":{"line":"","last":null,"unread":false,"failed":false},"threads_rev":"content-1"}
    """
    nonisolated static let absent = #"{"error":{"code":"not_found","message":"Not supported"}}"#

    func makeStore(_ server: StubServer, folder: URL) throws -> InstancesStore {
        InstancesStore(client: server.client, outbox: try Outbox(directory: folder.appending(path: "outbox")),
                       drafts: DraftStore(directory: folder.appending(path: "drafts")))
    }

    @Test func mainSublineUsesItsReplyThenPendingNotesThenItsLastLine() throws {
        var row = try SHAFTJSON.decoder().decode(InstancesResponse.self, from: Data(Self.instances.utf8)).instances[0]
        row.line = "Legacy preview"
        var main = FromRow(line: "you: Check the result", last: nil, unread: false)
        main.state = "idle"
        #expect(ConversationLabels.mainSubline(row, main: main, pendingNotes: 0) == "you: Check the result")
        #expect(ConversationLabels.mainSubline(row, main: main, pendingNotes: 2) == "☾ 2 for next cycle")
        main.state = "replying"
        #expect(ConversationLabels.mainSubline(row, main: main, pendingNotes: 2) == "replying…")
        row.replying = true
        main.state = "idle" // A reply in a topic thread does not replace Main's line.
        #expect(ConversationLabels.mainSubline(row, main: main, pendingNotes: 0) == "you: Check the result")
        main.line = ""
        #expect(ConversationLabels.mainSubline(row, main: main, pendingNotes: 0) == "")
        #expect(ConversationLabels.mainSubline(row, main: nil, pendingNotes: 0) == "replying…")
        row.replying = false
        #expect(ConversationLabels.mainSubline(row, main: nil, pendingNotes: 0) == "Legacy preview")
    }

    @Test func headerSecondLineNamesTheInstanceOnlyInAThread() {
        for state in ["idle", "in a cycle", "sleeping", "paused"] {
            #expect(ConversationLabels.header(instance: "example", state: state, replying: false, inThread: false) == state)
            #expect(ConversationLabels.header(instance: "example", state: state, replying: false, inThread: true) == "example · \(state)")
        }
        #expect(ConversationLabels.header(instance: "example", state: "idle", replying: true, inThread: true) == "example · replying")
        #expect(ConversationLabels.header(instance: "example", state: "idle", replying: true, inThread: false) == "replying")
    }

    @Test func navigationFieldsDecodeWithEitherRevisionTypeAndMayBeAbsent() throws {
        let decoder = SHAFTJSON.decoder()
        let list = try decoder.decode(InstancesResponse.self, from: Data(Self.instances.utf8))
        #expect(list.instancesRev == .number(1))
        #expect(list.instances[0].threadsRev == .text("content-1"))
        #expect(list.instances[0].unreadMain == false)
        #expect(list.instances[0].unreadThreads == 1)
        let numeric = Self.instances.replacingOccurrences(of: #""threads_rev":"content-1""#, with: #""threads_rev":7"#)
        #expect(try decoder.decode(InstancesResponse.self, from: Data(numeric.utf8)).instances[0].threadsRev == .number(7))
        let threads = try decoder.decode(ThreadsResponse.self, from: Data(Self.threads.utf8))
        #expect(threads.threads[0].failed == true)
        #expect(threads.threads[1].failed == nil)
        #expect(threads.from.failed == false)
        var old = try #require(JSONSerialization.jsonObject(with: Data(Self.threads.utf8)) as? [String: Any])
        old.removeValue(forKey: "threads_rev")
        var main = try #require(old["from"] as? [String: Any])
        main.removeValue(forKey: "failed")
        old["from"] = main
        let oldThreads = try decoder.decode(ThreadsResponse.self, from: JSONSerialization.data(withJSONObject: old))
        #expect(oldThreads.threadsRev == nil)
        #expect(oldThreads.from.failed == nil)
        var oldList = try #require(JSONSerialization.jsonObject(with: Data(Self.instances.utf8)) as? [String: Any])
        oldList.removeValue(forKey: "instances_rev")
        var rows = try #require(oldList["instances"] as? [[String: Any]])
        for key in ["threads_rev", "unread_main", "unread_threads"] { rows[0].removeValue(forKey: key) }
        oldList["instances"] = rows
        let legacy = try decoder.decode(InstancesResponse.self, from: JSONSerialization.data(withJSONObject: oldList))
        #expect(legacy.instancesRev == nil)
        #expect(legacy.instances[0].threadsRev == nil)
        #expect(legacy.instances[0].unreadMain == nil)
        #expect(legacy.instances[0].unreadThreads == nil)
        #expect(try decoder.decode(OrderMoveResponse.self, from: Data("{}".utf8)).rev == nil)
        #expect(try decoder.decode(PendingNotesResponse.self, from: Data("{}".utf8)).notes == nil)
    }

    @Test func defaultAndNotificationRoutingKeepMainAndEndedIdentity() async throws {
        let folder = try temporaryFolder("navigation-routing")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            if request.path == "/app/v1/instances" { return .json(200, Self.instances) }
            return .json(200, Self.threads)
        }
        let store = try makeStore(server, folder: folder)
        #expect(store.defaultRef("example") == .from)
        await store.refresh()
        await store.refreshThreads("example")
        #expect(store.defaultRef("example") == .from)
        #expect(store.resolve("example", .current) == .from)
        #expect(store.resolve("example", ChatTarget(instance: "example").ref) == .from)
        #expect(store.resolve("example", ChatTarget(instance: "example", cid: "web-b").ref) == .thread("web-b"))
        #expect(store.resolve("example", ChatTarget(instance: "example", cid: "web-ended").ref) == .earlier("web-ended"))
    }

    @Test func movesSendNullAnchorsAndRefusalRestoresOrder() async throws {
        let folder = try temporaryFolder("navigation-move")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            if request.path.hasSuffix("/move") {
                return .json(409, #"{"error":{"code":"refused","message":"Thread closed"}}"#)
            }
            if request.path == "/app/v1/instances" { return .json(200, Self.instances) }
            return .json(200, Self.threads)
        }
        let store = try makeStore(server, folder: folder)
        await store.refresh()
        await store.refreshThreads("example")
        await store.moveThread("example", id: "web-b", after: nil)
        #expect(store.threads("example")?.threads.map(\.id) == ["web-a", "web-b"])
        #expect(store.error?.status == 409)
        let move = try #require(server.requests.first { $0.path == "/app/v1/threads/example/move" })
        #expect(move.json?["id"] as? String == "web-b")
        #expect(move.json?["after"] is NSNull)
        #expect(move.method == "POST")
        _ = try? await server.client.moveThread("example", id: "web-b", after: "web-a")
        #expect(server.requests.last?.json?["after"] as? String == "web-a")
        _ = try? await server.client.moveInstance("example", after: nil)
        #expect(server.requests.last?.path == "/app/v1/instances/move")
        #expect(server.requests.last?.json?["name"] as? String == "example")
        #expect(server.requests.last?.json?["after"] is NSNull)
    }

    @Test func aContentRevisionRefetchesUnchangedThreadCounts() async throws {
        let folder = try temporaryFolder("navigation-refresh")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { request in
            if request.path == "/app/v1/instances" { return .json(200, Self.instances) }
            if request.path.hasPrefix("/app/v1/notes/") { return .json(404, Self.absent) }
            return .json(200, Self.threads)
        }
        let store = try makeStore(server, folder: folder)
        await store.refresh()
        await store.refreshMovedThreads()
        let count = server.requests.filter { $0.path == "/app/v1/threads/example" }.count
        await store.refreshMovedThreads()
        #expect(server.requests.filter { $0.path == "/app/v1/threads/example" }.count == count)
        server.setAnswer { request in
            if request.path == "/app/v1/instances" {
                return .json(200, Self.instances.replacingOccurrences(of: "content-1", with: "content-2"))
            }
            if request.path.hasPrefix("/app/v1/notes/") { return .json(404, Self.absent) }
            return .json(200, Self.threads.replacingOccurrences(of: #""title":"A""#, with: #""title":"Renamed""#))
        }
        await store.refresh()
        await store.refreshMovedThreads()
        #expect(store.threads("example")?.threads.first?.title == "Renamed")
        #expect(server.requests.filter { $0.path == "/app/v1/threads/example" }.count == count + 1)
        #expect(store.notesError["example"] == nil)
        #expect(store.pendingNotes["example"] == [])
        server.setAnswer { request in
            if request.path == "/app/v1/instances" {
                return .json(200, Self.instances.replacingOccurrences(of: "content-1", with: "content-2")
                    .replacingOccurrences(of: #""instances_rev":1"#, with: #""instances_rev":2"#))
            }
            if request.path.hasPrefix("/app/v1/notes/") { return .json(404, Self.absent) }
            return .json(200, Self.threads)
        }
        await store.refresh()
        await store.refreshMovedThreads()
        #expect(server.requests.filter { $0.path == "/app/v1/threads/example" }.count == count + 2)
    }

    @Test func pendingNotesDecodeDestinationsAndCancellationWithoutRequiringNewFields() throws {
        let response = try SHAFTJSON.decoder().decode(PendingNotesResponse.self, from: Data(#"{"notes":[{"file":"note.md","mid":"m1","text":"Read later","ts":"2026-11-10T09:00:00Z","cid":"from","title":"example","cancellable":true},{}]}"#.utf8))
        let note = try #require(response.notes?.first)
        #expect(note.file == "note.md")
        #expect(note.mid == "m1")
        #expect(note.text == "Read later")
        #expect(note.ts != nil)
        #expect(note.cid == "from")
        #expect(note.title == "example")
        #expect(note.cancellable == true)
        #expect(response.notes?.last?.cancellable == nil)
    }

    @Test func unknownCapabilityCannotPersistOrSendAnUntargetedMessage() async throws {
        let folder = try temporaryFolder("navigation-unknown")
        defer { try? FileManager.default.removeItem(at: folder) }
        let server = StubServer { _ in .json(404, Self.absent) }
        let store = try makeStore(server, folder: folder)
        let chat = store.chat("example", .current)
        chat.draft.text = "Keep in memory until routing is known"
        #expect(try store.drafts.load("example").isEmpty)
        if case .success = await chat.send() { Issue.record("Unknown destination was sent") }
        #expect(store.outbox.entries.isEmpty)
        #expect(server.requests.isEmpty)
    }
}
