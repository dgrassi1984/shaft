import Foundation

/// Markdown cut off mid-text: a reply still streaming, one stopped or failed, or a one-line preview of either. Left as
/// it is, a parser shows the delimiters it cannot match ("found for **Slow, and I") and a dot for a list item with no
/// text yet. `closed` ends what the text left open and drops what has not begun, so a partial text reads as the finished
/// one will. A finished answer is the instance's own text and does not come through here.
public enum PartialMarkdown {
    public static func closed(_ text: String) -> String {
        var lines = text.components(separatedBy: "\n")
        if inOpenFence(lines) { return text }           // an unclosed fence is already code to the end
        while let last = lines.last, isEmptyBlock(last) { lines.removeLast() }
        guard !lines.isEmpty else { return "" }
        // Inline marks do not cross a blank line: only the last paragraph can have one open.
        let start = (lines.lastIndex { $0.allSatisfy(\.isWhitespace) } ?? -1) + 1
        let head = lines[..<start]
        let paragraph = lines[start...].joined(separator: "\n")
        return (head + [closingMarks(paragraph)]).joined(separator: "\n")
    }

    private static func inOpenFence(_ lines: [String]) -> Bool {
        var fence: (mark: Character, length: Int)?
        for line in lines {
            let trimmed = line.drop { $0 == " " }
            guard line.count - trimmed.count <= 3, let mark = trimmed.first, mark == "`" || mark == "~" else { continue }
            let length = trimmed.prefix { $0 == mark }.count
            guard length >= 3 else { continue }
            if let open = fence {
                if mark == open.mark, length >= open.length,
                   trimmed.dropFirst(length).allSatisfy(\.isWhitespace) { fence = nil }
            } else {
                fence = (mark, length)
            }
        }
        return fence != nil
    }

    /// A blank line at the end, a list marker with nothing after it yet, or a heading's marks alone.
    private static func isEmptyBlock(_ line: String) -> Bool {
        let body = line.trimmingCharacters(in: .whitespaces)
        if body.isEmpty || body == "-" || body == "*" || body == "+" { return true }
        if body.allSatisfy({ $0 == "#" }) && body.count <= 6 { return true }
        if let tail = body.last, tail == "." || tail == ")" {
            let number = body.dropLast()
            return !number.isEmpty && number.count <= 9 && number.allSatisfy(\.isNumber)
        }
        return false
    }

    /// `paragraph` with its open code span, emphasis and strikethrough closed, and with a mark that opens nothing yet
    /// taken off its end.
    private static func closingMarks(_ paragraph: String) -> String {
        let characters = Array(paragraph)
        var open: [String] = []
        var index = 0
        var openTick: (at: Int, length: Int)?
        while index < characters.count {
            let character = characters[index]
            if character == "\\" {
                index += 2
                continue
            }
            let run = characters[index...].prefix { $0 == character }.count
            if character == "`" {
                if let tick = openTick {
                    if tick.length == run { openTick = nil }
                } else {
                    openTick = (index, run)
                }
            } else if openTick == nil, character == "*" || character == "_" || character == "~" {
                track(run: run, mark: character, at: index, in: characters, open: &open)
            }
            index += run
        }
        var out = paragraph
        if let tick = openTick {
            let end = out.trimmingCharacters(in: .whitespacesAndNewlines)
            if tick.at + tick.length >= end.count { return String(end.dropLast(tick.length)) }
            return end + String(repeating: "`", count: tick.length)
        }
        while let last = out.last, last.isWhitespace { out.removeLast() }
        // A run of marks at the very end has nothing after it: not yet an opener, and not a closer of anything left open.
        if let last = out.last, "*_~".contains(last) {
            let run = out.reversed().prefix { $0 == last }.count
            let before = out.dropLast(run).last
            if before == nil || before!.isWhitespace || !open.contains(where: { $0.first == last }) {
                out.removeLast(run)
                while let tail = out.last, tail.isWhitespace { out.removeLast() }
            }
        }
        return out + open.reversed().joined()
    }

    private static func track(run: Int, mark: Character, at index: Int, in characters: [Character], open: inout [String]) {
        let before = index > 0 ? characters[index - 1] : nil
        let after = index + run < characters.count ? characters[index + run] : nil
        let opens = after.map { !$0.isWhitespace } ?? false
        var closes = before.map { !$0.isWhitespace } ?? false
        var opensHere = opens
        if mark == "_" {                                 // inside a word, an underscore is a letter (snake_case)
            if before?.isLetter == true || before?.isNumber == true { opensHere = false }
            if after?.isLetter == true || after?.isNumber == true { closes = false }
        }
        if mark == "~" && run < 2 { return }
        var left = run
        while left > 0 {
            if closes, let top = open.last, top.first == mark, top.count <= left {
                open.removeLast()
                left -= top.count
            } else if opensHere {
                let width = mark == "~" ? 2 : min(left, 2)
                open.append(String(repeating: mark, count: width))
                left -= width
            } else {
                return
            }
        }
    }
}

/// The hint under Next cycle in a menu: a menu cuts a long line, so the server's sentence ("a cycle is running and read
/// its inbox when it began: this starts the next one once it ends") gives its last clause, the part that says when.
public enum NextCycleHint {
    public static func short(_ sentence: String) -> String {
        guard let colon = sentence.range(of: ": ", options: .backwards) else { return sentence }
        return String(sentence[colon.upperBound...])
    }
}
