import AppKit
import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

// The Mac halves of Shared's text seams (Shared/Platform/TextViews.swift has the iOS ones): the composer's field and
// the selectable text of Select text, as AppKit text views of the same shape.

/// The composer's main field (web-and-api.md, "The web UI"): Return sends, Shift- or Option-Return is a new line,
/// Cmd-Return queues after the reply (sent now when idle). An input method's Return confirms what it composes, as
/// typed. It grows with its text up to `maxHeight`, then scrolls; one line sits centred in the capsule. Pictures and files pasted or dropped on it go to
/// `onPaste` (attachments). Long text pastes become text files; short text stays inline.
/// Paste and Match Style always inserts text inline. Text drops stay inline.
struct ComposerTextView: NSViewRepresentable {
    @Binding var text: String
    @Binding var height: CGFloat
    var placeholder: String
    var maxHeight: CGFloat
    var focusRequest: Int
    var onKey: (ComposerKey) -> Void
    var onPaste: ([NSItemProvider]) -> Void

    /// Spec §4: the text's insets in the field, so nothing clips.
    static let insetX: CGFloat = 12
    static let insetY: CGFloat = 9
    /// How far in the placeholder starts: the caret stands on the text's first column, and its blue bar and glow lay over
    /// the first letter (typed text starts on that column, as the caret then stands before it).
    static let placeholderInset: CGFloat = 6
    /// The composer's capsule at one line, which sits centred in it.
    static let minimumHeight: CGFloat = 44

    func makeNSView(context: Context) -> NSScrollView {
        let textView = ComposerNSTextView(usingTextLayoutManager: false)
        textView.coordinator = context.coordinator
        textView.delegate = context.coordinator
        textView.isRichText = false
        textView.importsGraphics = false
        textView.allowsUndo = true
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isAutomaticTextReplacementEnabled = false
        textView.writingToolsBehavior = .none    // see MacChrome.writingToolsAffordance
        textView.font = .preferredFont(forTextStyle: .body)
        textView.textColor = MacColor.textPrimary
        textView.insertionPointColor = MacColor.brand
        textView.drawsBackground = false
        textView.textContainer?.lineFragmentPadding = 0
        textView.textContainerInset = NSSize(width: Self.insetX, height: Self.insetY)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        textView.string = text
        textView.placeholder = placeholder
        textView.setAccessibilityLabel(placeholder)
        textView.registerForDraggedTypes([.fileURL, .png, .tiff])

        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.borderType = .noBorder
        scroll.documentView = textView
        context.coordinator.textView = textView
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let textView = context.coordinator.textView else { return }
        if textView.string != text && !textView.hasMarkedText() {
            textView.string = text
        }
        if textView.placeholder != placeholder {
            textView.placeholder = placeholder
            textView.setAccessibilityLabel(placeholder)
            textView.needsDisplay = true
        }
        if context.coordinator.lastFocusRequest != focusRequest {
            context.coordinator.lastFocusRequest = focusRequest
            DispatchQueue.main.async {
                if textView.window == nil {
                    textView.focusWhenInWindow = true
                } else {
                    textView.window?.makeFirstResponder(textView)
                }
            }
        }
        context.coordinator.measure()
    }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ComposerTextView
        var lastFocusRequest: Int
        weak var textView: ComposerNSTextView?

        init(parent: ComposerTextView) {
            self.parent = parent
            lastFocusRequest = parent.focusRequest
        }

        func textDidChange(_ notification: Notification) {
            guard let textView else { return }
            parent.text = textView.string
            textView.needsDisplay = true
            measure()
        }

        /// Return's rules. Option-Return arrives as `insertNewlineIgnoringFieldEditor:` and inserts a new line as
        /// usual; Shift-Return arrives as `insertNewline:` with Shift held.
        func textView(_ textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            guard selector == #selector(NSResponder.insertNewline(_:)) else { return false }
            let flags = (textView as? ComposerNSTextView)?.keyFlags ?? []
            if flags.contains(.shift) || flags.contains(.option) {
                textView.insertNewlineIgnoringFieldEditor(nil)
            } else if flags.contains(.command) {
                parent.onKey(.queue)
            } else {
                parent.onKey(.send)
            }
            return true
        }

