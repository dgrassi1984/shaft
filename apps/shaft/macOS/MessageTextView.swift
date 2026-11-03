import AppKit
import SHAFTKit
import SwiftUI

// The Mac half of Shared's message-text seam (Shared/Platform/MessageText.swift): a message's text in an AppKit text
// view that selects natively, in place and across paragraphs. A selection shows ↩ Reply (+ Quote once a card waits)
// beside it, as the web's selection button does; its right-click menu has ↩ Reply / + Quote on the selection, the
// system's Copy and Look Up, then Copy and Reply for the whole message, Copy code inside a code block, and the thumbs.

/// An answer or a message the instance sent: its Markdown, rendered (MarkdownAttributed).
struct MessageMarkdown: View {
    let source: String

    var body: some View {
        MessageTextView(source: source, markdown: true)
    }
}

/// The operator's own message: its quotes, code and plain text.
struct MessageOperatorText: View {
    let text: String

    var body: some View {
        MessageTextView(source: text, markdown: false)
    }
}

struct MessageTextView: NSViewRepresentable {
    let source: String
    let markdown: Bool
    @Environment(\.messageMenu) private var menu
    @Environment(ChatScreenModel.self) private var screen

    func makeNSView(context: Context) -> MessageNSTextView {
        let view = MessageNSTextView(usingTextLayoutManager: false)
        view.isEditable = false
        view.isSelectable = true
        view.isRichText = true
        view.drawsBackground = false
        view.textContainerInset = .zero
        view.textContainer?.lineFragmentPadding = 0
        view.textContainer?.widthTracksTextView = true
        view.isVerticallyResizable = false
        view.isHorizontallyResizable = false
        view.clipsToBounds = false
        view.linkTextAttributes = [.foregroundColor: NSColor.linkColor, .underlineStyle: NSUnderlineStyle.single.rawValue,
                                   .cursor: NSCursor.pointingHand]
        view.delegate = context.coordinator
        view.coordinator = context.coordinator
        context.coordinator.view = view
        return view
    }

    func updateNSView(_ view: MessageNSTextView, context: Context) {
        context.coordinator.screen = screen
        context.coordinator.menu = menu
        context.coordinator.show(source, markdown: markdown)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: MessageNSTextView, context: Context) -> CGSize? {
        var width = proposal.width ?? 600
        if !width.isFinite || width <= 0 { width = 600 }
        return context.coordinator.size(fitting: width)
    }

    func makeCoordinator() -> Coordinator { Coordinator(screen: screen) }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        /// The chat screen and the message's menu, as of the last update.
        var screen: ChatScreenModel
        var menu: MessageMenuContext?
        weak var view: MessageNSTextView?
        private var shown: (source: String, markdown: Bool)?
        private(set) var rendered = MarkdownAttributed.Rendered(text: NSAttributedString(), codeBlocks: [])
        private var sizes: [CGFloat: CGSize] = [:]

        init(screen: ChatScreenModel) { self.screen = screen }

        var quoteTitle: String {
            screen.chat.draft.quotes.isEmpty ? String(localized: "\u{21A9}\u{FE0E} Reply") : String(localized: "+ Quote")
        }

        /// Draw `source` unless it is what is shown: a refresh of the feed keeps a selection made meanwhile.
        func show(_ source: String, markdown: Bool) {
            guard shown?.source != source || shown?.markdown != markdown, let view else {
                view?.updateReplyButton()
                return
            }
            shown = (source, markdown)
            rendered = markdown ? MarkdownAttributed.render(source) : MarkdownAttributed.operatorText(source)
            sizes = [:]
            view.textStorage?.setAttributedString(rendered.text)
            view.invalidateIntrinsicContentSize()
            view.updateReplyButton()
        }

        /// The text's size laid out `width` wide: as wide as its longest line (a short message's bubble hugs it),
        /// as tall as it needs.
        func size(fitting width: CGFloat) -> CGSize {
            let key = width.rounded()
            if let size = sizes[key] { return size }
            let storage = NSTextStorage(attributedString: rendered.text)
            let layout = NSLayoutManager()
            let container = NSTextContainer(size: NSSize(width: key, height: .greatestFiniteMagnitude))
            container.lineFragmentPadding = 0
            layout.addTextContainer(container)
            storage.addLayoutManager(layout)
            layout.ensureLayout(for: container)
            let used = layout.usedRect(for: container)
            let size = CGSize(width: min(key, (used.maxX + 1).rounded(.up)), height: max(used.height.rounded(.up), 1))
            sizes[key] = size
            return size
        }

        func textViewDidChangeSelection(_ notification: Notification) {
            view?.updateReplyButton()
        }

        /// ↩ Reply / + Quote on the selection: a card in the composer, its answer focused.
        func quoteSelection() {
            guard let view, let selected = view.selectedText else { return }
            screen.quote(selected)
            view.setSelectedRange(NSRange(location: NSMaxRange(view.selectedRange()), length: 0))
        }

