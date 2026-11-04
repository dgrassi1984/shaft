import AppKit
import SHAFTKit
import SwiftUI

// The Mac half of Shared's message-text seam (Shared/Platform/MessageText.swift): a message's text in AppKit text
// views that select natively, in place and across paragraphs and blocks. A selection shows ↩ Reply (+ Quote once a card waits)
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

    func makeNSView(context: Context) -> MessageNSView {
        let view = MessageNSView()
        view.coordinator = context.coordinator
        context.coordinator.view = view
        return view
    }

    func updateNSView(_ view: MessageNSView, context: Context) {
        context.coordinator.screen = screen
        context.coordinator.menu = menu
        context.coordinator.show(source, markdown: markdown)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: MessageNSView, context: Context) -> CGSize? {
        var width = proposal.width ?? Design.measure
        if !width.isFinite || width <= 0 { width = Design.measure }
        return nsView.size(fitting: min(width, Design.measure))
    }

    func makeCoordinator() -> Coordinator { Coordinator(screen: screen) }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        /// The chat screen and the message's menu, as of the last update.
        var screen: ChatScreenModel
        var menu: MessageMenuContext?
        weak var view: MessageNSView?
        private var shown: (source: String, markdown: Bool)?

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
            view.show(markdown ? MarkdownAttributed.render(source) : MarkdownAttributed.operatorText(source))
        }

        func textViewDidChangeSelection(_ notification: Notification) {
            view?.updateReplyButton()
        }

        /// ↩ Reply / + Quote on the selection: a card in the composer, its answer focused.
        func quoteSelection() {
            guard let view, let selected = view.selectedText else { return }
            screen.quote(selected)
            view.collapseSelection()
        }

        func textView(_ view: NSTextView, menu: NSMenu, for event: NSEvent, at charIndex: Int) -> NSMenu? {
            ReadOnlyTextMenu.trim(menu)
            var top: [NSMenuItem] = []
            // A selection made before the right-click (not the word the click itself selects, for Look Up).
            if !screen.archived, let message = self.view, message.selectedBeforeMenu, message.selectedText != nil {
                top.append(MenuAction.item(quoteTitle, symbol: "arrowshape.turn.up.left") { [weak self] in
                    self?.quoteSelection()
                })
            }
            for (index, item) in top.enumerated() { menu.insertItem(item, at: index) }
            if !top.isEmpty { menu.insertItem(.separator(), at: top.count) }

            var bottom: [NSMenuItem] = []
            let rendered = self.view?.rendered ?? .empty
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

/// A message's text: one attributed string in one layout, in as many text views as it has segments
/// (`MarkdownAttributed.Segment`). The text around a code block or a table wraps to the column; the block's text does
/// not, and scrolls sideways inside its rounded `elevated` block. The views share their selection, as the views of one
/// layout do, so a selection still runs from a paragraph through a code block into the next.
final class MessageNSView: NSView {
    weak var coordinator: MessageTextView.Coordinator?
    private(set) var rendered = MarkdownAttributed.Rendered.empty
    private var measure = MarkdownAttributed.Measure(.empty)
    private let storage = NSTextStorage()
    private let manager = MessageLayoutManager()
    private var parts: [Part] = []
    private let replyButton = NSButton(title: "", target: nil, action: nil)

    /// A segment's view: the text alone, or the text inside a block.
    private struct Part {
        let kind: MarkdownAttributed.Segment.Kind
        let container: MessageTextContainer
        let text: MessageNSTextView
        let block: MessageBlockView?
        var view: NSView { block ?? text }
    }

    override init(frame: NSRect) {
        super.init(frame: frame)
        storage.addLayoutManager(manager)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not from a nib") }

    override var isFlipped: Bool { true }

    func size(fitting width: CGFloat) -> CGSize {
        measure.layout(width: width).size
    }

    func show(_ rendered: MarkdownAttributed.Rendered) {
        self.rendered = rendered
        measure = MarkdownAttributed.Measure(rendered, after: measure)
        reconcile(rendered.segments)
        for (index, (part, segment)) in zip(parts, rendered.segments).enumerated() {
            part.container.end = index == parts.count - 1 ? .max : NSMaxRange(segment.range)
            part.block?.show(code: rendered.codeBlocks.first { $0.range == segment.range })
        }
        storage.setAttributedString(rendered.text)
        needsLayout = true
        updateReplyButton()
    }

    /// The views for these segments. A streaming reply only grows at its end: the views it has stay, so a selection
    /// or a block scrolled sideways does not jump with each token.
    private func reconcile(_ segments: [MarkdownAttributed.Segment]) {
        var same = 0
        while same < min(parts.count, segments.count), parts[same].kind == segments[same].kind { same += 1 }
        while parts.count > same {
            parts.removeLast().view.removeFromSuperview()
            manager.removeTextContainer(at: parts.count)
        }
        for segment in segments.dropFirst(same) { parts.append(makePart(segment.kind)) }
    }

    private func makePart(_ kind: MarkdownAttributed.Segment.Kind) -> Part {
        let container = MessageTextContainer()
        manager.addTextContainer(container)
        let text = MessageNSTextView(frame: .zero, textContainer: container)
        text.isEditable = false
        text.isSelectable = true
        text.isRichText = true
        text.drawsBackground = false
        text.textContainerInset = .zero
        text.isVerticallyResizable = false
        text.isHorizontallyResizable = false
        text.clipsToBounds = false
        text.linkTextAttributes = [.foregroundColor: MacColor.brand, .underlineStyle: NSUnderlineStyle.single.rawValue,
                                   .cursor: NSCursor.pointingHand]
        text.delegate = coordinator
        text.message = self
        let block = kind == .prose ? nil : MessageBlockView(text: text, code: kind == .code)
        let part = Part(kind: kind, container: container, text: text, block: block)
        addSubview(part.view)
        if replyButton.superview === self { addSubview(replyButton) }       // stays on top
        return part
    }

    override func layout() {
        super.layout()
        let placed = measure.layout(width: bounds.width).placed
        for (part, place) in zip(parts, placed) {
            if let block = part.block {
                block.frame = place.frame
                block.place(content: place.content)
            } else {
                part.container.limit = place.frame.width
                part.text.frame = place.frame
            }
        }
        updateReplyButton()
    }

    // MARK: Selection

    /// The selected text, trimmed; nil when nothing is selected.
    var selectedText: String? { parts.first?.text.selectedText }

    /// Whether text was selected when the right-click came (a right-click on a word selects it, for Look Up).
    var selectedBeforeMenu = false

    func collapseSelection() {
        guard let text = parts.first?.text else { return }
        text.setSelectedRange(NSRange(location: NSMaxRange(text.selectedRange()), length: 0))
        updateReplyButton()
    }

    /// The keyboard has gone: from a view of this message to another of it (a right-click on the selection), or away.
    func keyboardLeft() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if !self.holdsKeyboard, let text = self.parts.first?.text {
                text.setSelectedRange(NSRange(location: text.selectedRange().location, length: 0))
            }
            self.updateReplyButton()
        }
    }

    private var holdsKeyboard: Bool {
        guard let first = window?.firstResponder as? MessageNSTextView else { return false }
        return first.message === self
    }

    @objc private func replyClicked(_ sender: Any?) {
        coordinator?.quoteSelection()
    }

    /// Show ↩ Reply (or + Quote) under the selection's end while there is one, in the current chat.
    func updateReplyButton() {
        guard let coordinator, !coordinator.screen.archived, selectedText != nil, holdsKeyboard,
              let any = parts.first?.text else {
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
        let range = any.selectedRange()
        let glyphs = manager.glyphRange(forCharacterRange: NSRange(location: max(NSMaxRange(range) - 1, range.location),
                                                                   length: 1), actualCharacterRange: nil)
        guard glyphs.length > 0, let container = manager.textContainer(forGlyphAt: glyphs.location, effectiveRange: nil),
              let view = container.textView else { return }
        let rect = convert(manager.boundingRect(forGlyphRange: glyphs, in: container), from: view)
        let size = replyButton.frame.size
        let x = min(max(0, rect.maxX - size.width / 2), max(0, bounds.width - size.width))
        replyButton.frame = NSRect(x: x, y: rect.maxY + 4, width: size.width, height: size.height)
        if replyButton.superview !== self { addSubview(replyButton) }
    }
}

/// The layout of a message: an inline code's background is a pill.
final class MessageLayoutManager: NSLayoutManager {
    override func fillBackgroundRectArray(_ rectArray: UnsafePointer<NSRect>, count rectCount: Int,
                                          forCharacterRange charRange: NSRange, color: NSColor) {
        guard let storage = textStorage, charRange.location < storage.length,
              storage.attribute(.inlineCode, at: charRange.location, effectiveRange: nil) != nil,
              color == MacColor.elevated else {
            super.fillBackgroundRectArray(rectArray, count: rectCount, forCharacterRange: charRange, color: color)
            return
        }
        color.setFill()
        for index in 0..<rectCount {
            let rect = rectArray[index].insetBy(dx: -Design.Space.s4 / 2, dy: 0)
            NSBezierPath(roundedRect: rect, xRadius: Design.Radius.pill, yRadius: Design.Radius.pill).fill()
        }
    }
}

/// A code block or a table: an `elevated` rounded block whose text scrolls sideways inside it, and for code a header
/// row with the language at its left and Copy at its right (spec §3), which stay in place while the code scrolls.
final class MessageBlockView: NSView {
    private let text: MessageNSTextView
    private let scroll = MessageBlockScrollView()
    private let language = NSTextField(labelWithString: "")
    private let copyButton = NSButton(title: "", target: nil, action: nil)
    private let rule = NSBox()
    private let isCode: Bool
    private var code = ""
    private var copied: Task<Void, Never>?

    init(text: MessageNSTextView, code: Bool) {
        self.text = text
        isCode = code
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = Design.Radius.card
        layer?.cornerCurve = .continuous
        layer?.masksToBounds = true
        scroll.drawsBackground = false
        scroll.hasHorizontalScroller = true
        scroll.hasVerticalScroller = false
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.horizontalScrollElasticity = .allowed
        scroll.verticalScrollElasticity = .none
        scroll.borderType = .noBorder
        scroll.documentView = text
        addSubview(scroll)
        guard code else { return }
        let padding = MarkdownAttributed.blockPadding
        text.textContainerInset = NSSize(width: padding, height: padding)
        language.font = .systemFont(ofSize: NSFont.preferredFont(forTextStyle: .caption1).pointSize, weight: .medium)
        language.textColor = MacColor.textSecondary
        language.lineBreakMode = .byTruncatingTail
        addSubview(language)
        rule.boxType = .custom
        rule.borderWidth = 0
        rule.fillColor = MacColor.hairline
        addSubview(rule)
        copyButton.isBordered = false
        copyButton.imagePosition = .imageOnly
        copyButton.contentTintColor = MacColor.textTertiary
        copyButton.refusesFirstResponder = true
        copyButton.target = self
        copyButton.action = #selector(copyClicked(_:))
        copyButton.toolTip = String(localized: "Copy code")
        showCopy(done: false)
        addSubview(copyButton)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not from a nib") }

    override var isFlipped: Bool { true }
    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = MacColor.elevated.cgColor
        }
    }

    func show(code block: MarkdownAttributed.CodeBlock?) {
        code = block?.code ?? ""
        language.stringValue = block?.language ?? ""
    }

    /// Lays the block out inside its frame, for text of this size.
    func place(content: CGSize) {
        guard isCode else {
            scroll.frame = bounds
            text.frame = NSRect(x: 0, y: 0, width: max(content.width, bounds.width), height: content.height)
            scroll.scrollsSideways = content.width > bounds.width
            return
        }
        let header = MarkdownAttributed.codeHeader
        let side = MarkdownAttributed.blockPadding
        rule.frame = NSRect(x: 0, y: header, width: bounds.width, height: 1)
        scroll.frame = NSRect(x: 0, y: header + 1, width: bounds.width, height: bounds.height - header - 1)
        let wide = content.width + side * 2
        text.frame = NSRect(x: 0, y: 0, width: max(wide, bounds.width), height: scroll.frame.height)
        scroll.scrollsSideways = wide > bounds.width
        let button = NSSize(width: Design.Space.s24 + Design.Space.s12, height: header)
        copyButton.frame = NSRect(x: bounds.width - Design.Space.s4 - button.width, y: 0, width: button.width,
                                  height: button.height)
        language.sizeToFit()
        let height = language.frame.height
        language.frame = NSRect(x: side, y: (header - height) / 2, width: max(0, copyButton.frame.minX - side * 2),
                                height: height)
    }

    @objc private func copyClicked(_ sender: Any?) {
        Clipboard.copy(code)
        showCopy(done: true)
        copied?.cancel()
        copied = Task { [weak self] in
            try? await Task.sleep(for: .seconds(1.5))
            guard !Task.isCancelled else { return }
            self?.showCopy(done: false)
        }
    }

    /// Copy, or a check for a moment.
    private func showCopy(done: Bool) {
        let size = NSFont.preferredFont(forTextStyle: .footnote).pointSize
        copyButton.image = NSImage(systemSymbolName: done ? "checkmark" : "doc.on.doc", accessibilityDescription: nil)?
            .withSymbolConfiguration(.init(pointSize: size, weight: .regular))
        copyButton.setAccessibilityLabel(done ? String(localized: "Copied") : String(localized: "Copy code"))
    }
}

