import AppKit
import SHAFTKit

/// Markdown as AppKit text, for a message's text view on the Mac (MessageTextView): the same blocks MarkdownUI draws on
/// iOS (Shared/Markdown), as one attributed string, so a selection runs across paragraphs, lists, tables and code.
///
/// - Headings step down by level and stay at least as large as a paragraph, with more space above than below.
/// - Lists indent by level, with their bullet or number; a task item's `[ ]` / `[x]` becomes a box before its text.
/// - Code blocks and inline code are monospaced on a tinted block; a quote has a bar at its left.
/// - Tables keep their columns (an `NSTextTable`), the header row bold.
/// - Links are links (a click opens them).
///
/// It uses Foundation's own Markdown parser (`AttributedString(markdown:)`, full syntax): the blocks come from each
/// run's `presentationIntent`, the inline styles from its `inlinePresentationIntent`.
enum MarkdownAttributed {
    /// The ranges of the code blocks in the result, so a right-click inside one offers Copy code.
    struct Rendered {
        let text: NSAttributedString
        let codeBlocks: [(range: NSRange, code: String)]
    }

    static let codeBackground = NSColor.secondaryLabelColor.withAlphaComponent(0.12)
    static let barColor = NSColor.secondaryLabelColor.withAlphaComponent(0.5)

