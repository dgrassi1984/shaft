import SHAFTKit
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
        static let horizontalInset: CGFloat = 12
        /// Round the text inside its field, where a scrolled line dissolves (spec §4's 9 pt with `clipInset`).
        static let textInset: CGFloat = 6
        /// Between the field and the capsule: a scrolled line never reaches the capsule's edge.
        static let clipInset: CGFloat = 3
    }

    /// The header's controls: the height of the system's glass buttons, which the title's capsule matches, what that
    /// capsule keeps inside its curve, and how far below the header and above the composer the feed fades out.
    enum Header {
        static let height: CGFloat = {
            #if os(iOS)
            44
            #else
            36
            #endif
        }()
        static let titleInset: CGFloat = 16
        /// The least between the title's capsule and the toolbar items beside it.
        static let titleGap: CGFloat = 8
        static let fade: CGFloat = 32
    }

    enum Footer {
        static let icon: CGFloat = {
            #if os(iOS)
            13
            #else
            10
            #endif
        }()
        /// A footer's 28 pt row draws about 14 pt of time and icons: this much of it at each end is only target.
        static let slack: CGFloat = 7
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

/// Shared scaling keeps the feed's resting margins and its overlays in agreement.
struct FeedMetrics: DynamicProperty {
    @ScaledMetric(relativeTo: .body) var fade = Design.Header.fade
    @ScaledMetric(relativeTo: .caption) var pillHeight = Design.Space.s24 + Design.Space.s12

    var pillClearance: CGFloat { pillHeight + Design.Space.s4 }
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

/// Where the feed ends, the same on every platform and under Reduce Transparency: text fades into `canvas` over
/// a body-scaled `Header.fade` under the header and above the composer, and the glass sits on solid `canvas` (operator's decision of
/// 2026-11-04, beyond spec §2.4). It is one plain gradient over the feed, with the system's scroll edge hidden: that edge
/// left a blurred, half-cut line under the bar. `FeedView` keeps a resting feed clear of both fades.
extension View {
    /// `floating` (the Latest pill) sits on solid canvas, with the fade above it while it shows.
    func feedEdges<Floating: View>(pillVisible: Bool, @ViewBuilder floating: () -> Floating) -> some View {
        overlay(alignment: .top) { HeaderFade() }
            .overlay(alignment: .bottom) { FooterFade(pillVisible: pillVisible, floating: floating()) }
    }

    /// Behind a bar over the feed's foot: solid to the bottom of the screen.
    func feedFloor() -> some View {
        background(Color.canvas.ignoresSafeArea(edges: .bottom))
    }

    /// Text in a sideways scroll view on an `elevated` block fades at an edge with more beyond it (an overlay scroller
    /// only shows while scrolling; the Mac's text blocks do the same in `MessageBlockScrollView`).
    func sidewaysFade() -> some View {
        modifier(SidewaysFade())
    }
}

/// `canvas`, eased out from the bar: a straight ramp shows a line where it starts.
private func canvasFade(from start: UnitPoint, to end: UnitPoint) -> LinearGradient {
    LinearGradient(stops: [
        .init(color: .canvas, location: 0),
        .init(color: .canvas.opacity(0.84), location: 0.25),
        .init(color: .canvas.opacity(0.5), location: 0.5),
        .init(color: .canvas.opacity(0.16), location: 0.75),
        .init(color: .canvas.opacity(0), location: 1),
    ], startPoint: start, endPoint: end)
}

private struct HeaderFade: View {
    private var metrics = FeedMetrics()

    var body: some View {
        canvasFade(from: .top, to: .bottom)
            .frame(height: metrics.fade)
            .background(alignment: .bottom) {
                Color.canvas
                    .padding(.bottom, metrics.fade)
                    .ignoresSafeArea(edges: .top)
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

private struct FooterFade<Floating: View>: View {
    let pillVisible: Bool
    let floating: Floating
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var metrics = FeedMetrics()

    var body: some View {
        VStack(spacing: 0) {
            canvasFade(from: .bottom, to: .top)
                .frame(height: metrics.fade)
            Color.canvas.frame(height: pillVisible ? metrics.pillClearance : 0)
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
        .overlay(alignment: .bottom) { floating }
        .animation(reduceMotion ? nil : Design.Motion.spring, value: pillVisible)
    }
}

private struct SidewaysFade: ViewModifier {
    @State private var more = ScrollEdges.more(offset: 0, container: 0, content: 0)

    func body(content: Content) -> some View {
        content
            .onScrollGeometryChange(for: ScrollEdges.self) { geometry in
                ScrollEdges.more(offset: Double(geometry.contentOffset.x),
                            container: Double(geometry.containerSize.width), content: Double(geometry.contentSize.width))
            } action: { _, new in
                more = new
            }
            .overlay(alignment: .leading) { fade(.leading, to: .trailing).opacity(more.leading ? 1 : 0) }
            .overlay(alignment: .trailing) { fade(.trailing, to: .leading).opacity(more.trailing ? 1 : 0) }
    }

    private func fade(_ start: UnitPoint, to end: UnitPoint) -> some View {
        LinearGradient(colors: [.elevated, .elevated.opacity(0)], startPoint: start, endPoint: end)
            .frame(width: Design.Space.s24)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
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
