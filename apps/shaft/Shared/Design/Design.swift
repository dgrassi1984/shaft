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
    static let codeKeyword = Color("codeKeyword")
    static let codeString = Color("codeString")
    static let codeComment = Color("codeComment")
    static let codeNumber = Color("codeNumber")
    /// A commented passage in a document: `accent` at low strength, behind `textPrimary` (spec §5).
    static let passage = Color("accent").opacity(0.18)
    /// The selected row of a chat list: a quiet lift over `elevated`, never the system's accent. `textPrimary` and
    /// `textSecondary` keep their full contrast on it (pinned by the contrast test); unread stays the accent dot.
    static let rowSelected = Color("hairline")
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

    enum Composer {
        static let verticalInset: CGFloat = 9
        static let horizontalInset: CGFloat = 12
        static let minimumHeight: CGFloat = 44
    }

    /// The header's controls: the height of the system's glass buttons, which the title's capsule matches, and how far
    /// below the header and above the composer the feed fades out.
    enum Header {
        static let height: CGFloat = {
            #if os(iOS)
            44
            #else
            36
            #endif
        }()
        static let fade: CGFloat = 16
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

    /// A 44 pt target that takes no layout (spec §2.5): put it on a Button's label, which keeps its own compact size.
    /// On the Mac, where a pointer needs no finger's room and a click does not reach past a view's frame, the label is
    /// laid out at a compact 28 pt.
    func quietHitArea() -> some View {
        modifier(QuietHitArea())
    }
}

/// Where the feed ends. The system's soft scroll edge only blurs what passes under it, which leaves text legible behind
/// the header and in the gaps round the composer: the feed fades into `canvas` before it reaches either, and the glass
/// sits on solid `canvas`. Decision of 2026-11-04 (goes beyond spec §2.4), on every platform.
extension View {
    /// Under the header: solid to the top of the screen, fading out below the bar.
    func headerEdge() -> some View {
        safeAreaInset(edge: .top, spacing: 0) {
            LinearGradient(colors: [.canvas, .canvas.opacity(0)], startPoint: .top, endPoint: .bottom)
                .frame(height: Design.Header.fade)
                .background(alignment: .bottom) {
                    Color.canvas
                        .padding(.bottom, Design.Header.fade)
                        .ignoresSafeArea(edges: .top)
                }
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
    }

    /// Behind a bar over the feed's foot: solid to the bottom of the screen, fading out above the bar.
    func footerEdge() -> some View {
        background(alignment: .top) {
            VStack(spacing: 0) {
                LinearGradient(colors: [.canvas.opacity(0), .canvas], startPoint: .top, endPoint: .bottom)
                    .frame(height: Design.Header.fade)
                Color.canvas
            }
            .padding(.top, -Design.Header.fade)
            .ignoresSafeArea(edges: .bottom)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
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
        #if os(iOS)
        content.contentShape(QuietTarget())
        #else
        content.frame(minWidth: 28, minHeight: 28).contentShape(Rectangle())
        #endif
    }
}

#if os(iOS)
/// The view's own rectangle grown to 44 pt each way about its centre, however small the view is drawn.
private struct QuietTarget: Shape {
    func path(in rect: CGRect) -> Path {
        Path(rect.insetBy(dx: min(0, (rect.width - 44) / 2), dy: min(0, (rect.height - 44) / 2)))
    }
}
#endif
