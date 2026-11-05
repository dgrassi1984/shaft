import MarkdownUI
import SHAFTKit
import SwiftUI

/// A document as the chat draws Markdown, with its commented passages highlighted (`CommentHighlight`); a tap on one
/// opens its threads. Marking waits for nothing: the page shows the plain text until the marks arrive, and the two read
/// the same, so nothing moves.
struct DocumentMarkdown: View {
    let doc: DocumentDetail
    var opens: ([String]) -> Void
    @State private var marked: (sha: String, source: String)?

    private var source: String {
        if let marked, marked.sha == doc.sha { return marked.source }
        return doc.text
    }

    /// What the marks depend on: the text, and which threads are open on what.
    private var marks: [String] {
        [doc.sha] + doc.threads.filter { $0.state == "open" }.map { "\($0.id) \($0.anchor?.exact ?? "")" }
    }

    var body: some View {
        MarkdownText(source: source, theme: .document)
            .environment(\.openURL, OpenURLAction { url in
                guard let threads = CommentHighlight.threads(in: url) else { return .systemAction }
                opens(threads)
                return .handled
            })
            .task(id: marks) {
                let (sha, text, threads) = (doc.sha, doc.text, doc.threads)
                let source = await Task.detached(priority: .userInitiated) {
                    CommentHighlight.marked(TableColumns.aligned(AgentMarkdown.normalized(text)), threads: threads)
                }.value
                if !Task.isCancelled { marked = (sha, source) }
            }
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
