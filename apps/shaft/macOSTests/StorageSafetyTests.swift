import Foundation
import SHAFTKit
import Testing

@MainActor
struct StorageSafetyTests {
    @Test func devCredentialStoreRefusesNonLoopbackBearers() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = FileCredentialStore(url: root.appending(path: "credential.json"))
        for host in ["example.com", "localhost.example.com", "127.0.0.2"] {
            let credential = credential("https://\(host)")
            #expect(throws: APIError.self) { try store.save(credential) }
            #expect(!FileManager.default.fileExists(atPath: store.url.path))
        }
        for host in ["127.0.0.1", "localhost", "[::1]"] {
            let credential = credential("http://\(host):8444")
            try store.save(credential)
            #expect(try store.load() == credential)
        }
        try JSONEncoder().encode(credential("https://example.com")).write(to: store.url)
        #expect(throws: APIError.self) { try store.load() }
    }

    @Test func attachmentCacheSeparatesServersAndClearsOnPairingChanges() async throws {
        let loader = AttachmentLoader.shared
        loader.clear()
        defer { loader.clear() }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [AttachmentCacheProtocol.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let first = APIClient(credential: credential("https://first.example"), session: session)
        let second = APIClient(credential: credential("https://second.example"), session: session)
        let a = try await loader.data("/file", client: first)
        #expect(try await loader.data("/file", client: first) == a)
        let b = try await loader.data("/file", client: second)
        #expect(String(decoding: a, as: UTF8.self).hasPrefix("first.example"))
        #expect(String(decoding: b, as: UTF8.self).hasPrefix("second.example"))
        loader.clear()
        #expect(try await loader.data("/file", client: first) != a)
    }

    private func credential(_ base: String) -> ServerCredential {
        ServerCredential(baseURL: URL(string: base)!, device: "test", secret: "test", name: "test", login: "test")
    }
}

final class AttachmentCacheProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let url = request.url else { return }
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("\(url.host!) \(UUID())".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
