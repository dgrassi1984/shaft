import Foundation
import Testing
@testable import SHAFTKit

struct SharedItemsTests {
    @Test func textOnly() {
        let content = SharedItems([.text("Hello")])
        #expect(content.text == "Hello")
        #expect(content.files.isEmpty)
        #expect(content.isSendable)
    }

    @Test func urlOnly() throws {
        let url = try #require(URL(string: "https://example.com/a?q=1"))
        #expect(SharedItems([.url(url)]).text == "https://example.com/a?q=1")
    }

    @Test func notePrecedesTextAndURLInHostOrder() throws {
        let url = try #require(URL(string: "https://example.com"))
        #expect(SharedItems([.text("Read this"), .url(url)], note: "My note").text
                == "My note\nRead this\nhttps://example.com")
    }

    @Test func imageAndText() {
        let image = OutgoingFile(name: "image.jpg", mimeType: "image/jpeg", fileURL: URL(filePath: "/tmp/image.jpg"))
        let content = SharedItems([.file(image), .text("Look")])
        #expect(content.text == "Look")
        #expect(content.files == [image])
        #expect(content.isSendable)
    }

    @Test func severalFilesKeepNamesTypesAndOrder() {
        let files = ["report.pdf", "movie.mov", "data.csv"].map {
            OutgoingFile(name: $0, mimeType: MultipartForm.mimeType(for: $0), fileURL: URL(filePath: "/tmp/\($0)"))
        }
        let content = SharedItems(files.map { .file($0) })
        #expect(content.files == files)
        #expect(content.text.isEmpty)
        #expect(content.isSendable)
    }

    @Test func emptyAndWhitespaceCannotSend() {
        #expect(!SharedItems([]).isSendable)
        #expect(!SharedItems([.text(" \n")], note: " \n").isSendable)
        #expect(SharedItems([], note: "A note").isSendable)
    }

    @Test func attachmentWithoutMessageTextCanSend() {
        let file = OutgoingFile(name: "report.pdf", mimeType: "application/pdf", fileURL: URL(filePath: "/tmp/report.pdf"))
        let content = SharedItems([.file(file)], note: " \n")
        #expect(content.isSendable)
        #expect(content.text.isEmpty)
        #expect(content.files == [file])
    }

    @Test func unsupportedItemsHaveAMessageAndCannotSendAlone() {
        let content = SharedItems([.unsupported("item 1 (unsupported type)")])
        #expect(content.unsupportedMessages == ["Could not share item 1 (unsupported type)."])
        #expect(!content.isSendable)
        let mixed = SharedItems([.text("Hello"), .unsupported("item 2")])
        #expect(mixed.isSendable)
        #expect(mixed.unsupportedMessages == ["Could not share item 2."])
    }
}
