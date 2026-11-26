import AppKit
import Testing
import SwiftUI

/// The Mac's message text (macOS/MarkdownAttributed.swift): what a selection in a message reads and how its blocks are
/// laid out. Each test names what it pins; the regressions are the bugs found driving the app.
struct MarkdownAttributedTests {
    @Test @MainActor func theSelectionReplyButtonIsOpaqueOverText() throws {
        let button = SelectionReplyButton(title: "↩ Reply", target: nil, action: nil)
        button.sizeToFit()
        #expect(button.isBordered == false)
        #expect(button.window == nil)
        #expect(button.frame.size == button.intrinsicContentSize)
        for appearance in [NSAppearance.Name.aqua, .darkAqua] {
            button.appearance = try #require(NSAppearance(named: appearance))
            for pressed in [false, true] {
                button.highlight(pressed)
                let bitmap = try #require(button.bitmapImageRepForCachingDisplay(in: button.bounds))
                button.cacheDisplay(in: button.bounds, to: bitmap)
                let x = Int((Design.Space.s12 / 2) * CGFloat(bitmap.pixelsWide) / button.bounds.width)
                let pixel = try #require(bitmap.colorAt(x: x, y: bitmap.pixelsHigh / 2))
                #expect(pixel.alphaComponent == 1)
            }
        }
    }