    static func render(_ markdown: String, font: NSFont = .preferredFont(forTextStyle: .body)) -> Rendered {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .full,
                                                              failurePolicy: .returnPartiallyParsedIfPossible)
        guard let parsed = try? AttributedString(markdown: markdown, options: options) else {
            return Rendered(text: NSAttributedString(string: markdown, attributes: [.font: font,
                                                                                   .foregroundColor: NSColor.labelColor]),
                            codeBlocks: [])
        }
        var builder = Builder(font: font)
        for run in parsed.runs {
            builder.add(String(parsed[run.range].characters), intent: run.presentationIntent,
                        inline: run.inlinePresentationIntent, link: run.link)
        }
        return builder.finish()
    }

    /// Plain text as the operator wrote it (`OperatorText`): quoted lines as a quote, fenced code as code, the rest
    /// with its line breaks. Not Markdown (§4.4 `user.text`).
    static func operatorText(_ text: String, font: NSFont = .preferredFont(forTextStyle: .body)) -> Rendered {
        let out = NSMutableAttributedString()
        var codeBlocks: [(NSRange, String)] = []
        let blocks = OperatorText.blocks(text)
        for (index, block) in blocks.enumerated() {
            let last = index == blocks.count - 1
            switch block {
            case .text(let body):
                let style = NSMutableParagraphStyle()
                style.paragraphSpacing = last ? 0 : font.pointSize * 0.4
                out.append(NSAttributedString(string: body + (last ? "" : "\n"), attributes: [
                    .font: font, .foregroundColor: NSColor.labelColor, .paragraphStyle: style,
                ]))
            case .quote(let body):
                let block = quoteBlock(font: font)
                let style = NSMutableParagraphStyle()
                style.textBlocks = [block]
                out.append(NSAttributedString(string: body + "\n", attributes: [
                    .font: font, .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: style,
                ]))
            case .code(let body):
                let mono = NSFont.monospacedSystemFont(ofSize: font.pointSize * 0.9, weight: .regular)
                let style = NSMutableParagraphStyle()
                style.textBlocks = [codeBlock(font: font)]
                let start = out.length
                out.append(NSAttributedString(string: body + "\n", attributes: [
                    .font: mono, .foregroundColor: NSColor.labelColor, .paragraphStyle: style,
                ]))
                codeBlocks.append((NSRange(location: start, length: out.length - start), body))
            }
        }
        trimTrailingNewline(out)
        return Rendered(text: out, codeBlocks: codeBlocks)
    }

    static func codeBlock(font: NSFont) -> NSTextBlock {
        let block = NSTextBlock()
        block.backgroundColor = codeBackground
        block.setValue(100, type: .percentageValueType, for: .width)
        block.setWidth(font.pointSize * 0.7, type: .absoluteValueType, for: .padding)
        block.setWidth(font.pointSize * 0.5, type: .absoluteValueType, for: .margin, edge: .maxY)
        return block
    }

    static func quoteBlock(font: NSFont) -> NSTextBlock {
        let block = NSTextBlock()
        block.setWidth(3, type: .absoluteValueType, for: .border, edge: .minX)
        block.setBorderColor(barColor, for: .minX)
        block.setWidth(font.pointSize * 0.7, type: .absoluteValueType, for: .padding, edge: .minX)
        block.setWidth(font.pointSize * 0.4, type: .absoluteValueType, for: .margin, edge: .maxY)
        return block
    }

    /// No break after the last paragraph: TextKit would lay out an empty line there.
    fileprivate static func trimTrailingNewline(_ text: NSMutableAttributedString) {
        while text.length > 0, text.string.hasSuffix("\n") {
            text.deleteCharacters(in: NSRange(location: text.length - 1, length: 1))
        }
    }

    /// Lays the runs out block by block.
    private struct Builder {
        let font: NSFont
        let out = NSMutableAttributedString()
        var codeBlocks: [(NSRange, String)] = []
        /// The leaf block the last run belonged to; a new one starts a new paragraph.
        var lastBlock: Int?
        /// The list items whose marker is drawn (an item's later paragraphs have none).
        var markedItems = Set<Int>()
        var tables: [Int: (table: NSTextTable, rows: [Int: Int])] = [:]
        var quoteBlocks: [Int: NSTextBlock] = [:]
        var codeBlockObjects: [Int: NSTextBlock] = [:]
        var openCode: (identity: Int, start: Int)?

        init(font: NSFont) { self.font = font }

        mutating func add(_ text: String, intent: PresentationIntent?, inline: InlinePresentationIntent?, link: URL?) {
            let components = intent?.components ?? []
            let leaf = components.first?.identity
            var text = text
            var newBlock = false
            if leaf != lastBlock {
                newBlock = true
                closeCode(unless: leaf)
                // The break ends the paragraph before, in its style (a table cell is a paragraph of its own).
                if out.length > 0, !out.string.hasSuffix("\n") {
                    let before = out.attributes(at: out.length - 1, effectiveRange: nil)
                    out.append(NSAttributedString(string: "\n", attributes: before.filter { $0.key != .link }))
                }
                lastBlock = leaf
            }
            let style = paragraphStyle(components)
            var attributes: [NSAttributedString.Key: Any] = [.paragraphStyle: style.paragraph,
                                                              .foregroundColor: style.color]
            attributes[.font] = runFont(base: style.font, inline: inline, code: style.code)
            if let inline, inline.contains(.strikethrough) {
                attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue
            }
            if let inline, inline.contains(.code), !style.code {
                attributes[.backgroundColor] = MarkdownAttributed.codeBackground
            }
            if let link { attributes[.link] = link }
            if newBlock {
                if let marker = style.marker {
                    // A task item's box is drawn before its text, as the web does.
                    var box = ""
                    for (prefix, symbol) in [("[ ] ", "\u{2610} "), ("[x] ", "\u{2611} "), ("[X] ", "\u{2611} ")]
                        where text.hasPrefix(prefix) {
                        text = String(text.dropFirst(prefix.count))
                        box = symbol
                    }
                    var markerAttributes = attributes
                    markerAttributes[.font] = style.font
                    markerAttributes.removeValue(forKey: .link)
                    // A task item shows its box where the bullet was (full width, the box before the text).
                    let drawn = box.isEmpty || style.ordered ? marker : box.trimmingCharacters(in: .whitespaces)
                    out.append(NSAttributedString(string: drawn + "\t" + (drawn == marker ? box : ""),
                                                  attributes: markerAttributes))
                }
                if style.code, let identity = style.codeIdentity { openCode = (identity, out.length) }
            }
            out.append(NSAttributedString(string: text, attributes: attributes))
        }

        mutating func finish() -> Rendered {
            closeCode(unless: nil)
            MarkdownAttributed.trimTrailingNewline(out)
            return Rendered(text: out, codeBlocks: codeBlocks)
        }

        private mutating func closeCode(unless leaf: Int?) {
            guard let open = openCode, open.identity != leaf else { return }
            let range = NSRange(location: open.start, length: out.length - open.start)
            var code = (out.string as NSString).substring(with: range)
            if code.hasSuffix("\n") { code.removeLast() }
            codeBlocks.append((range, code))
            openCode = nil
        }

        private func runFont(base: NSFont, inline: InlinePresentationIntent?, code: Bool) -> NSFont {
            guard let inline else { return base }
            var font = base
            if inline.contains(.code), !code {
                font = .monospacedSystemFont(ofSize: base.pointSize * 0.92, weight: .regular)
            }
            var traits: NSFontDescriptor.SymbolicTraits = []
            if inline.contains(.stronglyEmphasized) { traits.insert(.bold) }
            if inline.contains(.emphasized) { traits.insert(.italic) }
            if !traits.isEmpty {
                let descriptor = font.fontDescriptor.withSymbolicTraits(font.fontDescriptor.symbolicTraits.union(traits))
                font = NSFont(descriptor: descriptor, size: font.pointSize) ?? font
            }
            return font
        }

        struct Style {
            var paragraph: NSParagraphStyle
            var font: NSFont
            var color: NSColor
            var marker: String?
            var ordered = false
            var code = false
            var codeIdentity: Int?
            var cell = false
        }

        /// The paragraph a leaf block makes, from its components (innermost first).
        private mutating func paragraphStyle(_ components: [PresentationIntent.IntentType]) -> Style {
            let paragraph = NSMutableParagraphStyle()
            let size = font.pointSize
            var style = Style(paragraph: paragraph, font: font, color: .labelColor)
            paragraph.paragraphSpacing = size * 0.6
            paragraph.lineSpacing = size * 0.12
            var blocks: [NSTextBlock] = []
            var listDepth = 0
            var markerFor: (item: Int, ordinal: Int)?
            var ordered = false
            var sawOrdered: Bool?

            for component in components {
                switch component.kind {
                case .header(let level):
                    let scale: CGFloat = [1.35, 1.2, 1.1, 1, 1, 1][min(max(level, 1), 6) - 1]
                    let weight: NSFont.Weight = level <= 2 ? .bold : .semibold
                    style.font = .systemFont(ofSize: (size * scale).rounded(), weight: weight)
                    if level == 6 { style.color = .secondaryLabelColor }
                    paragraph.paragraphSpacingBefore = size * (level <= 2 ? 0.9 : 0.7)
                    paragraph.paragraphSpacing = size * 0.4
                case .codeBlock:
                    style.font = .monospacedSystemFont(ofSize: size * 0.88, weight: .regular)
                    style.code = true
                    style.codeIdentity = component.identity
                    let block = codeBlockObjects[component.identity] ?? MarkdownAttributed.codeBlock(font: font)
                    codeBlockObjects[component.identity] = block
                    blocks.insert(block, at: 0)
                    paragraph.paragraphSpacing = 0
                    paragraph.lineSpacing = size * 0.1
                case .blockQuote:
                    style.color = .secondaryLabelColor
                    let block = quoteBlocks[component.identity] ?? MarkdownAttributed.quoteBlock(font: font)
                    quoteBlocks[component.identity] = block
                    blocks.insert(block, at: 0)
                case .listItem(let ordinal):
                    listDepth += 1
                    if markerFor == nil, !markedItems.contains(component.identity) {
                        markerFor = (component.identity, ordinal)
                    } else if markerFor == nil {
                        markerFor = (-1, 0)
                    }
                case .orderedList:
                    if sawOrdered == nil { sawOrdered = true }
                case .unorderedList:
                    if sawOrdered == nil { sawOrdered = false }
                case .table(let columns):
                    if tables[component.identity] == nil {
                        let table = makeTable()
                        table.numberOfColumns = max(columns.count, 1)
                        tables[component.identity] = (table, [:])
                    }
                case .tableCell(let column):
                    style.cell = true
                    paragraph.paragraphSpacing = 0
                    // The row and the table are further out: found below, applied after the loop.
                    tableCell = column
                case .tableHeaderRow:
                    tableRowIdentity = component.identity
                    headerRow = true
                case .tableRow:
                    tableRowIdentity = component.identity
                default:
                    break
                }
            }
            ordered = sawOrdered ?? false
            style.ordered = ordered

            if style.cell, let column = tableCell,
               let tableID = components.first(where: { if case .table = $0.kind { true } else { false } })?.identity,
               var entry = tables[tableID], let rowID = tableRowIdentity {
                let row = entry.rows[rowID] ?? entry.rows.count
                entry.rows[rowID] = row
                entry.table.numberOfColumns = max(entry.table.numberOfColumns, column + 1)
                tables[tableID] = entry
                let cell = NSTextTableBlock(table: entry.table, startingRow: row, rowSpan: 1, startingColumn: column,
                                            columnSpan: 1)
                cell.setWidth(1, type: .absoluteValueType, for: .border)
                cell.setBorderColor(.separatorColor)
                cell.setWidth(size * 0.35, type: .absoluteValueType, for: .padding)
                if headerRow {
                    style.font = .systemFont(ofSize: size, weight: .semibold)
                    cell.backgroundColor = MarkdownAttributed.codeBackground.withAlphaComponent(0.06)
                }
                blocks.append(cell)
            }
            tableCell = nil
            tableRowIdentity = nil
            headerRow = false

            if listDepth > 0 {
                let step = size * 1.5
                let indent = step * CGFloat(listDepth)
                paragraph.headIndent = indent
                paragraph.firstLineHeadIndent = indent - step
                paragraph.tabStops = [NSTextTab(textAlignment: .left, location: indent)]
                paragraph.paragraphSpacing = size * 0.25
                if let marker = markerFor, marker.item >= 0 {
                    markedItems.insert(marker.item)
                    style.marker = ordered ? "\(marker.ordinal)." : (listDepth > 1 ? "\u{25E6}" : "\u{2022}")
                } else {
                    paragraph.firstLineHeadIndent = indent
                }
            }
            paragraph.textBlocks = blocks
            style.paragraph = paragraph
            return style
        }

        private var tableCell: Int?
        private var tableRowIdentity: Int?
        private var headerRow = false

        private func makeTable() -> NSTextTable {
            let table = NSTextTable()
            table.numberOfColumns = 1
            table.layoutAlgorithm = .automaticLayoutAlgorithm
            table.collapsesBorders = true
            table.hidesEmptyCells = false
            table.setWidth(font.pointSize * 0.3, type: .absoluteValueType, for: .margin, edge: .minY)
            table.setWidth(font.pointSize * 0.7, type: .absoluteValueType, for: .margin, edge: .maxY)
            return table
        }
    }
}
