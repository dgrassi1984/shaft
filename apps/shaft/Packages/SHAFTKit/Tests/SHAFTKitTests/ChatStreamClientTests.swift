import Foundation
import Testing
@testable import SHAFTKit

/// A flag two tasks share.
final class Flag: @unchecked Sendable {
    private let lock = NSLock()
    private var set = false

    var value: Bool {
        get { lock.withLock { set } }
        set { lock.withLock { set = newValue } }
    }
}

/// The reply stream (docs/app-api.md §6.1) as `replyStream` and `followReply` read it, the SSE parser on reads split
/// anywhere, the send's `voice` field (§6) and the voice routes (§17). Every reply text is invented.
struct ChatStreamClientTests {
    static let reply = "3f0c9e21a4b7d658"

    /// §6.1's example stream, exactly as `app_api.sse` writes it: the reply comment, nine events, one keepalive.
    static let example = """
        : reply 3f0c9e21a4b7d658

        id: 1
        data: {"seq": 1, "type": "status", "reply": "3f0c9e21a4b7d658", "phase": "model", "detail": "thinking"}

        id: 2
        data: {"seq": 2, "type": "draft", "reply": "3f0c9e21a4b7d658", "text": "Let me look."}

        id: 3
        data: {"seq": 3, "type": "step_end", "reply": "3f0c9e21a4b7d658", "answer": false}

        id: 4
        data: {"seq": 4, "type": "tool_start", "reply": "3f0c9e21a4b7d658", "name": "read_file"}

        : keepalive

        id: 5
        data: {"seq": 5, "type": "tool_end", "reply": "3f0c9e21a4b7d658", "name": "read_file", "ok": true}

        id: 6
        data: {"seq": 6, "type": "draft", "reply": "3f0c9e21a4b7d658", "text": "It is sunny in Milan."}

        id: 7
        data: {"seq": 7, "type": "step_end", "reply": "3f0c9e21a4b7d658", "answer": true}

        id: 8
        data: {"seq": 8, "type": "answer", "reply": "3f0c9e21a4b7d658", "text": "It is sunny in Milan."}

        id: 9
        data: {"seq": 9, "type": "end", "reply": "3f0c9e21a4b7d658", "failure": null}


        """

    /// One event as the server writes it.
    static func sse(_ seq: Int, _ type: String, reply: String = reply, _ payload: String = "") -> String {
        let fields = payload.isEmpty ? "" : ", " + payload
        return "id: \(seq)\ndata: {\"seq\": \(seq), \"type\": \"\(type)\", \"reply\": \"\(reply)\"\(fields)}\n\n"
    }

    static func draft(_ seq: Int, _ text: String, reply: String = reply) -> String {
        sse(seq, "draft", reply: reply, "\"text\": \"\(text)\"")
    }

    static func end(_ seq: Int, reply: String = reply) -> String {
        sse(seq, "end", reply: reply, "\"failure\": null")
    }

    /// `seenBy`: the server that drops a connection once the test has the events sent before the drop.
    static func collect(_ stream: AsyncThrowingStream<ChatStreamEvent, Error>,
                        seenBy server: StubServer? = nil) async throws -> [ChatStreamEvent] {
        var events = [ChatStreamEvent]()
        for try await event in stream { events.append(event); server?.saw(event.seq) }
        return events
    }

    /// Run `stream` to its end; what it yielded and the error it ended with, if any.
    static func drain(_ stream: AsyncThrowingStream<ChatStreamEvent, Error>,
                      seenBy server: StubServer? = nil) async -> ([ChatStreamEvent], APIError?) {
        var events = [ChatStreamEvent]()
        do {
            for try await event in stream { events.append(event); server?.saw(event.seq) }
            return (events, nil)
        } catch {
            return (events, error as? APIError ?? .decoding(path: "test", message: "not an APIError: \(error)"))
        }
    }

    // MARK: replyStream

