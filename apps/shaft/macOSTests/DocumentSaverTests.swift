import Foundation
import Testing

/// Download on a document's row saves it under its own name: never under a path, never nameless.
struct DocumentSaverTests {
    @Test func theServersNameWinsElseThePathsLastPart() {
        #expect(SavedDocument.name("plan.md", path: "q4/old-name.md") == "plan.md")
        #expect(SavedDocument.name(nil, path: "q4/sub/notes.md") == "notes.md")
        #expect(SavedDocument.name("", path: "readme.md") == "readme.md")
    }

    @Test func aNameIsNeverAPathOrEmpty() {
        #expect(SavedDocument.name("a/b.md", path: "x.md") == "a_b.md")
        #expect(SavedDocument.name(nil, path: "") == "document")
    }

    @Test func theBytesAreWrittenAsTheyCame() throws {
        let bytes = Data("# Plan\n\nè ✓\n".utf8)
        let wrapper = FileWrapper(regularFileWithContents: SavedDocument(data: bytes, name: "plan.md").data)
        #expect(wrapper.regularFileContents == bytes)
    }
}
