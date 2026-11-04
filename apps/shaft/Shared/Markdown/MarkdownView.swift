import MarkdownUI
import SHAFTKit
import SwiftUI

/// An answer, a message or a document, rendered: GFM on the feed's type and tokens (spec §2.2, §3), with tables that
/// scroll sideways and keep their columns, task lists with a checkbox before the text, and code blocks with their
/// language, syntax colours and a Copy in a header that stays put while the code scrolls.
struct MarkdownText: View {
    let source: String
    var theme: Theme = .shaft
    /// A reply still arriving: nothing here may cost a frame per token, so its code stays plain and its tables as sent.
    @Environment(\.markdownStreaming) private var streaming

    var body: some View {
        Markdown(streaming ? source : TableColumns.aligned(source))
            .markdownTheme(theme)
            .markdownCodeSyntaxHighlighter(CodeColours(plain: streaming))
    }
}

extension EnvironmentValues {
    /// The Markdown below is a reply as it streams, not a finished text.
    @Entry var markdownStreaming = false
}

extension Theme {
    /// The feed's reading type: body text with its leading and paragraph gaps, headings that step down by level and
    /// stay at least as large as a paragraph, quotes on a hairline bar, tables and code in `elevated` blocks.
    @MainActor static let shaft = Theme.basic
        .text { ForegroundColor(.textPrimary) }
        .link { ForegroundColor(.brand) }
        .code {
            FontFamilyVariant(.monospaced)
            FontSize(.em(0.88))
            BackgroundColor(.elevated)
        }
        .heading1 { configuration in
            ShaftHeading(configuration: configuration, size: 1.25, weight: .bold)
        }
        .heading2 { configuration in
            ShaftHeading(configuration: configuration, size: 1.12, weight: .semibold)
        }
        .heading3 { configuration in
            ShaftHeading(configuration: configuration, size: 1.02, weight: .semibold)
        }
        .heading4 { configuration in
            ShaftHeading(configuration: configuration, size: 1.02, weight: .semibold)
        }
        .heading5 { configuration in
            ShaftHeading(configuration: configuration, size: 1.02, weight: .semibold)
        }
        .heading6 { configuration in
            ShaftHeading(configuration: configuration, size: 1.02, weight: .semibold, colour: .textSecondary)
        }
        .paragraph { configuration in
            ShaftParagraph(configuration: configuration)
                .markdownMargin(top: 0, bottom: Design.Text.paragraphSpacing)
        }
        .blockquote { configuration in
            configuration.label
                .markdownTextStyle { ForegroundColor(.textSecondary) }
                .padding(.leading, Design.Space.s12)
                .overlay(alignment: .leading) { AccentBar(color: .hairline) }
                .markdownMargin(top: 0, bottom: Design.Text.paragraphSpacing)
        }
        .codeBlock { configuration in
            CodeBlock(configuration: configuration)
                .markdownMargin(top: Design.Space.s4, bottom: Design.Space.s12)
        }
        .table { configuration in
            MarkdownTable(label: configuration.label)
                .markdownMargin(top: Design.Space.s4, bottom: Design.Space.s12)
        }
        .tableCell { configuration in
            configuration.label
                .markdownTextStyle {
                    FontSize(.em(MarkdownTable.textScale))
                    FontDigitVariant(.monospaced)
                    if configuration.row == 0 {
                        FontWeight(.semibold)
                        ForegroundColor(.textSecondary)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                .modifier(MarkdownTable.Capped())
                .padding(.horizontal, Design.Space.s12)
                .padding(.vertical, Design.Space.s8)
        }
        .listItem { configuration in
            configuration.label.markdownMargin(top: .em(0.2))
        }
        .thematicBreak {
            Rectangle().fill(Color.hairline).frame(height: 1)
                .markdownMargin(top: Design.Space.s12, bottom: Design.Space.s12)
        }
}

/// A paragraph: body text with its leading, and inline code on rounded pills. Text attributes can only fill a square
/// behind a run, so finished text draws the pills itself; a reply still streaming keeps the theme's square fill, as
/// nothing per token may go through a custom renderer.
private struct ShaftParagraph: View {
    let configuration: BlockConfiguration
    @Environment(\.markdownStreaming) private var streaming
    @ScaledMetric(relativeTo: .body) private var bodySize = FontProperties.defaultSize

    var body: some View {
        let text = configuration.label
            .fixedSize(horizontal: false, vertical: true)
            .lineSpacing(Design.Text.lineSpacing)
        // Only the few paragraphs that have inline code leave the system's text drawing.
        if streaming || !configuration.content.renderMarkdown().contains("`") {
            text
        } else {
            text
                .markdownTextStyle(\.code) {
                    FontFamilyVariant(.monospaced)
                    FontSize(.em(0.88))
                }
                .textRenderer(InlineCodePills(bodySize: bodySize))
        }
    }
}

/// Draws an `elevated` pill behind each run of inline code. A renderer is not told a run's attributes; in a paragraph
/// only code is set smaller than the body, so a run's ascent tells it apart (bold, italics and links keep the body's).
private struct InlineCodePills: TextRenderer {
    let bodySize: CGFloat
    /// Into the gap either side of the run: more would slide under the comma that follows it.
    private let inset: CGFloat = 1

    var displayPadding: EdgeInsets {
        EdgeInsets(top: 0, leading: inset, bottom: 0, trailing: inset)
    }

    func draw(layout: Text.Layout, in context: inout GraphicsContext) {
        for line in layout {
            for run in line where run.typographicBounds.ascent < bodySize * 0.92 {
                let shape = run.typographicBounds.rect.insetBy(dx: -inset, dy: 0)
                context.fill(Path(roundedRect: shape, cornerRadius: Design.Radius.pill, style: .continuous),
                             with: .color(.elevated))
            }
        }
        for line in layout { context.draw(line) }
    }
}

/// A heading in prose (spec §2.2): 1 em above, 0.35 em below, a header to VoiceOver.
private struct ShaftHeading: View {
    let configuration: BlockConfiguration
    let size: CGFloat
    let weight: Font.Weight
    var colour = Color.textPrimary

    var body: some View {
        configuration.label
            .markdownMargin(top: .em(Design.Text.headingSpaceAbove), bottom: .em(Design.Text.headingSpaceBelow))
            .markdownTextStyle {
                FontWeight(weight)
                FontSize(.em(size))
                ForegroundColor(colour)
            }
            .accessibilityAddTraits(.isHeader)
    }
}

/// A table: an `elevated` block with hairlines between its rows only. It hugs a table that fits the column and scrolls
/// sideways, inside the block, one that does not: the column never widens and a cell never squeezes.
private struct MarkdownTable: View {
    let label: BlockConfiguration.Label
    /// `.subheadline` over `.body`, as MarkdownUI sizes text in ems of the paragraph.
    static let textScale = 0.88
    /// A cell of a paragraph wraps here rather than make a line a screen wide.
    static let widestCell: CGFloat = 280

    var body: some View {
        ViewThatFits(in: .horizontal) {
            table
            ScrollView(.horizontal) { table }.sidewaysFade()
        }
        .elevatedBlock()
        .clipShape(.rect(cornerRadius: Design.Radius.card, style: .continuous))
    }

    private var table: some View {
        label
            .fixedSize(horizontal: false, vertical: true)
            .markdownTableBorderStyle(.init(.insideHorizontalBorders, color: .hairline, width: 1))
    }

    /// A cell's text, wrapped at `widestCell`. Unlike a `frame(maxWidth:)` it stays as wide as its text: a cell that
    /// fills its column would take the column's alignment away from it.
    struct Capped: ViewModifier {
        func body(content: Content) -> some View {
            CappedLayout { content }
        }
    }

    private struct CappedLayout: Layout {
        private func proposal(_ proposal: ProposedViewSize) -> ProposedViewSize {
            ProposedViewSize(width: min(proposal.width ?? .infinity, MarkdownTable.widestCell), height: nil)
        }

        func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
            subviews.first?.sizeThatFits(self.proposal(proposal)) ?? .zero
        }

        func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
            subviews.first?.place(at: bounds.origin, proposal: self.proposal(proposal))
        }
    }
}

