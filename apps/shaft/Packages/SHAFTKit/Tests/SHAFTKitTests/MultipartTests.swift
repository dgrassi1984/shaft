import Foundation
import Testing
@testable import SHAFTKit

struct MultipartTests {
    @Test func diskBodyPreservesTheExistingWireFormat() throws {
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appending(path: "input")
        let output = directory.appending(path: "body")
        let bytes = Data([0, 255, 13, 10, 42])
        try bytes.write(to: file)
        var form = MultipartForm(boundary: "test")
        form.addField("text", "Hello\nworld")
        form.addField("mid", "m-1")
        var old = form
        old.addFile("file", filename: "a\"\r\n.pdf", mimeType: "application/pdf", data: bytes)
        try form.writeBody(files: [OutgoingFile(name: "a\"\r\n.pdf", mimeType: "application/pdf", fileURL: file)], to: output)
        #expect(try Data(contentsOf: output) == old.body())
    }

    @Test func largeAttachmentStaysOnDiskToFitShareExtensionMemory() throws {
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = directory.appending(path: "large.bin")
        let output = directory.appending(path: "body")
        try Data().write(to: source)
        let writer = try FileHandle(forWritingTo: source)
        let chunk = Data((0..<65536).map { UInt8($0 % 251) })
        for _ in 0..<256 { try writer.write(contentsOf: chunk) }
        try writer.close()
        let form = MultipartForm(boundary: "large")
        try form.writeBody(files: [OutgoingFile(name: "large.bin", mimeType: "application/octet-stream", fileURL: source)],
                           to: output)
        let reader = try FileHandle(forReadingFrom: output)
        defer { try? reader.close() }
        let header = Data(("--large\r\nContent-Disposition: form-data; name=\"file\"; filename=\"large.bin\"\r\n"
                           + "Content-Type: application/octet-stream\r\n\r\n").utf8)
        #expect(try reader.read(upToCount: header.count) == header)
        for _ in 0..<256 { #expect(try reader.read(upToCount: chunk.count) == chunk) }
        #expect(try reader.readToEnd() == Data("\r\n--large--\r\n".utf8))
        // The form retains only its fields; the attachment was passed to the disk writer by URL.
        #expect(form.body() == Data("--large--\r\n".utf8))
    }

    @Test func missingAttachmentRemovesPartialBodyAndNamesTheFile() throws {
        let output = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: output) }
        let file = OutgoingFile(name: "missing.pdf", mimeType: "application/pdf",
                                fileURL: output.appending(path: "missing"))
        let error = #expect(throws: APIError.self) { try MultipartForm().writeBody(files: [file], to: output) }
        #expect(error?.message.contains("Could not read missing.pdf to send it") == true)
        #expect(!FileManager.default.fileExists(atPath: output.path))
    }
}
