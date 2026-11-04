import Foundation

/// Numeric columns read right-aligned (spec §2.2). Agents rarely write `---:`, so a table's delimiter row is rewritten
/// for a column that states no alignment and holds only numbers; a column that says what it wants keeps it, and text
/// inside a code fence is left alone. Only what is drawn changes, never the source Copy and Reply use.
enum TableColumns {
    static func aligned(_ source: String) -> String {
        guard source.contains("|") else { return source }
        var lines = source.components(separatedBy: "\n")
        var fence: Character?
        var index = 0
        while index < lines.count {
            let line = lines[index].trimmingCharacters(in: .whitespaces)
            if let mark = line.first, mark == "`" || mark == "~", line.hasPrefix(String(repeating: mark, count: 3)) {
                if fence == mark { fence = nil } else if fence == nil { fence = mark }
            } else if fence == nil, index + 1 < lines.count, line.contains("|"), let widths = delimiter(lines[index + 1]),
                      cells(line).count == widths.count {
                var end = index + 2
                while end < lines.count, lines[end].contains("|") { end += 1 }
                lines[index + 1] = rewritten(widths, rows: lines[(index + 2)..<end].map(cells))
                index = end
                continue
            }
            index += 1
        }
        return lines.joined(separator: "\n")
    }

    /// The cells of a delimiter row, or nil when the line is not one.
    private static func delimiter(_ line: String) -> [String]? {
        let parts = cells(line)
        let valid = !parts.isEmpty && parts.allSatisfy { part in
            let body = part.drop { $0 == ":" }
            let dashes = body.hasSuffix(":") ? body.dropLast() : body
            return !dashes.isEmpty && dashes.allSatisfy { $0 == "-" }
        }
        return valid ? parts : nil
    }

    private static func rewritten(_ columns: [String], rows: [[String]]) -> String {
        let parts = columns.enumerated().map { column, part -> String in
            guard !part.contains(":") else { return part }
            let values = rows.compactMap { $0.indices.contains(column) ? $0[column] : nil }.filter { !isBlank($0) }
            return !values.isEmpty && values.allSatisfy(isNumber) ? part + ":" : part
        }
        return "| " + parts.joined(separator: " | ") + " |"
    }

    /// A row's cells, trimmed: `|` splits unless it is escaped, and the outer pipes are optional.
    private static func cells(_ line: String) -> [String] {
        var parts: [String] = [""]
        var escaped = false
        for character in line.trimmingCharacters(in: .whitespaces) {
            if character == "|" && !escaped {
                parts.append("")
            } else {
                parts[parts.count - 1].append(character)
            }
            escaped = character == "\\" && !escaped
        }
        if parts.first?.trimmingCharacters(in: .whitespaces).isEmpty == true { parts.removeFirst() }
        if parts.last?.trimmingCharacters(in: .whitespaces).isEmpty == true { parts.removeLast() }
        return parts.map { $0.trimmingCharacters(in: .whitespaces) }
    }

    private static func isBlank(_ cell: String) -> Bool {
        ["", "-", "–", "—", "n/a", "N/A"].contains(cell)
    }

    /// "12,401.19", "-3.2", "+4", "$1,200", "(12.00)", "12%", "**7**": digits with a number's own punctuation.
    private static func isNumber(_ cell: String) -> Bool {
        let plain = cell.filter { !"*`_".contains($0) }
        guard plain.contains(where: \.isNumber) else { return false }
        return plain.allSatisfy { $0.isNumber || "+-−.,%()$€£ ".contains($0) }
    }
}
