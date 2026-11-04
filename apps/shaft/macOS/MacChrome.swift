import AppKit
import SwiftUI

// The Mac's window chrome over Shared's views: the chat title's capsule, the inspector's toolbar, the sidebar's quiet
// selection.

extension View {
    /// The chat's title in the toolbar: a glass capsule of its own, as tall as the toolbar's glass buttons, so the name
    /// never sits bare over the chat (decided 2026-11-04, beyond spec §2.4). `limit` is what the column leaves it.
    func titleCapsule(limit: CGFloat) -> some View {
        WidthLimit(limit: max(0, limit - Design.Space.s16 * 2)) { self }
            .padding(.horizontal, Design.Space.s16)
            .frame(height: Design.Header.height)
            .glassEffect(.regular, in: .capsule)
            .fixedSize()
    }
}

enum MacChrome {
    /// The toolbar's glass group of three buttons at the chat's right, with its gap: the title stays clear of it on
    /// both sides, being centred.
    static let trailingControls: CGFloat = 124

    // writingToolsAffordance: an `NSTextView` that takes Writing Tools draws AppKit's floating Siri tile for it
    // (`NSCampoLightweightUIController`: a child window at the text's gutter, which stayed behind at stale places, on the
    // composer's + and outside the window). `.none` on the app's text views is the one setting that ends it; `.limited`
    // keeps it.
}

extension View {
    /// A sheet's title on the Mac, on the gutter the sheet's text starts on (the system's title sits further in).
    func sheetTitle(_ title: LocalizedStringKey) -> some View {
        safeAreaInset(edge: .top, spacing: 0) {
            Text(title)
                .font(.headline)
                .foregroundStyle(Color.textPrimary)
                .accessibilityAddTraits(.isHeader)
                .padding(.horizontal, Design.Space.gutter)
                .padding(.vertical, Design.Space.s12)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .toolbar(removing: .title)
        .background(Color.canvas.ignoresSafeArea())
    }
}

/// Its content at its own width, up to `limit`: what is proposed then is what `ViewThatFits` chooses by. (A toolbar
/// proposes nothing useful, so with `fixedSize` the status line never dropped a part and ran the bar into overflow.)
private struct WidthLimit: Layout {
    let limit: CGFloat

    private func width(_ subview: LayoutSubview) -> CGFloat {
        min(subview.sizeThatFits(.unspecified).width, limit)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard let subview = subviews.first else { return .zero }
        return subview.sizeThatFits(ProposedViewSize(width: width(subview), height: nil))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard let subview = subviews.first else { return }
        subview.place(at: bounds.origin, proposal: ProposedViewSize(width: width(subview), height: nil))
    }
}

/// A page of the inspector's header, in its part of the window's toolbar: back (or close), the page's name, its actions
/// at the right. A navigation stack in an inspector puts its back button in the toolbar's leading end, over the chat,
/// and shows no title.
struct InspectorHeader<Actions: View>: ToolbarContent {
    let title: String
    let symbol: String
    let label: LocalizedStringKey
    var action: () -> Void
    @ViewBuilder var actions: () -> Actions

    var body: some ToolbarContent {
        ToolbarItem(placement: .primaryAction) {
            Button(action: action) { Image(systemName: symbol) }.accessibilityLabel(label)
        }
        ToolbarItem(placement: .primaryAction) {
            Text(title)
                .font(.headline)
                .foregroundStyle(Color.textPrimary)
                .lineLimit(1)
                .truncationMode(.middle)
                .accessibilityAddTraits(.isHeader)
        }
        .sharedBackgroundVisibility(.hidden)
        ToolbarSpacer(.flexible, placement: .primaryAction)
        ToolbarItemGroup(placement: .primaryAction) { actions() }
    }
}

/// Put in a list row: the list's own selection stops drawing, since the row draws a quiet one of its own
/// (`Color.rowSelected`). The selection, its keys and its accessibility stay the list's. Decided 2026-11-04: the
/// system's fill is a solid block of the accent that greyed text cannot be read on.
struct QuietListSelection: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView { Finder() }
    func updateNSView(_ view: NSView, context: Context) {}

    private final class Finder: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            let list = sequence(first: superview) { $0?.superview }.compactMap { $0 as? NSTableView }.first
            list?.selectionHighlightStyle = .none
        }
    }
}
