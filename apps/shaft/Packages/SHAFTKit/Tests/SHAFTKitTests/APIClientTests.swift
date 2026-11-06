import Foundation
import Testing
@testable import SHAFTKit

struct APIClientTests {
    @Test func replyIdentitySurvivesPersistenceAndIsSentAsMultipart() async throws {
        var quote = Quote(quote: "Keep this?", answer: "Yes")
        quote.inReplyTo = "stable.md"
        let original = OutgoingMessage(mid: "reply-1", text: "", quotes: [quote])
        let restored = try SHAFTJSON.decoder().decode(OutgoingMessage.self,
                            from: SHAFTJSON.encoder().encode(original))
        #expect(restored.inReplyTo == "stable.md")
        let server = StubServer { _ in .json(200, #"{"said":"sent","chat":"web-1","file":null}"#) }
        _ = try await server.client.send("thelos", restored)
        let request = try #require(server.requests.first)
        let form = try ParsedMultipart(body: request.body, contentType: #require(request.header("Content-Type")))
        #expect(form.field("in_reply_to") == "stable.md")
        let old = try SHAFTJSON.decoder().decode(Quote.self, from: Data(#"{"quote":"old","answer":"","doc":""}"#.utf8))
        #expect(old.inReplyTo == nil)
        let encoded = try #require(JSONSerialization.jsonObject(with: SHAFTJSON.encoder().encode(old)) as? [String: String])
        #expect(encoded == ["quote": "old", "answer": "", "doc": ""])
    }

    static let idle = try! DecodingTests.fixture("idle")
    static let live = try! DecodingTests.fixture("live")
    static let livePartial = try! DecodingTests.fixture("live-partial")

    @Test func everyRequestCarriesTheBearer() async throws {
        let server = StubServer { _ in .json(200, String(decoding: try DecodingTests.fixture("instances"), as: UTF8.self)) }
        _ = try await server.client.instances()
        let request = try #require(server.requests.first)
        #expect(request.header("Authorization") == "Bearer 8c1f4e2a9b07d3e5.s3cret")
        #expect(request.path == "/app/v1/instances")
        #expect(request.method == "GET")
    }

    @Test func aConditionalListAnswers304AsNotModified() async throws {
        let server = StubServer { request in
            request.header("If-None-Match") == "\"abc\""
                ? .notModified
                : .json(200, String(decoding: try DecodingTests.fixture("instances"), as: UTF8.self),
                        headers: ["ETag": "\"abc\""])
        }
        guard case .value(let list, let etag) = try await server.client.instances() else {
            Issue.record("the first answer is not a value"); return
        }
        #expect(list.instances.map(\.name) == ["wren"])
        #expect(etag == "\"abc\"")
        guard case .notModified = try await server.client.instances(etag: etag) else {
            Issue.record("a matching ETag did not answer 304"); return
        }
    }

    @Test func aRefusalCarriesTheServersMessage() async throws {
        let server = StubServer { _ in
            .json(409, #"{"error": {"code": "refused", "message": "it is still replying: stop the reply first"}}"#)
        }
        let error = await #expect(throws: APIError.self) { try await server.client.end("thelos", cid: "web-1") }
        #expect(error == .server(status: 409, error: ServerError(code: "refused",
                                                                 message: "it is still replying: stop the reply first")))
        #expect(error?.message == "it is still replying: stop the reply first")
        #expect(error?.isRetryable == false)
    }

    @Test func unpairedIsTypedAsUnpaired() async throws {
        let server = StubServer { _ in .json(401, #"{"error": {"code": "unpaired", "message": "Pair this device again."}}"#) }
        let error = await #expect(throws: APIError.self) { _ = try await server.client.chats("thelos") }
        #expect(error?.isUnpaired == true)
        #expect(error?.status == 401)
    }

    @Test func aStaleSaveCarriesShaAndDiff() async throws {
        let server = StubServer { _ in
            .json(409, #"{"error": {"code": "stale", "message": "the document changed", "sha": "abc", "diff": "-a\n+b"}}"#)
        }
        let error = await #expect(throws: APIError.self) {
            _ = try await server.client.saveDoc("thelos", path: "a.md", text: "x", sha: "old")
        }
        guard case .server(_, let body) = error else { Issue.record("not a server error"); return }
        #expect(body.sha == "abc")
        #expect(body.diff == "-a\n+b")
        let request = try #require(server.requests.first?.json)
        #expect(request["path"] as? String == "a.md")
        #expect(request["sha"] as? String == "old")
    }

    @Test func anErrorWithoutTheJSONBodyKeepsItsStatusAndText() async throws {
        let server = StubServer { _ in StubServer.Response(status: 502, headers: ["Content-Type": "text/html"],
                                                           body: Data("<html>Bad Gateway</html>".utf8)) }
        let error = await #expect(throws: APIError.self) { _ = try await server.client.pushStatus() }
        #expect(error == .http(status: 502, body: "<html>Bad Gateway</html>"))
        #expect(error?.isRetryable == true)
    }

    @Test func aNetworkFailureIsATransportError() async throws {
        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let error = await #expect(throws: APIError.self) { _ = try await server.client.pushLatest() }
        guard case .transport = error else { Issue.record("not a transport error: \(String(describing: error))"); return }
        #expect(error?.isRetryable == true)
    }

    @Test func latestWithoutAnInstanceOmitsTheQuery() async throws {
        let server = StubServer { _ in .json(200, #"{"events": [], "badge": 0}"#) }
        _ = try await server.client.pushLatest()
        let request = try #require(server.requests.first)
        #expect(request.path == "/app/v1/push/latest")
        #expect(request.url.query == nil)
    }

    @Test func anAnswerThatBreaksTheContractIsADecodingError() async throws {
        let server = StubServer { _ in .json(200, #"{"chats": [{"id": "web-1"}]}"#) }
        let error = await #expect(throws: APIError.self) { _ = try await server.client.chats("thelos") }
        guard case .decoding(let path, _) = error else { Issue.record("not a decoding error"); return }
        #expect(path == "/app/v1/chats/thelos")
    }

    @Test func aSendIsMultipartWithItsFieldsAndFiles() async throws {
        let folder = FileManager.default.temporaryDirectory.appending(path: "shaft-send-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let png = folder.appending(path: "ci-run.png")
        let bytes = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0xFF])
        try bytes.write(to: png)

        let server = StubServer { _ in
            .json(200, #"{"said": "sent to thelos · answers its question", "chat": "web-3f9a1c0d7e2b4a65", "file": null}"#)
        }
        let message = OutgoingMessage(
            mid: "0b8e6c3a-5d1f-4c2e-9a7b-2f4d6e8a1c3b", text: "Rerun it", mode: .queue,
            quotes: [Quote(quote: "Should the nightly report keep the per-account lines?", answer: "Yes")],
            files: [OutgoingFile(name: "ci-run.png", mimeType: "image/png", fileURL: png)])
        let answer = try await server.client.send("thelos", message)
        #expect(answer.said == "sent to thelos · answers its question")

        let request = try #require(server.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/app/v1/chat/thelos/send")
        let contentType = try #require(request.header("Content-Type"))
        #expect(contentType.hasPrefix("multipart/form-data; boundary="))
        let form = try ParsedMultipart(body: request.body, contentType: contentType)
        #expect(form.field("text") == "Rerun it")
        #expect(form.field("mid") == "0b8e6c3a-5d1f-4c2e-9a7b-2f4d6e8a1c3b")
        #expect(form.field("mode") == "queue")
        #expect(form.field("retry_of") == nil)
        #expect(form.field("in_reply_to") == nil)
        let quotes = try JSONSerialization.jsonObject(with: Data(try #require(form.field("quotes")).utf8)) as? [[String: String]]
        #expect(quotes == [["quote": "Should the nightly report keep the per-account lines?", "answer": "Yes", "doc": ""]])
        let file = try #require(form.parts.first { $0.filename != nil })
        #expect(file.filename == "ci-run.png")
        #expect(file.contentType == "image/png")
        #expect(file.data == bytes)
    }

    @Test func aSendWithoutQuotesOmitsThemAndARetryCarriesRetryOf() async throws {
        let server = StubServer { _ in .json(200, #"{"said": "sent to thelos", "chat": "web-1", "file": null}"#) }
        _ = try await server.client.send("thelos", OutgoingMessage(mid: "m-1", text: "Rerun the report for October.",
                                                                    retryOf: 3))
        let request = try #require(server.requests.first)
        let form = try ParsedMultipart(body: request.body, contentType: try #require(request.header("Content-Type")))
        #expect(form.field("quotes") == nil)
        #expect(form.field("retry_of") == "3")
        #expect(form.field("mode") == "now")
    }

    @Test func aFileDownloadKeepsItsNameAndType() async throws {
        let server = StubServer { _ in
            StubServer.Response(status: 200, headers: ["Content-Type": "application/octet-stream",
                                                       "Content-Disposition": "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"],
                                body: Data("%PDF".utf8))
        }
        let file = try await server.client.download("/app/v1/file/message/20261103-140230-118204-from-thelos.md/0")
        #expect(file.data == Data("%PDF".utf8))
        #expect(file.filename == "résumé.pdf")
        #expect(file.mimeType == "application/octet-stream")
        let request = try #require(server.requests.first)
        #expect(request.path == "/app/v1/file/message/20261103-140230-118204-from-thelos.md/0")
        #expect(request.header("Authorization") == "Bearer 8c1f4e2a9b07d3e5.s3cret")
    }

    @Test func aDocumentPathWithAPlusIsEncoded() async throws {
        let server = StubServer { _ in .json(404, #"{"error": {"code": "not_found", "message": "no such document"}}"#) }
        _ = try? await server.client.doc("thelos", path: "c++ notes.md")
        let request = try #require(server.requests.first)
        #expect(request.query("path") == "c++ notes.md")
        #expect(request.url.absoluteString.contains("c%2B%2B"))
    }

    @Test func pairingPostsTheLinkToItsServerWithoutABearer() async throws {
        let server = StubServer { _ in
            .json(200, #"{"device": "8c1f4e2a9b07d3e5", "secret": "kP0v", "name": "iphone-app", "login": "operator@example.com"}"#)
        }
        let store = MemoryCredentialStore()
        let pasted = "  https://\(server.host):8444/pair?t=Zq3tok \n"
        let credential = try await PairingStore(store: store, session: server.session).pair(pasted: pasted, platform: "ios")
        #expect(credential.baseURL.absoluteString == "https://\(server.host):8444")
        #expect(credential.bearer == "Bearer 8c1f4e2a9b07d3e5.kP0v")
        #expect(try store.load() == credential)
        let request = try #require(server.requests.first)
        #expect(request.path == "/app/v1/pair")
        #expect(request.header("Authorization") == nil)
        #expect(request.json?["link"] as? String == "https://\(server.host):8444/pair?t=Zq3tok")
        #expect(request.json?["platform"] as? String == "ios")
    }
}

struct PairingLinkTests {
    @Test func theBaseIsEverythingBeforePair() throws {
        let link = try PairingLink("https://mac.tail1234.ts.net:8444/pair?t=Zq3abc")
        #expect(link.baseURL.absoluteString == "https://mac.tail1234.ts.net:8444")
        #expect(link.token == "Zq3abc")
    }

    @Test func aLinkWithoutATokenOrNotAPairingLinkIsRefused() {
        #expect(throws: APIError.self) { try PairingLink("https://mac.tail1234.ts.net:8444/pair") }
        #expect(throws: APIError.self) { try PairingLink("https://mac.tail1234.ts.net:8444/chat?t=x") }
        #expect(throws: APIError.self) { try PairingLink("thelos") }
    }
}