        /// Grow with the text, between one line and `maxHeight`, then scroll.
        func measure() {
            guard let textView, let container = textView.textContainer, let layout = textView.layoutManager else {
                return
            }
            layout.ensureLayout(for: container)
            let line = textView.font.map(layout.defaultLineHeight(for:)) ?? 0
            let content = max(layout.usedRect(for: container).height, line)
            let inset = max(ComposerTextView.insetY, ((ComposerTextView.minimumHeight - content) / 2).rounded(.down))
            if textView.textContainerInset.height != inset {
                textView.textContainerInset = NSSize(width: ComposerTextView.insetX, height: inset)
            }
            let target = min((content + inset * 2).rounded(.up), parent.maxHeight)
            if abs(parent.height - target) > 0.5 {
                let binding = parent.$height
                DispatchQueue.main.async { binding.wrappedValue = target }
            }
        }

        func key(_ key: ComposerKey) { parent.onKey(key) }
        func paste(_ providers: [NSItemProvider]) { parent.onPaste(providers) }
    }
}

final class ComposerNSTextView: NSTextView {
    weak var coordinator: ComposerTextView.Coordinator?
    var focusWhenInWindow = false
    var placeholder = ""
    /// The modifiers of the key being handled: Return's command arrives without its event.
    private(set) var keyFlags: NSEvent.ModifierFlags = []

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if focusWhenInWindow && window != nil {
            focusWhenInWindow = false
            window?.makeFirstResponder(self)
        }
    }

    override func keyDown(with event: NSEvent) {
        keyFlags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        defer { keyFlags = [] }
        super.keyDown(with: event)
    }

    // Cmd-Return reaches the field only when the Chat menu's Queue after reply is off (nothing to send): it queues
    // then too, which does nothing.
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if window?.firstResponder === self, flags == .command, event.keyCode == 36 || event.keyCode == 76 {
            coordinator?.key(.queue)
            return true
        }
        return super.performKeyEquivalent(with: event)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty, !placeholder.isEmpty else { return }
        let origin = NSPoint(x: textContainerInset.width + (textContainer?.lineFragmentPadding ?? 5)
                                + ComposerTextView.placeholderInset,
                             y: textContainerInset.height)
        (placeholder as NSString).draw(at: origin, withAttributes: [
            .font: font ?? NSFont.preferredFont(forTextStyle: .body),
            .foregroundColor: MacColor.textTertiary,
        ])
    }

    // MARK: Files in: paste and drop

    /// What the pasteboard holds as attachments: files (a copy in Finder), or a picture with no text (a screenshot
    /// copied). Text stays inline on drop; `pasteAttachments(on:)` also turns long text into a file on paste.
    static func attachments(on board: NSPasteboard) -> [NSItemProvider] {
        if let urls = board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL],
           !urls.isEmpty {
            return urls.compactMap { url in
                let provider = NSItemProvider(contentsOf: url)
                provider?.suggestedName = url.lastPathComponent
                return provider
            }
        }
        guard board.string(forType: .string) == nil else { return [] }
        for (type, uti) in [(NSPasteboard.PasteboardType.png, UTType.png), (.tiff, .tiff)] {
            if let data = board.data(forType: type) {
                return [ItemImport.fileProvider(data: data, type: uti, name: "pasted")]
            }
        }
        return []
    }

    /// Files and pictures win over text; only a paste turns long text into an attachment.
    static func pasteAttachments(on board: NSPasteboard) -> [NSItemProvider] {
        let files = attachments(on: board)
        if !files.isEmpty { return files }
        if let text = board.string(forType: .string), PastedText.belongsInFile(text) {
            return [ItemImport.textFile(text)]
        }
        return []
    }

    override func paste(_ sender: Any?) {
        if !pasteFiles(from: .general) { super.paste(sender) }
    }

    /// Attach files, pictures or long text; false for short text or an empty board.
    @discardableResult
    func pasteFiles(from board: NSPasteboard) -> Bool {
        let files = Self.pasteAttachments(on: board)
        guard !files.isEmpty else { return false }
        coordinator?.paste(files)
        return true
    }

    /// Menu validation checks long text without copying its UTF-8 bytes or building a provider.
    static func hasPasteAttachments(on board: NSPasteboard) -> Bool {
        !attachments(on: board).isEmpty || board.string(forType: .string).map(PastedText.belongsInFile) == true
    }

    override func validateUserInterfaceItem(_ item: any NSValidatedUserInterfaceItem) -> Bool {
        if item.action == #selector(paste(_:)), Self.hasPasteAttachments(on: .general) { return true }
        return super.validateUserInterfaceItem(item)
    }

    override func draggingEntered(_ sender: any NSDraggingInfo) -> NSDragOperation {
        Self.attachments(on: sender.draggingPasteboard).isEmpty ? super.draggingEntered(sender) : .copy
    }

    override func draggingUpdated(_ sender: any NSDraggingInfo) -> NSDragOperation {
        Self.attachments(on: sender.draggingPasteboard).isEmpty ? super.draggingUpdated(sender) : .copy
    }

    // A file dropped on the field is attached, not typed in as its path.
    override func performDragOperation(_ sender: any NSDraggingInfo) -> Bool {
        let files = Self.attachments(on: sender.draggingPasteboard)
        guard !files.isEmpty else { return super.performDragOperation(sender) }
        coordinator?.paste(files)
        return true
    }
}

