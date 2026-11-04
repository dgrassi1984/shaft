import SwiftUI
#if os(iOS)
import UIKit
#endif

extension Color {
    static let canvas = Color("canvas")
    static let elevated = Color("elevated")
    static let hairline = Color("hairline")
    static let operatorChip = Color("operator")
    static let textPrimary = Color("textPrimary")
    static let textSecondary = Color("textSecondary")
    static let textTertiary = Color("textTertiary")
    static let brand = Color("accent")
    static let onAccent = Color("onAccent")
    static let warning = Color("warning")
    static let danger = Color("danger")
}

/// The native apps' shared type, spacing and shape.
enum Design {
    static let measure: CGFloat = 620

    enum Space {
        @MainActor static var gutter: CGFloat {
            #if os(iOS)
            UIDevice.current.userInterfaceIdiom == .phone ? 16 : 24
            #else
            24
            #endif
        }
        static let turn: CGFloat = 20
        static let inTurn: CGFloat = 8
        static let s4: CGFloat = 4
        static let s8: CGFloat = 8
        static let s12: CGFloat = 12
        static let s16: CGFloat = 16
        static let s20: CGFloat = 20
        static let s24: CGFloat = 24
    }

    enum Radius {
        static let chip: CGFloat = 18
        static let composer: CGFloat = 24
        static let card: CGFloat = 10
        static let pill: CGFloat = 6
    }

    enum Text {
        static let prose: Font = .body
        static let code: Font = .system(.body, design: .monospaced).scaled(by: 0.88)
        static let table: Font = .subheadline.monospacedDigit()
        static let metadata: Font = .caption2.monospacedDigit()
        static let pill: Font = .caption.weight(.medium)
        static let lineSpacing: CGFloat = 4
        static let paragraphSpacing: CGFloat = 10

        static func heading(_ level: Int) -> Font {
            switch level {
            case 1: .body.scaled(by: 1.25).bold()
            case 2: .body.scaled(by: 1.12).weight(.semibold)
            default: .body.scaled(by: 1.02).weight(.semibold)
            }
        }

        static let headingSpaceAbove: CGFloat = 1
        static let headingSpaceBelow: CGFloat = 0.35
    }

    enum Motion {
        static let spring: Animation = .spring(duration: 0.22, bounce: 0)
    }
}

extension View {
    func elevatedBlock() -> some View {
        background(Color.elevated, in: RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous))
    }

    func pill() -> some View {
        background(Color.elevated, in: RoundedRectangle(cornerRadius: Design.Radius.pill, style: .continuous))
    }

    /// A 44 pt target. Compact layout still needs a solution for Button labels.
    func quietHitArea() -> some View {
        modifier(QuietHitArea())
    }
}

struct AccentBar: View {
    var color: Color = .brand

    var body: some View {
        Rectangle().fill(color).frame(width: 3)
    }
}

private struct QuietHitArea: ViewModifier {
    func body(content: Content) -> some View {
        // Preserve working targets until a compact region also passes the Button click probe.
        content.frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
    }
}