        func textView(_ view: NSTextView, menu: NSMenu, for event: NSEvent, at charIndex: Int) -> NSMenu? {
            ReadOnlyTextMenu.trim(menu)
            var top: [NSMenuItem] = []
            // A selection made before the right-click (not the word the click itself selects, for Look Up).
            if !screen.archived, let message = view as? MessageNSTextView, message.selectedBeforeMenu,
               message.selectedText != nil {
                top.append(MenuAction.item(quoteTitle, symbol: "arrowshape.turn.up.left") { [weak self] in
                    self?.quoteSelection()
                })
            }
            for (index, item) in top.enumerated() { menu.insertItem(item, at: index) }
            if !top.isEmpty { menu.insertItem(.separator(), at: top.count) }

            var bottom: [NSMenuItem] = []
            let length = rendered.text.length
            if let code = rendered.codeBlocks.first(where: {
                NSLocationInRange(charIndex, NSIntersectionRange($0.range, NSRange(location: 0, length: length)))
            }) {
                bottom.append(MenuAction.item(String(localized: "Copy code"), symbol: "chevron.left.forwardslash.chevron.right") {
                    Clipboard.copy(code.code)
                })
            }
            if let context = self.menu, !context.source.isEmpty {
                let source = context.source
                bottom.append(MenuAction.item(String(localized: "Copy message"), symbol: "doc.on.doc") {
                    Clipboard.copy(source)
                })
                if !screen.archived {
                    bottom.append(MenuAction.item(String(localized: "Reply to message"), symbol: "arrowshape.turn.up.left") {
                        [weak self] in self?.screen.reply(with: source)
                    })
                }
            }
            if let context = self.menu, context.rateable {
                let rating = context.rating
                bottom.append(.separator())
                for thumb in ["up", "down"] {
                    let lit = rating?.thumb == thumb
                    let title = thumb == "up" ? (lit ? "Remove thumbs up" : "Thumbs up")
                                              : (lit ? "Remove thumbs down" : "Thumbs down")
                    bottom.append(MenuAction.item(String(localized: String.LocalizationValue(title)),
                                                  symbol: "hand.thumbs\(thumb)\(lit ? ".fill" : "")") { [weak self] in
                        self?.screen.thumb(thumb, k: context.k, file: context.file, current: rating)
                    })
                }
            }
            if !bottom.isEmpty {
                menu.addItem(.separator())
                for item in bottom { menu.addItem(item) }
            }
            return menu
        }
    }
}

/// The message's text view: the ↩ Reply button beside a selection, and no selection left behind once the focus
/// moves on (the web clears it as another selection starts).
final class MessageNSTextView: NSTextView {
    weak var coordinator: MessageTextView.Coordinator?
    private let replyButton = NSButton(title: "", target: nil, action: nil)

    /// The selected text, trimmed; nil when nothing is selected.
    var selectedText: String? {
        let range = selectedRange()
        guard range.length > 0, NSMaxRange(range) <= (string as NSString).length else { return nil }
        let text = (string as NSString).substring(with: range).trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }

    /// Whether text was selected when the right-click came (a right-click on a word selects it, for Look Up).
    private(set) var selectedBeforeMenu = false

    override func menu(for event: NSEvent) -> NSMenu? {
        selectedBeforeMenu = selectedText != nil
        return super.menu(for: event)
    }

    override func resignFirstResponder() -> Bool {
        let resigned = super.resignFirstResponder()
        if resigned {
            setSelectedRange(NSRange(location: selectedRange().location, length: 0))
            updateReplyButton()
        }
        return resigned
    }

    // The feed scrolls, not the message: a wheel over the text goes on to the feed.
    override func scrollWheel(with event: NSEvent) {
        nextResponder?.scrollWheel(with: event)
    }

    @objc private func replyClicked(_ sender: Any?) {
        coordinator?.quoteSelection()
    }

    /// Show ↩ Reply (or + Quote) under the selection's end while there is one, in the current chat.
    func updateReplyButton() {
        guard let coordinator, !coordinator.screen.archived, selectedText != nil, window?.firstResponder === self,
              let layout = layoutManager, let container = textContainer else {
            replyButton.removeFromSuperview()
            return
        }
        replyButton.title = coordinator.quoteTitle
        replyButton.bezelStyle = .push
        replyButton.controlSize = .small
        replyButton.refusesFirstResponder = true
        replyButton.target = self
        replyButton.action = #selector(replyClicked(_:))
        replyButton.setAccessibilityLabel(coordinator.quoteTitle)
        replyButton.toolTip = String(localized: "Quote the selection in your message")
        replyButton.sizeToFit()
        let range = selectedRange()
        let end = layout.glyphRange(forCharacterRange: NSRange(location: max(NSMaxRange(range) - 1, range.location),
                                                               length: 1), actualCharacterRange: nil)
        let rect = layout.boundingRect(forGlyphRange: end, in: container)
        let size = replyButton.frame.size
        let x = min(max(0, rect.maxX - size.width / 2), max(0, bounds.width - size.width))
        replyButton.frame = NSRect(x: x, y: rect.maxY + textContainerOrigin.y + 4, width: size.width, height: size.height)
        if replyButton.superview !== self { addSubview(replyButton) }
    }
}