/// A block's sideways scroll. The wheel moves it only along its own axis: a gesture that starts up or down goes on to
/// the feed, whole, so a long chat scrolls past a wide table without catching on it. Its text fades at an edge with more
/// beyond it, since an overlay scroller only shows while scrolling.
final class MessageBlockScrollView: NSScrollView {
    var scrollsSideways = false {
        didSet { if scrollsSideways != oldValue { updateFade() } }
    }
    private(set) var passing = false
    private let fade = CAGradientLayer()

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        fade.startPoint = CGPoint(x: 0, y: 0.5)
        fade.endPoint = CGPoint(x: 1, y: 0.5)
        contentView.postsBoundsChangedNotifications = true
        NotificationCenter.default.addObserver(self, selector: #selector(clipMoved(_:)),
                                               name: NSView.boundsDidChangeNotification, object: contentView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not from a nib") }

    override func scrollWheel(with event: NSEvent) {
        if event.phase == .began || (event.phase == [] && event.momentumPhase == []) {
            passing = !scrollsSideways || abs(event.scrollingDeltaY) >= abs(event.scrollingDeltaX)
        }
        if passing {
            nextResponder?.scrollWheel(with: event)
        } else {
            super.scrollWheel(with: event)
        }
    }

    override func layout() {
        super.layout()
        updateFade()
    }

    @objc private func clipMoved(_ note: Notification) {
        updateFade()
    }

    private func updateFade() {
        guard scrollsSideways, let width = documentView?.frame.width, bounds.width > 0 else {
            layer?.mask = nil
            return
        }
        let x = contentView.bounds.minX
        let edge = min(Design.Space.s24 / bounds.width, 0.5)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        fade.frame = bounds
        fade.locations = [0, NSNumber(value: Double(edge)), NSNumber(value: Double(1 - edge)), 1]
        let opaque = CGColor(gray: 0, alpha: 1), clear = CGColor(gray: 0, alpha: 0)
        fade.colors = [x > 1 ? clear : opaque, opaque, opaque, x + bounds.width < width - 1 ? clear : opaque]
        layer?.mask = fade
        CATransaction.commit()
    }
}

/// A text view of a message. The ↩ Reply button and the selection's end of life are the message's (`MessageNSView`).
final class MessageNSTextView: NSTextView {
    weak var message: MessageNSView?

