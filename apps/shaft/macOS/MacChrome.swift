import AppKit
import SwiftUI

// The Mac's window chrome over Shared's views: the room the chat's title has, the inspector's toolbar, a sheet's title.

extension View {
    /// The chat's title in the toolbar at its own width, up to `limit`.
    func titleWidth(limit: CGFloat) -> some View {
        WidthLimit(limit: max(0, limit)) { self }.fixedSize()
    }
}

enum MacChrome {
    /// What the toolbar keeps at the chat's ends: the three buttons at its right, and at its left, with the sidebar
    /// collapsed, the window's buttons and the sidebar's two; each with its gap.
    private static let trailingControls: CGFloat = 132
    private static let collapsedLeadingControls: CGFloat = 180
    private static let backButton: CGFloat = 44

    /// How wide the title may be in a chat column: it is centred, so the wider end counts twice. Past this the toolbar
    /// hides the buttons behind its overflow.
    static func titleLimit(column: CGFloat, sidebar: CGFloat, earlier: Bool) -> CGFloat {
        let leading = (sidebar > 0 ? 0 : collapsedLeadingControls) + (earlier ? backButton : 0)
        return column - 2 * max(leading, trailingControls)
    }

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
        .presentationBackground(Color.canvas)    // its button bar too: the system's is white
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

extension View {
    /// A page in the inspector: `InspectorHeader` has its back button, and the toolbar draws no band or line over it.
    func inspectorPage() -> some View {
        navigationBarBackButtonHidden(true)
            .toolbarBackgroundVisibility(.hidden, for: .windowToolbar)
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
