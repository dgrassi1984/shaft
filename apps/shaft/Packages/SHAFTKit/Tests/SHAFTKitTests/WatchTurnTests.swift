import Foundation
import Testing
@testable import SHAFTKit

/// The Apple Watch's messages to the iPhone (WatchProtocol.swift) and the iPhone's half of a watch turn
/// (WatchTurn.swift): send as a voice message, follow the reply, hand back what the watch speaks. Every reply text is
/// invented.
struct WatchTurnTests {
    // MARK: The messages

    @Test func everyRequestSurvivesTheTripAsAPropertyList() throws {
        for request in [WatchRequest.instances, .send(instance: "tundra", text: "Com'è andata?", mid: "m-1"),
                        .latest(instance: "tundra")] {
            // Through a real property-list encoding, as WatchConnectivity carries it.
            let data = try PropertyListSerialization.data(fromPropertyList: request.dictionary, format: .binary,
                                                          options: 0)
            let back = try #require(PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
            #expect(WatchRequest(back) == request)
        }
    }

    @Test func aRequestMissingWhatItNeedsIsRefused() {
        #expect(WatchRequest(["op": "send", "instance": "tundra", "text": "hi"]) == nil)       // no mid
        #expect(WatchRequest(["op": "send", "instance": "", "text": "hi", "mid": "m"]) == nil)
        #expect(WatchRequest(["op": "latest"]) == nil)
        #expect(WatchRequest(["op": "dance"]) == nil)
        #expect(WatchRequest([:]) == nil)
    }