    @Test func theStreamReplaysEveryEventInOrderSkippingCommentsAndKeepalives() async throws {
        let server = StubServer { _ in .events(Self.example) }
        let events = try await Self.collect(server.client.replyStream("thelos"))
        #expect(events.map(\.seq) == Array(1...9))
        #expect(events.map(\.type) == [.status, .draft, .stepEnd, .toolStart, .toolEnd, .draft, .stepEnd, .answer, .end])
        #expect(events.allSatisfy { $0.reply == Self.reply })
        #expect(events[0].detail == "thinking")
        #expect(events[3].name == "read_file")
        #expect(events[4].ok == true)
        #expect(events[7].text == "It is sunny in Milan.")
        #expect(events[8].failure == nil)

        let request = try #require(server.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/app/v1/chat/thelos/stream")
        #expect(request.query("after") == "0")
        #expect(request.query("reply") == nil)
        #expect(request.header("Accept") == "text/event-stream")
        #expect(request.header("Authorization") == "Bearer 8c1f4e2a9b07d3e5.s3cret")
        // An idle timeout (reset by every byte): the 2 s keepalives keep a long reply open, a dead server is noticed.
        #expect(request.timeout == APIClient.streamIdleTimeout)
        #expect(APIClient.streamIdleTimeout >= 10 && APIClient.streamIdleTimeout <= 30)
    }

    @Test func theStreamSendsTheAfterAndReplyItHolds() async throws {
        let server = StubServer { _ in .events(": reply \(Self.reply)\n\n" + Self.draft(6, "More.") + Self.end(7)) }
        let events = try await Self.collect(server.client.replyStream("thelos", after: 5, reply: Self.reply))
        #expect(events.map(\.seq) == [6, 7])
        let request = try #require(server.requests.first)
        #expect(request.query("after") == "5")
        #expect(request.query("reply") == Self.reply)
    }

    @Test func theStreamFinishesAtEndAndReadsNothingAfterIt() async throws {
        let server = StubServer { _ in
            .events(Self.draft(1, "Done.") + Self.end(2) + Self.draft(3, "Never yielded."))
        }
        let events = try await Self.collect(server.client.replyStream("thelos"))
        #expect(events.map(\.seq) == [1, 2])
        #expect(events.last?.type == .end)
    }

