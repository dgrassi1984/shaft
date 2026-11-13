import AppKit
import Testing
import UniformTypeIdentifiers

@MainActor
struct ComposerPasteTests {
    @Test func longPasteLoadsAsUTF8TextFileAndShortPasteStaysInline() async throws {
        let board = NSPasteboard(name: .init("SHAFT-test-\(UUID().uuidString)"))
        defer { board.releaseGlobally() }
        let text = String(repeating: "é", count: 2001)
        board.setString(text, forType: .string)
        let providers = ComposerNSTextView.pasteAttachments(on: board)
        #expect(providers.count == 1)
        let provider = try #require(providers.first)
        let loaded = try #require(await ItemImport.load(provider, index: 0))
        #expect(loaded.0 == Data(text.utf8))
        #expect(loaded.1.hasPrefix("pasted-text-"))
        #expect(loaded.1.hasSuffix(".txt"))
        #expect(ComposerNSTextView.attachments(on: board).isEmpty, "Text drops stay inline")
        board.clearContents()
        board.setString("short", forType: .string)
        #expect(ComposerNSTextView.pasteAttachments(on: board).isEmpty)
    }

    @Test func pastedTextTypeConvertsToTXTWithoutChangingUTF8Bytes() throws {
        let text = "UTF-8: é"
        let provider = ItemImport.textFile(text)
        let type = try #require(provider.registeredContentTypes.first)
        let converted = ItemImport.convert(Data(text.utf8), type: type)
        #expect(converted.0 == Data(text.utf8))
        #expect(converted.1 == "txt")
    }

    @Test func pasteValidationRecognizesLongTextAndRejectsShortText() {
        let board = NSPasteboard(name: .init("SHAFT-test-\(UUID().uuidString)"))
        defer { board.releaseGlobally() }
        board.setString(String(repeating: "a", count: 2001), forType: .string)
        #expect(ComposerNSTextView.hasPasteAttachments(on: board))
        board.clearContents()
        board.setString("short", forType: .string)
        #expect(!ComposerNSTextView.hasPasteAttachments(on: board))
    }

    @Test func fileURLWinsOverLongText() async throws {
        let board = NSPasteboard(name: .init("SHAFT-test-\(UUID().uuidString)"))
        defer { board.releaseGlobally() }
        let url = FileManager.default.temporaryDirectory.appending(path: "SHAFT-test-\(UUID().uuidString).txt")
        try Data("file contents".utf8).write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        board.writeObjects([url as NSURL])
        board.setString(String(repeating: "a", count: 2001), forType: .string)
        let providers = ComposerNSTextView.pasteAttachments(on: board)
        #expect(providers.count == 1)
        let provider = try #require(providers.first)
        let loaded = try #require(await ItemImport.load(provider, index: 0))
        #expect(loaded.0 == Data("file contents".utf8))
        #expect(loaded.1 == url.lastPathComponent)
    }
}