    @Test func anAnswerAndAReplyKeepEveryField() throws {
        let reply = WatchReplyPayload(instance: "tundra", mid: "m-1", text: "**Done.** 214 accounts.",
                                      spoken: ["Done.", "214 accounts."], language: "en-US",
                                      voice: "com.example.voice.ava", failure: "cut short")
        let answer = WatchAnswer(instances: ["tundra", "wren"], said: "sent", reply: reply, error: "offline")
        let data = try PropertyListSerialization.data(fromPropertyList: answer.dictionary, format: .binary, options: 0)
        let back = try #require(PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
        #expect(WatchAnswer(back) == answer)
        #expect(WatchReplyPayload(reply.dictionary) == reply)
        #expect(WatchReplyPayload(["instance": "tundra", "text": "x", "language": "en-US"]) == nil,
                "a dictionary that is not a reply (no kind) is not taken for one")
    }

    // MARK: The turn

    static let reply = "5b1e7c9d2a3f4e60"

    static func stream(_ answer: String, failure: String? = nil) -> String {
        let failed = failure.map { "\"\($0)\"" } ?? "null"
        return """
            : reply \(reply)

            id: 1
            data: {"seq": 1, "type": "draft", "reply": "\(reply)", "text": "Let me check."}

            id: 2
            data: {"seq": 2, "type": "answer", "reply": "\(reply)", "text": "\(answer)"}

            id: 3
            data: {"seq": 3, "type": "end", "reply": "\(reply)", "failure": \(failed)}


            """
    }

    static let notReplying = StubServer.Response.json(409, #"{"error": {"code": "not_replying", "message": "no reply running"}}"#)

    @Test func theSendIsAVoiceMessageInTheCurrentChatWithTheWatchesMid() async throws {
        let server = StubServer { _ in .json(200, #"{"said": "sent to tundra", "chat": "web-1", "file": null}"#) }
        let answer = try await WatchTurn.send("How did October close?", mid: "watch-7", to: "tundra",
                                              client: server.client)
        #expect(answer.said == "sent to tundra")
        #expect(answer.chat == "web-1")
        let request = try #require(server.requests.first)
        #expect(request.path == "/app/v1/chat/tundra/send")
        let body = String(decoding: request.body, as: UTF8.self)
        for field in ["name=\"voice\"\r\n\r\n1", "name=\"mid\"\r\n\r\nwatch-7", "name=\"mode\"\r\n\r\nnow",
                      "name=\"text\"\r\n\r\nHow did October close?"] {
            #expect(body.contains(field), "the send carries \(field.debugDescription)")
        }
        #expect(!body.contains("name=\"cid\""), "no cid: the instance's current chat, as the plan says")
    }

    @Test func theReplyIsFollowedToItsEndAndSpokenInItsOwnLanguageWithTheInstancesVoice() async throws {
        let server = StubServer { request in
            switch request.path {
            case "/app/v1/chat/tundra/stream": .events(Self.stream("**Fatto.** Ottobre si è chiuso con 214 conti riconciliati, nessun errore."))
            case "/app/v1/voices/tundra": .json(200, #"{"voices": {"en": null, "it": {"identifier": "com.example.voice.alice", "name": "Alice", "by": "operator", "ts": "2026-11-09T10:15:00+01:00"}}, "proposals": [], "available": {}}"#)
            default: .json(404, #"{"error": {"code": "not_found", "message": "no"}}"#)
            }
        }
        let payload = try #require(try await WatchTurn.reply(to: "watch-7", in: "tundra", chat: "web-1",
                                                             client: server.client, fallback: .en))
        #expect(payload.mid == "watch-7")
        #expect(payload.instance == "tundra")
        #expect(payload.text == "**Fatto.** Ottobre si è chiuso con 214 conti riconciliati, nessun errore.")
        #expect(payload.language == "it-IT", "Italian words: spoken in Italian although the fallback is English")
        #expect(payload.voice == "com.example.voice.alice")
        #expect(!payload.spoken.isEmpty)
        #expect(!payload.spoken.joined().contains("**"), "the markup is not spoken")
        #expect(payload.failure == nil)
        let stream = try #require(server.requests.first { $0.path == "/app/v1/chat/tundra/stream" })
        #expect(stream.query("cid") == "web-1", "the reply of the chat the send answered with")
        #expect(stream.query("watch") == "0", "following for the watch must not mute the iPhone's push for it")
    }

    @Test func aFailedReplySaysWhyAndAMissingVoiceLeavesTheSystemOne() async throws {
        let server = StubServer { request in
            request.path.hasSuffix("/stream")
                ? .events(Self.stream("Partial", failure: "the model stopped"))
                : .json(200, #"{"voices": {"en": null, "it": null}, "proposals": [], "available": {}}"#)
        }
        let payload = try #require(try await WatchTurn.reply(to: "m", in: "tundra", chat: nil, client: server.client,
                                                             fallback: .en))
        #expect(payload.failure == "the model stopped")
        #expect(payload.voice == nil)
    }

    /// A reply that ended before the iPhone began following it: the stream says `409`, and the watch waited for good
    /// (review, 2026-11-14). The send starts the reply before it answers, so the reply is in the chat.
    @Test func aReplyThatEndedBeforeTheFollowIsReadFromTheChat_20261114() async throws {
        let feed = String(decoding: try DecodingTests.fixture("idle"), as: UTF8.self)
        let server = StubServer { request in
            switch request.path {
            case "/app/v1/chat/tundra/stream": Self.notReplying
            case "/app/v1/chat/tundra/web-1": .json(200, feed)
            default: .json(200, #"{"voices": {}, "proposals": [], "available": {}}"#)
            }
        }
        let payload = try #require(try await WatchTurn.reply(to: "m-9", in: "tundra", chat: "web-1",
                                                             client: server.client, fallback: .en))
        #expect(payload.text == "October is done: 214 accounts.")
        #expect(payload.mid == "m-9", "it answers the watch's turn")
    }

    @Test func aChatWithNoReplyAtAllIsNil() async throws {
        let server = StubServer { request in
            request.path.hasSuffix("/stream") ? Self.notReplying
                : .json(200, String(decoding: try! DecodingTests.fixture("idle"), as: UTF8.self)
                    .replacingOccurrences(of: "\"type\": \"assistant\"", with: "\"type\": \"retired\""))
        }
        let payload = try await WatchTurn.reply(to: "m", in: "tundra", chat: nil, client: server.client, fallback: .en)
        #expect(payload == nil)
    }

    @Test func aVoiceLookupThatHangsDoesNotHoldTheReply() async throws {
        let server = StubServer { request in
            if request.path.hasSuffix("/stream") { return .events(Self.stream("Done.")) }
            var slow = StubServer.Response.json(200, #"{"voices": {}, "proposals": [], "available": {}}"#)
            slow.beforeReply = { try? await Task.sleep(for: .seconds(6)) }    // longer than the lookup may take
            return slow
        }
        let start = Date()
        let payload = try #require(try await WatchTurn.reply(to: "m", in: "tundra", chat: nil, client: server.client,
                                                             fallback: .en))
        #expect(payload.voice == nil)
        #expect(Date().timeIntervalSince(start) < 5, "the reply went out after the 2 s lookup limit")
    }

    @Test func latestFollowsAReplyStillRunning() async throws {
        let server = StubServer { request in
            request.path.hasSuffix("/stream")
                ? .events(Self.stream("Still on it: 3 of 5 done."))
                : .json(200, #"{"voices": {}, "proposals": [], "available": {}}"#)
        }
        let payload = try #require(try await WatchTurn.latest("tundra", client: server.client, fallback: .en))
        #expect(payload.text == "Still on it: 3 of 5 done.")
        #expect(payload.mid == nil)
        #expect(!server.requests.contains { $0.path == "/app/v1/chat/tundra" }, "no feed read when a reply runs")
    }

    @Test func latestWithNothingRunningIsTheLastMessageTheInstanceWrote() async throws {
        let feed = String(decoding: try DecodingTests.fixture("idle"), as: UTF8.self)
        let server = StubServer { request in
            switch request.path {
            case "/app/v1/chat/tundra/stream": Self.notReplying
            case "/app/v1/chat/tundra": .json(200, feed)
            default: .json(200, #"{"voices": {}, "proposals": [], "available": {}}"#)
            }
        }
        let payload = try #require(try await WatchTurn.latest("tundra", client: server.client, fallback: .en))
        #expect(payload.text == "October is done: 214 accounts.")
        let read = try #require(server.requests.first { $0.path == "/app/v1/chat/tundra" })
        #expect(read.query("watch") == "0", "the watch's read must not mute the iPhone's notifications for the chat")
    }
}
