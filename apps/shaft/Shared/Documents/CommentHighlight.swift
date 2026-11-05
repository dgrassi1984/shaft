import Foundation
import SHAFTKit

/// The commented passages of a document, marked in its Markdown so the page shows them (spec §5): an anchor names a
/// passage in the rendered text (§10.2), and each piece of it becomes `[~~piece~~](shaft-comment:c1)` in the source
/// MarkdownUI draws. The link makes a piece tappable and the strikethrough (`CommentMark`, which draws these as a soft
/// highlight) marks it, so a passage in a table cell, a list item or across bold text needs no drawing of its own.
///
/// What the page reads never changes: a marking that would change the rendered text is dropped, so a passage that
/// cannot be marked (in a fenced code block or a real link, or an anchor no longer found) simply has no highlight, and
/// its thread stays in the list below the document.
enum CommentHighlight {
    static let scheme = "shaft-comment"

    /// The threads a tapped link opens; nil for any other link.
    static func threads(in url: URL?) -> [String]? {
        guard let url, url.scheme == scheme else { return nil }
        return url.absoluteString.dropFirst(scheme.count + 1).split(separator: ",")
            .compactMap { String($0).removingPercentEncoding }
    }

    /// The normalized drawing source with its open threads' passages marked. `plain` is how the page's text reads.
    static func marked(_ source: String, threads: [CommentThread],
                       plain: (String) -> String = RenderedText.plain) -> String {
        let source = AgentMarkdown.normalized(source)
        let rendered = plain(source)
        let passages = threads.compactMap { thread -> Passage? in
            guard thread.state == "open", let anchor = thread.anchor,
                  let range = Anchoring.locate(anchor, in: rendered) else { return nil }
            return Passage(id: thread.id, range: range)
        }
        guard !passages.isEmpty else { return source }
        let map = SourceMap(source: source, rendered: rendered)
        let all = map.marking(passages)
        if plain(all) == rendered { return all }
        // One passage that cannot be marked does not cost the others theirs.
        let some = map.marking(passages.filter { plain(map.marking([$0])) == rendered })
        return plain(some) == rendered ? some : source
    }

    fileprivate struct Passage {
        var id: String
        var range: NSRange
    }
}

/// Where each UTF-16 unit of the rendered text sits in the Markdown source. Rendered text is the source minus its
/// markup, so the two are walked together, taking the earliest fit and stepping over what never renders.
private struct SourceMap {
    private let text: NSString
    private let units: [UInt16]
    /// Rendered offset to source offset; -1 for what has no place of its own (a block break, an entity).
    private let places: [Int]
    private let fenced: [NSRange]
    private let spans: [NSRange]
    private let links: [NSRange]

    private enum Unit {
        static let space: UInt16 = 0x20
        static let newline: UInt16 = 0x0A
        static let lineSeparator: UInt16 = 0x2028
        static let backslash: UInt16 = 0x5C
        /// `*`, `[`, `]`, `\`, `_`, `` ` ``, `~`: what can stand between a word and the space after it.
        static let markup: Set<UInt16> = [0x2A, 0x5B, 0x5D, 0x5C, 0x5F, 0x60, 0x7E]
    }

    /// What never renders: a fence's own lines, a table's delimiter row or a rule, a link's destination, a reference
    /// definition, an HTML comment, a list item's marker and box, a quote's marker, a heading's hashes.
    private static let unrendered = [
        #"^ {0,3}(?:`{3,}|~{3,}).*$"#,
        #"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$"#,
        #"^ {0,3}=+[ \t]*$"#,
        #"(?<=\])\((?:[^()\n]|\([^()\n]*\))*\)"#,
        #"^ {0,3}\[[^\]\n]+\]:.*$"#,
        #"<!--[\s\S]*?-->"#,
        #"^[ \t>]*(?:[-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?"#,
        #"^[ \t]*(?:>[ \t]?)+"#,
        #"^ {0,3}#{1,6}[ \t]+"#,
    ]

