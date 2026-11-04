import SHAFTKit
import SwiftUI

// The platform seam of a message's text in the feed. iOS draws it with MarkdownUI (MarkdownText, OperatorTextView) and
// selects through the long-press menu's Select text; the Mac draws it in AppKit text views that select natively,
// in place, with ↩ Reply / + Quote on the selection (macOS/MessageTextView.swift).

/// What a message's menu offers: its whole Markdown source (Copy, Reply), and its thumbs where they apply. The
/// `messageMenu` modifier puts it in the environment of the message's text, whose own menu (the Mac's right-click
/// on the text view) draws from it.
struct MessageMenuContext {
    var source: String
    var rating: Rating?
    var k: Int?
    var file: String?
    var rateable: Bool
}

extension EnvironmentValues {
    @Entry var messageMenu: MessageMenuContext? = nil
}

#if os(iOS)

/// An answer or a message the instance sent: rendered Markdown.
struct MessageMarkdown: View {
    let source: String

    var body: some View {
        MarkdownText(source: source)
    }
}

/// The operator's own message: its quotes, code and plain text.
struct MessageOperatorText: View {
    let text: String

    var body: some View {
        OperatorTextView(text: text)
    }
}

#endif