    @Test @MainActor func theSelectionReplyButtonTakesClicksPastItsParentsEdge() {
        let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 100, height: 20),
                              styleMask: .borderless, backing: .buffered, defer: true)
        let parent = NSView(frame: NSRect(x: 0, y: 0, width: 100, height: 20))
        window.contentView = parent
        let button = SelectionReplyButton(frame: NSRect(x: 10, y: -10, width: 60, height: 20))
        parent.addSubview(button)
        defer { button.removeFromSuperview() }

        let inside = parent.convert(NSPoint(x: 20, y: -5), to: nil)
        #expect(parent.hitTest(parent.convert(NSPoint(x: 20, y: -5), to: parent.superview)) == nil)
        #expect(button.accepts(inside))
        #expect(!button.accepts(parent.convert(NSPoint(x: 90, y: -5), to: nil)))
    }

    @Test @MainActor func selectionReplyRejectsClicksUnderAHiddenAncestor() {
        let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 100, height: 100),
                              styleMask: .borderless, backing: .buffered, defer: true)
        let parent = NSView(frame: NSRect(x: 0, y: 0, width: 100, height: 100))
        window.contentView = parent
        let button = SelectionReplyButton(frame: NSRect(x: 10, y: 10, width: 60, height: 20))
        parent.addSubview(button)
        let point = parent.convert(NSPoint(x: 20, y: 15), to: nil)
        #expect(button.accepts(point))
        parent.isHidden = true
        #expect(!button.accepts(point))
    }

    @Test @MainActor func splitViewUpdateConstraintsCrashZeroProposalHasContentIndependentWidth() {
        let short = MarkdownAttributed.Measure(MarkdownAttributed.operatorText("OK"))
        let long = MarkdownAttributed.Measure(render(
            String(repeating: "A long reply with **formatting** and many words. ", count: 80)))
        for hugging in [false, true] {
            for width in [CGFloat?(0), nil, -1] {
                let proposal = ProposedViewSize(width: width, height: nil)
                let a = short.size(for: proposal, hugging: hugging)
                let b = long.size(for: proposal, hugging: hugging)
                #expect(a.width == 1)
                #expect(b.width == 1)
                #expect(a.height == short.layout(width: 1).size.height)
                #expect(b.height == long.layout(width: 1).size.height)
            }
        }
    }

    @Test @MainActor func positiveMessageProposalFillsAnswersAndHugsOperatorChips() {
        let view = MarkdownAttributed.Measure(MarkdownAttributed.operatorText("OK"))
        let proposal = ProposedViewSize(width: 300, height: nil)
        #expect(view.size(for: proposal, hugging: false).width == 300)
        #expect(view.size(for: proposal, hugging: true).width == view.layout(width: 300).size.width)
        #expect(view.size(for: proposal, hugging: true).width < 100)
    }

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

    @Test @MainActor func aMultiRunListMarkerSharesTheFirstTextLineAtTheGutter() throws {
        for marker in ["1.", "-"] {
            let text = render("\(marker) one **bold** and a [link](https://example.com) plus enough words to wrap past the column " +
                              String(repeating: "more words ", count: 20)).text
            text.enumerateAttribute(.paragraphStyle, in: NSRange(location: 0, length: text.length)) { value, _, _ in
                #expect((value as? NSParagraphStyle)?.firstLineHeadIndent == 0)
            }
            let storage = NSTextStorage(attributedString: text)
            let layout = NSLayoutManager()
            storage.addLayoutManager(layout)
            let container = MessageTextContainer()
            container.limit = 560
            layout.addTextContainer(container)
            layout.ensureLayout(for: container)
            let markerGlyph = layout.glyphIndexForCharacter(at: 0)
            let wordGlyph = layout.glyphIndexForCharacter(at: (text.string as NSString).range(of: "one").location)
            var markerLine = NSRange()
            var wordLine = NSRange()
            let used = layout.lineFragmentUsedRect(forGlyphAt: markerGlyph, effectiveRange: &markerLine)
            _ = layout.lineFragmentUsedRect(forGlyphAt: wordGlyph, effectiveRange: &wordLine)
            #expect(markerLine == wordLine)
            #expect(used.minX == 0)
            var fragments = 0
            layout.enumerateLineFragments(forGlyphRange: layout.glyphRange(for: container)) { _, _, _, _, _ in
                fragments += 1
            }
            #expect(fragments > 1)
        }
    }

    @Test func aNumberAfterExpandedPointsStartsAnOrderedList() {
        let lines = render("1. first\n\nLead: a · b · c\n2. second").text.string.components(separatedBy: "\n")
        #expect(lines.contains("2.\tsecond"))
    }

    @Test func agentSectionsRenderBulletsAndTheirWrittenOrdinals() {
        let sample = "AGENDA\n• 08:45 first thing\n• 10:00 second thing\n\nNEWS\nSoftware\n1. Item one text.\nhttps://example.com/one\n\nResearch\n2. Item two text.\nhttps://example.com/two\n\nTools\n3. Item three text."
        let lines = render(sample).text.string.components(separatedBy: "\n")
        #expect(lines.contains("•\t08:45 first thing"))
        #expect(lines.contains("•\t10:00 second thing"))
        #expect(lines.contains(where: { $0.hasPrefix("1.\tItem one text.") }))
        #expect(lines.contains(where: { $0.hasPrefix("2.\tItem two text.") }))
        #expect(lines.contains("3.\tItem three text."))
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
        let firstRun = (text.string as NSString).range(of: "Operating").location
        let laterRun = (text.string as NSString).range(of: "cash").location
        let firstCell = paragraphStyle(text, at: firstRun)?.textBlocks.last
        let laterCell = paragraphStyle(text, at: laterRun)?.textBlocks.last
        #expect(firstCell === laterCell)
        let header = text.attribute(.font, at: 0, effectiveRange: nil) as? NSFont
        #expect(header?.fontDescriptor.symbolicTraits.contains(.bold) == true)
    }

    @Test func aCodeBlockIsOneMonospacedSegmentWithItsCodeForCopy() throws {
        let rendered = render("Before.\n\n```swift\nlet x = 1\nlet y = 2\n```\n\nAfter.")
        #expect(rendered.text.string == "Before.\nlet x = 1\nlet y = 2\nAfter.")
        let code = try #require(rendered.codeBlocks.first)
        #expect(code.code == "let x = 1\nlet y = 2")
        #expect(code.language == "swift")
        #expect(rendered.segments.map(\.kind) == [.prose, .code, .prose])
        #expect(rendered.segments[1].range == code.range)
        let font = rendered.text.attribute(.font, at: 8, effectiveRange: nil) as? NSFont
        #expect(font?.isFixedPitch == true)
    }

    @Test func segmentsCoverTheTextEndToEnd() {
        let rendered = render("Para.\n\n```\na\n```\n\n| H |\n|---|\n| 1 |\n\n```\nb\n```")
        #expect(rendered.segments.map(\.kind) == [.prose, .code, .table, .code])
        var at = 0
        for segment in rendered.segments {
            #expect(segment.range.location == at)
            at = NSMaxRange(segment.range)
        }
        #expect(at == rendered.text.length)
    }

    @Test func codeBlockCarriesTheTokenColours() throws {
        let source = "def f():\n    return 'x' # note\n"
        let rendered = render("```python\n\(source)```")
        let text = rendered.text.string as NSString
        func color(_ word: String) -> NSColor? {
            rendered.text.attribute(.foregroundColor, at: text.range(of: word).location, effectiveRange: nil) as? NSColor
        }
        #expect(color("def") == MacColor.codeKeyword)
        #expect(color("'x'") == MacColor.codeString)
        #expect(color("# note") == MacColor.codeComment)
        #expect(color("f()") == MacColor.textPrimary)
    }

    /// Spec §3.1: the tokeniser runs on finished blocks only, not on every token of a reply streaming through code.
    @Test func aCodeBlockStillStreamingStaysPlain() {
        let rendered = render("```python\ndef f():\n    return 1\n")
        #expect(MarkdownAttributed.endsInsideCodeFence("```python\ndef f():\n"))
        #expect(!MarkdownAttributed.endsInsideCodeFence("```python\ndef f():\n```\n"))
        #expect(rendered.text.attribute(.foregroundColor, at: 0, effectiveRange: nil) as? NSColor == MacColor.textPrimary)
    }

    @Test func aLanguageIsTheFirstWordOfTheInfoString() {
        #expect(render("```python title=x\nx = 1\n```").codeBlocks.first?.language == "python")
        #expect(render("```\nx = 1\n```").codeBlocks.first?.language == nil)
    }

    @Test func aDiffsRemovedLinesAreNotKeywords() {
        let rendered = render("```diff\n-old\n+new\n```")
        #expect(rendered.text.attribute(.foregroundColor, at: 0, effectiveRange: nil) as? NSColor == MacColor.codeNumber)
        #expect(rendered.text.attribute(.foregroundColor, at: 5, effectiveRange: nil) as? NSColor == MacColor.codeString)
    }

    @Test func anUnknownLanguageIsPlain() {
        let rendered = render("```brainfuck\ndef x\n```")
        #expect(rendered.text.attribute(.foregroundColor, at: 0, effectiveRange: nil) as? NSColor == MacColor.textPrimary)
    }

    /// Review Focus 1: a very long code line scrolls inside its block, it never widens the column or clips.
    @Test func aLongCodeLineStaysInsideTheColumn() throws {
        let line = String(repeating: "x", count: 400)
        let rendered = render("Before.\n\n```\n\(line)\n```")
        let layout = MarkdownAttributed.Measure(rendered).layout(width: 620)
        #expect(layout.size.width <= 620)
        let code = try #require(layout.placed.last)
        #expect(code.frame.width == 620)
        #expect(code.content.width > 2000)        // one line, not wrapped: the block scrolls
        #expect(code.content.height < 30)
    }

    /// Review Focus 1, and the plan's pin: twelve columns neither squeeze nor widen the column.
    @Test func aWideTableStaysInsideTheColumn() throws {
        let columns = (1...12).map { "Column \($0)" }
        let row = "| " + columns.joined(separator: " | ") + " |"
        let rule = "|" + String(repeating: "---|", count: 12)
        let rendered = render("Before.\n\n\(row)\n\(rule)\n\(row)")
        let layout = MarkdownAttributed.Measure(rendered).layout(width: 620)
        #expect(layout.size.width <= 620)
        let table = try #require(layout.placed.last)
        #expect(table.frame.width == 620)
        #expect(table.content.width > 620)
        // Each cell on its one line: two rows' worth of height, however many columns.
        let font = NSFont.preferredFont(forTextStyle: .subheadline)
        #expect(table.content.height < 2 * (font.pointSize * 2 + 2 * Design.Space.s8))
    }

    @Test func aNarrowTableHugsItsColumns() throws {
        let rendered = render("| A | B |\n|---|---|\n| 1 | 2 |")
        let layout = MarkdownAttributed.Measure(rendered).layout(width: 620)
        let table = try #require(layout.placed.first)
        #expect(table.frame.width < 200)
        #expect(table.frame.width == table.content.width)
    }

    /// Regression (driven 2026-11-04): TextKit sized a table against the narrow container before it, then moved it on
    /// to its own: the first cell kept that squeezed line ("Col" / "A") and the block clipped its last row.
    @Test func aTableAfterTextKeepsItsCellsOnOneLine() throws {
        let columns = (1...12).map { "Col \($0)" }
        let row = "| " + columns.joined(separator: " | ") + " |"
        let rule = "|" + String(repeating: "---|", count: 12)
        let rendered = render("Before.\n\n\(row)\n\(rule)\n\(row)")
        let storage = NSTextStorage(attributedString: rendered.text)
        let layout = NSLayoutManager()
        storage.addLayoutManager(layout)
        var containers: [MessageTextContainer] = []
        for (index, segment) in rendered.segments.enumerated() {
            let container = MessageTextContainer()
            container.end = index == rendered.segments.count - 1 ? .max : NSMaxRange(segment.range)
            if segment.kind == .prose { container.limit = 300 }
            layout.addTextContainer(container)
            containers.append(container)
        }
        layout.ensureLayout(for: containers[containers.count - 1])
        let table = rendered.segments[1].range
        #expect(layout.glyphRange(for: containers[0]).length == table.location)
        let first = layout.glyphRange(forCharacterRange: NSRange(location: table.location, length: 5),
                                      actualCharacterRange: nil)
        var fragments = 0
        layout.enumerateLineFragments(forGlyphRange: first) { _, _, _, _, _ in fragments += 1 }
        #expect(fragments == 1)
        let measured = MarkdownAttributed.Measure(rendered).layout(width: 300).placed[1].content.height
        #expect(layout.usedRect(for: containers[1]).height.rounded(.up) == measured)
    }

    @Test func numericColumnsSitRightAndTheHeaderIsQuiet() throws {
        let text = render("| Account | Total |\n|---|---|\n| Operating | 1,204.50 |\n| Reserve | -3% |").text
        let string = text.string as NSString
        #expect(paragraphStyle(text, at: string.range(of: "1,204.50").location)?.alignment == .right)
        #expect(paragraphStyle(text, at: string.range(of: "Total").location)?.alignment == .right)
        #expect(paragraphStyle(text, at: string.range(of: "Operating").location)?.alignment != .right)
        let header = text.attribute(.foregroundColor, at: 0, effectiveRange: nil) as? NSColor
        #expect(header == MacColor.textSecondary)
        let figure = text.attribute(.font, at: string.range(of: "1,204.50").location, effectiveRange: nil) as? NSFont
        #expect(figure?.pointSize == NSFont.preferredFont(forTextStyle: .subheadline).pointSize)
    }

    @Test func hairlinesRunBetweenRowsOnly() throws {
        let text = render("| A |\n|---|\n| 1 |\n| 2 |").text
        let string = text.string as NSString
        func cell(_ word: String) throws -> NSTextTableBlock {
            try #require(paragraphStyle(text, at: string.range(of: word).location)?.textBlocks.last as? NSTextTableBlock)
        }
        #expect(try cell("A").width(for: .border, edge: .maxY) == 1)
        #expect(try cell("A").width(for: .border, edge: .minX) == 0)
        #expect(try cell("2").width(for: .border, edge: .maxY) == 0)
    }

    /// Regression (driven 2026-11-04): the paragraph's break took the last run's background, so inline code at the end
    /// of a line painted to the column's edge.
    @Test func inlineCodeIsAPillThatStopsWithItsText() throws {
        let text = render("Run `make`\n\nThen stop.").text
        #expect(text.attribute(.inlineCode, at: 4, effectiveRange: nil) != nil)
        #expect(text.attribute(.backgroundColor, at: 4, effectiveRange: nil) as? NSColor == MacColor.elevated)
        let end = (text.string as NSString).range(of: "\n").location
        #expect(text.attribute(.backgroundColor, at: end, effectiveRange: nil) == nil)
        #expect(text.attribute(.inlineCode, at: end, effectiveRange: nil) == nil)
    }

    @Test func typeFollowsTheSpec() throws {
        let text = render("# One\n\n## Two\n\n### Three\n\nBody text.").text
        let string = text.string as NSString
        let body = try #require(text.attribute(.font, at: string.range(of: "Body").location, effectiveRange: nil) as? NSFont)
        let sizes = try ["One", "Two", "Three"].map { word in
            try #require(text.attribute(.font, at: string.range(of: word).location, effectiveRange: nil) as? NSFont).pointSize
        }
        #expect(sizes == [(body.pointSize * 1.25).rounded(), (body.pointSize * 1.12).rounded(),
                          (body.pointSize * 1.02).rounded()])
        let paragraph = try #require(paragraphStyle(text, at: string.range(of: "Body").location))
        #expect(paragraph.lineSpacing == Design.Text.lineSpacing)
        let two = try #require(paragraphStyle(text, at: string.range(of: "Two").location))
        #expect(two.paragraphSpacingBefore == sizes[1])
        #expect(abs(two.paragraphSpacing - sizes[1] * 0.35) < 0.01)
        let code = render("Use `x`.").text
        let mono = try #require(code.attribute(.font, at: 4, effectiveRange: nil) as? NSFont)
        #expect(abs(mono.pointSize - body.pointSize * 0.88) < 0.01)
    }

    @Test func aListGivesWayWithAParagraphsSpace() {
        let text = render("- one\n- two\n\nAfter.").text
        let two = (text.string as NSString).range(of: "two").location
        #expect(paragraphStyle(text, at: 0)?.paragraphSpacing == Design.Space.s4)
        #expect(paragraphStyle(text, at: two)?.paragraphSpacing == Design.Text.paragraphSpacing)
    }

    @Test func aQuoteHasABarAndLinksStayLinks() throws {
        let quote = render("> quoted words").text
        let block = try #require(paragraphStyle(quote, at: 0)?.textBlocks.first)
        #expect(block.width(for: .border, edge: .minX) == 3)
        // Regression (driven 2026-11-04): with no width of its own, the block held only the quote's first line.
        #expect(block.value(for: .width) == 100)
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

    /// The bug: Foundation hands a rule over as "———", and the Mac drew those three dashes as text.
    @Test @MainActor func aRuleIsALineNotThreeDashes() {
        let rendered = MarkdownAttributed.render("Above\n\n---\n\nBelow")
        #expect(!rendered.text.string.contains("\u{2014}"))
        var rules = 0
        rendered.text.enumerateAttribute(.thematicRule, in: NSRange(location: 0, length: rendered.text.length)) { value, _, _ in
            if value != nil { rules += 1 }
        }
        #expect(rules == 1)
        #expect(rendered.text.string.hasPrefix("Above"))
        #expect(rendered.text.string.hasSuffix("Below"))
    }
}

