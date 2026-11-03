import MarkdownUI
import SHAFTKit
import SwiftUI

/// An answer, a message or a document, rendered: GFM with tables that scroll sideways and keep their columns, task
/// lists with a checkbox before the text, and code blocks with a Copy that stays at their corner while the code
/// scrolls (web-and-api.md, "The web UI").
struct MarkdownText: View {
    let source: String

    var body: some View {
        Markdown(source)
            .markdownTheme(.shaft)
            .markdownCodeSyntaxHighlighter(.plainText)
    }
}

extension Theme {
    /// The basic theme with headings that step down by level and stay at least as large as a paragraph, quotes as the
    /// web draws them, scrolling tables and code blocks with Copy.
    @MainActor static let shaft = Theme.basic
        .heading1 { configuration in
            configuration.label
                .markdownMargin(top: .em(1.2), bottom: .em(0.5))
                .markdownTextStyle {
                    FontWeight(.bold)
                    FontSize(.em(1.35))
                }
                .accessibilityAddTraits(.isHeader)
        }
        .heading2 { configuration in
            configuration.label
                .markdownMargin(top: .em(1.1), bottom: .em(0.45))
                .markdownTextStyle {
                    FontWeight(.bold)
                    FontSize(.em(1.2))
                }
                .accessibilityAddTraits(.isHeader)
        }
        .heading3 { configuration in
            configuration.label
                .markdownMargin(top: .em(1), bottom: .em(0.4))
                .markdownTextStyle {
                    FontWeight(.semibold)
                    FontSize(.em(1.1))
                }
                .accessibilityAddTraits(.isHeader)
        }
        .heading4 { configuration in
            configuration.label
                .markdownMargin(top: .em(0.9), bottom: .em(0.35))
                .markdownTextStyle { FontWeight(.semibold) }
                .accessibilityAddTraits(.isHeader)
        }
        .heading5 { configuration in
            configuration.label
                .markdownMargin(top: .em(0.9), bottom: .em(0.35))
                .markdownTextStyle { FontWeight(.semibold) }
                .accessibilityAddTraits(.isHeader)
        }
        .heading6 { configuration in
            configuration.label
                .markdownMargin(top: .em(0.9), bottom: .em(0.35))
                .markdownTextStyle {
                    FontWeight(.semibold)
                    ForegroundColor(.secondary)
                }
                .accessibilityAddTraits(.isHeader)
        }
        .paragraph { configuration in
            configuration.label
                .fixedSize(horizontal: false, vertical: true)
                .relativeLineSpacing(.em(0.15))
                .markdownMargin(top: .zero, bottom: .em(0.7))
        }
        .blockquote { configuration in
            configuration.label
                .markdownTextStyle { ForegroundColor(.secondary) }
                .relativePadding(.leading, length: .em(0.8))
                .overlay(alignment: .leading) {
                    Rectangle().fill(Color.secondary.opacity(0.5)).frame(width: 3)
                }
                .markdownMargin(top: .zero, bottom: .em(0.7))
        }
        .codeBlock { configuration in
            CodeBlock(configuration: configuration)
                .markdownMargin(top: .zero, bottom: .em(0.7))
        }
        .table { configuration in
            ScrollView(.horizontal) {
                configuration.label
                    .fixedSize(horizontal: false, vertical: true)
                    .markdownTableBorderStyle(.init(color: .secondary.opacity(0.4)))
            }
            .markdownMargin(top: .zero, bottom: .em(0.7))
        }
        .tableCell { configuration in
            configuration.label
                .markdownTextStyle {
                    if configuration.row == 0 { FontWeight(.semibold) }
                }
                .fixedSize(horizontal: false, vertical: true)
                .relativePadding(.horizontal, length: .em(0.6))
                .relativePadding(.vertical, length: .em(0.3))
        }
        .listItem { configuration in
            configuration.label.markdownMargin(top: .em(0.15))
        }
}

/// A fenced code block: monospaced, scrolling sideways, its Copy fixed at the top corner.
struct CodeBlock: View {
    let configuration: CodeBlockConfiguration
    @State private var copied = false

    var body: some View {
        ScrollView(.horizontal) {
            configuration.label
                .fixedSize(horizontal: false, vertical: true)
                .relativeLineSpacing(.em(0.15))
                .markdownTextStyle {
                    FontFamilyVariant(.monospaced)
                    FontSize(.em(0.88))
                }
                .padding(.vertical, 10)
                .padding(.leading, 12)
                .padding(.trailing, 48)
        }
        .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
        .overlay(alignment: .topTrailing) {
            CopyButton(text: configuration.content, label: "Copy code")
        }
    }
}

/// A Copy icon that says "Copied" for a moment.
struct CopyButton: View {
    let text: String
    var label: LocalizedStringKey = "Copy"
    @State private var copied = false

    var body: some View {
        Button {
            Clipboard.copy(text)
            copied = true
            Task {
                try? await Task.sleep(for: .seconds(1.5))
                copied = false
            }
        } label: {
            Image(systemName: copied ? "checkmark" : "doc.on.doc")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .touchTarget()
        }
        .buttonStyle(.plain)
        .accessibilityLabel(copied ? Text("Copied") : Text(label))
    }
}

/// What the operator wrote (§4.4 `user.text`): not Markdown. Quoted lines as a quote, fenced code as code, the rest as
/// plain text with its line breaks.
struct OperatorTextView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(OperatorText.blocks(text).enumerated()), id: \.offset) { _, block in
                switch block {
                case .quote(let body):
                    Text(body)
                        .foregroundStyle(.secondary)
                        .padding(.leading, 10)
                        .overlay(alignment: .leading) {
                            Rectangle().fill(Color.secondary.opacity(0.5)).frame(width: 3)
                        }
                        .fixedSize(horizontal: false, vertical: true)
                case .code(let body):
                    ScrollView(.horizontal) {
                        Text(body)
                            .font(.system(.callout, design: .monospaced))
                            .padding(8)
                    }
                    .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
                case .text(let body):
                    Text(body).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

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
        return out
    }
}