/// A fenced code block: its language and Copy in a header row that stays in place, the code below scrolling sideways.
struct CodeBlock: View {
    let configuration: CodeBlockConfiguration

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: Design.Space.s8) {
                Text(CodeColours.language(configuration.language) ?? String(localized: "text"))
                    .font(Design.Text.pill)
                    .foregroundStyle(Color.textSecondary)
                    .lineLimit(1)
                Spacer(minLength: 0)
                CopyButton(text: configuration.content, label: "Copy code")
            }
            .padding(.leading, Design.Space.s12)
            .padding(.trailing, Design.Space.s4)
            .frame(minHeight: Design.Space.s24 + Design.Space.s8)
            Rectangle().fill(Color.hairline).frame(height: 1)
            ScrollView(.horizontal) {
                configuration.label
                    .fixedSize(horizontal: false, vertical: true)
                    .relativeLineSpacing(.em(0.15))
                    .markdownTextStyle {
                        FontFamilyVariant(.monospaced)
                        FontSize(.em(0.88))
                    }
                    .padding(Design.Space.s12)
            }
            .sidewaysFade()
        }
        .elevatedBlock()
        .clipShape(.rect(cornerRadius: Design.Radius.card, style: .continuous))
    }
}

/// Syntax colours from SHAFTKit's tokeniser, for a finished block; an unknown language stays plain.
struct CodeColours: CodeSyntaxHighlighter {
    var plain = false

