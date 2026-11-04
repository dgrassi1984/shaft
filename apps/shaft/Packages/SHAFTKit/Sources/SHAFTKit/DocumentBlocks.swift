import Foundation

/// One top-level block of a document's Markdown, as `DocumentBlocks.split` cuts it.
public struct DocumentBlock: Identifiable, Hashable, Sendable {
    /// What a block opens or closes with: the gap beside it depends on that (`BlockGap`).
    public enum Kind: Hashable, Sendable {
        case paragraph
        case heading(Int)
        case list
        case quote
        case code
        case table
        case rule
    }

    /// The same text keeps its id across a new version of the document, so a block that did not change is not rebuilt.
    public let id: Int
    public let text: String
    public let first: Kind
    public let last: Kind
}

/// A document's Markdown cut at the blank lines between its top-level blocks, for a page that draws only what is on
/// screen. Every block must parse alone as it did in the whole, so a cut never falls inside a fenced code block, an
/// HTML comment, a list (a blank line and then an indented line or the next item go on with it) or an indented code
/// block, and the link reference definitions, which reach the whole document, follow every block that may use them.
public enum DocumentBlocks {
    public static func split(_ source: String) -> [DocumentBlock] {
        var cutter = Cutter()
        for line in source.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "\n" || $0 == "\r\n" }) {
            cutter.add(line)
        }
        cutter.cut()
        return blocks(cutter.pieces)
    }

    private static func blocks(_ pieces: [Piece]) -> [DocumentBlock] {
        var kept: [Piece] = []
        var definitions: [Substring] = []
        for var piece in pieces {
            while let line = piece.lines.first, Line.isDefinition(line) {
                definitions.append(line)
                piece.lines.removeFirst()
            }
            if !piece.lines.isEmpty { kept.append(piece) }
        }
        let appendix = definitions.isEmpty ? "" : "\n\n" + definitions.joined(separator: "\n")
        var seen: [String: Int] = [:]
        return kept.map { piece in
            var text = piece.lines.joined(separator: "\n")
            if !piece.openFence, text.contains("[") { text += appendix }
            let repeated = seen[text, default: 0]
            seen[text] = repeated + 1
            var hasher = Hasher()
            hasher.combine(text)
            hasher.combine(repeated)
            return DocumentBlock(id: hasher.finalize(), text: text, first: piece.first, last: piece.last)
        }
    }

    private struct Piece {
        var lines: [Substring]
        var first: DocumentBlock.Kind
        var last: DocumentBlock.Kind
        var openFence: Bool
    }

    /// Walks the lines, holding the blank ones until the next line says whether they end the block.
    private struct Cutter {
        var pieces: [Piece] = []
        private var lines: [Substring] = []
        private var blanks: [Substring] = []
        private var fence: (mark: Character, length: Int)?
        private var inComment = false
        /// The column a list's item text starts at, in a block with a list in it.
        private var itemText: Int?
        private var indentedCode = false

        mutating func add(_ line: Substring) {
            if fence != nil || inComment {
                lines.append(line)
                if let open = fence {
                    if Line.closes(line, open.mark, open.length) { fence = nil }
                } else if line.contains("-->") {
                    inComment = false
                }
                return
            }
            guard !Line.isBlank(line) else {
                if !lines.isEmpty { blanks.append(line) }
                return
            }
            let indent = Line.indent(line)
            let rule = Line.isRule(line)
            let marker = rule ? nil : Line.marker(line, indent)
            if !blanks.isEmpty {
                let inList = itemText.map { indent >= $0 || marker != nil } ?? false
                if inList || (indentedCode && indent >= 4) {
                    lines += blanks
                    blanks = []
                } else {
                    cut()
                }
            }
            if lines.isEmpty { indentedCode = indent >= 4 }
            if let marker, indent < min(itemText ?? 4, 4) { itemText = marker }
            let column = indent - min(itemText ?? 0, indent)     // a fence in an item is read from the item's text
            if column <= 3, let open = Line.fence(line) {
                fence = open
            } else if indent <= 3, line.drop(while: \.isWhitespace).hasPrefix("<!--"), !line.contains("-->") {
                inComment = true
            }
            lines.append(line)
        }

        mutating func cut() {
            defer {
                lines = []
                blanks = []
                itemText = nil
                indentedCode = false
                fence = nil
                inComment = false
            }
            guard let opening = lines.first, let closing = lines.last else { return }
            let underline = lines.count > 1 ? Line.setextLevel(closing) : nil
            var first = Line.kind(opening, next: lines.count > 1 ? lines[1] : nil)
            if first == .paragraph, let underline { first = .heading(underline) }
            let last: DocumentBlock.Kind
            if lines.count == 1 {
                last = first
            } else if fence != nil || Line.fence(closing) != nil || indentedCode {
                last = .code
            } else if itemText != nil {
                last = .list
            } else if first == .table, closing.contains("|") {
                last = .table
            } else if first == .heading(underline ?? 0) {
                last = first
            } else {
                last = Line.kind(closing, next: nil)
            }
            pieces.append(Piece(lines: lines, first: first, last: last, openFence: fence != nil))
        }
    }

    /// What one line of Markdown is, read from its start alone.
    private enum Line {
        static func isBlank(_ line: Substring) -> Bool {
            line.allSatisfy(\.isWhitespace)
        }

        static func indent(_ line: Substring) -> Int {
            var column = 0
            for character in line {
                if character == " " { column += 1 } else if character == "\t" { column += 4 - column % 4 } else { break }
            }
            return column
        }

        static func kind(_ line: Substring, next: Substring?) -> DocumentBlock.Kind {
            let column = indent(line)
            if column >= 4 { return .code }
            let text = line.drop { $0 == " " || $0 == "\t" }
            if fence(line) != nil { return .code }
            if let level = headingLevel(text) { return .heading(level) }
            if isRule(line) { return .rule }
            if text.hasPrefix(">") { return .quote }
            if marker(line, column) != nil { return .list }
            if let next, line.contains("|"), isTableDelimiter(next) { return .table }
            return .paragraph
        }

        private static func headingLevel(_ text: Substring) -> Int? {
            let level = text.prefix { $0 == "#" }.count
            guard (1...6).contains(level) else { return nil }
            let after = text.dropFirst(level).first
            return after == nil || after == " " || after == "\t" ? level : nil
        }

        /// The level of a line that underlines the text above it as a heading.
        static func setextLevel(_ line: Substring) -> Int? {
            guard indent(line) <= 3 else { return nil }
            let text = line.trimmingCharacters(in: .whitespaces)
            guard let mark = text.first, mark == "=" || mark == "-", text.allSatisfy({ $0 == mark }) else { return nil }
            return mark == "=" ? 1 : 2
        }

        static func isRule(_ line: Substring) -> Bool {
            guard indent(line) <= 3 else { return false }
            let marks = line.filter { !$0.isWhitespace }
            guard let mark = marks.first, "-*_".contains(mark), marks.count >= 3 else { return false }
            return marks.allSatisfy { $0 == mark }
        }

        private static func isTableDelimiter(_ line: Substring) -> Bool {
            line.contains("-") && line.allSatisfy { "|-: \t".contains($0) }
        }

        /// The column an item's text starts at, when the line opens a list item.
        static func marker(_ line: Substring, _ column: Int) -> Int? {
            let text = line.drop { $0 == " " || $0 == "\t" }
            var width = 0
            if let mark = text.first, "-*+".contains(mark) {
                width = 1
            } else {
                let digits = text.prefix { $0.isASCII && $0.isNumber }.count
                guard (1...9).contains(digits), let mark = text.dropFirst(digits).first, mark == "." || mark == ")" else {
                    return nil
                }
                width = digits + 1
            }
            let gap = text.dropFirst(width).prefix { $0 == " " || $0 == "\t" }.count
            guard gap > 0 else { return text.count == width ? column + width + 1 : nil }
            return column + width + (gap > 4 ? 1 : gap)
        }

        /// The fence the line opens.
        static func fence(_ line: Substring) -> (mark: Character, length: Int)? {
            let text = line.drop { $0 == " " || $0 == "\t" }
            guard let mark = text.first, mark == "`" || mark == "~" else { return nil }
            let length = text.prefix { $0 == mark }.count
            guard length >= 3, mark == "~" || !text.dropFirst(length).contains("`") else { return nil }
            return (mark, length)
        }

        static func closes(_ line: Substring, _ mark: Character, _ length: Int) -> Bool {
            let text = line.trimmingCharacters(in: .whitespaces)
            return text.count >= length && text.allSatisfy { $0 == mark }
        }

        /// `[label]: destination "title"`: never drawn, and any block may name it. (A note written `[^1]: words` is not
        /// one: it is a paragraph.)
        static func isDefinition(_ line: Substring) -> Bool {
            guard indent(line) <= 3, line.drop(while: { $0 == " " }).hasPrefix("[") else { return false }
            return line.wholeMatch(of: definition) != nil
        }

        private nonisolated(unsafe) static let definition =
            /\ {0,3}\[[^\]]+\]:[ \t]*\S+(?:[ \t]+(?:"[^"]*"|'[^']*'|\([^)]*\)))?[ \t]*/
    }
}
