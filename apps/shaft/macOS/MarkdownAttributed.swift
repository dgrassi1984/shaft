import AppKit
import SwiftUI
import SHAFTKit

/// The colour tokens as AppKit colours: the same catalog sets as `Color.canvas` and the rest (Shared/Design), which
/// follow the appearance as they draw. The unit tests' bundle has no asset catalog: the second value is for them.
enum MacColor {
    private static func token(_ name: String, tests: NSColor) -> NSColor { NSColor(named: name) ?? tests }

    static let canvas = token("canvas", tests: .textBackgroundColor)
    static let elevated = token("elevated", tests: .controlBackgroundColor)
    static let hairline = token("hairline", tests: .separatorColor)
    static let textPrimary = token("textPrimary", tests: .labelColor)
    static let textSecondary = token("textSecondary", tests: .secondaryLabelColor)
    static let textTertiary = token("textTertiary", tests: .tertiaryLabelColor)
    static let brand = token("accent", tests: .linkColor)
    static let onAccent = token("onAccent", tests: .white)
    static var codeKeyword: NSColor { token("codeKeyword", tests: NSColor(Color.codeKeyword)) }
    static var codeString: NSColor { token("codeString", tests: NSColor(Color.codeString)) }
    static var codeComment: NSColor { token("codeComment", tests: NSColor(Color.codeComment)) }
    static var codeNumber: NSColor { token("codeNumber", tests: NSColor(Color.codeNumber)) }
}

extension NSAttributedString.Key {
    /// On inline code: the layout manager draws its background as a rounded pill.
    static let inlineCode = NSAttributedString.Key("io.github.dgrassi1984.shaft.inlineCode")
    /// On a Markdown rule's paragraph: the layout manager draws it as a hairline across the column.
    static let thematicRule = NSAttributedString.Key("io.github.dgrassi1984.shaft.thematicRule")
}

/// Markdown as AppKit text, for a message's text view on the Mac (MessageTextView): the same blocks as iOS
/// (Shared/Markdown), as one attributed string, so a selection runs across paragraphs, lists, tables and code.
///
/// - Type is spec §2.2: `Design.Text`'s line and paragraph spacing, its heading scales, code at 0.88 em.
/// - Headings step down by level and stay at least as large as a paragraph, with more space above than below.
/// - Agent bullets and lines of points are normalized, and sequential numbers can interrupt a paragraph.
/// - Lists indent by level, with their bullet or number; a task item's `[ ]` / `[x]` becomes a box before its text.
/// - Code blocks are monospaced with their syntax colours; inline code is a pill; a quote has a bar at its left.
/// - Tables keep their columns (an `NSTextTable`) at the width their text wants, in `.subheadline` with tabular
///   figures, numeric columns to the right, the header in `textSecondary`, hairlines between rows only.
/// - Links are links (a click opens them).
///
/// A code block and a table draw in a block of their own that scrolls sideways (`Segment`, MessageNSView); the text
/// around them is laid out to the column. It stays one string in one layout: the segments are its text containers.
///
/// It uses Foundation's own Markdown parser (`AttributedString(markdown:)`, full syntax): the blocks come from each
/// run's `presentationIntent`, the inline styles from its `inlinePresentationIntent`.
enum MarkdownAttributed {
    /// A fenced block in the text. `range` is where it lies, so a right-click inside one offers Copy code.
    struct CodeBlock {
        let range: NSRange
        let code: String
        let language: String?
    }

    /// A table in the text, and the width its columns want (it scrolls when that is wider than the column).
    struct Table {
        let range: NSRange
        let width: CGFloat
    }

    /// A stretch of the text laid out on its own: running text at the column's width, or a block that scrolls.
    struct Segment: Equatable {
        enum Kind: Equatable { case prose, code, table }
        let kind: Kind
        let range: NSRange
    }

    struct Rendered {
        let text: NSAttributedString
        var textScale: CGFloat = 1
        let codeBlocks: [CodeBlock]
        let tables: [Table]
        /// The text in order, end to end. Each but the last ends with its paragraph's break.
        let segments: [Segment]

        static var empty: Rendered { Rendered(text: NSAttributedString(), codeBlocks: [], tables: [], segments: []) }
    }

    /// Between a block and the text around it, inside a block, and the height of a code block's header row.
    static let blockGap = Design.Space.s12
    static let blockPadding = Design.Space.s12
    static let codeHeader = Design.Space.s24 + Design.Space.s8
    /// Wider than any line a block lays out: its text wraps only at a real line break.
    static let unwrapped: CGFloat = 200_000
    /// A table's cell wraps here rather than make a line a screen wide (the same cap as iOS's).
    static let widestCell: CGFloat = 280

