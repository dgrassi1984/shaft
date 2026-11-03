import Foundation
import Testing
@testable import SHAFTKit

struct OriginSafetyTests {
    @Test func attachmentURLsCannotSendTheBearerToAnotherOrigin() async throws {
        let server = StubServer { _ in .data(Data("file".utf8)) }
        let client = server.client
        for url in ["https://elsewhere.example/file", "//elsewhere.example/file",
                    "http://\(server.host):8444/file", "https://\(server.host):8445/file"] {
            await #expect(throws: APIError.self) { try await client.download(url) }
        }
        #expect(server.requests.isEmpty)
        #expect(try await client.download("/file").data == Data("file".utf8))
        #expect(server.requests.first?.header("Authorization") == server.credential.bearer)
    }

    @Test func redirectsCannotSendTheBearerToAnotherOrigin() async throws {
        let origin = URL(string: "https://example.com")!
        let delegate = OriginRedirectDelegate(origin: origin)
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let task = session.dataTask(with: origin)
        let response = HTTPURLResponse(url: origin, statusCode: 302, httpVersion: nil, headerFields: nil)!
        for destination in ["https://other.example/file", "http://example.com/file", "https://example.com:8444/file"] {
            let redirected = await withCheckedContinuation { continuation in
                delegate.urlSession(session, task: task, willPerformHTTPRedirection: response,
                                    newRequest: URLRequest(url: URL(string: destination)!)) {
                    continuation.resume(returning: $0)
                }
            }
            #expect(redirected == nil)
        }
        let redirected = await withCheckedContinuation { continuation in
            delegate.urlSession(session, task: task, willPerformHTTPRedirection: response,
                                newRequest: URLRequest(url: URL(string: "https://EXAMPLE.com:443/file")!)) {
                continuation.resume(returning: $0)
            }
        }
        #expect(redirected?.url?.path == "/file")
    }
}
