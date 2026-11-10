import Foundation

// What is spoken (M2 lane A). A reply streams in as markdown pieces cut anywhere; the voice must start
// on the first complete sentence and never read markup aloud. SpeechChunker turns the pieces into spoken sentences;
// SpokenText turns one block of markdown into speakable text.
//
// One rule keeps the chunker honest: every decision looks at the text it has and, when the next character could
// change the answer, waits for it (`needMore`). So the sentences come out the same however the reply was cut into
// pieces, from one piece to one character at a time. The text is held as unicode scalars, so a cut inside an emoji or
// an accent changes nothing either.

/// Markdown pieces of a reply in (as they stream), spoken sentences out. A sentence is returned only once it is
/// complete; a fenced block or a table is held until it closes and then becomes its one announcement.
public struct SpeechChunker: SpeechChunking {
    private let language: VoiceLanguage
    /// Text received and not yet spoken or dropped.
    private var buf: [Unicode.Scalar] = []
    /// A "\r" ended the last piece: it joins the next piece, so "\r\n" is one line break however the text was cut.
    private var heldCR = false
    /// `buf` starts a line (so a marker there makes a heading, an item, a fence, a table); false after a sentence end
    /// inside a line.
    private var atLineStart = true
    private var mode: Mode = .paragraph
    /// "1." of a numbered item, spoken before the item's first sentence.
    private var pendingPrefix = ""
    /// Inside a double quote opened in an earlier sentence of this paragraph.
    private var quoteOpen = false

    private enum Mode { case paragraph, listItem, quote }

    public init(language: VoiceLanguage) {
        self.language = language
    }

    public mutating func feed(_ markdown: String) -> [String] {
        var scalars = Array(markdown.unicodeScalars)
        if heldCR { scalars.insert("\r", at: 0); heldCR = false }
        if scalars.last == "\r" { scalars.removeLast(); heldCR = true }
        var index = 0
        while index < scalars.count {
            if scalars[index] == "\r" {
                buf.append("\n")
                if index + 1 < scalars.count, scalars[index + 1] == "\n" { index += 1 }
            } else {
                buf.append(scalars[index])
            }
            index += 1
        }
        return drain(final: false)
    }

    public mutating func finish() -> [String] {
        if heldCR { buf.append("\n"); heldCR = false }
        let out = drain(final: true)
        buf = []
        atLineStart = true
        mode = .paragraph
        pendingPrefix = ""
        quoteOpen = false
        return out
    }

    // MARK: - Driving

    private mutating func drain(final: Bool) -> [String] {
        var out: [String] = []
        while step(final: final, out: &out) {}
        return out
    }

    /// Does one unit of work on the front of `buf`. False: nothing more can be decided without more text.
    private mutating func step(final: Bool, out: inout [String]) -> Bool {
        if buf.isEmpty { return false }
        if atLineStart { return blockStep(final: final, out: &out) }
        // Inside a line: spaces are skipped; a line break ends the line, whatever mode it was in.
        var skip = 0
        while skip < buf.count, Self.isSpace(buf[skip]) { skip += 1 }
        if skip > 0 { buf.removeFirst(skip) }
        if buf.isEmpty { return false }
        if buf[0] == "\n" {
            buf.removeFirst()
            startLine()
            return true
        }
        switch scan(final: final) {
        case .needMore:
            return false
        case let .end(raw, consume, lineBreak, quote):
            let text = String(String.UnicodeScalarView(buf[0..<raw]))
            buf.removeFirst(consume)
            quoteOpen = quote
            emit(text, into: &out)
            if lineBreak { startLine() }
            return true
        }
    }

    private mutating func startLine() {
        atLineStart = true
        mode = .paragraph
        pendingPrefix = ""
    }

    private mutating func emit(_ raw: String, into out: inout [String]) {
        let text = SpokenText.sentence(raw, language: language)
        let prefix = pendingPrefix
        pendingPrefix = ""
        guard !text.isEmpty else { return }
        out.append(prefix.isEmpty ? text : prefix + " " + text)
    }