/// RenderCache (macOS/MessageTextView.swift): a message rendered once is not rendered again while it is kept, a
/// different appearance or scale renders anew, and the oldest entries go first past the capacity.
struct RenderCacheTests {
    @Test @MainActor func aKeptMessageIsNotRenderedAgain() {
        var renders = 0
        let key = RenderCache.Key(source: "cache-\(UUID())", markdown: true, scale: 1, appearance: .aqua)
        for _ in 0..<3 {
            _ = RenderCache.rendered(key) {
                renders += 1
                return MarkdownAttributed.render(key.source)
            }
        }
        #expect(renders == 1)
        let dark = RenderCache.Key(source: key.source, markdown: true, scale: 1, appearance: .darkAqua)
        _ = RenderCache.rendered(dark) {
            renders += 1
            return MarkdownAttributed.render(key.source)
        }
        #expect(renders == 2)
    }

    @Test @MainActor func theOldestGoFirstPastTheCapacity() {
        let first = RenderCache.Key(source: "oldest-\(UUID())", markdown: false, scale: 1, appearance: .aqua)
        _ = RenderCache.rendered(first) { .empty }
        for index in 0..<RenderCache.capacity {
            let key = RenderCache.Key(source: "filler-\(index)-\(UUID())", markdown: false, scale: 1, appearance: .aqua)
            _ = RenderCache.rendered(key) { .empty }
        }
        var renderedAgain = false
        _ = RenderCache.rendered(first) {
            renderedAgain = true
            return .empty
        }
        #expect(renderedAgain)
    }
}

/// Review 2026-11-26: two messages whose sources differ only in how "é" is written must not share a rendering.
struct RenderCacheBytesTests {
    @Test @MainActor func canonicallyEqualSourcesAreRenderedApart() {
        let composed = "```\nlet name = \"caf\u{E9}-\(UUID())\"\n```"
        let decomposed = composed.replacingOccurrences(of: "\u{E9}", with: "e\u{301}")
        #expect(composed == decomposed)          // String equality is canonical: the reason the key compares bytes
        let one = RenderCache.Key(source: composed, markdown: false, scale: 1, appearance: .aqua)
        let two = RenderCache.Key(source: decomposed, markdown: false, scale: 1, appearance: .aqua)
        #expect(one != two)
        let first = RenderCache.rendered(one) { MarkdownAttributed.operatorText(composed) }
        let second = RenderCache.rendered(two) { MarkdownAttributed.operatorText(decomposed) }
        #expect(first.codeBlocks.first?.code.unicodeScalars.contains("\u{301}") == false)
        #expect(second.codeBlocks.first?.code.unicodeScalars.contains("\u{301}") == true)
    }
}
