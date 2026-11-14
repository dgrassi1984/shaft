import Foundation
import Testing
@testable import SHAFTKit

struct QuestionAnswerTests {
    static let answerJSON = #"{"picked":[0],"other":"Later","ts":"2026-11-03T14:02:11+01:00"}"#
    static let responseJSON = #"{"said":"answered","answer":\#(answerJSON)}"#
    static let choices = Choices(options: [ChoiceOption(label: "Now", detail: "Today"),
                                          ChoiceOption(label: "Wait", detail: nil)], multiple: false)

    private func sentObject() throws -> [String: Any] {
        let feed = try #require(JSONSerialization.jsonObject(with: DecodingTests.fixture("sent")) as? [String: Any])
        let items = try #require(feed["items"] as? [[String: Any]])
        return try #require(items.first { $0["type"] as? String == "sent" })
    }

    @Test func choicesAnswerAndOpenDecodeWithStampedDates() throws {
        var object = try sentObject()
        object["choices"] = try JSONSerialization.jsonObject(with: SHAFTJSON.encoder().encode(Self.choices))
        object["answer"] = try JSONSerialization.jsonObject(with: Data(Self.answerJSON.utf8))
        object["open"] = true
        let item = try SHAFTJSON.decoder().decode(SentItem.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(item.choices == Self.choices)
        #expect(item.answer?.picked == [0])
        #expect(item.answer?.other == "Later")
        #expect(item.answer?.ts == SHAFTJSON.parseStamp("2026-11-03T13:02:11Z"))
        #expect(item.open)
        #expect(item.answer?.summary(in: Self.choices) == "Now; Other: Later")
    }

    @Test func anOlderServerAndNullFieldsStillDecode() throws {
        var object = try sentObject()
        for key in ["choices", "answer", "open"] { object.removeValue(forKey: key) }
        let old = try SHAFTJSON.decoder().decode(SentItem.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(old.choices == nil && old.answer == nil && !old.open)
        object["choices"] = NSNull()
        object["answer"] = NSNull()
        object["open"] = false
        let current = try SHAFTJSON.decoder().decode(SentItem.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(current.choices == nil && current.answer == nil && !current.open)
    }

    @Test func answerPostsJSONToTheInstanceWithTheDisplayedCID() async throws {
        let server = StubServer { _ in .json(200, Self.responseJSON) }
        let response = try await server.client.answer("tundra", ChoiceAnswerRequest(
            cid: "from", file: "question one.md", picked: [1, 0], other: "Later", mid: "answer-1"))
        #expect(response.said == "answered")
        #expect(response.answer.picked == [0])
        let request = try #require(server.requests.first)
        #expect(request.path == "/app/v1/chat/tundra/answer")
        #expect(request.method == "POST")
        #expect(request.header("Content-Type") == "application/json")
        let body = try #require(request.json)
        #expect(body.count == 5)
        #expect(body["cid"] as? String == "from")
        #expect(body["file"] as? String == "question one.md")
        #expect(body["picked"] as? [Int] == [0, 1])
        #expect(body["other"] as? String == "Later")
        #expect(body["mid"] as? String == "answer-1")
    }

    @Test(arguments: [(409, "answered"), (400, "bad_answer"), (404, "not_found")])
    func refusalsRetainStatusCodeMessageAndExistingAnswer(status: Int, code: String) async throws {
        let server = StubServer { _ in
            .json(status, #"{"error":{"code":"\#(code)","message":"Cannot answer","answer":\#(status == 409 ? Self.answerJSON : "null")}}"#)
        }
        let error = await #expect(throws: APIError.self) {
            try await server.client.answer("tundra", ChoiceAnswerRequest(cid: "web-1", file: "q.md", picked: [0], other: nil))
        }
        #expect(error?.status == status)
        #expect(error?.code == code)
        #expect(error?.message == "Cannot answer")
        guard case .server(_, let body) = error else { Issue.record("Missing server error"); return }
        #expect(body.answer?.picked == (status == 409 ? [0] : nil))
    }

    @Test @MainActor func failedSubmissionRetriesItsFrozenBodyAndMid() async throws {
        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let model = QuestionAnswers()
        await model.send(file: "q.md", cid: "web-1", picked: [0], other: nil, instance: "tundra", client: server.client)
        let failed = try #require(model.pending["q.md"])
        #expect(!failed.sending)
        #expect(failed.error != nil)
        #expect(!failed.request.mid.isEmpty)
        server.setAnswer { _ in .json(200, Self.responseJSON) }
        await model.retry(file: "q.md", instance: "tundra", client: server.client)
        #expect(server.requests.count == 2)
        #expect(server.requests.first?.body == server.requests.last?.body)
        #expect(model.pending["q.md"]?.answer?.picked == [0])
        #expect(model.pending["q.md"]?.error == nil)
        #expect(model.pending["q.md"]?.sending == false)
        await model.retry(file: "q.md", instance: "tundra", client: server.client)
        #expect(server.requests.count == 2)
    }

    @Test @MainActor func pendingShowsSendingAndPreventsDuplicateSubmissions() async throws {
        let gate = AnswerGate()
        let server = StubServer { _ in
            var response = StubServer.Response.json(200, Self.responseJSON)
            response.beforeReply = { await gate.wait() }
            return response
        }
        let model = QuestionAnswers()
        let task = Task { await model.send(file: "q.md", cid: "from", picked: [0], other: nil,
                                          instance: "tundra", client: server.client) }
        while model.pending["q.md"] == nil { await Task.yield() }
        let pending = try #require(model.pending["q.md"])
        #expect(pending.sending)
        #expect(pending.error == nil)
        await model.send(file: "q.md", cid: "from", picked: [1], other: nil, instance: "tundra", client: server.client)
        await model.retry(file: "q.md", instance: "tundra", client: server.client)
        #expect(model.pending["q.md"]?.request.mid == pending.request.mid)
        await gate.release()
        await task.value
        #expect(server.requests.count == 1)
        #expect(model.pending["q.md"]?.sending == false)
    }

    @Test @MainActor func alreadyAnsweredBecomesReadOnlyWithTheExistingAnswer() async throws {
        let server = StubServer { _ in .json(409, #"{"error":{"code":"answered","message":"Answered elsewhere","answer":\#(Self.answerJSON)}}"#) }
        let model = QuestionAnswers()
        await model.send(file: "q.md", cid: "from", picked: [1], other: nil, instance: "tundra", client: server.client)
        #expect(model.pending["q.md"]?.answer?.picked == [0])
        #expect(model.pending["q.md"]?.error?.message == "Answered elsewhere")
        await model.retry(file: "q.md", instance: "tundra", client: server.client)
        #expect(server.requests.count == 1)
    }

    @Test func validSelectionsRequireAPickOrNonblankOtherAndRespectLimits() {
        #expect(!QuestionAnswers.valid(picked: [], other: " \n", choices: Self.choices))
        #expect(QuestionAnswers.valid(picked: [], other: "Another plan", choices: Self.choices))
        #expect(QuestionAnswers.valid(picked: [0], other: "", choices: Self.choices))
        #expect(!QuestionAnswers.valid(picked: [0, 1], other: "", choices: Self.choices))
        #expect(!QuestionAnswers.valid(picked: [2], other: "", choices: Self.choices))
        #expect(!QuestionAnswers.valid(picked: [-1], other: "", choices: Self.choices))
        #expect(!QuestionAnswers.valid(picked: [0], other: String(repeating: "a", count: 2001), choices: Self.choices))
        var multiple = Self.choices
        multiple.multiple = true
        #expect(QuestionAnswers.valid(picked: [0, 1], other: "Also", choices: multiple))
    }

    @Test func waitingQuestionsCountOnlyOpenQuestionsAndChooseTheOldest() throws {
        var feed = try SHAFTJSON.decoder().decode(Feed.self, from: DecodingTests.fixture("sent"))
        var item = try SHAFTJSON.decoder().decode(SentItem.self, from: JSONSerialization.data(withJSONObject: sentObject()))
        item.kind = "question"
        item.open = true
        item.key = "newer"
        item.ts = Date(timeIntervalSince1970: 200)
        var oldest = item
        oldest.key = "oldest"
        oldest.ts = Date(timeIntervalSince1970: 100)
        oldest.choices = nil
        var closed = item
        closed.open = false
        var message = item
        message.kind = "message"
        feed.content?.items = [.sent(item), .sent(closed), .sent(message), .sent(oldest)]
        #expect(FeedRows.waitingQuestions(feed).map(\.key) == ["oldest", "newer"])
        feed.content?.items = []
        #expect(FeedRows.waitingQuestions(feed).isEmpty)
    }
}

private actor AnswerGate {
    private var continuation: CheckedContinuation<Void, Never>?
    private var released = false
    func wait() async {
        if released { return }
        await withCheckedContinuation { continuation = $0 }
    }
    func release() {
        released = true
        continuation?.resume()
        continuation = nil
    }
}
