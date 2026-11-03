import Foundation
@testable import SHAFTKit

/// A server stand-in behind `URLProtocol`: each test gets its own host, so tests running in parallel never share
/// answers. `answer` sees every request with its body and says what comes back.
final class StubServer: @unchecked Sendable {
    struct Request: Sendable {
        let method: String
        let url: URL
        let headers: [String: String]
        let body: Data

        var path: String { url.path }

        func query(_ name: String) -> String? {
            URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == name }?.value
        }

        func header(_ name: String) -> String? {
            headers.first { $0.key.caseInsensitiveCompare(name) == .orderedSame }?.value
        }

        var json: [String: Any]? {
            (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
        }
    }

    struct Response: Sendable {
        var status: Int
        var headers: [String: String] = ["Content-Type": "application/json; charset=utf-8"]
        var body: Data

        static func json(_ status: Int = 200, _ text: String, headers: [String: String] = [:]) -> Response {
            var response = Response(status: status, body: Data(text.utf8))
            response.headers.merge(headers) { $1 }
            return response
        }

        static func data(_ data: Data, status: Int = 200) -> Response {
            Response(status: status, headers: [:], body: data)
        }

        static let notModified = Response(status: 304, headers: [:], body: Data())
    }

    /// Thrown by `answer` to fail the request as the network would.
    struct NetworkDown: Error {}

    let host = "stub-\(UUID().uuidString.lowercased()).example"
    private let lock = NSLock()
    private var answer: @Sendable (Request) throws -> Response
    private var log: [Request] = []

    nonisolated(unsafe) private static var servers: [String: StubServer] = [:]
    private static let registry = NSLock()

    init(answer: @escaping @Sendable (Request) throws -> Response) {
        self.answer = answer
        Self.registry.withLock { Self.servers[host] = self }
    }

    deinit {
        Self.registry.withLock { _ = Self.servers.removeValue(forKey: host) }
    }

    var baseURL: URL { URL(string: "https://\(host):8444")! }

    var credential: ServerCredential {
        ServerCredential(baseURL: baseURL, device: "8c1f4e2a9b07d3e5", secret: "s3cret", name: "test", login: "op@example.com")
    }

    var session: URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubProtocol.self]
        return URLSession(configuration: configuration)
    }

    var client: APIClient { APIClient(credential: credential, session: session) }

    var requests: [Request] { lock.withLock { log } }

    func setAnswer(_ answer: @escaping @Sendable (Request) throws -> Response) {
        lock.withLock { self.answer = answer }
    }

    fileprivate static func server(for host: String?) -> StubServer? {
        guard let host else { return nil }
        return registry.withLock { servers[host] }
    }

    fileprivate func handle(_ request: Request) throws -> Response {
        let answer = lock.withLock {
            log.append(request)
            return self.answer
        }
        return try answer(request)
    }
}

final class StubProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool {
        StubServer.server(for: request.url?.host) != nil
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let server = StubServer.server(for: request.url?.host), let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotFindHost))
            return
        }
        let body = request.httpBody ?? request.httpBodyStream.map(Self.read) ?? Data()
        let recorded = StubServer.Request(method: request.httpMethod ?? "GET", url: url,
                                          headers: request.allHTTPHeaderFields ?? [:], body: body)
        do {
            let response = try server.handle(recorded)
            let http = HTTPURLResponse(url: url, statusCode: response.status, httpVersion: "HTTP/1.1",
                                       headerFields: response.headers)!
            client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: response.body)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
        }
    }

    override func stopLoading() {}

    private static func read(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}

/// A multipart body split into its parts, to check what a send carries.
struct ParsedMultipart {
    struct Part {
        let name: String
        let filename: String?
        let contentType: String?
        let data: Data

        var text: String { String(decoding: data, as: UTF8.self) }
    }

    let parts: [Part]

    init(body: Data, contentType: String) throws {
        guard let boundary = contentType.components(separatedBy: "boundary=").last, !boundary.isEmpty else {
            throw StubServer.NetworkDown()
        }
        let delimiter = Data("--\(boundary)".utf8)
        var parts = [Part]()
        var cursor = body.startIndex
        var chunks = [Data]()
        while let range = body.range(of: delimiter, in: cursor..<body.endIndex) {
            if cursor != body.startIndex { chunks.append(body[cursor..<range.lowerBound]) }
            cursor = range.upperBound
        }
        for chunk in chunks {
            // Each chunk: "\r\n" headers "\r\n\r\n" content "\r\n".
            guard let split = chunk.range(of: Data("\r\n\r\n".utf8)) else { continue }
            let head = String(decoding: chunk[chunk.startIndex..<split.lowerBound], as: UTF8.self)
            var content = chunk[split.upperBound..<chunk.endIndex]
            if content.suffix(2) == Data("\r\n".utf8) { content = content.dropLast(2) }
            var name = "", filename: String?, type: String?
            for line in head.split(separator: "\r\n") {
                if line.hasPrefix("Content-Disposition:") {
                    for field in line.split(separator: ";").map({ $0.trimmingCharacters(in: .whitespaces) }) {
                        if field.hasPrefix("name=") { name = String(field.dropFirst(5)).trimmingCharacters(in: CharacterSet(charactersIn: "\"")) }
                        if field.hasPrefix("filename=") { filename = String(field.dropFirst(9)).trimmingCharacters(in: CharacterSet(charactersIn: "\"")) }
                    }
                } else if line.hasPrefix("Content-Type:") {
                    type = line.dropFirst("Content-Type:".count).trimmingCharacters(in: .whitespaces)
                }
            }
            parts.append(Part(name: name, filename: filename, contentType: type, data: Data(content)))
        }
        self.parts = parts
    }

    func field(_ name: String) -> String? {
        parts.first { $0.name == name && $0.filename == nil }?.text
    }
}