    private mutating func blockStep(final: Bool, out: inout [String]) -> Bool {
        let n = buf.count
        switch classify(0, final: final) {
        case .needMore:
            return false
        case let .blank(end), let .rule(end):
            buf.removeFirst(min(end, n))
            quoteOpen = false
            return true
        case let .heading(content, end):
            guard let end else { return false }
            var stop = end
            while stop > content, Self.isSpace(buf[stop - 1]) { stop -= 1 }
            var closing = stop                              // "## Title ##": the closing marks are not part of it
            while closing > content, buf[closing - 1] == "#" { closing -= 1 }
            if closing < stop, closing == content || Self.isSpace(buf[closing - 1]) { stop = closing }
            let text = String(String.UnicodeScalarView(buf[content..<stop]))
            buf.removeFirst(min(end + 1, n))
            quoteOpen = false
            emit(text, into: &out)
            return true
        case let .fence(mark, length, end):
            var p = end
            while true {
                if p >= n {
                    guard final else { return false }
                    break
                }
                guard let newline = lineEnd(from: p) else {
                    guard final else { return false }
                    if Self.closesFence(buf[p..<n], mark: mark, length: length) { p = n }
                    break
                }
                if Self.closesFence(buf[p..<newline], mark: mark, length: length) { p = newline + 1; break }
                p = newline + 1
            }
            buf.removeFirst(min(p, n))
            out.append(SpokenText.codeAnnouncement(language))
            return true
        case .table:
            var p = 0
            while true {
                guard let newline = lineEnd(from: p) else {
                    guard final else { return false }
                    p = n
                    break
                }
                p = newline + 1
                var q = p
                while q < n, Self.isSpace(buf[q]) { q += 1 }
                if q >= n {
                    guard final else { return false }
                    p = n
                    break
                }
                if buf[q] != "|" { break }
            }
            buf.removeFirst(min(p, n))
            out.append(SpokenText.tableAnnouncement(language))
            return true
        case let .item(prefix, content):
            buf.removeFirst(min(content, n))
            pendingPrefix = prefix
            mode = .listItem
            atLineStart = false
            quoteOpen = false
            return true
        case let .quote(content):
            buf.removeFirst(min(content, n))
            mode = .quote
            atLineStart = false
            quoteOpen = false
            return true
        case .text:
            mode = .paragraph
            atLineStart = false
            quoteOpen = false
            return true
        }
    }

    // MARK: - Lines

    private enum Line {
        case needMore
        /// Blank line; `end` is the index after its line break.
        case blank(end: Int)
        case rule(end: Int)
        /// Opening line of a fenced block; `end` is the index after it.
        case fence(mark: Unicode.Scalar, length: Int, end: Int)
        /// `end` is the index of the line break (nil: the line is still arriving).
        case heading(content: Int, end: Int?)
        case item(prefix: String, content: Int)
        case quote(content: Int)
        case table
        case text
    }

    private func lineEnd(from p: Int) -> Int? {
        var i = p
        while i < buf.count {
            if buf[i] == "\n" { return i }
            i += 1
        }
        return nil
    }

    /// What kind of line starts at `p`. `needMore` when the line is too short to tell yet.
    private func classify(_ p: Int, final: Bool) -> Line {
        let n = buf.count
        var q = p
        while q < n, Self.isSpace(buf[q]) { q += 1 }
        if q >= n { return final ? .blank(end: n) : .needMore }
        let c = buf[q]
        if c == "\n" { return .blank(end: q + 1) }
        let newline = lineEnd(from: q)
        let more: Line = final ? .text : .needMore
        switch c {
        case "#":
            var r = q
            while r < n, buf[r] == "#" { r += 1 }
            if r >= n { return more }
            guard r - q <= 6, Self.isSpace(buf[r]) || buf[r] == "\n" else { return .text }
            return .heading(content: r, end: newline ?? (final ? n : nil))
        case "-", "*", "+", "_", "•":
            if q + 1 >= n { return more }
            let d = buf[q + 1]
            if c != "_", Self.isSpace(d) { return .item(prefix: "", content: q + 2) }
            guard c != "•", d == c else { return .text }
            // "---", "***", "___": a rule only when the whole line is the mark.
            let stop = newline ?? n
            var count = 0
            for r in q..<stop {
                if buf[r] == c { count += 1 } else if !Self.isSpace(buf[r]) { return .text }
            }
            if newline == nil && !final { return .needMore }
            return count >= 3 ? .rule(end: newline.map { $0 + 1 } ?? n) : .text
        case "0"..."9":
            var r = q
            while r < n, buf[r].value >= 48, buf[r].value <= 57 { r += 1 }
            if r >= n { return more }
            guard r - q <= 9, buf[r] == "." || buf[r] == ")" else { return .text }
            if r + 1 >= n { return more }
            guard Self.isSpace(buf[r + 1]) else { return .text }
            return .item(prefix: String(String.UnicodeScalarView(buf[q..<r])) + ".", content: r + 2)
        case "`", "~":
            var r = q
            while r < n, buf[r] == c { r += 1 }
            if r >= n { return final && r - q >= 3 ? .fence(mark: c, length: r - q, end: n) : more }
            guard r - q >= 3 else { return .text }
            if c == "`", buf[r..<(newline ?? n)].contains("`") { return .text }   // "```code```" is inline
            guard let newline else { return final ? .fence(mark: c, length: r - q, end: n) : .needMore }
            return .fence(mark: c, length: r - q, end: newline + 1)
        case ">":
            var r = q
            while r < n, buf[r] == ">" || Self.isSpace(buf[r]) { r += 1 }
            return .quote(content: r)
        case "|":
            return .table
        default:
            return .text
        }
    }

