import SHAFTKit
import SwiftUI

/// The conversation, scrolled: it lands on the last message and stays there while what loads after grows the feed or
/// moves it (pictures, the reply streaming, the keyboard), until the operator scrolls up. Scrolled up, a new message
/// below shows "↓ New below". It reports when the newest item is on screen, which is when the chat may be marked seen.
struct FeedView: View {
    let feed: Feed
    let rows: [FeedRow]
    /// The newest item is on screen (or not): the chat screen marks the chat seen.
    var newestVisible: (Bool) -> Void
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var position = ScrollPosition(edge: .bottom)
    @State private var pinned = true
    @State private var distance: CGFloat = 0
    @State private var newBelow = false
    @State private var interacting = false
    /// "Show the N earlier messages" was tapped: land on the first of them once they are there.
    @State private var toTop = false

    private let bottomID = "feed-bottom"

    /// What the feed draws: End the conversation lives in the ⋯ menu, and as the last row it would hide every new
    /// message from the check for one below.
    private var shown: [FeedRow] {
        rows.filter { if case .end = $0 { false } else { true } }
    }

    var body: some View {
        let newest = FeedRows.newestKey(feed)
        let shown = shown
        ScrollView {
            VStack(alignment: .leading, spacing: Design.Space.turn) {
                if feed.content?.chat == nil && shown.allSatisfy({ if case .day = $0 { true } else { false } }) {
                    EmptyChat(instance: feed.instance).padding(.horizontal, Design.Space.gutter)
                }
                ForEach(shown) { row in
                    // Each row keeps its own margins, capped and centred on the column (spec §5).
                    FeedRowView(row: row, archived: screen.archived)
                        .frame(maxWidth: Design.measure, alignment: .leading)
                        .frame(maxWidth: .infinity)
                        .padding(.horizontal, Design.Space.gutter)
                        .id(row.id)
                        .onScrollVisibilityChange(threshold: 0.25) { visible in
                            if row.id == newest { newestVisible(visible) }
                        }
                }
                Color.clear.frame(height: 1).id(bottomID)
            }
            .padding(.vertical, Design.Space.s12)
            .scrollTargetLayout()
        }
        .foregroundStyle(Color.textPrimary)
        .background(Color.canvas)
        .scrollPosition($position)
        .defaultScrollAnchor(.bottom, for: .initialOffset)
        .scrollDismissesKeyboard(.interactively)
        .onScrollGeometryChange(for: CGFloat.self) { geometry in
            geometry.contentSize.height + geometry.contentInsets.bottom - geometry.visibleRect.maxY
        } action: { _, new in
            distance = new
            if new < 40 {
                newBelow = false
                if !pinned && !interacting { pinned = true }
            }
        }
        .onScrollGeometryChange(for: CGFloat.self) { geometry in
            geometry.contentSize.height + geometry.containerSize.height
        } action: { _, _ in
            if pinned && !interacting { stickToBottom() }
        }
        .onScrollPhaseChange { _, phase in
            interacting = phase == .interacting || phase == .tracking
            if phase == .idle || phase == .decelerating {
                pinned = distance < 40
            } else if phase == .interacting {
                pinned = false
            }
        }
        .onChange(of: screen.chat.showingAll) { _, all in
            guard all else { return }
            pinned = false
            toTop = true
        }
        .onChange(of: rows.first?.id) { _, _ in
            guard toTop else { return }
            toTop = false
            position.scrollTo(edge: .top)
        }
        .onChange(of: shown.last?.id) { _, _ in
            if pinned {
                stickToBottom()
            } else {
                newBelow = true
            }
        }
        .overlay(alignment: .bottom) {
            if !pinned && distance > 200 {
                Button {
                    pinned = true
                    newBelow = false
                    withAnimation(reduceMotion ? nil : .default) { position.scrollTo(edge: .bottom) }
                } label: {
                    Label(newBelow ? "New below" : "Latest", systemImage: "arrow.down")
                        .font(Design.Text.pill)
                        .foregroundStyle(newBelow ? Color.brand : Color.textPrimary)
                        .padding(.horizontal, Design.Space.s12)
                        .frame(minHeight: Design.Space.s24 + Design.Space.s12)
                        .quietHitArea()
                }
                .buttonStyle(.plain)
                .floatingPill()
                .padding(.bottom, Design.Space.s8)
                .accessibilityLabel(newBelow ? "New messages below" : "Scroll to the latest message")
            }
        }
    }

    private func stickToBottom() {
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) { position.scrollTo(edge: .bottom) }
    }
}

private extension View {
    /// The system's glass on a floating control; solid `elevated` under Reduce Transparency.
    func floatingPill() -> some View { modifier(FloatingPill()) }
}

private struct FloatingPill: ViewModifier {
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    func body(content: Content) -> some View {
        if reduceTransparency {
            content.background(Color.elevated, in: .capsule)
        } else {
            content.glassEffect(.regular.interactive(), in: .capsule)
        }
    }
}

/// "Chat with <n>": no chat shown (§4.2 `chat` null).
struct EmptyChat: View {
    let instance: String

    var body: some View {
        VStack(spacing: Design.Space.inTurn) {
            Image(systemName: "bubble.left.and.bubble.right").font(.largeTitle).foregroundStyle(Color.textTertiary)
                .accessibilityHidden(true)
            Text("Chat with \(instance)").font(.title3.weight(.semibold))
            Text("Your message starts a new conversation.").font(.callout).foregroundStyle(Color.textSecondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }
}