    static func render(_ markdown: String, font: NSFont = .preferredFont(forTextStyle: .body),
                       scale: CGFloat = 1) -> Rendered {
        let font = scale == 1 ? font : (NSFont(descriptor: font.fontDescriptor, size: font.pointSize * scale) ?? font)
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .full,
                                                              failurePolicy: .returnPartiallyParsedIfPossible)
        guard let parsed = try? AttributedString(markdown: TableColumns.aligned(AgentMarkdown.normalized(markdown)), options: options) else {
            let text = NSMutableAttributedString(string: markdown, attributes: [.font: font,
                                                                                .foregroundColor: MacColor.textPrimary])
            return finish(text: text, codeBlocks: [], tables: [], scale: scale)
        }
        var builder = Builder(font: font, scale: scale, streamingCode: endsInsideCodeFence(markdown))
        for run in parsed.runs {
            builder.add(String(parsed[run.range].characters), intent: run.presentationIntent,
                        inline: run.inlinePresentationIntent, link: run.link)
        }
        return builder.finish()
    }

    /// Plain text as the operator wrote it (`OperatorText`): quoted lines as a quote, fenced code as code, the rest
    /// with its line breaks. Not Markdown (§4.4 `user.text`).
    static func operatorText(_ text: String, font: NSFont = .preferredFont(forTextStyle: .body),
                             scale: CGFloat = 1) -> Rendered {
        let font = scale == 1 ? font : (NSFont(descriptor: font.fontDescriptor, size: font.pointSize * scale) ?? font)
        let out = NSMutableAttributedString()
        var codeBlocks: [CodeBlock] = []
        let blocks = OperatorText.blocks(text)
        for (index, block) in blocks.enumerated() {
            let last = index == blocks.count - 1
            switch block {
            case .text(let body):
                let style = NSMutableParagraphStyle()
                style.lineSpacing = Design.Text.lineSpacing
                style.paragraphSpacing = last ? 0 : Design.Text.paragraphSpacing
                out.append(NSAttributedString(string: body + (last ? "" : "\n"), attributes: [
                    .font: font, .foregroundColor: MacColor.textPrimary, .paragraphStyle: style,
                ]))
            case .quote(let body):
                let block = quoteBlock()
                let style = NSMutableParagraphStyle()
                style.textBlocks = [block]
                style.lineSpacing = Design.Text.lineSpacing
                out.append(NSAttributedString(string: body + "\n", attributes: [
                    .font: font, .foregroundColor: MacColor.textSecondary, .paragraphStyle: style,
                ]))
            case .code(let body):
                let start = out.length
                out.append(NSAttributedString(string: body + "\n", attributes: [
                    .font: codeFont(font), .foregroundColor: MacColor.textPrimary, .paragraphStyle: codeParagraph(),
                ]))
                codeBlocks.append(CodeBlock(range: NSRange(location: start, length: out.length - start), code: body,
                                            language: nil))
            }
        }
        return finish(text: out, codeBlocks: codeBlocks, tables: [], scale: scale)
    }

    static func codeFont(_ font: NSFont) -> NSFont {
        .monospacedSystemFont(ofSize: font.pointSize * codeScale, weight: .regular)
    }

    /// Spec §2.2: code is 0.88 em, fenced and inline.
    private static let codeScale: CGFloat = 0.88

    static func codeParagraph() -> NSParagraphStyle {
        let style = NSMutableParagraphStyle()
        style.lineSpacing = Design.Text.lineSpacing / 2
        return style
    }

    static func quoteBlock() -> NSTextBlock {
        let block = NSTextBlock()
        block.setValue(100, type: .percentageValueType, for: .width)     // without a width, only its first line is in it
        block.setWidth(3, type: .absoluteValueType, for: .border, edge: .minX)
        block.setBorderColor(MacColor.hairline, for: .minX)
        block.setWidth(Design.Space.s12 - 3, type: .absoluteValueType, for: .padding, edge: .minX)
        block.setWidth(Design.Text.paragraphSpacing, type: .absoluteValueType, for: .margin, edge: .maxY)
        return block
    }

    /// Whether the text stops inside a fenced block: a reply streaming through its code, whose colours wait for the
    /// block to finish (spec §3.1).
    static func endsInsideCodeFence(_ markdown: String) -> Bool {
        var fence: (marker: Character, count: Int)?
        for line in markdown.split(separator: "\n", omittingEmptySubsequences: false) {
            let body = line.drop { $0 == " " }
            guard line.count - body.count <= 3, let marker = body.first, marker == "`" || marker == "~" else { continue }
            let count = body.prefix { $0 == marker }.count
            guard count >= 3 else { continue }
            if let open = fence {
                if marker == open.marker, count >= open.count, body.dropFirst(count).allSatisfy(\.isWhitespace) {
                    fence = nil
                }
            } else {
                fence = (marker, count)
            }
        }
        return fence != nil
    }

    /// A table cell that reads as a number: "12", "1,204.50", "-3", "$40", "12%", "(7)".
    static func isNumeric(_ cell: String) -> Bool {
        let trimmed = cell.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.contains(where: \.isNumber) else { return false }
        let allowed = CharacterSet(charactersIn: "0123456789.,+-\u{2212}()%$\u{20AC}\u{00A3} ")
        return trimmed.unicodeScalars.allSatisfy(allowed.contains)
    }

    /// The text ends without a break, its blocks are cut to it, and it is split into segments.
    fileprivate static func finish(text: NSMutableAttributedString, codeBlocks: [CodeBlock],
                                   tables: [Table], scale: CGFloat = 1) -> Rendered {
        trimTrailingNewline(text)
        let all = NSRange(location: 0, length: text.length)
        let code = codeBlocks.map { CodeBlock(range: NSIntersectionRange($0.range, all), code: $0.code,
                                              language: $0.language) }
        let tables = tables.map { Table(range: NSIntersectionRange($0.range, all), width: $0.width) }
        let segments = split(length: text.length, code: code, tables: tables)
        // Space before a paragraph applies at the top of a later container but not of the first: the gap is the block's.
        for segment in segments.dropFirst() where segment.kind == .prose && segment.range.length > 0 {
            let first = (text.string as NSString).paragraphRange(for: NSRange(location: segment.range.location, length: 0))
            guard let style = text.attribute(.paragraphStyle, at: first.location, effectiveRange: nil) as? NSParagraphStyle,
                  style.paragraphSpacingBefore > 0, let copy = style.mutableCopy() as? NSMutableParagraphStyle else { continue }
            copy.paragraphSpacingBefore = 0
            text.addAttribute(.paragraphStyle, value: copy, range: first)
        }
        return Rendered(text: text, textScale: scale, codeBlocks: code, tables: tables, segments: segments)
    }

    private static func split(length: Int, code: [CodeBlock], tables: [Table]) -> [Segment] {
        let blocks = (code.map { Segment(kind: .code, range: $0.range) } + tables.map { Segment(kind: .table, range: $0.range) })
            .filter { $0.range.length > 0 }
            .sorted { $0.range.location < $1.range.location }
        var segments: [Segment] = []
        var at = 0
        for block in blocks {
            if block.range.location > at {
                segments.append(Segment(kind: .prose, range: NSRange(location: at, length: block.range.location - at)))
            }
            segments.append(block)
            at = NSMaxRange(block.range)
        }
        if at < length || segments.isEmpty {
            segments.append(Segment(kind: .prose, range: NSRange(location: at, length: length - at)))
        }
        return segments
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
        let scale: CGFloat
        /// The source stops inside its last fenced block.
        let streamingCode: Bool
        let out = NSMutableAttributedString()
        var codeBlocks: [CodeBlock] = []
        /// The leaf block the last run belonged to; a new one starts a new paragraph.
        var lastBlock: Int?
        var blockStyle: Style?
        /// The list items whose marker is drawn (an item's later paragraphs have none).
        var markedItems = Set<Int>()
        var tables: [Int: TableBuild] = [:]
        var tableOrder: [Int] = []
        var openTable: Int?
        var openCell: (table: Int, row: Int, column: Int, block: NSTextTableBlock, start: Int)?
        var quoteBlocks: [Int: NSTextBlock] = [:]
        var openCode: (identity: Int, start: Int, language: String?)?
        /// The outermost list of the last paragraph, while it was a list item.
        var openList: Int?

        /// A table as it comes in, cell by cell.
        struct TableBuild {
            let table: NSTextTable
            let alignments: [PresentationIntent.TableColumn.Alignment]
            var rows: [Int: Int] = [:]
            var cells: [(row: Int, column: Int, block: NSTextTableBlock, range: NSRange)] = []
            var start: Int?
            var end: Int?
        }

        init(font: NSFont, scale: CGFloat, streamingCode: Bool) {
            self.font = font
            self.scale = scale
            self.streamingCode = streamingCode
        }

        mutating func add(_ text: String, intent: PresentationIntent?, inline: InlinePresentationIntent?, link: URL?) {
            let components = intent?.components ?? []
            let leaf = components.first?.identity
            var text = text
            var newBlock = false
            if leaf != lastBlock {
                newBlock = true
                closeCell()
                // The break ends the paragraph before, in its style (a table cell is a paragraph of its own).
                if out.length > 0, !out.string.hasSuffix("\n") {
                    let before = out.attributes(at: out.length - 1, effectiveRange: nil)
                    out.append(NSAttributedString(string: "\n", attributes: before.filter {
                        ![.link, .backgroundColor, .inlineCode].contains($0.key)
                    }))
                }
                closeCode(unless: leaf)
                closeTable(unless: components.first(where: { if case .table = $0.kind { true } else { false } })?.identity)
                closeList(unless: components.last(where: Self.isList)?.identity)
                lastBlock = leaf
            }
            if newBlock || blockStyle == nil { blockStyle = paragraphStyle(components) }
            guard let style = blockStyle else { return }
            var attributes: [NSAttributedString.Key: Any] = [.paragraphStyle: style.paragraph,
                                                              .foregroundColor: style.color]
            attributes[.font] = runFont(base: style.font, inline: inline, code: style.code)
            if let inline, inline.contains(.strikethrough) {
                attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue
            }
            if let inline, inline.contains(.code), !style.code {
                attributes[.backgroundColor] = MacColor.elevated
                attributes[.inlineCode] = true
            }
            if let link, ChatLink.document(link) != nil || ChatLink.opensOutside(link) {
                attributes[.link] = link
            }
            if style.rule {
                // Foundation hands a rule over as three em-dashes: it is a line, not text.
                text = " "
                attributes[.thematicRule] = true
            }
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
                    markerAttributes.removeValue(forKey: .backgroundColor)
                    markerAttributes.removeValue(forKey: .inlineCode)
                    // A task item shows its box where the bullet was (full width, the box before the text).
                    let drawn = box.isEmpty || style.ordered ? marker : box.trimmingCharacters(in: .whitespaces)
                    out.append(NSAttributedString(string: drawn + "\t" + (drawn == marker ? box : ""),
                                                  attributes: markerAttributes))
                }
                if style.code, let identity = style.codeIdentity {
                    openCode = (identity, out.length, style.language)
                }
                if let cell = style.cell { openCell = (cell.table, cell.row, cell.column, cell.block, out.length) }
            }
            out.append(NSAttributedString(string: text, attributes: attributes))
        }

        mutating func finish() -> Rendered {
            closeCode(unless: nil, finished: !streamingCode)
            closeCell()
            closeTable(unless: nil)
            return MarkdownAttributed.finish(text: out, codeBlocks: codeBlocks, tables: finishTables(), scale: scale)
        }

        private mutating func closeCode(unless leaf: Int?, finished: Bool = true) {
            guard let open = openCode, open.identity != leaf else { return }
            let range = NSRange(location: open.start, length: out.length - open.start)
            var code = (out.string as NSString).substring(with: range)
            if code.hasSuffix("\n") { code.removeLast() }
            codeBlocks.append(CodeBlock(range: range, code: code, language: open.language))
            openCode = nil
            guard finished else { return }
            for token in CodeTokens.tokens(in: code, language: open.language) {
                let color: NSColor? = switch token.kind {
                case .keyword: open.language?.lowercased() == "diff" ? MacColor.codeNumber : MacColor.codeKeyword   // a removed line
                case .string: MacColor.codeString
                case .comment: MacColor.codeComment
                case .number: MacColor.codeNumber
                case .plain: nil
                }
                guard let color else { continue }
                let local = NSRange(token.range, in: code)
                out.addAttribute(.foregroundColor, value: color,
                                 range: NSRange(location: open.start + local.location, length: local.length))
            }
        }

        private mutating func closeCell() {
            guard let cell = openCell else { return }
            openCell = nil
            let range = NSRange(location: cell.start, length: out.length - cell.start)
            tables[cell.table]?.cells.append((cell.row, cell.column, cell.block, range))
            if tables[cell.table]?.start == nil { tables[cell.table]?.start = cell.start }
        }

        private mutating func closeTable(unless identity: Int?) {
            guard let table = openTable, table != identity else { return }
            tables[table]?.end = out.length
            openTable = nil
        }

        /// A list gives way to what follows it with a paragraph's space, not an item's.
        private mutating func closeList(unless identity: Int?) {
            guard let list = openList, list != identity else { return }
            openList = nil
            guard out.length > 0, let style = out.attribute(.paragraphStyle, at: out.length - 1, effectiveRange: nil)
                as? NSParagraphStyle, let copy = style.mutableCopy() as? NSMutableParagraphStyle else { return }
            copy.paragraphSpacing = Design.Text.paragraphSpacing
            let last = (out.string as NSString).paragraphRange(for: NSRange(location: out.length - 1, length: 0))
            out.addAttribute(.paragraphStyle, value: copy, range: last)
        }

        private static func isList(_ component: PresentationIntent.IntentType) -> Bool {
            switch component.kind {
            case .orderedList, .unorderedList: true
            default: false
            }
        }

        /// Each table's columns, now that all its text is in: as wide as their widest cell wants (up to the cap, past
        /// which a cell wraps), numbers to the right, a hairline under every row but the last.
        private func finishTables() -> [Table] {
            tableOrder.compactMap { identity in
                guard let table = tables[identity], let start = table.start else { return nil }
                let columns = table.table.numberOfColumns
                let header = table.cells.map(\.row).min() ?? 0
                let lastRow = table.cells.map(\.row).max() ?? 0
                var widths = [CGFloat](repeating: 0, count: columns)
                var filled = [Bool](repeating: false, count: columns)
                var numeric = [Bool](repeating: true, count: columns)
                for cell in table.cells {
                    let measured = NSMutableAttributedString(attributedString: out.attributedSubstring(from: cell.range))
                    measured.removeAttribute(.paragraphStyle, range: NSRange(location: 0, length: measured.length))
                    let width = measured.boundingRect(with: NSSize(width: unwrapped, height: unwrapped),
                                                      options: [.usesLineFragmentOrigin]).width
                    widths[cell.column] = max(widths[cell.column], min(width.rounded(.up) + 1, widestCell))
                    guard cell.row != header else { continue }
                    let text = (out.string as NSString).substring(with: cell.range)
                    guard !text.trimmingCharacters(in: .whitespaces).isEmpty else { continue }
                    filled[cell.column] = true
                    if !isNumeric(text) { numeric[cell.column] = false }
                }
                for cell in table.cells {
                    cell.block.setContentWidth(widths[cell.column], type: .absoluteValueType)
                    if cell.row != lastRow {
                        cell.block.setWidth(1, type: .absoluteValueType, for: .border, edge: .maxY)
                        cell.block.setBorderColor(MacColor.hairline, for: .maxY)
                    }
                    let alignment = alignment(column: cell.column, of: table, numeric: filled[cell.column] && numeric[cell.column])
                    guard alignment != .left else { continue }
                    let paragraph = (out.string as NSString).paragraphRange(for: cell.range)
                    guard let style = out.attribute(.paragraphStyle, at: paragraph.location, effectiveRange: nil)
                        as? NSParagraphStyle, let copy = style.mutableCopy() as? NSMutableParagraphStyle else { continue }
                    copy.alignment = alignment
                    out.addAttribute(.paragraphStyle, value: copy, range: paragraph)
                }
                let padding = Self.cellPaddingX * 2 * CGFloat(columns)
                let width = (widths.reduce(0, +) + padding).rounded(.up) + 1
                table.table.setContentWidth(width, type: .absoluteValueType)
                let range = NSRange(location: start, length: (table.end ?? out.length) - start)
                return Table(range: range, width: width)
            }
        }

        private func alignment(column: Int, of table: TableBuild, numeric: Bool) -> NSTextAlignment {
            let stated = column < table.alignments.count ? table.alignments[column] : .left
            switch stated {
            case .right: return .right
            case .center: return .center
            default: return numeric ? .right : .left
            }
        }

        private func runFont(base: NSFont, inline: InlinePresentationIntent?, code: Bool) -> NSFont {
            guard let inline else { return base }
            var font = base
            if inline.contains(.code), !code {
                font = codeFont(base)
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
            var language: String?
            var cell: (table: Int, row: Int, column: Int, block: NSTextTableBlock)?
            var rule = false
        }

        static let cellPaddingX = Design.Space.s12
        static let cellPaddingY = Design.Space.s8

        /// The paragraph a leaf block makes, from its components (innermost first).
        private mutating func paragraphStyle(_ components: [PresentationIntent.IntentType]) -> Style {
            let paragraph = NSMutableParagraphStyle()
            let size = font.pointSize
            var style = Style(paragraph: paragraph, font: font, color: MacColor.textPrimary)
            paragraph.paragraphSpacing = Design.Text.paragraphSpacing
            paragraph.lineSpacing = Design.Text.lineSpacing
            var blocks: [NSTextBlock] = []
            var listDepth = 0
            var markerFor: (item: Int, ordinal: Int)?
            var ordered = false
            var sawOrdered: Bool?
            var cellColumn: Int?
            var rowIdentity: Int?
            var headerRow = false

            for component in components {
                switch component.kind {
                case .header(let level):
                    // Spec §2.2: 1.25, 1.12, 1.02 em; 1 em above, 0.35 em below.
                    let (scale, weight): (CGFloat, NSFont.Weight) = switch level {
                    case 1: (1.25, .bold)
                    case 2: (1.12, .semibold)
                    default: (1.02, .semibold)
                    }
                    let heading = (size * scale).rounded()
                    style.font = .systemFont(ofSize: heading, weight: weight)
                    paragraph.paragraphSpacingBefore = heading * Design.Text.headingSpaceAbove
                    paragraph.paragraphSpacing = heading * Design.Text.headingSpaceBelow
                case .codeBlock(let language):
                    style.font = codeFont(font)
                    style.code = true
                    style.codeIdentity = component.identity
                    style.language = language?.split(whereSeparator: \.isWhitespace).first.map(String.init)  // "python title=x"
                    paragraph.paragraphSpacing = 0
                    paragraph.lineSpacing = Design.Text.lineSpacing / 2
                case .thematicBreak:
                    style.rule = true
                    paragraph.paragraphSpacingBefore = Design.Space.s4
                    paragraph.paragraphSpacing = Design.Space.s8
                    paragraph.lineSpacing = 0
                case .blockQuote:
                    style.color = MacColor.textSecondary
                    let block = quoteBlocks[component.identity] ?? MarkdownAttributed.quoteBlock()
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
                    openList = component.identity
                case .unorderedList:
                    if sawOrdered == nil { sawOrdered = false }
                    openList = component.identity
                case .table(let columns):
                    if tables[component.identity] == nil {
                        let table = makeTable()
                        table.numberOfColumns = max(columns.count, 1)
                        tables[component.identity] = TableBuild(table: table, alignments: columns.map(\.alignment))
                        tableOrder.append(component.identity)
                    }
                    openTable = component.identity
                case .tableCell(let column):
                    paragraph.paragraphSpacing = 0
                    paragraph.lineSpacing = 0
                    // The row and the table are further out: found below, applied after the loop.
                    cellColumn = column
                case .tableHeaderRow:
                    rowIdentity = component.identity
                    headerRow = true
                case .tableRow:
                    rowIdentity = component.identity
                default:
                    break
                }
            }
            ordered = sawOrdered ?? false
            style.ordered = ordered

            if let column = cellColumn,
               let tableID = components.first(where: { if case .table = $0.kind { true } else { false } })?.identity,
               var entry = tables[tableID], let rowID = rowIdentity {
                let row = entry.rows[rowID] ?? entry.rows.count
                entry.rows[rowID] = row
                entry.table.numberOfColumns = max(entry.table.numberOfColumns, column + 1)
                tables[tableID] = entry
                let cell = NSTextTableBlock(table: entry.table, startingRow: row, rowSpan: 1, startingColumn: column,
                                            columnSpan: 1)
                cell.setWidth(Self.cellPaddingX, type: .absoluteValueType, for: .padding, edge: .minX)
                cell.setWidth(Self.cellPaddingX, type: .absoluteValueType, for: .padding, edge: .maxX)
                cell.setWidth(Self.cellPaddingY, type: .absoluteValueType, for: .padding, edge: .minY)
                cell.setWidth(Self.cellPaddingY, type: .absoluteValueType, for: .padding, edge: .maxY)
                let tableSize = NSFont.preferredFont(forTextStyle: .subheadline).pointSize * scale
                style.font = .monospacedDigitSystemFont(ofSize: tableSize, weight: headerRow ? .semibold : .regular)
                if headerRow { style.color = MacColor.textSecondary }
                blocks.append(cell)
                style.cell = (tableID, row, column, cell)
            }

            if listDepth > 0 {
                let step = size * 1.5
                let indent = step * CGFloat(listDepth)
                paragraph.headIndent = indent
                paragraph.firstLineHeadIndent = indent - step
                paragraph.tabStops = [NSTextTab(textAlignment: .left, location: indent)]
                paragraph.paragraphSpacing = Design.Space.s4
                if style.code {
                    // A block of its own: its first line carries no marker and its lines no indent.
                    paragraph.headIndent = 0
                    paragraph.firstLineHeadIndent = 0
                    paragraph.tabStops = []
                } else if let marker = markerFor, marker.item >= 0 {
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

        private func makeTable() -> NSTextTable {
            let table = NSTextTable()
            table.numberOfColumns = 1
            table.layoutAlgorithm = .fixedLayoutAlgorithm
            table.collapsesBorders = false
            table.hidesEmptyCells = false
            return table
        }
    }
}

/// A segment's text container. It ends where its segment does, however much room it has, so what follows goes on in
/// the next container. Its own width is always `MarkdownAttributed.unwrapped`: TextKit sizes a table against the
/// container before it, then moves it on, so a narrow one squeezed the wide table after it. Running text wraps at
/// `limit` instead.
final class MessageTextContainer: NSTextContainer {
    /// The first character past the segment.
    var end = Int.max
    /// Where its lines wrap.
    var limit = CGFloat.greatestFiniteMagnitude {
        didSet { if limit != oldValue { layoutManager?.textContainerChangedGeometry(self) } }
    }

    init() {
        super.init(size: NSSize(width: MarkdownAttributed.unwrapped, height: .greatestFiniteMagnitude))
        lineFragmentPadding = 0
    }

    @available(*, unavailable)
    required init(coder: NSCoder) { fatalError("not from a nib") }

    override var isSimpleRectangularTextContainer: Bool { false }

    override func lineFragmentRect(forProposedRect proposedRect: NSRect, at characterIndex: Int,
                                   writingDirection baseWritingDirection: NSWritingDirection,
                                   remaining remainingRect: UnsafeMutablePointer<NSRect>?) -> NSRect {
        guard characterIndex < end else { return .zero }
        var rect = super.lineFragmentRect(forProposedRect: proposedRect, at: characterIndex,
                                          writingDirection: baseWritingDirection, remaining: remainingRect)
        if rect.maxX > limit { rect.size.width = max(limit - rect.minX, 0) }
        return rect
    }
}

extension MarkdownAttributed {
    /// Where each segment of a message sits at a width, and how big the message is: what the view lays out and what
    /// SwiftUI is told. A block fills the column and scrolls when its text is wider; a table hugs its columns.
    struct Layout: Equatable {
        struct Placed: Equatable {
            /// In the message.
            var frame: CGRect
            /// The text's own size: the block scrolls when this is wider than `frame`.
            var content: CGSize
        }
        var placed: [Placed] = []
        var size: CGSize = .zero
    }

    /// Measures a rendered message, keeping what it measured: the same text is asked for again and again as SwiftUI
    /// settles a width, and a block's size does not depend on the width at all.
    final class Measure {
        let rendered: Rendered
        private var prose: [Int: [CGFloat: CGSize]] = [:]
        private var blocks: [Int: CGSize] = [:]
        private var layouts: [CGFloat: Layout] = [:]

        init(_ rendered: Rendered) { self.rendered = rendered }

        /// A new render of the same message keeps the sizes of the segments that did not change: only a streaming
        /// reply's tail does.
        init(_ rendered: Rendered, after previous: Measure?) {
            self.rendered = rendered
            guard let previous, rendered.textScale == previous.rendered.textScale else { return }
            let count = min(rendered.segments.count, previous.rendered.segments.count) - 1
            guard count > 0 else { return }
            let new = rendered.text.string as NSString
            let old = previous.rendered.text.string as NSString
            for index in 0..<count {
                let (a, b) = (rendered.segments[index], previous.rendered.segments[index])
                guard a == b, new.substring(with: a.range) == old.substring(with: b.range) else { break }
                prose[index] = previous.prose[index]
                blocks[index] = previous.blocks[index]
            }
        }

        /// Minimum probes must not feed a content-dependent minimum into AppKit's split-view constraints.
        /// Operator chips hug at real widths; ChipColumn supplies their column's width.
        func size(for proposal: ProposedViewSize, hugging: Bool) -> CGSize {
            guard let width = proposal.width, width.isFinite, width > 0 else {
                return CGSize(width: 1, height: layout(width: 1).size.height)
            }
            let measured = layout(width: min(width, Design.measure)).size
            return CGSize(width: hugging ? measured.width : width, height: measured.height)
        }

        func layout(width: CGFloat) -> Layout {
            let width = width.rounded()
            if let known = layouts[width] { return known }
            var result = Layout()
            var y: CGFloat = 0
            var widest: CGFloat = 0
            for (index, segment) in rendered.segments.enumerated() {
                if index > 0 { y += blockGap }
                var placed: Layout.Placed
                switch segment.kind {
                case .prose:
                    let size = proseSize(index, segment, width: width)
                    placed = .init(frame: CGRect(x: 0, y: y, width: width, height: size.height), content: size)
                    widest = max(widest, size.width)
                case .code:
                    let size = blockSize(index, segment)
                    let height = codeHeader + 1 + blockPadding * 2 + size.height
                    placed = .init(frame: CGRect(x: 0, y: y, width: width, height: height), content: size)
                    widest = width
                case .table:
                    let size = blockSize(index, segment)
                    placed = .init(frame: CGRect(x: 0, y: y, width: min(size.width, width), height: size.height),
                                   content: size)
                    widest = max(widest, placed.frame.width)
                }
                placed.frame.size.height = placed.frame.height.rounded(.up)
                y += placed.frame.height
                result.placed.append(placed)
            }
            result.size = CGSize(width: min(width, (widest + 1).rounded(.up)), height: max(y, 1))
            layouts[width] = result
            return result
        }

        private func proseSize(_ index: Int, _ segment: Segment, width: CGFloat) -> CGSize {
            if let size = prose[index]?[width] { return size }
            let size = measure(segment, limit: width)
            let fitted = CGSize(width: min(width, size.width), height: size.height)
            prose[index, default: [:]][width] = fitted
            return fitted
        }

        private func blockSize(_ index: Int, _ segment: Segment) -> CGSize {
            if let size = blocks[index] { return size }
            var size: CGSize
            switch segment.kind {
            case .table:
                let width = rendered.tables.first { $0.range == segment.range }?.width ?? 0
                size = CGSize(width: width, height: measure(segment, limit: .greatestFiniteMagnitude).height)
            default:
                size = measure(segment, limit: .greatestFiniteMagnitude)
            }
            size.width = size.width.rounded(.up)
            size.height = size.height.rounded(.up)
            blocks[index] = size
            return size
        }

        /// The text of `segment` alone, in a layout of its own: as wide as its longest line, as tall as it needs.
        private func measure(_ segment: Segment, limit: CGFloat) -> CGSize {
            let text = NSMutableAttributedString(attributedString: rendered.text.attributedSubstring(from: segment.range))
            trimTrailingNewline(text)
            let storage = NSTextStorage(attributedString: text)
            let layout = NSLayoutManager()
            let container = MessageTextContainer()
            container.limit = limit
            layout.addTextContainer(container)
            storage.addLayoutManager(layout)
            layout.ensureLayout(for: container)
            let used = layout.usedRect(for: container)
            return CGSize(width: (used.maxX + 1).rounded(.up), height: max(used.maxY.rounded(.up), 1))
        }
    }
}

/// Messages rendered lately, kept across the views that show them (macOS/MessageTextView.swift): the feed builds a
/// message's view as it scrolls into sight (LazyVStack) and builds them all again when a chat is opened again, and the
/// same source at the same scale and appearance renders the same. A rendered message is immutable (its text is copied
/// into each view's storage).
@MainActor
enum RenderCache {
    struct Key: Hashable {
        let source: String
        let markdown: Bool
        let scale: CGFloat
        let appearance: NSAppearance.Name

        /// The source's exact bytes: String equality is canonical, so "é" written two ways would share one rendering
        /// and Copy code would hand back the other message's characters.
        static func == (lhs: Key, rhs: Key) -> Bool {
            lhs.markdown == rhs.markdown && lhs.scale == rhs.scale && lhs.appearance == rhs.appearance
                && lhs.source.utf8.elementsEqual(rhs.source.utf8)
        }

        func hash(into hasher: inout Hasher) {
            hasher.combine(markdown)
            hasher.combine(scale)
            hasher.combine(appearance)
            for byte in source.utf8 { hasher.combine(byte) }
        }
    }

    /// About two long chats' worth of messages (80 turns each), with room for a streaming reply's passing versions.
    static let capacity = 400
    private static var entries: [Key: MarkdownAttributed.Rendered] = [:]
    private static var order: [Key] = []

    static func rendered(_ key: Key, _ make: () -> MarkdownAttributed.Rendered) -> MarkdownAttributed.Rendered {
        if let hit = entries[key] { return hit }
        let made = make()
        entries[key] = made
        order.append(key)
        if order.count > capacity {
            let dropped = order.prefix(order.count - capacity)
            for old in dropped { entries[old] = nil }
            order.removeFirst(dropped.count)
        }
        return made
    }
}
