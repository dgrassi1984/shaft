import Foundation
import UniformTypeIdentifiers

/// A `multipart/form-data` body, as a browser builds one for the web's composer (RFC 7578): the send's fields and its
/// files (§6). File names go as UTF-8, with `"`, CR and LF percent-escaped, as browsers send them.
public struct MultipartForm: Sendable {
    public let boundary: String
    private var parts: [Data] = []

    public init(boundary: String = "shaft-" + UUID().uuidString.lowercased()) {
        self.boundary = boundary
    }

    public var contentType: String { "multipart/form-data; boundary=\(boundary)" }

    public mutating func addField(_ name: String, _ value: String) {
        var part = Data()
        part.append("--\(boundary)\r\n")
        part.append("Content-Disposition: form-data; name=\"\(Self.escape(name))\"\r\n\r\n")
        part.append(value)
        part.append("\r\n")
        parts.append(part)
    }

    public mutating func addFile(_ name: String, filename: String, mimeType: String, data: Data) {
        var part = Data()
        part.append("--\(boundary)\r\n")
        part.append("Content-Disposition: form-data; name=\"\(Self.escape(name))\"; filename=\"\(Self.escape(filename))\"\r\n")
        part.append("Content-Type: \(mimeType)\r\n\r\n")
        part.append(data)
        part.append("\r\n")
        parts.append(part)
    }

    public func body() -> Data {
        var body = Data()
        for part in parts { body.append(part) }
        body.append("--\(boundary)--\r\n")
        return body
    }

    static func escape(_ text: String) -> String {
        text.replacingOccurrences(of: "\"", with: "%22")
            .replacingOccurrences(of: "\r", with: "%0D")
            .replacingOccurrences(of: "\n", with: "%0A")
    }

    /// A file's MIME type from its extension; `application/octet-stream` when the system does not know it.
    public static func mimeType(for filename: String) -> String {
        let ext = (filename as NSString).pathExtension
        return UTType(filenameExtension: ext)?.preferredMIMEType ?? "application/octet-stream"
    }
}

extension Data {
    mutating func append(_ text: String) {
        append(Data(text.utf8))
    }
}
