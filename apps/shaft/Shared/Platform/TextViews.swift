import SHAFTKit
import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

// The platform seams of text: the composer's field and the selectable text Select text shows. iOS has its own UIKit
// views here (the PWA's keyboard rules need key commands; a selection needs the edit menu). The Mac app has AppKit
// views of the same shape in macOS/MacTextViews.swift (ComposerTextView, SelectableTextView).

/// What Return asks of the composer.
enum ComposerKey {
    case send
    case queue
}

#if os(iOS)

/// The composer's main field. The keyboard rules of the PWA: on the on-screen keyboard Return is a new line and the
/// Send arrow sends; on a hardware keyboard Return sends (after its autocorrection), Shift- or Option-Return is a new
/// line and Cmd-Return queues. It grows with its text up to `maxHeight`, then scrolls; a pasted picture, file or long text goes to
/// `onPaste`. Short text pastes stay inline.
struct ComposerTextView: UIViewRepresentable {
    @Binding var text: String
    @Binding var height: CGFloat
    var focused: Binding<Bool> = .constant(false)
    var placeholder: String
    var maxHeight: CGFloat
    var focusRequest: Int
    var onKey: (ComposerKey) -> Void
    var onPaste: ([NSItemProvider]) -> Void

    func makeUIView(context: Context) -> ComposerUITextView {
        let view = ComposerUITextView()
        view.delegate = context.coordinator
        view.font = .preferredFont(forTextStyle: .body)
        view.adjustsFontForContentSizeCategory = true
        view.backgroundColor = .clear
        view.textColor = UIColor(Color.textPrimary)
        view.textContainerInset = UIEdgeInsets(top: Design.Composer.textInset, left: Design.Composer.horizontalInset,
                                               bottom: Design.Composer.textInset, right: Design.Composer.horizontalInset)
        view.textContainer.lineFragmentPadding = 0
        view.isScrollEnabled = false
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        view.accessibilityLabel = placeholder
        view.text = text
        view.coordinator = context.coordinator
        return view
    }

    func updateUIView(_ view: ComposerUITextView, context: Context) {
        context.coordinator.parent = self
        if view.text != text {
            view.text = text
        }
        view.placeholder.text = placeholder
        view.placeholder.isHidden = !text.isEmpty
        view.accessibilityLabel = placeholder
        if context.coordinator.lastFocusRequest != focusRequest {
            context.coordinator.lastFocusRequest = focusRequest
            DispatchQueue.main.async { view.becomeFirstResponder() }
        }
        context.coordinator.measure(view)
    }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    @MainActor
    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: ComposerTextView
        var lastFocusRequest: Int

        init(parent: ComposerTextView) {
            self.parent = parent
            lastFocusRequest = parent.focusRequest
        }

        func textViewDidChange(_ textView: UITextView) {
            parent.text = textView.text
            (textView as? ComposerUITextView)?.placeholder.isHidden = !textView.text.isEmpty
            measure(textView)
        }

        func textViewDidBeginEditing(_ textView: UITextView) {
            if !parent.focused.wrappedValue { parent.focused.wrappedValue = true }
        }

        func textViewDidEndEditing(_ textView: UITextView) {
            if parent.focused.wrappedValue { parent.focused.wrappedValue = false }
        }

        func measure(_ view: UITextView) {
            let width = view.bounds.width > 0 ? view.bounds.width : 300
            let fitting = view.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude)).height
            let target = min(fitting, parent.maxHeight)
            let scrolls = fitting > parent.maxHeight
            if view.isScrollEnabled != scrolls { view.isScrollEnabled = scrolls }
            if abs(parent.height - target) > 0.5 {
                let binding = parent.$height
                DispatchQueue.main.async { binding.wrappedValue = target }
            }
        }

        func key(_ key: ComposerKey) { parent.onKey(key) }
        func paste(_ providers: [NSItemProvider]) { parent.onPaste(providers) }
    }
}

final class ComposerUITextView: UITextView {
    weak var coordinator: ComposerTextView.Coordinator?
    let placeholder = UILabel()

    /// A field that does not scroll stays at the top: UIKit keeps an offset for the height a shrinking field had.
    override var contentOffset: CGPoint {
        get { super.contentOffset }
        set { super.contentOffset = isScrollEnabled ? newValue : .zero }
    }

