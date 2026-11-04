import MarkdownUI
import SHAFTKit
import SwiftUI

/// A document as the chat draws Markdown, with its commented passages highlighted (`CommentHighlight`); a tap on one
/// opens its threads. Marking waits for nothing: the page shows the plain text until the marks arrive, and the two read
/// the same, so nothing moves.
///
/// It is drawn a top-level block at a time in a lazy stack (`DocumentBlocks`): only what is on screen is built, measured
/// and drawn, whatever the document's size. `header` and `footer` sit in the same stack, so that a `ScrollViewProxy` can
/// reach a row of theirs from anywhere in the page: a lazy stack finds its direct rows, drawn yet or not.
struct DocumentMarkdown<Header: View, Footer: View>: View {
    let doc: DocumentDetail
    var opens: ([String]) -> Void
    @ViewBuilder var header: Header
    @ViewBuilder var footer: Footer
    @State private var marked: (sha: String, blocks: [DocumentBlock])?
    @ScaledMetric(relativeTo: .body) private var bodySize = FontProperties.defaultSize

    private var blocks: [DocumentBlock] {
        if let marked, marked.sha == doc.sha { return marked.blocks }
        return DocumentBlocks.split(doc.text)
    }

    /// What the marks depend on: the text, and which threads are open on what.
    private var marks: [String] {
        [doc.sha] + doc.threads.filter { $0.state == "open" }.map { "\($0.id) \($0.anchor?.exact ?? "")" }
    }

    var body: some View {
        let blocks = blocks
        LazyVStack(alignment: .leading, spacing: 0) {
            header
            ForEach(Array(blocks.enumerated()), id: \.element.id) { index, block in
                MarkdownText(source: block.text, theme: .document)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, index == 0 ? 0 : gap(after: blocks[index - 1], before: block))
            }
            footer
        }
        .environment(\.openURL, OpenURLAction { url in
            guard let threads = CommentHighlight.threads(in: url) else { return .systemAction }
            opens(threads)
            return .handled
        })
        .task(id: marks) {
            let (sha, text, threads) = (doc.sha, doc.text, doc.threads)
            let marked = await Task.detached(priority: .userInitiated) {
                DocumentBlocks.split(CommentHighlight.marked(TableColumns.aligned(text), threads: threads))
            }.value
            if !Task.isCancelled { self.marked = (sha, marked) }
        }
    }

    /// What `markdownMargin` would have left between the two, were they in one Markdown view.
    private func gap(after above: DocumentBlock, before below: DocumentBlock) -> CGFloat {
        max(margins(above.last).bottom, margins(below.first).top)
    }

    private func margins(_ kind: DocumentBlock.Kind) -> (top: CGFloat, bottom: CGFloat) {
        switch kind {
        case .paragraph, .quote: BlockGap.paragraph
        case .code, .table: BlockGap.panel
        case .rule: BlockGap.rule
        case .list: ((BlockGap.listItemTop * bodySize).rounded(), BlockGap.paragraph.bottom)
        case .heading(let level):
            ((Design.Text.headingSpaceAbove * headingSize(level)).rounded(),
             (Design.Text.headingSpaceBelow * headingSize(level)).rounded())
        }
    }

    private func headingSize(_ level: Int) -> CGFloat {
        BlockGap.headingScale[level - 1] * bodySize
    }
}

extension Theme {
    /// The chat's theme, reading a document's marks. `CommentHighlight` marks a passage as a strikethrough in a link of
    /// its own, the one place a text style can see which link it is in; a real strikethrough stays one.
    @MainActor fileprivate static let document: Theme = {
        var theme = Theme.shaft
        theme.strikethrough = CommentMark(unmarked: theme.strikethrough)
        theme.code = CommentMark(unmarked: theme.code, keepsStyle: true)
        return theme
    }()
}

/// A passage's soft highlight, with the reading colour the link would otherwise take away.
private struct CommentMark: TextStyle {
    let unmarked: any TextStyle
    var keepsStyle = false

    func _collectAttributes(in attributes: inout AttributeContainer) {
        let marked = CommentHighlight.threads(in: attributes.link) != nil
        if !marked || keepsStyle { unmarked._collectAttributes(in: &attributes) }
        if marked {
            attributes.backgroundColor = .passage
            attributes.foregroundColor = .textPrimary
        }
    }
}