    @Test func notReplyingFinishesEmptyWithoutAnError() async throws {
        let server = StubServer { _ in
            .json(409, #"{"error": {"code": "not_replying", "message": "thelos is not replying: nothing to stream."}}"#)
        }
        let events = try await Self.collect(server.client.replyStream("thelos"))
        #expect(events.isEmpty)
        #expect(server.requests.count == 1)
    }

    @Test func anotherRefusalIsTheServersAPIError() async throws {
        let server = StubServer { _ in
            .json(409, #"{"error": {"code": "refused", "message": "the chat ended"}}"#)
        }
        let (events, error) = await Self.drain(server.client.replyStream("thelos"))
        #expect(events.isEmpty)
        #expect(error == .server(status: 409, error: ServerError(code: "refused", message: "the chat ended")))
    }

    @Test func unpairedAndAnHTMLErrorMapAsTheOtherRoutesMapThem() async throws {
        let unpaired = StubServer { _ in .json(401, #"{"error": {"code": "unpaired", "message": "Pair this device again."}}"#) }
        let (_, first) = await Self.drain(unpaired.client.replyStream("thelos"))
        #expect(first?.isUnpaired == true)

        let proxy = StubServer { _ in
            StubServer.Response(status: 502, headers: ["Content-Type": "text/html"], body: Data("<html>Bad Gateway</html>".utf8))
        }
        let (_, second) = await Self.drain(proxy.client.replyStream("thelos"))
        #expect(second == .http(status: 502, body: "<html>Bad Gateway</html>"))
    }

    @Test func aDataLineThatIsNotAnEventIsADecodingError() async throws {
        let server = StubServer { _ in .events(Self.draft(1, "Fine.") + "id: 2\ndata: {\"seq\": 2, \"type\": \"draft\"}\n\n") }
        let (events, error) = await Self.drain(server.client.replyStream("thelos"))
        #expect(events.map(\.seq) == [1])
        guard case .decoding(let path, _) = error else { Issue.record("not a decoding error: \(String(describing: error))"); return }
        #expect(path == "/app/v1/chat/thelos/stream")
    }

    /// A newer server's event type must not drop the stream on an older app: it is skipped and the reply goes on.
    @Test func anEventTypeThisAppDoesNotKnowIsSkippedAndTheReplyGoesOn() async throws {
        let unknown = #"id: 2\#ndata: {"seq": 2, "type": "mood", "reply": "\#(Self.reply)", "text": "calm"}\#n\#n"#
        let server = StubServer { _ in .events(Self.draft(1, "Fine.") + unknown + Self.end(3)) }
        let (events, error) = await Self.drain(server.client.replyStream("thelos"))
        #expect(error == nil)
        #expect(events.map(\.seq) == [1, 3])
    }

    @Test func aStreamClosedBeforeEndIsADroppedConnectionNotASilentFinish() async throws {
        let server = StubServer { _ in .events(Self.draft(1, "Half") + Self.draft(2, " a sentence")) }
        let (events, error) = await Self.drain(server.client.replyStream("thelos"))
        #expect(events.map(\.seq) == [1, 2])
        guard case .transport(let code, _) = error else { Issue.record("not a transport error: \(String(describing: error))"); return }
        #expect(code == URLError.networkConnectionLost.rawValue)
        #expect(error?.isRetryable == true)
    }

    @Test func aDroppedConnectionIsATransportError() async throws {
        let server = StubServer { _ in .events(Self.draft(1, "Half"), pieces: [Self.draft(2, " more")], dropsAtEnd: true) }
        let (events, error) = await Self.drain(server.client.replyStream("thelos"), seenBy: server)
        #expect(events.map(\.seq) == [1, 2])
        guard case .transport = error else { Issue.record("not a transport error: \(String(describing: error))"); return }
    }

    @Test func eventsArriveAsTheyComeNotWhenTheBodyEnds() async throws {
        // The rest of the body is held until the test has the first event. A client that waited for the whole body
        // would get nothing until the hold gave up after 5 s: `releasedByTest` would be false.
        let gotFirst = Flag(), releasedByTest = Flag()
        let server = StubServer { _ in
            .events(": reply \(Self.reply)\n\n" + Self.draft(1, "First."),
                    pieces: [": keepalive\n\n" + Self.draft(2, "Second.") + Self.end(3)]) { _ in
                for _ in 0..<500 {
                    if gotFirst.value { break }
                    try? await Task.sleep(for: .milliseconds(10))
                }
                releasedByTest.value = gotFirst.value
            }
        }
        var events = [ChatStreamEvent]()
        for try await event in server.client.replyStream("thelos") {
            if event.seq == 1 { gotFirst.value = true }
            events.append(event)
        }
        #expect(releasedByTest.value)
        #expect(events.map(\.seq) == [1, 2, 3])
    }

    @Test func anEventSplitAcrossReadsArrivesWhole() async throws {
        let whole = Self.example
        let bytes = Array(whole.utf8)
        // Cut inside the `data:` JSON, inside `id:`, and between the two LFs of a blank line.
        let cuts = [37, 101, 102, 250, 251, 252, bytes.count - 1]
        var pieces = [String]()
        var start = 40
        for cut in cuts where cut > start {
            pieces.append(String(decoding: bytes[start..<cut], as: UTF8.self))
            start = cut
        }
        pieces.append(String(decoding: bytes[start...], as: UTF8.self))
        let reads = pieces
        let server = StubServer { _ in .events(String(decoding: bytes[..<40], as: UTF8.self), pieces: reads) }
        let events = try await Self.collect(server.client.replyStream("thelos"))
        #expect(events.map(\.seq) == Array(1...9))
    }

    // MARK: followReply

    @Test func followReplyResumesWithAfterAndReplyAndNeverYieldsASeqTwice() async throws {
        let server = StubServer { request in
            switch request.query("after") {
            case "0":
                return .events(": reply \(Self.reply)\n\n" + Self.draft(1, "One.") + Self.draft(2, " Two."),
                               pieces: [Self.draft(3, " Three.")], dropsAtEnd: true)
            default:
                // The server replays from 2 again: an overlap the client must drop.
                return .events(": reply \(Self.reply)\n\n" + Self.draft(2, " Two.") + Self.draft(3, " Three.")
                               + Self.draft(4, " Four.") + Self.end(5))
            }
        }
        let started = ContinuousClock.now
        let events = try await Self.collect(server.client.followReply("thelos"), seenBy: server)
        let waited = ContinuousClock.now - started
        #expect(events.map(\.seq) == [1, 2, 3, 4, 5])
        #expect(events.map(\.text) == ["One.", " Two.", " Three.", " Four.", nil])
        let requests = server.requests
        #expect(requests.count == 2)
        #expect(requests[0].query("after") == "0")
        #expect(requests[0].query("reply") == nil)
        #expect(requests[1].query("after") == "3")
        #expect(requests[1].query("reply") == Self.reply)
        #expect(waited >= .milliseconds(500))
    }

    @Test func followReplyReconnectsWhenTheStreamClosesBeforeEnd() async throws {
        let server = StubServer { request in
            request.query("after") == "0"
                ? .events(Self.draft(1, "Cut"))
                : .events(Self.draft(2, " short.") + Self.end(3))
        }
        let events = try await Self.collect(server.client.followReply("thelos", after: 0, reply: nil, reconnects: 3,
                                                                      delay: .milliseconds(10)))
        #expect(events.map(\.seq) == [1, 2, 3])
        #expect(server.requests.map { $0.query("after") } == ["0", "1"])
    }

    @Test func aNewReplyIdOnReconnectResetsTheSeqItHolds() async throws {
        let newer = "9a8b7c6d5e4f3a2b"
        let server = StubServer { request in
            request.query("after") == "0"
                ? .events(Self.draft(1, "Old one.") + Self.draft(2, " Still old."), dropsAtEnd: true)
                // The held reply is gone and a newer one began: it comes whole, from seq 1.
                : .events(": reply \(newer)\n\n" + Self.draft(1, "New one.", reply: newer) + Self.end(2, reply: newer))
        }
        let events = try await Self.collect(server.client.followReply("thelos", after: 0, reply: nil, reconnects: 3,
                                                                      delay: .milliseconds(10)), seenBy: server)
        #expect(events.map(\.reply) == [Self.reply, Self.reply, newer, newer])
        #expect(events.map(\.seq) == [1, 2, 1, 2])
        #expect(server.requests.last?.query("reply") == Self.reply)
        #expect(server.requests.last?.query("after") == "2")
    }

    @Test func followReplyContinuesAnInterjectionFromTheReplyItHolds() async throws {
        let server = StubServer { _ in .events(Self.draft(4, "Overlap.") + Self.draft(5, "New.") + Self.end(6)) }
        let events = try await Self.collect(server.client.followReply("thelos", after: 4, reply: Self.reply))
        #expect(events.map(\.seq) == [5, 6])
        #expect(server.requests.first?.query("after") == "4")
        #expect(server.requests.first?.query("reply") == Self.reply)
    }

    @Test func followReplyEndsQuietlyOnNotReplyingAfterADrop() async throws {
        let server = StubServer { request in
            request.query("after") == "0"
                ? .events(Self.draft(1, "Some."), dropsAtEnd: true)
                : .json(409, #"{"error": {"code": "not_replying", "message": "thelos is not replying: nothing to stream."}}"#)
        }
        let events = try await Self.collect(server.client.followReply("thelos", after: 0, reply: nil, reconnects: 3,
                                                                      delay: .milliseconds(10)), seenBy: server)
        #expect(events.map(\.seq) == [1])
        #expect(server.requests.count == 2)
    }

    @Test func followReplyGivesUpAfterThreeReconnectsWithTheNetworksError() async throws {
        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let (events, error) = await Self.drain(server.client.followReply("thelos", after: 0, reply: nil, reconnects: 3,
                                                                         delay: .milliseconds(10)))
        #expect(events.isEmpty)
        guard case .transport = error else { Issue.record("not a transport error: \(String(describing: error))"); return }
        #expect(server.requests.count == 4)
    }

    @Test func aConnectionThatBroughtEventsStartsTheReconnectCountAgain() async throws {
        // One reconnect allowed in a row, and the stream drops twice: each drop came after new events, so both
        // reconnect.
        let server = StubServer { request in
            switch request.query("after") {
            case "0": .events(Self.draft(1, "A."), dropsAtEnd: true)
            case "1": .events(Self.draft(2, "B."), dropsAtEnd: true)
            default: .events(Self.draft(3, "C.") + Self.end(4))
            }
        }
        let events = try await Self.collect(server.client.followReply("thelos", after: 0, reply: nil, reconnects: 1,
                                                                      delay: .milliseconds(10)), seenBy: server)
        #expect(events.map(\.seq) == [1, 2, 3, 4])
        #expect(server.requests.count == 3)
    }

    @Test func followReplyThrowsARefusalAtOnce() async throws {
        let server = StubServer { _ in .json(401, #"{"error": {"code": "unpaired", "message": "Pair this device again."}}"#) }
        let (_, error) = await Self.drain(server.client.followReply("thelos"))
        #expect(error?.isUnpaired == true)
        #expect(server.requests.count == 1)
    }

    // MARK: The SSE parser on reads split anywhere

    /// A seeded generator, so a failing split can be replayed.
    struct SplitMix64: RandomNumberGenerator {
        var state: UInt64
        mutating func next() -> UInt64 {
            state &+= 0x9E37_79B9_7F4A_7C15
            var z = state
            z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
            z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
            return z ^ (z >> 31)
        }
    }

    static func parse(_ reads: [[UInt8]]) -> [ServerSentEvents.Message] {
        var parser = ServerSentEvents()
        return reads.flatMap { parser.consume($0) }
    }

    static func randomReads(_ bytes: [UInt8], seed: UInt64) -> [[UInt8]] {
        var generator = SplitMix64(state: seed)
        var reads = [[UInt8]]()
        var start = 0
        while start < bytes.count {
            let size = Int.random(in: 1...17, using: &generator)
            reads.append(Array(bytes[start..<min(start + size, bytes.count)]))
            start += size
        }
        return reads
    }

    @Test func theParserGivesTheSameEventsWhateverTheReadsAre() throws {
        let italian = Self.draft(1, "È così, perché il caffè costa 3,50 € — più di ieri…")
        for text in [Self.example, italian] {
            let bytes = Array(text.utf8)
            let whole = Self.parse([bytes])
            #expect(!whole.isEmpty)
            #expect(Self.parse(bytes.map { [$0] }) == whole)
            for cut in 1..<bytes.count {
                #expect(Self.parse([Array(bytes[..<cut]), Array(bytes[cut...])]) == whole, "cut at \(cut)")
            }
            for seed in UInt64(1)...8 {
                #expect(Self.parse(Self.randomReads(bytes, seed: seed)) == whole, "seed \(seed)")
            }
        }
        let decoded = try SHAFTJSON.decoder().decode(ChatStreamEvent.self,
                                                     from: Data(Self.parse([Array(italian.utf8)])[0].data.utf8))
        #expect(decoded.text == "È così, perché il caffè costa 3,50 € — più di ieri…")
    }

    @Test func theExampleParsesToNineEventsWithTheirIds() {
        let messages = Self.parse([Array(Self.example.utf8)])
        #expect(messages.map(\.id) == (1...9).map(String.init))
        #expect(messages.allSatisfy { $0.event == "message" })
        #expect(messages.first?.data.hasPrefix(#"{"seq": 1, "type": "status""#) == true)
    }

    @Test func aCRLFSplitBetweenReadsIsOneLineEnd() {
        // Read as two line ends, the CR and the LF would make a blank line that dispatches "a" alone.
        let reads: [[UInt8]] = [Array("data: a\r".utf8), Array("\ndata: b\r\n\r\n".utf8)]
        #expect(Self.parse(reads) == [ServerSentEvents.Message(id: nil, event: "message", data: "a\nb")])
        #expect(Self.parse([Array("data: c\r\rdata: d\n\n".utf8)]).map(\.data) == ["c", "d"])
    }

    @Test func theParserFollowsTheEventStreamRules() {
        let text = "\u{FEFF}: a comment\n\n"           // a byte order mark, then a comment block: nothing
            + "id: 7\ndata:no space\ndata:  two spaces\n\n"
            + "data: keeps id 7\n\n"
            + "event: other\ndata: named\n\n"
            + "event:\ndata: empty event name\n\n"
            + "id: 8\n\n"                               // an id with no data: nothing dispatched, the id kept
            + "data\n\n"                                // a field with no colon: an empty data line
            + "retry: 3000\nunknown: x\ndata: last\n\n"
            + "data: never finished\n"                  // the stream closes before its blank line
        let messages = Self.parse([Array(text.utf8)])
        #expect(messages == [
            .init(id: "7", event: "message", data: "no space\n two spaces"),
            .init(id: "7", event: "message", data: "keeps id 7"),
            .init(id: "7", event: "other", data: "named"),
            .init(id: "7", event: "message", data: "empty event name"),
            .init(id: "8", event: "message", data: ""),
            .init(id: "8", event: "message", data: "last"),
        ])
    }

    @Test func anEventNamedOtherThanMessageIsNotDecoded() async throws {
        let server = StubServer { _ in .events("event: debug\ndata: not json\n\n" + Self.draft(1, "Hi.") + Self.end(2)) }
        let events = try await Self.collect(server.client.replyStream("thelos"))
        #expect(events.map(\.seq) == [1, 2])
    }

    // MARK: The send's voice field (§6)

    @Test func aVoiceSendCarriesVoice1AndATypedOneNoVoiceField() async throws {
        let server = StubServer { _ in .json(200, #"{"said": "sent to thelos", "chat": "web-1", "file": null}"#) }
        _ = try await server.client.send("thelos", OutgoingMessage(mid: "m-voice", text: "What is the weather in Milan?",
                                                                    voice: true))
        _ = try await server.client.send("thelos", OutgoingMessage(mid: "m-typed", text: "What is the weather in Milan?"))
        let requests = server.requests
        let spoken = try ParsedMultipart(body: requests[0].body, contentType: try #require(requests[0].header("Content-Type")))
        #expect(spoken.field("voice") == "1")
        #expect(spoken.field("mode") == "now")
        #expect(spoken.field("text") == "What is the weather in Milan?")
        let typed = try ParsedMultipart(body: requests[1].body, contentType: try #require(requests[1].header("Content-Type")))
        #expect(typed.field("voice") == nil)
    }

    @Test func aMessageStoredBeforeVoiceExistedDecodesAsTyped() throws {
        // Written by the app before `voice`: snake_case keys, no `voice`.
        let old = #"{"files":[],"mid":"m-old","mode":"queue","quotes":[{"answer":"Yes","doc":"","quote":"Keep it?"}],"retry_of":2,"text":"Rerun the October report."}"#
        let message = try SHAFTJSON.decoder().decode(OutgoingMessage.self, from: Data(old.utf8))
        #expect(message.voice == false)
        #expect(message.mid == "m-old")
        #expect(message.mode == .queue)
        #expect(message.retryOf == 2)
        #expect(message.quotes.first?.quote == "Keep it?")
        #expect(message.text == "Rerun the October report.")

        let spoken = OutgoingMessage(mid: "m-new", text: "Tell me more.", voice: true)
        let restored = try SHAFTJSON.decoder().decode(OutgoingMessage.self, from: SHAFTJSON.encoder().encode(spoken))
        #expect(restored == spoken)
    }

    @MainActor
    @Test func anOutboxWrittenBeforeVoiceExistedStillLoads() throws {
        let folder = try temporaryFolder("outbox-old")
        defer { try? FileManager.default.removeItem(at: folder) }
        let old = #"[{"created":"2026-11-03T14:02:11+01:00","files":[],"instance":"thelos","mid":"m-old","mode":"now","quotes":[],"reason":"The Internet connection appears to be offline.","state":"notSent","text":"Rerun the October report."}]"#
        try Data(old.utf8).write(to: folder.appending(path: "outbox.json"))
        let outbox = try Outbox(directory: folder)
        let entry = try #require(outbox.entries(for: "thelos").first)
        #expect(entry.mid == "m-old")
        #expect(entry.state == .notSent)
        #expect(entry.text == "Rerun the October report.")
    }

    // MARK: Voices (§17)

    static let voicesBody = #"""
        {"voices": {"en": {"identifier": "com.example.voice.ava", "name": "Ava", "by": "operator",
          "ts": "2026-11-09T10:15:00+01:00"}, "it": null},
         "proposals": [{"id": "vp-1", "language": "it", "identifier": "com.example.voice.alice", "name": "Alice",
          "why": "A calm voice for Italian.", "ts": "2026-11-09T10:20:00+01:00", "state": "open"}],
         "available": {"macos": [{"identifier": "com.example.voice.ava", "name": "Ava", "language": "en-US",
          "quality": "premium"}]}}
        """#

    @Test func voicesReadsTheInstancesVoices() async throws {
        let server = StubServer { _ in .json(200, Self.voicesBody) }
        let answer = try await server.client.voices("thelos")
        #expect(answer.voices["en"]??.name == "Ava")
        #expect(answer.voices["it"]! == nil)
        #expect(answer.proposals.map(\.id) == ["vp-1"])
        #expect(answer.available["macos"]?.first?.quality == "premium")
        let request = try #require(server.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/app/v1/voices/thelos")
        #expect(request.body.isEmpty)
    }

    @Test func setAndClearSendExactlyTheKeysTheServerTakes() async throws {
        let server = StubServer { _ in .json(200, Self.voicesBody) }
        _ = try await server.client.setVoice("thelos", language: "en", identifier: "com.example.voice.ava")
        _ = try await server.client.clearVoice("thelos", language: "it")
        let requests = server.requests
        #expect(requests.map(\.method) == ["POST", "POST"])
        #expect(requests.map(\.path) == ["/app/v1/voices/thelos", "/app/v1/voices/thelos"])
        #expect(requests[0].header("Content-Type") == "application/json")
        let set = try #require(requests[0].json)
        #expect(Set(set.keys) == ["language", "identifier"])
        #expect(set["language"] as? String == "en")
        #expect(set["identifier"] as? String == "com.example.voice.ava")
        let clear = try #require(requests[1].json)
        #expect(Set(clear.keys) == ["language", "clear"])
        #expect(clear["clear"] as? Bool == true)
    }

    @Test func aDecisionSendsAcceptAndItsReasonOnlyWhenThereIsOne() async throws {
        let server = StubServer { _ in .json(200, Self.voicesBody) }
        _ = try await server.client.decideVoiceProposal("thelos", id: "vp-1", accept: false, reason: "Too fast.")
        _ = try await server.client.decideVoiceProposal("thelos", id: "vp-2", accept: true)
        let requests = server.requests
        #expect(requests.map(\.path) == ["/app/v1/voices/thelos/proposals/vp-1", "/app/v1/voices/thelos/proposals/vp-2"])
        let rejected = try #require(requests[0].json)
        #expect(rejected["accept"] as? Bool == false)
        #expect(rejected["reason"] as? String == "Too fast.")
        let accepted = try #require(requests[1].json)
        #expect(Set(accepted.keys) == ["accept"])
        #expect(accepted["accept"] as? Bool == true)
    }

    @Test func reportSendsThePlatformsVoicesAndReturnsWhatIsAvailable() async throws {
        let server = StubServer { _ in
            .json(200, #"{"available": {"macos": [{"identifier": "com.example.voice.ava", "name": "Ava", "language": "en-US", "quality": "premium"}], "ios": []}}"#)
        }
        let ava = VoicesResponse.AvailableVoice(identifier: "com.example.voice.ava", name: "Ava", language: "en-US",
                                                quality: "premium")
        let available = try await server.client.reportVoices(platform: "macos", voices: [ava])
        #expect(available["macos"] == [ava])
        #expect(available["ios"] == [])
        let request = try #require(server.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/app/v1/voices/report")
        let body = try #require(request.json)
        #expect(body["platform"] as? String == "macos")
        let voices = try #require(body["voices"] as? [[String: String]])
        #expect(voices == [["identifier": "com.example.voice.ava", "name": "Ava", "language": "en-US", "quality": "premium"]])
    }

    @Test func aVoiceRefusalCarriesTheServersReason() async throws {
        let server = StubServer { _ in
            .json(409, #"{"error": {"code": "refused", "message": "wren already speaks with Ava"}}"#)
        }
        let error = await #expect(throws: APIError.self) {
            _ = try await server.client.setVoice("thelos", language: "en", identifier: "com.example.voice.ava")
        }
        #expect(error == .server(status: 409, error: ServerError(code: "refused", message: "wren already speaks with Ava")))
    }
}