    private static func closesFence(_ line: ArraySlice<Unicode.Scalar>, mark: Unicode.Scalar, length: Int) -> Bool {
        var i = line.startIndex
        while i < line.endIndex, isSpace(line[i]) { i += 1 }
        var run = 0
        while i < line.endIndex, line[i] == mark { run += 1; i += 1 }
        guard run >= length else { return false }
        return line[i...].allSatisfy(isSpace)
    }

    // MARK: - Sentences

    private enum Scan {
        case needMore
        /// The sentence is `buf[..<raw]`; `consume` characters leave the buffer; `lineBreak`: the end took a line
        /// break with it; `quote`: whether a double quote is open after it.
        case end(raw: Int, consume: Int, lineBreak: Bool, quote: Bool)
    }

    /// The first sentence of `buf`, or `needMore`. Characters are looked at left to right; each decision that needs
    /// the next character waits for it.
    private func scan(final: Bool) -> Scan {
        let n = buf.count
        var i = 0
        var quote = quoteOpen
        while true {
            if i >= n { return final ? .end(raw: n, consume: n, lineBreak: false, quote: quote) : .needMore }
            let c = buf[i]
            switch c {
            case "\n":
                if mode == .listItem { return .end(raw: i, consume: i + 1, lineBreak: true, quote: quote) }
                let next = classify(i + 1, final: final)
                if case .needMore = next { return .needMore }
                var continues = false
                if case .text = next, mode == .paragraph { continues = true }
                if case .quote = next, mode == .quote { continues = true }
                if continues { i += 1 } else { return .end(raw: i, consume: i + 1, lineBreak: true, quote: quote) }
            case "`":
                var r = i
                while r < n, buf[r] == "`" { r += 1 }
                if r >= n {
                    if final { i = r; continue }
                    return .needMore
                }
                let run = r - i
                var j = r
                var closed = false
                while j < n, buf[j] != "\n" {
                    guard buf[j] == "`" else { j += 1; continue }
                    var k = j
                    while k < n, buf[k] == "`" { k += 1 }
                    if k >= n, !final { return .needMore }      // the run may still grow
                    if k - j == run { i = k; closed = true; break }
                    j = k
                }
                if closed { continue }
                if j >= n, !final { return .needMore }
                i = r                                            // no closing run on this line: the ticks are text
            case "[":
                switch linkEnd(from: i, final: final) {
                case .needMore: return .needMore
                case .literal: i += 1
                case let .end(e): i = e
                }
            case "\"":
                quote.toggle()
                i += 1
            case "“":
                quote = true
                i += 1
            case "”":
                quote = false
                i += 1
            case ".", "?", "!", "…":
                var j = i
                while j < n, Self.terminators.contains(buf[j]) { j += 1 }
                let clusterEnd = j
                var after = quote
                while j < n, Self.closers.contains(buf[j]) {
                    if buf[j] == "\"" { after.toggle() } else if buf[j] == "”" { after = false }
                    j += 1
                }
                if j >= n {
                    if final { return .end(raw: n, consume: n, lineBreak: false, quote: after) }
                    return .needMore
                }
                guard Self.isWS(buf[j]) else { i = clusterEnd; continue }
                let cluster = buf[i..<clusterEnd]
                if cluster.count == 1, c == ".", abbreviation(endingAt: i) { i = clusterEnd; continue }
                let ellipsis = cluster.allSatisfy { $0 == "." || $0 == "…" } && (cluster.count >= 2 || c == "…")
                if ellipsis {
                    if quote { i = clusterEnd; continue }       // a trailing-off inside a quotation goes on
                    var m = j
                    while m < n, Self.isSpace(buf[m]) { m += 1 }
                    if m >= n, !final { return .needMore }
                    if m < n, buf[m].properties.isLowercase { i = clusterEnd; continue }
                }
                return .end(raw: j, consume: j, lineBreak: false, quote: after)
            case ":":
                var j = i + 1
                while j < n, Self.isSpace(buf[j]) { j += 1 }
                if j >= n {
                    if final { return .end(raw: i + 1, consume: n, lineBreak: false, quote: quote) }
                    return .needMore
                }
                if buf[j] == "\n" { return .end(raw: i + 1, consume: j + 1, lineBreak: true, quote: quote) }
                i += 1
            case ",", ";":
                if i >= Self.runOnLimit {
                    if i + 1 >= n {
                        if !final { return .needMore }
                    } else if Self.isWS(buf[i + 1]) {
                        return .end(raw: i + 1, consume: i + 1, lineBreak: false, quote: quote)
                    }
                }
                i += 1
            default:
                i += 1
            }
        }
    }

