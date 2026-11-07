import Foundation
import Testing
@testable import SHAFTKit

struct MarkdownFileTests {
    private func write(_ name: String, _ bytes: Data) throws -> URL {
        let folder = FileManager.default.temporaryDirectory
            .appending(path: "shaft-markdownfile-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appending(path: name)
        try bytes.write(to: url)
        return url
    }

    @Test(arguments: ["report.md", "REPORT.MD", "notes.Markdown", "with space.md"])
    func aMarkdownNameIsMarkdown(_ name: String) {
        #expect(MarkdownFile.matches(name: name))
    }

    @Test(arguments: ["totals.csv", "report.md.txt", "md", "report", ".md.zip", "report.mdx"])
    func anyOtherNameIsNot(_ name: String) {
        #expect(!MarkdownFile.matches(name: name))
    }

    /// The bug: a Markdown file sent in a chat opened in Quick Look as its source, `#` and `**` and all.
    @Test func aMarkdownFileReadsAsItsText() throws {
        let url = try write("report.md", Data("# Report\n\n**Net** totals.\n".utf8))
        #expect(MarkdownFile.text(at: url) == "# Report\n\n**Net** totals.\n")
    }

    @Test func anEmptyFileIsEmptyTextNotAnotherKindOfFile() throws {
        #expect(MarkdownFile.text(at: try write("empty.md", Data())) == "")
    }

    /// With the mark left in, the first line's `#` is no heading.
    @Test func aByteOrderMarkIsNotText() throws {
        let url = try write("bom.md", Data([0xEF, 0xBB, 0xBF]) + Data("# Report\n".utf8))
        #expect(MarkdownFile.text(at: url) == "# Report\n")
    }

    @Test func otherFilesStayWithQuickLook() throws {
        #expect(MarkdownFile.text(at: try write("totals.csv", Data("a,b\n".utf8))) == nil)
    }

    @Test func aFileThatIsNotUTF8StaysWithQuickLook() throws {
        #expect(MarkdownFile.text(at: try write("binary.md", Data([0xFF, 0xFE, 0x00, 0xC3]))) == nil)
    }

    @Test func theCeilingIsInclusive() throws {
        let fits = Data(repeating: 0x61, count: MarkdownFile.maxBytes)
        #expect(MarkdownFile.text(at: try write("fits.md", fits))?.utf8.count == MarkdownFile.maxBytes)
        #expect(MarkdownFile.text(at: try write("over.md", fits + Data([0x61]))) == nil)
    }

    @Test func aFileThatIsGoneIsNoText() {
        #expect(MarkdownFile.text(at: URL(fileURLWithPath: "/nonexistent/shaft/report.md")) == nil)
    }
}