    override init(frame: CGRect, textContainer: NSTextContainer?) {
        super.init(frame: frame, textContainer: textContainer)
        placeholder.font = .preferredFont(forTextStyle: .body)
        placeholder.adjustsFontForContentSizeCategory = true
        placeholder.textColor = UIColor(Color.textTertiary)
        placeholder.isAccessibilityElement = false
        // At the largest text "Message tundra" is wider than the field: it shrinks to fit rather than cut to "Messag…".
        placeholder.adjustsFontSizeToFitWidth = true
        placeholder.minimumScaleFactor = 0.4
        placeholder.baselineAdjustment = .alignCenters
        placeholder.translatesAutoresizingMaskIntoConstraints = false
        contentInsetAdjustmentBehavior = .never
        addSubview(placeholder)
        registerForTraitChanges([UITraitPreferredContentSizeCategory.self]) { (view: ComposerUITextView, _) in
            view.followTextSize()
        }
        // On the field's frame, where the first line sits: it never scrolls with the content.
        NSLayoutConstraint.activate([
            placeholder.leadingAnchor.constraint(equalTo: frameLayoutGuide.leadingAnchor, constant: Design.Composer.horizontalInset),
            placeholder.topAnchor.constraint(equalTo: frameLayoutGuide.topAnchor, constant: Design.Composer.textInset),
            placeholder.trailingAnchor.constraint(lessThanOrEqualTo: frameLayoutGuide.trailingAnchor, constant: -Design.Composer.horizontalInset),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not from a storyboard") }

    /// The system rescales the text, but an empty field keeps its line at the old size. With the keyboard up, a change of
    /// text size (bug of 2026-11-04) left the capsule tall, or the placeholder and the caret sliced at its top: the empty
    /// line is made again at the new size, once the system has set `font`.
    private func followTextSize() {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.text.isEmpty else { return }
            self.font = .preferredFont(forTextStyle: .body, compatibleWith: self.traitCollection)
            self.text = " "
            self.text = ""
            self.setNeedsLayout()
        }
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        coordinator?.measure(self)
    }

    // Hardware keyboard only: the on-screen keyboard's Return inserts text and never reaches a key command.
    override var keyCommands: [UIKeyCommand]? {
        let send = UIKeyCommand(input: "\r", modifierFlags: [], action: #selector(sendKey))
        let queue = UIKeyCommand(input: "\r", modifierFlags: .command, action: #selector(queueKey))
        queue.discoverabilityTitle = String(localized: "Queue after reply")
        let ctrlQueue = UIKeyCommand(input: "\r", modifierFlags: .control, action: #selector(queueKey))
        let shift = UIKeyCommand(input: "\r", modifierFlags: .shift, action: #selector(newLine))
        let option = UIKeyCommand(input: "\r", modifierFlags: .alternate, action: #selector(newLine))
        send.discoverabilityTitle = String(localized: "Send")
        for command in [send, queue, ctrlQueue, shift, option] { command.wantsPriorityOverSystemBehavior = true }
        return [send, queue, ctrlQueue, shift, option]
    }

    @objc private func sendKey() {
        if markedTextRange != nil {               // an input method composing: Return confirms it, as typed
            unmarkText()
            return
        }
        coordinator?.key(.send)
    }

    @objc private func queueKey() {
        coordinator?.key(.queue)
    }

    @objc private func newLine() {
        insertText("\n")
    }

    // Pictures, files and long text pastes become attachments; short text stays inline.
    private var pasteHasFiles: Bool {
        let board = UIPasteboard.general
        return board.hasImages || (!board.hasStrings && !board.hasURLs && board.numberOfItems > 0)
    }

    override func canPerformAction(_ action: Selector, withSender sender: Any?) -> Bool {
        if action == #selector(paste(_:)) && pasteHasFiles { return true }
        return super.canPerformAction(action, withSender: sender)
    }

    override func paste(_ sender: Any?) {
        if pasteHasFiles {
            coordinator?.paste(UIPasteboard.general.itemProviders)
        } else if let text = UIPasteboard.general.string, PastedText.belongsInFile(text) {
            coordinator?.paste([ItemImport.textFile(text)])
        } else {
            super.paste(sender)
        }
    }
}

/// Text to select from (Select text, a document's text): a selection's edit menu offers ↩ Reply (or + Quote once a
/// card waits) and, for a document, 💬 Comment. `selection` follows what is selected, for the sheet's own buttons.
struct SelectableTextView: UIViewRepresentable {
    let text: String
    @Binding var selection: NSRange
    var quoteTitle: String
    var onQuote: (String) -> Void
    var onComment: ((String, NSRange) -> Void)?

    func makeUIView(context: Context) -> UITextView {
        let view = UITextView()
        view.isEditable = false
        view.isSelectable = true
        view.font = .preferredFont(forTextStyle: .body)
        view.adjustsFontForContentSizeCategory = true
        view.backgroundColor = .clear
        view.textColor = UIColor(Color.textPrimary)
        view.textContainerInset = UIEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        view.delegate = context.coordinator
        view.text = text
        view.dataDetectorTypes = []
        return view
    }

    func updateUIView(_ view: UITextView, context: Context) {
        context.coordinator.parent = self
        if view.text != text { view.text = text }
    }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    @MainActor
    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: SelectableTextView

        init(parent: SelectableTextView) { self.parent = parent }

        func textViewDidChangeSelection(_ textView: UITextView) {
            let range = textView.selectedRange
            let binding = parent.$selection
            DispatchQueue.main.async { binding.wrappedValue = range }
        }

        func textView(_ textView: UITextView, editMenuForTextIn range: NSRange,
                      suggestedActions: [UIMenuElement]) -> UIMenu? {
            let selected = (textView.text as NSString).substring(with: range)
            var mine: [UIMenuElement] = [
                UIAction(title: parent.quoteTitle, image: UIImage(systemName: "arrowshape.turn.up.left")) { [weak self] _ in
                    self?.parent.onQuote(selected)
                },
            ]
            if let comment = parent.onComment {
                mine.append(UIAction(title: String(localized: "Comment"), image: UIImage(systemName: "text.bubble")) { _ in
                    comment(selected, range)
                })
            }
            return UIMenu(children: mine + suggestedActions)
        }
    }
}

#endif