    private enum Probe { case needMore, literal, end(Int) }

    /// `[text](url)` starting at `i`: where it ends, or why it is not a link (yet).
    private func linkEnd(from i: Int, final: Bool) -> Probe {
        let n = buf.count
        var j = i + 1
        var depth = 1
        while j < n, buf[j] != "\n" {
            if buf[j] == "\\" { j += 2; continue }
            if buf[j] == "[" { depth += 1 } else if buf[j] == "]" {
                depth -= 1
                if depth == 0 { break }
            }
            j += 1
        }
        if j >= n { return final ? .literal : .needMore }
        if buf[j] == "\n" { return .literal }
        if j + 1 >= n { return final ? .literal : .needMore }
        guard buf[j + 1] == "(" else { return .literal }
        var k = j + 2
        var open = 1
        while k < n, buf[k] != "\n" {
            if buf[k] == "(" { open += 1 } else if buf[k] == ")" {
                open -= 1
                if open == 0 { return .end(k + 1) }
            }
            k += 1
        }
        if k >= n { return final ? .literal : .needMore }
        return .literal
    }

    /// The word before the dot at `i` is an abbreviation ("e.g.", "Dr.", "ecc.", "p.es.").
    private func abbreviation(endingAt i: Int) -> Bool {
        var k = i
        while k > 0, !Self.isWS(buf[k - 1]) { k -= 1 }
        var word = String(String.UnicodeScalarView(buf[k..<i])).lowercased()
        while let first = word.unicodeScalars.first, Self.openers.contains(first) { word.removeFirst() }
        return Self.abbreviations.contains(word)
    }

    // MARK: - Character classes

    private static let runOnLimit = 220
    private static let terminators: Set<Unicode.Scalar> = [".", "?", "!", "…"]
    /// Marks that may follow a sentence's last punctuation and still belong to it: `He said "go."` `**Done.**` `(yes.)`
    private static let closers: Set<Unicode.Scalar> = Set("\"'”’)]»*_~".unicodeScalars)
    private static let openers: Set<Unicode.Scalar> = Set("(\"'“‘*_[{".unicodeScalars)
    private static let abbreviations: Set<String> = [
        "e.g", "i.e", "etc", "vs", "dr", "mr", "mrs", "ms", "prof", "jr", "sr", "approx", "ca", "cf",
        "ecc", "es", "p.es", "sig", "sigg", "sig.ra", "sig.na", "dott", "dott.ssa", "proff", "ing", "avv", "cfr",
        "pag", "pagg", "fig", "tel",
    ]

    static func isSpace(_ s: Unicode.Scalar) -> Bool { s == " " || s == "\t" || s == "\u{00A0}" }
    static func isWS(_ s: Unicode.Scalar) -> Bool { isSpace(s) || s == "\n" }
}

/// One block of markdown as text to read aloud. Also the sentence-level renderer the chunker uses, so the two cannot
/// disagree about what a mark or a link sounds like.
public enum SpokenText {
    /// `markdown` as speakable text: marks dropped, links read by their text, paths by their file name, URLs by their
    /// host, tables and code announced instead of read. "" when there is nothing to say.
    public static func render(_ markdown: String, language: VoiceLanguage) -> String {
        var chunker = SpeechChunker(language: language)
        let sentences = chunker.feed(markdown) + chunker.finish()
        var text = ""
        for sentence in sentences {
            if !text.isEmpty { text += " " }
            text += sentence
            // A heading or an item has no full stop of its own; the pause the reader needs is the voice's, so add one.
            if let last = sentence.unicodeScalars.last(where: { !closing.contains($0) }), !stops.contains(last) {
                text += "."
            }
        }
        return text
    }

