import AppKit
import Testing

/// The Mac's message text (macOS/MarkdownAttributed.swift): what a selection in a message reads and how its blocks are
/// laid out. Each test names what it pins; the regressions are the bugs found driving the app.
struct MarkdownAttributedTests {
    func render(_ markdown: String) -> MarkdownAttributed.Rendered { MarkdownAttributed.render(markdown) }

    func paragraphStyle(_ text: NSAttributedString, at index: Int) -> NSParagraphStyle? {
        text.attribute(.paragraphStyle, at: index, effectiveRange: nil) as? NSParagraphStyle
    }

    @Test func paragraphsAreLinesWithNoBreakAfterTheLast() {
        let text = render("First **bold** line.\n\nSecond line.").text
        #expect(text.string == "First bold line.\nSecond line.")
        let bold = text.attribute(.font, at: 6, effectiveRange: nil) as? NSFont
        #expect(bold?.fontDescriptor.symbolicTraits.contains(.bold) == true)
    }

    @Test func listsHaveTheirBulletOrNumberBeforeATab() {
        #expect(render("- one\n- two").text.string == "\u{2022}\tone\n\u{2022}\ttwo")
        #expect(render("1. one\n2. two").text.string == "1.\tone\n2.\ttwo")
    }

    /// Regression (driven 2026-11-03): a task item drew its bullet and then its box ("• ☑ reconcile").
    @Test func aTaskItemShowsItsBoxInsteadOfTheBullet() {
        let string = render("- [x] reconcile October\n- [ ] reconcile November").text.string
        #expect(string == "\u{2611}\treconcile October\n\u{2610}\treconcile November")
        #expect(!string.contains("\u{2022}"))
    }

    /// Regression (found writing the renderer): a cell with more than one run (bold, then plain) was split into two
    /// paragraphs, so its second half fell into a cell of its own.
    @Test func aTableCellStaysOneParagraphWhateverItsStyles() throws {
        let text = render("| Account | Total |\n|---|---:|\n| **Operating** cash | 12 |\n| Reserve | 3 |").text
        let lines = text.string.components(separatedBy: "\n")
        #expect(lines == ["Account", "Total", "Operating cash", "12", "Reserve", "3"])
        var cells: [(row: Int, column: Int)] = []
        var index = 0
        for line in lines {
            let block = try #require(paragraphStyle(text, at: index)?.textBlocks.last as? NSTextTableBlock)
            cells.append((block.startingRow, block.startingColumn))
            #expect(block.table.numberOfColumns == 2)
            index += (line as NSString).length + 1
        }
        #expect(cells.map(\.row) == [0, 0, 1, 1, 2, 2])
        #expect(cells.map(\.column) == [0, 1, 0, 1, 0, 1])
        let header = text.attribute(.font, at: 0, effectiveRange: nil) as? NSFont
        #expect(header?.fontDescriptor.symbolicTraits.contains(.bold) == true)
    }

    @Test func aCodeBlockIsOneTintedMonospacedBlockWithItsCodeForCopy() throws {
        let rendered = render("Before.\n\n```swift\nlet x = 1\nlet y = 2\n```\n\nAfter.")
        #expect(rendered.text.string == "Before.\nlet x = 1\nlet y = 2\nAfter.")
        let code = try #require(rendered.codeBlocks.first)
        #expect(code.code == "let x = 1\nlet y = 2")
        let first = try #require(paragraphStyle(rendered.text, at: 8)?.textBlocks.first)
        let second = try #require(paragraphStyle(rendered.text, at: 18)?.textBlocks.first)
        #expect(first === second)                     // one block, not one per line
        let font = rendered.text.attribute(.font, at: 8, effectiveRange: nil) as? NSFont
        #expect(font?.isFixedPitch == true)
        #expect(paragraphStyle(rendered.text, at: 0)?.textBlocks.isEmpty == true)
    }

    @Test func aQuoteHasABarAndLinksStayLinks() throws {
        let quote = render("> quoted words").text
        let block = try #require(paragraphStyle(quote, at: 0)?.textBlocks.first)
        #expect(block.width(for: .border, edge: .minX) == 3)
        let link = render("See [the docs](https://example.com/docs).").text
        let index = (link.string as NSString).range(of: "the docs").location
        #expect(link.attribute(.link, at: index, effectiveRange: nil) as? URL == URL(string: "https://example.com/docs"))
    }

    @Test func headingsAreLargerThanTheParagraphUnderThem() {
        let text = render("# Title\n\nBody").text
        let title = text.attribute(.font, at: 0, effectiveRange: nil) as? NSFont
        let body = text.attribute(.font, at: 6, effectiveRange: nil) as? NSFont
        #expect((title?.pointSize ?? 0) > (body?.pointSize ?? 0))
    }

    @Test func theOperatorsTextIsNotMarkdown() throws {
        let rendered = MarkdownAttributed.operatorText("> Ship it?\n\nYes, **today**.\n```\nmake\n```")
        #expect(rendered.text.string.contains("**today**"))         // as typed: not Markdown
        #expect(rendered.codeBlocks.map(\.code) == ["make"])
        let quote = try #require(paragraphStyle(rendered.text, at: 0)?.textBlocks.first)
        #expect(quote.width(for: .border, edge: .minX) == 3)
    }
}
