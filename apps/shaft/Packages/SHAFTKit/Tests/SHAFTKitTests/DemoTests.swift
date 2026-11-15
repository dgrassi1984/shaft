import Foundation
import Testing
@testable import SHAFTKit

@MainActor
struct DemoTests {
    private func session() throws -> (SHAFTSession, URL) {
        let root = FileManager.default.temporaryDirectory.appending(path: "shaft-demo-test-\(UUID().uuidString)")
        let session = SHAFTSession(pairing: PairingStore(store: MemoryCredentialStore()),
                                   outbox: try Outbox(directory: root.appending(path: "outbox")),
                                   drafts: DraftStore(directory: root.appending(path: "drafts")))
        session.startDemo()
        return (session, root)
    }

    private func feed(_ client: APIClient, _ name: String, cid: String? = nil) async throws -> Feed {
        guard case .value(let feed, _) = try await client.feed(name, cid: cid) else {
            throw APIError.decoding(path: "demo", message: "Expected a full feed")
        }
        return feed
    }

    @Test func DemoOpensWithoutSavingPairingAndLeavesNoFiles() async throws {
        let (session, root) = try session()
        defer { session.leaveDemo(); try? FileManager.default.removeItem(at: root) }
        #expect(session.isDemo)
        #expect(session.isPaired)
        #expect(session.credential == ServerCredential(baseURL: URL(string: "https://demo.shaft.invalid")!,
                                                       device: "demo", secret: "demo", name: "demo", login: "demo"))
        #expect(try session.pairing.current() == nil)
        let directory = try #require(session.demoDirectory)
        let store = try #require(session.store)
        #expect(store.outbox.directory.path.hasPrefix(directory.path + "/"))
        #expect(store.drafts.directory.path.hasPrefix(directory.path + "/"))
        try store.drafts.save(Draft(text: "A throwaway draft"), for: "tundra")
        try store.outbox.enqueue(OutgoingMessage(text: "A throwaway message"), to: "tundra")
        await session.registerPush(token: Data([1, 2, 3]))
        #expect(session.pushStatus == "Not used in the demo")
        let relaunched = SHAFTSession(pairing: session.pairing,
                                      outbox: try Outbox(directory: root.appending(path: "outbox")),
                                      drafts: DraftStore(directory: root.appending(path: "drafts")))
        #expect(!relaunched.isPaired)
        session.leaveDemo()
        #expect(!session.isDemo)
        #expect(session.store == nil)
        #expect(session.credential == nil)
        #expect(session.demoDirectory == nil)
        #expect(session.problem == nil)
        #expect(session.loss == nil)
        #expect(!FileManager.default.fileExists(atPath: directory.path))
        #expect(try session.pairing.current() == nil)
    }

    @Test func DemoEveryCannedJSONBodyDecodes() async throws {
        let (session, root) = try session()
        defer { session.leaveDemo(); try? FileManager.default.removeItem(at: root) }
        let client = try #require(session.store?.client)
        func decode<T: Decodable>(_ type: T.Type, _ path: String, method: String = "GET",
                                   json: String? = nil) async throws -> T {
            var request = URLRequest(url: URL(string: "https://demo.shaft.invalid/app/v1/" + path)!)
            request.httpMethod = method
            request.httpBody = json.map { Data($0.utf8) }
            let (data, response) = try await client.session.data(for: request)
            #expect((response as? HTTPURLResponse)?.statusCode == 200)
            return try SHAFTJSON.decoder().decode(type, from: data)
        }
        _ = try await decode(ServerVersion.self, "version")
        let instances = try await decode(InstancesResponse.self, "instances")
        #expect(instances.instances.map(\.name) == ["tundra", "wisp", "ada"])
        #expect(instances.badge > 0)
        for name in ["tundra", "wisp", "ada"] {
            let threads = try await decode(ThreadsResponse.self, "threads/\(name)")
            #expect(!threads.threads.isEmpty)
            _ = try await decode(ChatsResponse.self, "chats/\(name)")
            _ = try await decode(PendingNotesResponse.self, "notes/\(name)")
            _ = try await decode(Feed.self, "chat/\(name)")
            _ = try await decode(Feed.self, "from/\(name)")
            for thread in threads.threads {
                let feed = try await decode(Feed.self, "chat/\(name)/\(thread.id)")
                #expect(feed.content?.chat?.title == thread.title)
            }
            _ = try await decode(DocsList.self, "docs/\(name)")
            _ = try await decode(CyclePlan.self, "plan/\(name)")
            let days = try await decode(CycleDaysResponse.self, "cycles/\(name)")
            #expect(days.days.first?.rows.count == 3)
            for row in days.days.flatMap(\.rows) {
                _ = try await decode(CycleDetail.self, "cycles/\(name)/\(row.id)")
            }
            _ = try await decode(VoicesResponse.self, "voices/\(name)")
            _ = try await decode(VoicesResponse.self, "voices/\(name)", method: "POST",
                                 json: #"{"language":"en","identifier":"demo-device-voice"}"#)
            _ = try await decode(VoicesResponse.self, "voices/\(name)", method: "POST",
                                 json: #"{"language":"en","clear":true}"#)
            _ = try await decode(SeenResponse.self, "chat/\(name)/seen", method: "POST")
            _ = try await decode(OkResponse.self, "chat/\(name)/away", method: "POST")
        }
        let doc = try await decode(DocumentDetail.self, "docs/ada/doc?path=field-notes.md")
        #expect(doc.text.contains("# Field notes"))
        _ = try await decode(PushStatus.self, "push/status")
        struct Report: Decodable { let available: [String: [VoicesResponse.AvailableVoice]] }
        _ = try await decode(Report.self, "voices/report", method: "POST", json: #"{"platform":"ios","voices":[]}"#)
        let created = try await decode(NewThreadResponse.self, "chat/ada/new", method: "POST", json: #"{"title":"A new idea"}"#)
        _ = try await decode(SaidResponse.self, "chat/ada/rename", method: "POST",
                             json: "{\"cid\":\"\(created.chat.id)\",\"title\":\"A small experiment\"}")
        let sent = try await client.send("tundra", OutgoingMessage(text: "Hello from the demo test"))
        #expect(sent.said == "sent")
        // Decode every event body supplied by the stream using the actual SSE client.
        var kinds: [ChatStreamEvent.Kind] = []
        for try await event in client.replyStream("tundra", cid: sent.chat) { kinds.append(event.type) }
        #expect(kinds == [.status, .draft, .stepEnd, .answer, .end])
        let answer = try await client.answer("wisp", ChoiceAnswerRequest(cid: "wisp-chat", file: "demo-question.md", picked: [0], other: nil))
        #expect(answer.answer.picked == [0])
        for route in ["unknown", "chat/unknown/stream"] {
            let (data, response) = try await client.session.data(from: URL(string: "https://demo.shaft.invalid/app/v1/" + route)!)
            #expect((response as? HTTPURLResponse)?.statusCode == 404)
            #expect(try SHAFTJSON.decoder().decode(ErrorBody.self, from: data).error.message == "Not available in the demo.")
        }
        let raw = try await client.rawDoc("ada", path: "field-notes.md")
        #expect(String(decoding: raw.data, as: UTF8.self) == DemoData.document)

    }

    @Test func DemoSendAppendsOperatorThenCannedReplyAndDeduplicates() async throws {
        let (session, root) = try session()
        defer { session.leaveDemo(); try? FileManager.default.removeItem(at: root) }
        let client = try #require(session.store?.client)
        let message = OutgoingMessage(text: "A message with a line break\nand a second line.", cid: "tundra-chat")
        let sent = try await client.send("tundra", message)
        #expect(try await client.send("tundra", message) == sent)
        let running = try await feed(client, "tundra")
        #expect(running.reply != nil)
        #expect(running.content?.items.count == 5)
        guard case .user(let user) = running.content?.items.last else { Issue.record("Missing operator message"); return }
        #expect(user.text == message.text)
        #expect(user.mid == message.mid)
        try await Task.sleep(for: .milliseconds(1650))
        let done = try await feed(client, "tundra")
        #expect(done.reply == nil)
        #expect(done.content?.items.count == 6)
        #expect(WatchTurn.lastWritten(in: done) == DemoData.reply)
    }

    @Test func DemoSendFromTheDefaultConversationKeepsItsFeedShape() async throws {
        let (session, root) = try session()
        defer { session.leaveDemo(); try? FileManager.default.removeItem(at: root) }
        let client = try #require(session.store?.client)
        let sent = try await client.send("tundra", OutgoingMessage(text: "Hello from Main", cid: "from"))
        #expect(sent.chat == "from")
        guard case .value(let running, _) = try await client.feed("tundra", ref: .from) else {
            Issue.record("Missing running From feed"); return
        }
        #expect(running.content?.chat == nil)
        #expect(running.reply != nil)
        for try await _ in client.replyStream("tundra", cid: "from") {}
        guard case .value(let done, _) = try await client.feed("tundra", ref: .from) else {
            Issue.record("Missing finished From feed"); return
        }
        #expect(done.content?.chat == nil)
        #expect(done.reply == nil)
        #expect(WatchTurn.lastWritten(in: done) == DemoData.reply)
    }

    @Test func DemoChoiceAnswerClosesCardAndRecordsAnswer() async throws {
        let (session, root) = try session()
        defer { session.leaveDemo(); try? FileManager.default.removeItem(at: root) }
        let client = try #require(session.store?.client)
        #expect(FeedRows.waitingQuestions(try await feed(client, "wisp")).first?.choices?.options.map(\.label)
                == ["Bullet points", "Short paragraph", "Table"])
        let request = ChoiceAnswerRequest(cid: "wisp-chat", file: "demo-question.md", picked: [2], other: nil)
        let answer = try await client.answer("wisp", request)
        #expect(answer.answer.picked == [2])
        #expect(try await client.answer("wisp", request) == answer)
        for cid in ["wisp-chat", "from"] {
            let result: Feed
            if cid == "from" {
                guard case .value(let from, _) = try await client.feed("wisp", ref: .from) else { Issue.record("Missing From"); return }
                result = from
            } else { result = try await feed(client, "wisp", cid: cid) }
            #expect(FeedRows.waitingQuestions(result).isEmpty)
            guard case .sent(let question) = result.content?.items.first else { Issue.record("Missing question"); return }
            #expect(question.answer?.picked == [2])
            #expect(!question.open)
        }
    }

    @Test func DemoUnknownRouteReturnsServerError() async throws {
        let (session, root) = try session()
        defer { session.leaveDemo(); try? FileManager.default.removeItem(at: root) }
        let client = try #require(session.store?.client)
        let (data, response) = try await client.session.data(from: URL(string: "https://demo.shaft.invalid/app/v1/unknown")!)
        #expect((response as? HTTPURLResponse)?.statusCode == 404)
        let error = try SHAFTJSON.decoder().decode(ErrorBody.self, from: data)
        #expect(error.error.message == "Not available in the demo.")
    }

    @Test func DemoSessionBlocksOtherOrigins() async throws {
        let (session, root) = try session()
        defer { session.leaveDemo(); try? FileManager.default.removeItem(at: root) }
        let client = try #require(session.store?.client)
        for address in ["https://example.invalid/app/v1/instances", "http://demo.shaft.invalid/app/v1/instances",
                        "https://demo.shaft.invalid:8444/app/v1/instances"] {
            do {
                _ = try await client.session.data(from: URL(string: address)!)
                Issue.record("A non-demo origin was allowed")
            } catch let error as URLError { #expect(error.code == .notConnectedToInternet) }
        }
    }

    @Test func DemoWatchTurnUsesTheSameOfflineStream() async throws {
        let (session, root) = try session()
        defer { session.leaveDemo(); try? FileManager.default.removeItem(at: root) }
        let client = try #require(session.store?.client)
        let mid = OutgoingMessage.newMid()
        let sent = try await WatchTurn.send("A spoken sample", mid: mid, to: "ada", client: client)
        let reply = try await WatchTurn.reply(to: mid, in: "ada", chat: sent.chat, client: client, fallback: .en)
        #expect(reply?.text == DemoData.reply)
        #expect(reply?.failure == nil)
    }

    @Test func DemoRestartUsesFreshStateAndStorage() async throws {
        let (session, root) = try session()
        defer { session.leaveDemo(); try? FileManager.default.removeItem(at: root) }
        let directory = session.demoDirectory
        let client = try #require(session.store?.client)
        _ = try await client.answer("wisp", ChoiceAnswerRequest(cid: "wisp-chat", file: "demo-question.md", picked: [1], other: nil))
        session.leaveDemo()
        session.startDemo()
        #expect(session.demoDirectory != directory)
        let fresh = try #require(session.store?.client)
        #expect(FeedRows.waitingQuestions(try await feed(fresh, "wisp")).count == 1)
        #expect(session.store?.outbox.entries.isEmpty == true)
    }
    @Test func DemoLeaveDuringSendDoesNotRecreateStorage() async throws {
        let (session, root) = try session()
        defer { session.leaveDemo(); try? FileManager.default.removeItem(at: root) }
        let directory = try #require(session.demoDirectory)
        let outbox = try #require(session.store?.outbox)
        let held = HeldDemoSend(client: try #require(session.store?.client))
        outbox.sender = held
        let send = Task { await outbox.send(OutgoingMessage(text: "Leaving now"), to: "tundra") }
        while !(await held.started) { await Task.yield() }
        session.leaveDemo()
        await held.release()
        _ = await send.value
        #expect(!FileManager.default.fileExists(atPath: directory.path))
    }

}

/// Hold a real client send before its upload starts, so leaving during that suspension is deterministic.
private actor HeldDemoSend: MessageSending {
    let client: APIClient
    var started = false
    private var continuation: CheckedContinuation<Void, Never>?

    init(client: APIClient) { self.client = client }

    func send(_ instance: String, _ message: OutgoingMessage) async throws -> SendResponse {
        await withCheckedContinuation {
            continuation = $0
            started = true
        }
        return try await client.send(instance, message)
    }

    func release() { continuation?.resume(); continuation = nil }
}
