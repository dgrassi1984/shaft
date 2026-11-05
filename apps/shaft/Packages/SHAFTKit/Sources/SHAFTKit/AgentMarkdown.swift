import Foundation

/// Rewrites agent bullets, lines of points and sequential numbered sections as Markdown for drawing.
/// Normalization is idempotent and leaves code alone.
public enum AgentMarkdown {
    public static func normalized(_ source: String) -> String {
        guard source.contains("•") || source.contains("·") || match(numberedLinePattern, source) != nil else {
            return source
        }
        var out: [String] = []
        var fence: Fence?
        var lastNumber: Int?
        var previousPlain = false
        var previousQuote = ""
        var listIndent: Int?
        var separated = true
        var inTable = false
        let scanning = source.utf8.contains(13)
            ? source.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n") : source
        let lines = scanning.components(separatedBy: "\n")
        for (index, line) in lines.enumerated() {
            if let open = fence {
                let (prefix, content) = quoted(line, depth: open.quoteDepth)
                if prefix.filter({ $0 == ">" }).count == open.quoteDepth {
                    if open.closes(content) { fence = nil }
                    out.append(line)
                    previousPlain = false
                    continue
                }
                fence = nil
            }
            let (quote, body) = quoted(line)
            if quote != previousQuote { previousPlain = false; listIndent = nil }
            previousQuote = quote
            let tableRow = body.contains("|") && (inTable || (index + 1 < lines.count &&
                match(tableSeparator, quoted(lines[index + 1]).1) != nil))
            inTable = tableRow
            let (indent, trimmed) = indentation(body)
            let base = listIndent ?? 0
            if indent - base <= 3, let mark = trimmed.first, mark == "`" || mark == "~" {
                let length = trimmed.prefix { $0 == mark }.count
                if length >= 3, mark != "`" || !trimmed.dropFirst(length).contains("`") {
                    fence = Fence(mark: mark, length: length, base: base, quoteDepth: quote.filter { $0 == ">" }.count)
                    out.append(line)
                    previousPlain = false
                    continue
                }
            }
            if trimmed.isEmpty {
                out.append(line)
                previousPlain = false
                separated = true
                continue
            }
            let indentedCode = indent >= base + 4
            if indentedCode || tableRow {
                out.append(line)
                previousPlain = false
                continue
            }
            let item = match(itemPattern, body).flatMap { match -> NSTextCheckingResult? in
                let content = (body as NSString).substring(with: match.range(at: 4))
                return content.allSatisfy(\.isWhitespace) ? nil : match
            }
            let marker = item.map { (body as NSString).substring(with: $0.range(at: 2)) }
            let number = marker.flatMap { Int($0.dropLast()) }
            let interrupts = number == 1 || (lastNumber != nil && number == lastNumber.map { $0 + 1 })
            let isItem = item != nil && (number == nil || !previousPlain || interrupts)
            if let number, isItem {
                if previousPlain && interrupts && number != 1 { out.append(quote) }
                lastNumber = number
            }
            var rewritten = body
            var content = body
            var prefix = ""
            if let item, isItem {
                let ns = body as NSString
                content = ns.substring(with: item.range(at: 4))
                prefix = ns.substring(to: item.range(at: 4).location)
                if marker == "•" {
                    let range = item.range(at: 2)
                    rewritten = ns.replacingCharacters(in: range, with: "-")
                    prefix = String(rewritten.prefix(prefix.count))
                }
                listIndent = prefix.count
            } else if let width = listIndent, indent < width, separated {
                listIndent = nil
            }
            separated = false
            let block = match(headingPattern, body) != nil || match(rulePattern, body) != nil
                || (previousPlain && trimmed.allSatisfy { $0 == "=" })
            if !block, match(referencePattern, body) == nil, let (lead, points) = points(content) {
                out.append(quote + prefix + lead)
                // Nested points stay tight within the parent item.
                if !isItem { out.append(quote) }
                let nested = isItem ? String(repeating: " ", count: prefix.count) : String(repeating: " ", count: indent)
                out.append(contentsOf: points.map { quote + nested + "- " + $0 })
                if !isItem { listIndent = indent + 2 }
                previousPlain = false
            } else {
                out.append(quote + rewritten)
                previousPlain = !isItem && !block && listIndent == nil
            }
        }
        let result = out.joined(separator: "\n")
        return result == source ? source : result
    }

    private struct Fence {
        let mark: Character
        let length: Int
        let base: Int
        let quoteDepth: Int

        func closes(_ line: String) -> Bool {
            let (indent, text) = AgentMarkdown.indentation(line)
            let run = text.prefix { $0 == mark }.count
            return indent - base <= 3 && run >= length && text.dropFirst(run).allSatisfy(\.isWhitespace)
        }
    }

