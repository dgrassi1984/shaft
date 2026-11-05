import AppKit

/// Draws its own fill because the system push bezel is translucent on macOS 26 and this button floats on text.
final class SelectionReplyButton: NSButton {
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