    private static let closing: Set<Unicode.Scalar> = Set("\"'”’)]»".unicodeScalars)
    private static let stops: Set<Unicode.Scalar> = Set(".?!…:;,".unicodeScalars)

    static func tableAnnouncement(_ language: VoiceLanguage) -> String {
        language == .en ? "There's a table in the chat." : "C'è una tabella nella chat."
    }

    static func codeAnnouncement(_ language: VoiceLanguage) -> String {
        language == .en ? "There's some code in the chat." : "C'è del codice nella chat."
    }

    private static func codeWord(_ language: VoiceLanguage) -> String {
        language == .en ? "some code" : "del codice"
    }

    private static func linkPhrase(_ host: String, _ language: VoiceLanguage) -> String {
        language == .en ? "a link to \(host)" : "un link a \(host)"
    }

    /// Inline markdown of one sentence (it may span soft line breaks) as text to read aloud.
    static func sentence(_ raw: String, language: VoiceLanguage) -> String {
        var lines = raw.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        for k in lines.indices where k > 0 {
            lines[k] = String(lines[k].drop { $0 == ">" || $0 == " " || $0 == "\t" })
        }
        var text = lines.joined(separator: " ")
        for box in ["[ ] ", "[x] ", "[X] "] where text.hasPrefix(box) {   // a task item's check box
            text = String(text.dropFirst(box.count))
            break
        }
        let spoken = tokens(inline(Array(text), language), language)
        return spoken.contains(where: { $0.isLetter || $0.isNumber }) ? spoken : ""
    }

    // MARK: Inline marks

    private static func inline(_ chars: [Character], _ language: VoiceLanguage) -> String {
        var out = ""
        var i = 0
        let n = chars.count
        while i < n {
            let c = chars[i]
            switch c {
            case "\\":
                if i + 1 < n, chars[i + 1].isPunctuation || chars[i + 1].isSymbol {
                    out.append(chars[i + 1])
                    i += 2
                } else {
                    i += 1
                }
            case "`":
                var r = i
                while r < n, chars[r] == "`" { r += 1 }
                let run = r - i
                var j = r
                var found: Int?
                while j < n {
                    guard chars[j] == "`" else { j += 1; continue }
                    var k = j
                    while k < n, chars[k] == "`" { k += 1 }
                    if k - j == run { found = j; break }
                    j = k
                }
                if let end = found {
                    let code = String(chars[r..<end]).trimmingCharacters(in: .whitespaces)
                    out += code.count > 40 ? codeWord(language) : code
                    i = end + run
                } else {
                    i = r
                }
            case "!" where i + 1 < n && chars[i + 1] == "[":
                if let link = linkParts(chars, at: i + 1) {
                    out += inline(link.text, language)
                    i = link.end
                } else {
                    i += 1
                }
            case "[":
                if let link = linkParts(chars, at: i) {
                    out += inline(link.text, language)
                    i = link.end
                } else {
                    i += 1
                }
            case "]":
                i += 1
            case "<":
                if let close = chars[i...].firstIndex(of: ">"), close - i <= 300 {
                    let inner = String(chars[(i + 1)..<close])
                    if inner.hasPrefix("http://") || inner.hasPrefix("https://") {
                        out += " " + linkPhrase(host(of: inner), language) + " "
                        i = close + 1
                        continue
                    }
                    if let first = inner.first, first.isLetter || first == "/", !inner.contains(" ") || inner.contains("=") {
                        out += " "                                   // an HTML tag: <br>, </b>
                        i = close + 1
                        continue
                    }
                }
                out.append(c)
                i += 1
            case "*":
                var r = i
                while r < n, chars[r] == "*" { r += 1 }
                i = r
            case "_":
                var r = i
                while r < n, chars[r] == "_" { r += 1 }
                let before = i > 0 ? chars[i - 1] : " "
                let after = r < n ? chars[r] : " "
                if (before.isLetter || before.isNumber) && (after.isLetter || after.isNumber) {
                    out += String(repeating: "_", count: r - i)     // snake_case is a word
                }
                i = r
            case "~" where i + 1 < n && chars[i + 1] == "~":
                var r = i
                while r < n, chars[r] == "~" { r += 1 }
                i = r
            case "|":
                out += " "
                i += 1
            default:
                if !isEmoji(c) { out.append(c) }
                i += 1
            }
        }
        return out
    }

