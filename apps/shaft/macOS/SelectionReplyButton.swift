import AppKit

/// Draws its own fill because the system push bezel is translucent on macOS 26 and this button floats on text.
/// Its frame can run past its parent's bounds, so it takes its own clicks.
final class SelectionReplyButton: NSButton {
    private var clickMonitor: Any?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        configure()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        configure()
    }

    private func configure() {
        isBordered = false
        refusesFirstResponder = true
        font = .systemFont(ofSize: NSFont.smallSystemFontSize, weight: .medium)
    }

    func accepts(_ locationInWindow: NSPoint) -> Bool {
        window != nil && !isHiddenOrHasHiddenAncestor && bounds.contains(convert(locationInWindow, from: nil))
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if let clickMonitor {
            NSEvent.removeMonitor(clickMonitor)
            self.clickMonitor = nil
        }
        guard window != nil else { return }
        clickMonitor = NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { [weak self] event in
            nonisolated(unsafe) let click = event
            let taken = MainActor.assumeIsolated { () -> Bool in
                guard let self, click.window === self.window, self.accepts(click.locationInWindow) else { return false }
                self.performClick(nil)
                return true
            }
            return taken ? nil : event
        }
    }

    override var intrinsicContentSize: NSSize {
        let size = (title as NSString).size(withAttributes: [.font: font!])
        return NSSize(width: ceil(size.width) + 2 * Design.Space.s12, height: ceil(size.height) + 10)
    }

    override func sizeToFit() {
        setFrameSize(intrinsicContentSize)
    }

    override func draw(_ dirtyRect: NSRect) {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            let brand = MacColor.brand.usingColorSpace(.deviceRGB) ?? MacColor.brand
            let canvas = MacColor.canvas.usingColorSpace(.deviceRGB) ?? MacColor.canvas
            let fill = isHighlighted ? (brand.blended(withFraction: 0.25, of: canvas) ?? brand) : brand
            fill.withAlphaComponent(1).setFill()
            NSBezierPath(roundedRect: bounds, xRadius: Design.Radius.pill, yRadius: Design.Radius.pill).fill()

            let attributes: [NSAttributedString.Key: Any] = [.font: font!, .foregroundColor: MacColor.onAccent]
            let size = (title as NSString).size(withAttributes: attributes)
            (title as NSString).draw(at: NSPoint(x: (bounds.width - size.width) / 2,
                                               y: (bounds.height - size.height) / 2), withAttributes: attributes)
        }
    }
}