/// Text to select from (Select text: a document's text): the selection's right-click menu offers ↩ Reply (or + Quote
/// once a card waits) and, for a document, 💬 Comment, before the system's own items. `selection` follows what is
/// selected, for the sheet's own buttons.
struct SelectableTextView: NSViewRepresentable {
    let text: String
    @Binding var selection: NSRange
    var quoteTitle: String
    var onQuote: (String) -> Void
    var onComment: ((String, NSRange) -> Void)?

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        scroll.drawsBackground = false
        scroll.autohidesScrollers = true         // no empty track beside a short text
        scroll.scrollerStyle = .overlay
        guard let textView = scroll.documentView as? NSTextView else { return scroll }
        textView.writingToolsBehavior = .none    // see MacChrome.writingToolsAffordance
        textView.textContainer?.lineFragmentPadding = 0     // the text starts on the gutter, under the sheet's title
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.drawsBackground = false
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = Design.Text.lineSpacing
        textView.defaultParagraphStyle = paragraph
        textView.font = .preferredFont(forTextStyle: .body)
        textView.textColor = MacColor.textPrimary
        textView.textContainerInset = NSSize(width: Design.Space.gutter, height: Design.Space.s16)
        textView.delegate = context.coordinator
        textView.string = text
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let textView = scroll.documentView as? NSTextView else { return }
        if textView.string != text { textView.string = text }
    }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: SelectableTextView

        init(parent: SelectableTextView) { self.parent = parent }

        func textViewDidChangeSelection(_ notification: Notification) {
            guard let textView = notification.object as? NSTextView else { return }
            let range = textView.selectedRange()
            let binding = parent.$selection
            DispatchQueue.main.async { binding.wrappedValue = range }
        }

        func textView(_ view: NSTextView, menu: NSMenu, for event: NSEvent, at charIndex: Int) -> NSMenu? {
            ReadOnlyTextMenu.trim(menu)
            let range = view.selectedRange()
            guard range.length > 0 else { return menu }
            let selected = (view.string as NSString).substring(with: range)
            var mine = [MenuAction.item(parent.quoteTitle, symbol: "arrowshape.turn.up.left") { [weak self] in
                self?.parent.onQuote(selected)
            }]
            if let comment = parent.onComment {
                mine.append(MenuAction.item(String(localized: "Comment"), symbol: "text.bubble") {
                    comment(selected, range)
                })
            }
            for (index, item) in mine.enumerated() { menu.insertItem(item, at: index) }
            menu.insertItem(.separator(), at: mine.count)
            return menu
        }
    }
}