    init(source: String, rendered: String) {
        let text = source as NSString
        let units = Array(source.utf16)
        self.text = text
        self.units = units
        let fences = Self.matches(#"^ {0,3}(`{3,}|~{3,}).*\n(?:[\s\S]*?\n)??(?: {0,3}\1[`~]*[ \t]*$|\z)"#, in: text)
        let inFence = Self.mask(fences, count: units.count)
        fenced = fences
        let codeSpans = Self.matches(#"(?<!`)(`+)(?!`)[^\n]*?(?<!`)\1(?!`)"#, in: text).filter { !inFence[$0.location] }
        spans = codeSpans
        links = Self.matches(#"!?\[[^\]\n]*\](?=\()"#, in: text).filter { !inFence[$0.location] }
        let inSpan = Self.mask(codeSpans, count: units.count)

        // A fence's body renders whole: only its own two lines are markup.
        let fenceLines = Self.matches(Self.unrendered[0], in: text).filter { line in
            fences.contains { line.location == $0.location || NSMaxRange(line) == NSMaxRange($0) }
        }
        var hidden = [Bool](repeating: false, count: units.count)
        for range in fenceLines { for k in range.location..<NSMaxRange(range) { hidden[k] = true } }
        for pattern in Self.unrendered.dropFirst() {
            for range in Self.matches(pattern, in: text) where !inFence[min(range.location, units.count - 1)] {
                for k in range.location..<NSMaxRange(range) where !inFence[k] { hidden[k] = true }
            }
        }
        var entities: [Int: Int] = [:]
        for range in Self.matches(#"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6});"#, in: text)
        where !inFence[range.location] && !inSpan[range.location] {
            entities[range.location] = NSMaxRange(range)
        }
        places = Self.align(Array(rendered.utf16), units, hidden: hidden, entities: entities)
    }

    private static func align(_ rendered: [UInt16], _ source: [UInt16], hidden: [Bool], entities: [Int: Int]) -> [Int] {
        var places = [Int](repeating: -1, count: rendered.count)
        var next = 0
        for (offset, unit) in rendered.enumerated() {
            if unit == Unit.newline || unit == Unit.lineSeparator { continue }
            if unit == Unit.space {
                var k = next
                while k < source.count, hidden[k] || Unit.markup.contains(source[k]) { k += 1 }
                guard k < source.count else { continue }
                if source[k] == Unit.space {
                    places[offset] = k
                    next = k + 1
                } else if source[k] == Unit.newline {
                    next = k + 1                // a soft break renders as a space
                }
                continue
            }
            var k = next
            while k < source.count {
                if !hidden[k], let end = entities[k] {  // decoded, one rendered unit: it has no place to wrap
                    next = end
                    break
                }
                if source[k] == unit && !hidden[k] {
                    places[offset] = k
                    next = k + 1
                    break
                }
                k += 1
            }
        }
        return places
    }

    /// The source with `passages` marked, each unbroken piece on its own so bold, links and cells stay balanced.
    fileprivate func marking(_ passages: [CommentHighlight.Passage]) -> String {
        var owners: [Int: [String]] = [:]
        for passage in passages {
            for offset in passage.range.location..<NSMaxRange(passage.range) { owners[offset, default: []].append(passage.id) }
        }
        var pieces: [(range: NSRange, ids: [String])] = []
        for offset in owners.keys.sorted() {
            let place = places[offset]
            guard place >= 0, let ids = owners[offset] else { continue }
            if let last = pieces.last, last.ids == ids, NSMaxRange(last.range) == place {
                pieces[pieces.count - 1].range.length += 1
            } else {
                pieces.append((NSRange(location: place, length: 1), ids))
            }
        }
        var merged: [(range: NSRange, ids: [String])] = []
        for piece in pieces.compactMap(whole).sorted(by: { $0.range.location < $1.range.location }) {
            if let last = merged.last, NSMaxRange(last.range) > piece.range.location {
                let end = max(NSMaxRange(last.range), NSMaxRange(piece.range))
                merged[merged.count - 1].range.length = end - last.range.location
                merged[merged.count - 1].ids = Array(Set(last.ids + piece.ids)).sorted()
            } else {
                merged.append(piece)
            }
        }
        let out = NSMutableString(string: text)
        for piece in merged.reversed() {
            out.replaceCharacters(in: piece.range, with: wrapped(piece.range, ids: piece.ids))
        }
        return out as String
    }

    /// A piece as it can be marked: a whole code span (nothing goes inside one), with its leading backslash; nil for
    /// one that cannot be (in a fenced block, in a link's text, or nothing but space).
    private func whole(_ piece: (range: NSRange, ids: [String])) -> (range: NSRange, ids: [String])? {
        var range = piece.range
        guard !text.substring(with: range).allSatisfy(\.isWhitespace),
              !fenced.contains(where: { NSIntersectionRange($0, range).length > 0 }),
              !links.contains(where: { NSIntersectionRange($0, range).length > 0 }) else { return nil }
        if let span = spans.first(where: { NSIntersectionRange($0, range).length > 0 }) {
            range = NSUnionRange(range, span)
        } else if range.location > 0, units[range.location - 1] == Unit.backslash {
            range = NSRange(location: range.location - 1, length: range.length + 1)
        }
        return (range, piece.ids)
    }

    private func wrapped(_ range: NSRange, ids: [String]) -> String {
        var inner = text.substring(with: range)
        // `~~` beside a space is no delimiter; an entity is the same space to the reader and punctuation to the parser.
        if inner.hasPrefix(" ") { inner = "&#32;" + inner.dropFirst() }
        if inner.hasSuffix(" ") { inner = inner.dropLast() + "&#32;" }
        let names = ids.compactMap { $0.addingPercentEncoding(withAllowedCharacters: .alphanumerics) }
        return "[~~\(inner)~~](\(CommentHighlight.scheme):\(names.joined(separator: ",")))"
    }

    private static func matches(_ pattern: String, in text: NSString) -> [NSRange] {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: .anchorsMatchLines) else { return [] }
        return regex.matches(in: text as String, range: NSRange(location: 0, length: text.length)).map(\.range)
            .filter { $0.length > 0 }
    }

    private static func mask(_ ranges: [NSRange], count: Int) -> [Bool] {
        var mask = [Bool](repeating: false, count: count + 1)
        for range in ranges { for k in range.location..<NSMaxRange(range) { mask[k] = true } }
        return mask
    }
}