    /// The selected text, trimmed; nil when nothing is selected.
    var selectedText: String? {
        let range = selectedRange()
        guard range.length > 0, NSMaxRange(range) <= (string as NSString).length else { return nil }
        let text = (string as NSString).substring(with: range).trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }

    override func menu(for event: NSEvent) -> NSMenu? {
        message?.selectedBeforeMenu = selectedText != nil
        return super.menu(for: event)
    }

    override func resignFirstResponder() -> Bool {
        let resigned = super.resignFirstResponder()
        if resigned { message?.keyboardLeft() }
        return resigned
    }

    // The feed scrolls, not the message: a wheel over the text goes on to the feed (or the block's sideways scroll).
    override func scrollWheel(with event: NSEvent) {
        nextResponder?.scrollWheel(with: event)
    }
}

#if DEBUG
extension MessageBlockView {
    /// What the script driver logs of a block.
    var driveDescription: String {
        let clip = subviews.compactMap { $0 as? NSScrollView }.first
        let document = clip?.documentView
        return "frame \(Int(frame.width))x\(Int(frame.height)) text \(Int(document?.frame.width ?? 0))x\(Int(document?.frame.height ?? 0)) at \(Int(clip?.contentView.bounds.minX ?? 0))"
    }

    var drivePassing: Bool { subviews.compactMap { $0 as? MessageBlockScrollView }.first?.passing ?? false }
    var driveText: NSView? { subviews.compactMap { ($0 as? NSScrollView)?.documentView }.first }

    func scrollSideways(to x: CGFloat) {
        guard let scroll = subviews.compactMap({ $0 as? NSScrollView }).first else { return }
        scroll.contentView.scroll(to: NSPoint(x: x, y: 0))
        scroll.reflectScrolledClipView(scroll.contentView)
    }
}
#endif
