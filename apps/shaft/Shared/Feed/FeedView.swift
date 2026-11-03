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

    var body: some View {
        let newest = FeedRows.newestKey(feed)
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if feed.content?.chat == nil && rows.allSatisfy({ if case .day = $0 { true } else { false } }) {
                    EmptyChat(instance: feed.instance).padding(.horizontal, 16)
                }
                ForEach(rows) { row in
                    // Each row keeps its own margins: the rows the lazy stack lays out first, landing at the bottom,
                    // were placed without the stack's own padding (shifted 16 pt left) until scrolled away.
                    FeedRowView(row: row, archived: screen.archived)
                        .padding(.horizontal, 16)
                        .id(row.id)
                        .onScrollVisibilityChange(threshold: 0.25) { visible in
                            if row.id == newest { newestVisible(visible) }
                        }
                }
                Color.clear.frame(height: 1).id(bottomID)
            }
            .padding(.vertical, 12)
            .scrollTargetLayout()
        }
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
        .onChange(of: rows.last?.id) { _, _ in
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
                        .font(.footnote.weight(.semibold))
                        .padding(.horizontal, 14)
                        .frame(minHeight: 44)
                        .background(.regularMaterial, in: Capsule())
                }
                .buttonStyle(.plain)
                .padding(.bottom, 8)
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

/// "Chat with <n>": no chat shown (§4.2 `chat` null).
struct EmptyChat: View {
    let instance: String

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "bubble.left.and.bubble.right").font(.largeTitle).foregroundStyle(.tertiary)
                .accessibilityHidden(true)
            Text("Chat with \(instance)").font(.title3.weight(.semibold))
            Text("Your message starts a new conversation.").font(.callout).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }
}