    private static func indentation(_ line: String) -> (Int, Substring) {
        let whitespace = line.prefix { $0 == " " || $0 == "\t" }
        return (whitespace.reduce(0) { $0 + ($1 == "\t" ? 4 : 1) }, line.dropFirst(whitespace.count))
    }

    private static let referencePattern = try! NSRegularExpression(pattern: #"^ {0,3}\[[^\]]+\]:"#)
    private static let numberedLinePattern = try! NSRegularExpression(pattern: #"(?m)^[ >]*[0-9]{1,9}[.)][ \t]+"#)
    private static let itemPattern = try! NSRegularExpression(pattern: #"^( *)([-*+•]|[0-9]{1,9}[.)])([ \t]+)(.*)$"#)
    private static let tableSeparator = try! NSRegularExpression(pattern: #"^ *(?:\| *)?:?-+:? *(?:\| *:?-+:? *)*(?:\| *)?$"#)
    private static let headingPattern = try! NSRegularExpression(pattern: #"^ {0,3}#{1,6}[ \t]+"#)
    private static let rulePattern = try! NSRegularExpression(pattern: #"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$"#)

    private static func match(_ pattern: NSRegularExpression, _ text: String) -> NSTextCheckingResult? {
        pattern.firstMatch(in: text, range: NSRange(text.startIndex..., in: text))
    }

    private static func quoted(_ line: String, depth: Int = .max) -> (String, String) {
        var index = line.startIndex
        var end = index
        var count = 0
        while index < line.endIndex, count < depth {
            var spaces = 0
            while index < line.endIndex, line[index] == " ", spaces < 3 {
                spaces += 1
                index = line.index(after: index)
            }
            guard index < line.endIndex, line[index] == ">" else { break }
            index = line.index(after: index)
            if index < line.endIndex, line[index] == " " { index = line.index(after: index) }
            end = index
            count += 1
        }
        return (String(line[..<end]), String(line[end...]))
    }

    /// Cut only unshielded, space-delimited dots, with the last colon before the first cut as the lead.
    private static func points(_ text: String) -> (String, [String])? {
        guard text.contains("·"), text.contains(": ") else { return nil }
        let chars = Array(text)
        var boundaries = [Int](repeating: 0, count: chars.count + 1)
        func shield(_ range: Range<Int>) {
            boundaries[range.lowerBound] += 1
            boundaries[range.upperBound] -= 1
        }
        var ticks: [Int: [Range<Int>]] = [:]
        var i = 0
        while i < chars.count {
            if chars[i] == "`" {
                let start = i
                while i < chars.count, chars[i] == "`" { i += 1 }
                ticks[i - start, default: []].append(start..<i)
            } else { i += 1 }
        }
        // Equal tick runs pair in order; a longer run inside a span is literal code.
        var tickEnds: [Int: Int] = [:]
        for runs in ticks.values {
            for (a, b) in zip(runs, runs.dropFirst()) { tickEnds[a.lowerBound] = b.upperBound }
        }
        var parens: [Int] = []
        var brackets: [Int] = []
        var angle: Int?
        i = 0
        while i < chars.count {
            if let end = tickEnds[i] {
                shield(i..<end)
                i = end
                continue
            }
            switch chars[i] {
            case "[": brackets.append(i)
            case "]":
                if let start = brackets.popLast(), i + 1 < chars.count, chars[i + 1] == "(" {
                    shield(start..<(i + 1))
                }
            case "(": parens.append(i)
            case ")":
                if let start = parens.popLast() {
                    shield(start..<(i + 1))
                }
            case "<": angle = i
            case ">":
                if let start = angle, ["http://", "https://", "mailto:"].contains(where: { String(chars[(start + 1)..<i]).hasPrefix($0) }) {
                    shield(start..<(i + 1))
                }
                angle = nil
            default: break
            }
            i += 1
        }
        func space(_ c: Character) -> Bool { c == " " || c == "\t" }
        var cuts: [Int] = []
        var colon: Int?
        var shielding = 0
        for k in chars.indices {
            shielding += boundaries[k]
            if shielding > 0 { continue }
            if cuts.isEmpty, chars[k] == ":", k + 1 < chars.count, chars[k + 1] == " " { colon = k }
            if chars[k] == "·", k > 0, k + 1 < chars.count, space(chars[k - 1]), space(chars[k + 1]) { cuts.append(k) }
        }
        guard cuts.count >= 2, let colon else { return nil }
        func part(_ range: Range<Int>) -> String { String(chars[range]).trimmingCharacters(in: .whitespaces) }
        let lead = part(0..<(colon + 1))
        guard !part(0..<colon).isEmpty else { return nil }
        var items: [String] = []
        var start = colon + 2
        for end in cuts + [chars.count] {
            let value = part(start..<end)
            guard !value.isEmpty else { return nil }
            items.append(value)
            start = end + 1
        }
        return (lead, items)
    }
}