    /// A fence's language: the first word of its info string ("python title=x").
    static func language(_ info: String?) -> String? {
        info?.split(whereSeparator: \.isWhitespace).first.map(String.init)
    }

    func highlightCode(_ code: String, language: String?) -> Text {
        guard !plain else { return Text(code) }
        let language = Self.language(language)
        let diff = language?.lowercased() == "diff"
        var out = AttributedString()
        for token in CodeTokens.tokens(in: code, language: language) {
            var piece = AttributedString(code[token.range])
            switch token.kind {
            case .keyword: piece.foregroundColor = diff ? .codeNumber : .codeKeyword   // a removed line, in a diff
            case .string: piece.foregroundColor = .codeString
            case .comment: piece.foregroundColor = .codeComment
            case .number: piece.foregroundColor = .codeNumber
            case .plain: break
            }
            out += piece
        }
        return Text(out)
    }
}

/// A footer's icon: scales with the metadata from its footnote size, its 44 pt target taking no layout (`quietHitArea`).
struct QuietIcon: View {
    let symbol: String
    var colour = Color.textTertiary
    @ScaledMetric(relativeTo: .caption2) private var size = Design.Footer.icon

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: size))
            .foregroundStyle(colour)
            .frame(minWidth: Design.Space.s24 + Design.Space.s12, minHeight: Design.Space.s24 + Design.Space.s4)
            .quietHitArea()
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
            QuietIcon(symbol: copied ? "checkmark" : "doc.on.doc")
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
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            ForEach(Array(OperatorText.blocks(text).enumerated()), id: \.offset) { _, block in
                switch block {
                case .quote(let body):
                    Text(body)
                        .foregroundStyle(Color.textSecondary)
                        .padding(.leading, Design.Space.s12)
                        .overlay(alignment: .leading) { AccentBar(color: .textSecondary) }
                        .fixedSize(horizontal: false, vertical: true)
                case .code(let body):
                    ScrollView(.horizontal) {
                        Text(body)
                            .font(Design.Text.code)
                            .padding(Design.Space.s8)
                    }
                    .sidewaysFade()
                    .elevatedBlock()
                    .clipShape(.rect(cornerRadius: Design.Radius.card, style: .continuous))
                case .text(let body):
                    Text(body)
                        .lineSpacing(Design.Text.lineSpacing)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}