    /// `[text](url)` at `i`: the text and where the link ends.
    private static func linkParts(_ chars: [Character], at i: Int) -> (text: [Character], end: Int)? {
        let n = chars.count
        var j = i + 1
        var depth = 1
        while j < n {
            if chars[j] == "\\" { j += 2; continue }
            if chars[j] == "[" { depth += 1 } else if chars[j] == "]" {
                depth -= 1
                if depth == 0 { break }
            }
            j += 1
        }
        guard j + 1 < n, chars[j] == "]", chars[j + 1] == "(" else { return nil }
        var k = j + 2
        var open = 1
        while k < n {
            if chars[k] == "(" { open += 1 } else if chars[k] == ")" {
                open -= 1
                if open == 0 { return (Array(chars[(i + 1)..<j]), k + 1) }
            }
            k += 1
        }
        return nil
    }

    private static func isEmoji(_ c: Character) -> Bool {
        c.unicodeScalars.contains { s in
            let v = s.value
            return v >= 0x1F000 || (0x2600...0x27BF).contains(v) || (0x2B00...0x2BFF).contains(v)
                || v == 0xFE0F || v == 0x20E3 || v == 0x200D
        }
    }

    // MARK: Words: links and paths

    private static func tokens(_ text: String, _ language: VoiceLanguage) -> String {
        text.split(whereSeparator: { $0.isWhitespace }).map { convert(String($0), language) }.joined(separator: " ")
    }

    private static let leadMarks: Set<Character> = Set("(\"'“‘[{<")
    private static let trailMarks: Set<Character> = Set(".,;:!?)\"'”’]}>")

    private static func convert(_ token: String, _ language: VoiceLanguage) -> String {
        var core = Substring(token)
        var lead = ""
        var trail = ""
        while let first = core.first, leadMarks.contains(first) { lead.append(first); core = core.dropFirst() }
        while let last = core.last, trailMarks.contains(last) { trail = String(last) + trail; core = core.dropLast() }
        let lower = core.lowercased()
        if lower.hasPrefix("http://") || lower.hasPrefix("https://") || lower.hasPrefix("ftp://")
            || (lower.hasPrefix("www.") && lower.dropFirst(4).contains(".")) {
            return lead + linkPhrase(host(of: String(core)), language) + trail
        }
        if let name = fileName(of: String(core)) { return lead + name + trail }
        return token
    }

    /// The host of a URL, lower case, without credentials or port.
    private static func host(of url: String) -> String {
        var rest = Substring(url)
        if let scheme = rest.range(of: "://") { rest = rest[scheme.upperBound...] }
        if let end = rest.firstIndex(where: { $0 == "/" || $0 == "?" || $0 == "#" }) { rest = rest[..<end] }
        if let at = rest.lastIndex(of: "@") { rest = rest[rest.index(after: at)...] }
        if let colon = rest.lastIndex(of: ":"), rest[rest.index(after: colon)...].allSatisfy(\.isNumber) {
            rest = rest[..<colon]
        }
        return rest.lowercased()
    }

    /// The last part of a path-looking word: `notes/docs/plan.md` → `plan.md`. A word is a path when it starts at a
    /// root (`/`, `~/`, `./`, `../`) or its last part is a file name with an extension, so "and/or", "24/7" and
    /// "TCP/IP" stay as they are.
    private static func fileName(of word: String) -> String? {
        guard word.contains("/"), !word.contains("://"), !word.hasPrefix("//") else { return nil }
        let parts = word.split(separator: "/").map(String.init)
        guard let last = parts.last else { return nil }
        if parts.allSatisfy({ $0.allSatisfy(\.isNumber) }) { return nil }
        let rooted = word.hasPrefix("/") || word.hasPrefix("~/") || word.hasPrefix("./") || word.hasPrefix("../")
        if rooted { return last }
        if let dot = last.lastIndex(of: "."), dot != last.startIndex {
            let ext = last[last.index(after: dot)...]
            if let first = ext.first, first.isLetter, ext.allSatisfy({ $0.isLetter || $0.isNumber }) { return last }
        }
        return nil
    }
}
