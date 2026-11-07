import Foundation
import Testing

@MainActor
struct FilePreviewTests {
    private func file(_ name: String, _ text: String) throws -> URL {
        try AttachmentLoader.write(Data(text.utf8), name: name, key: UUID().uuidString)
    }

    /// The bug: a Markdown file, from a chat's chip or a document's Download, opened in Quick Look as its source.
    @Test func aMarkdownFileOpensAsAPageNotInQuickLook() throws {
        let preview = FilePreview()
        let url = try file("report.md", "# Report\n")
        preview.open(local: url)
        #expect(preview.page == MarkdownPage(file: url, text: "# Report\n"))
        #expect(preview.url == nil)
        #expect(preview.showing)
    }

    @Test func anyOtherFileStaysInTheSystemPreview() throws {
        let preview = FilePreview()
        let url = try file("totals.csv", "a,b\n")
        preview.open(local: url)
        #expect(preview.url == url)
        #expect(preview.page == nil)
        #expect(preview.showing)
    }

    @Test func closingLeavesNothingShowing() throws {
        let preview = FilePreview()
        preview.open(local: try file("report.md", "# Report\n"))
        preview.close()
        #expect(!preview.showing)
    }
}
