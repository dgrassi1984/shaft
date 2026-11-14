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

    /// Write the send's body to disk. Attachment bytes are copied in bounded chunks for extension memory limits.
    func writeBody(files: [OutgoingFile], to url: URL) throws {
        do {
            try Data().write(to: url)
            let output = try FileHandle(forWritingTo: url)
            defer { try? output.close() }
            for part in parts { try output.write(contentsOf: part) }
            for file in files {
                let header = "--\(boundary)\r\n"
                    + "Content-Disposition: form-data; name=\"file\"; filename=\"\(Self.escape(file.name))\"\r\n"
                    + "Content-Type: \(file.mimeType)\r\n\r\n"
                try output.write(contentsOf: Data(header.utf8))
                do {
                    let input = try FileHandle(forReadingFrom: file.fileURL)
                    defer { try? input.close() }
                    while let chunk = try input.read(upToCount: 64 * 1024), !chunk.isEmpty {
                        try Task.checkCancellation()
                        try output.write(contentsOf: chunk)
                    }
                } catch {
                    if APIError.isCancellation(error) { throw error }
                    throw APIError.transport(code: (error as NSError).code,
                                             message: "Could not read \(file.name) to send it: \(error.localizedDescription)")
                }
                try output.write(contentsOf: Data("\r\n".utf8))
            }
            try output.write(contentsOf: Data("--\(boundary)--\r\n".utf8))
        } catch {
            try? FileManager.default.removeItem(at: url)
            throw error
        }
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
