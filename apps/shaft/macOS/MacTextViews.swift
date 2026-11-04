import AppKit
import SwiftUI
import UniformTypeIdentifiers

// The Mac halves of Shared's text seams (Shared/Platform/TextViews.swift has the iOS ones): the composer's field and
// the selectable text of Select text, as AppKit text views of the same shape.

/// A Mac always has a hardware keyboard: the composer's shortcuts line shows while its field is focused.
@MainActor
@Observable
final class HardwareKeyboard {
    static let shared = HardwareKeyboard()
    let attached = true
}

/// The composer's main field (web-and-api.md, "The web UI"): Return sends, Shift- or Option-Return is a new line,
/// Cmd-Return queues after the reply (sent now when idle). An input method's Return confirms what it composes, as
/// typed. It grows with its text up to `maxHeight`, then scrolls. Pictures and files pasted or dropped on it go to
/// `onPaste` (attachments); text pastes as text.
struct ComposerTextView: NSViewRepresentable {
    @Binding var text: String
    @Binding var height: CGFloat
    @Binding var focused: Bool
    var placeholder: String
    var maxHeight: CGFloat
    var focusRequest: Int
    var onKey: (ComposerKey) -> Void
    var onPaste: ([NSItemProvider]) -> Void

    /// Spec §4: the text's insets in the field, so nothing clips.
    static let insetX: CGFloat = 12
    static let insetY: CGFloat = 9

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
            DispatchQueue.main.async { textView.window?.makeFirstResponder(textView) }
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

        func focusChanged(_ focused: Bool) {
            if parent.focused != focused {
                let binding = parent.$focused
                DispatchQueue.main.async { binding.wrappedValue = focused }
            }
        }

        /// Grow with the text, between one line and `maxHeight`, then scroll.
        func measure() {
            guard let textView, let container = textView.textContainer, let layout = textView.layoutManager else {
                return
            }
            layout.ensureLayout(for: container)
            let line = textView.font.map(layout.defaultLineHeight(for:)) ?? 0
            let used = max(layout.usedRect(for: container).height, line) + textView.textContainerInset.height * 2
            let target = min(used.rounded(.up), parent.maxHeight)
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
    var placeholder = ""
    /// The modifiers of the key being handled: Return's command arrives without its event.
    private(set) var keyFlags: NSEvent.ModifierFlags = []

    override func keyDown(with event: NSEvent) {
        keyFlags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        defer { keyFlags = [] }
        super.keyDown(with: event)
    }

    override func becomeFirstResponder() -> Bool {
        let became = super.becomeFirstResponder()
        if became { coordinator?.focusChanged(true) }
        return became
    }

    override func resignFirstResponder() -> Bool {
        let resigned = super.resignFirstResponder()
        if resigned { coordinator?.focusChanged(false) }
        return resigned
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
        let origin = NSPoint(x: textContainerInset.width + (textContainer?.lineFragmentPadding ?? 5),
                             y: textContainerInset.height)
        (placeholder as NSString).draw(at: origin, withAttributes: [
            .font: font ?? NSFont.preferredFont(forTextStyle: .body),
            .foregroundColor: MacColor.textTertiary,
        ])
    }

    // MARK: Files in: paste and drop

    /// What the pasteboard holds as attachments: files (a copy in Finder), or a picture with no text (a screenshot
    /// copied). Text, even with a picture beside it (a copy from a web page), pastes as text.
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
                return [picture(data, type: uti)]
            }
        }
        return []
    }

    /// A pasted picture as a file representation, which `ItemImport` reads (and converts to PNG/JPEG for the web).
    private static func picture(_ data: Data, type: UTType) -> NSItemProvider {
        let provider = NSItemProvider()
        provider.suggestedName = "pasted"
        provider.registerFileRepresentation(for: type, visibility: .all, openInPlace: false) { done in
            let url = FileManager.default.temporaryDirectory
                .appending(path: "SHAFT-paste-\(UUID().uuidString).\(type.preferredFilenameExtension ?? "png")")
            do {
                try data.write(to: url)
                done(url, false, nil)
            } catch {
                done(nil, false, error)
            }
            return nil
        }
        return provider
    }

    override func paste(_ sender: Any?) {
        if !pasteFiles(from: .general) { super.paste(sender) }
    }

    /// Attach what `board` holds as files; false when it holds text (or nothing) to paste.
    @discardableResult
    func pasteFiles(from board: NSPasteboard) -> Bool {
        let files = Self.attachments(on: board)
        guard !files.isEmpty else { return false }
        coordinator?.paste(files)
        return true
    }

    override func validateUserInterfaceItem(_ item: any NSValidatedUserInterfaceItem) -> Bool {
        if item.action == #selector(paste(_:)), !Self.attachments(on: .general).isEmpty { return true }
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
        guard let textView = scroll.documentView as? NSTextView else { return scroll }
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
