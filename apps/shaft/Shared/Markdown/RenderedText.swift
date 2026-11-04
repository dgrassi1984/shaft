import Foundation

/// Markdown as plain text, as the rendered page reads (the document's text anchors live in, §10.2; what Select text
/// shows). Block breaks become newlines.
enum RenderedText {
    static func plain(_ markdown: String) -> String {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .full,
                                                              failurePolicy: .returnPartiallyParsedIfPossible)
        guard let parsed = try? AttributedString(markdown: markdown, options: options) else { return markdown }
        var out = ""
        var lastBlock: Int?
        for run in parsed.runs {
            let block = run.presentationIntent?.components.first?.identity
            if let lastBlock, let block, block != lastBlock { out += "\n" }
            if let block { lastBlock = block }
            out += String(parsed[run.range].characters)
        }
        // A task list's box is drawn, not text: "[x] " and "[ ] " are not part of what the page reads.
        return out.split(separator: "\n", omittingEmptySubsequences: false).map { line -> String in
            for box in ["[x] ", "[X] ", "[ ] "] where line.hasPrefix(box) { return String(line.dropFirst(box.count)) }
            return String(line)
        }.joined(separator: "\n")
    }
}
