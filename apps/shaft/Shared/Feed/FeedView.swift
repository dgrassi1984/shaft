import SHAFTKit
import SwiftUI

/// The conversation, scrolled: it lands on the last message and stays there while what loads after grows the feed or
/// moves it (pictures, the reply streaming, the keyboard), until the operator scrolls up. Scrolled up, a new message
/// below shows "↓ New below" (drawn by the chat screen, in the fade above the composer). It reports when the end of the
/// newest item is on screen, which is when the chat may be marked seen.
struct FeedView: View {
    let feed: Feed
    let rows: [FeedRow]
    /// The end of the newest item is on screen (or not): the chat screen marks the chat seen.
    var newestVisible: (Bool) -> Void
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var position = ScrollPosition(edge: .bottom)
    @State private var pinned = true
    @State private var distance: CGFloat = 0
    @State private var newBelow = false
    @State private var interacting = false
    /// Which row's end is on screen. Keyed by row, so a report from the row that was newest cannot speak for the new one,
    /// whichever order the two arrive in.
    @State private var endOnScreen: String?
    /// "Show the N earlier messages" was tapped: land on the first of them once they are there.
    @State private var toTop = false

    private var metrics = FeedMetrics()

    private let bottomID = "feed-bottom"

    var body: some View {
        let newest = FeedRows.newestKey(feed)
        ScrollView {
            VStack(alignment: .leading, spacing: Design.Space.turn) {
                if !screen.archived && rows.allSatisfy({ if case .day = $0 { true } else { false } }) {
                    EmptyChat(instance: feed.instance, main: screen.chat.ref.isFrom).padding(.horizontal, Design.Space.gutter)
                }
                ForEach(rows) { row in
                    // Each row keeps its own margins, capped and centred on the column (spec §5).
                    FeedRowView(row: row, archived: screen.archived)
                        .frame(maxWidth: Design.measure, alignment: .leading)
                        .frame(maxWidth: .infinity)
                        .padding(.horizontal, Design.Space.gutter)
                        .id(row.id)
                        .background(alignment: .bottom) {
                            if let newest, row.id == newest {
                                Color.clear.frame(height: 1)
                                    .id(newest)
                                    .onScrollVisibilityChange { visible in
                                        if visible { endOnScreen = newest } else if endOnScreen == newest { endOnScreen = nil }
                                    }
                            }
                        }
                }
                Color.clear.frame(height: 1).id(bottomID)
            }
            // At rest at either end nothing lies in the fades (`feedEdges`): the turn's spacing and the 1 pt of
            // `bottomID` count towards the foot.
            .padding(.top, metrics.fade)
            .padding(.bottom, metrics.fade - Design.Space.turn)
            .scrollTargetLayout()
        }
        .safeAreaInset(edge: .top, spacing: 0) {
            let questions = FeedRows.waitingQuestions(feed)
            if let oldest = questions.first {
                Button {
                    pinned = false
                    withAnimation(reduceMotion ? nil : .default) { position.scrollTo(id: oldest.key, anchor: .top) }
                } label: {
                    Text("\(questions.count) \(questions.count == 1 ? "question" : "questions") waiting")
                        .font(Design.Text.pill)
                        .foregroundStyle(Color.brand)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, Design.Space.gutter)
                        .padding(.vertical, Design.Space.s8)
                        .quietHitArea()
                }
                .buttonStyle(.plain)
                .background(Color.canvas)
                .accessibilityHint("Scrolls to the oldest unanswered question")
            }
        }
        .readingTextSize()
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
        .onChange(of: newest != nil && endOnScreen == newest, initial: true) { _, onScreen in newestVisible(onScreen) }
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
        .onChange(of: pill, initial: true) { _, pill in screen.latestPill = pill }
        .onChange(of: screen.jumpToLatest) {
            pinned = true
            newBelow = false
            withAnimation(reduceMotion ? nil : .default) { position.scrollTo(edge: .bottom) }
        }
    }

    private var pill: ChatScreenModel.LatestPill? {
        guard !pinned && distance > 200 else { return nil }
        return newBelow ? .newBelow : .latest
    }

    private func stickToBottom() {
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) { position.scrollTo(edge: .bottom) }
    }
}

/// "↓ Latest", or "↓ New below" with a message waiting: above the composer, with the feed fading out above it.
struct LatestPillButton: View {
    let pill: ChatScreenModel.LatestPill
    var action: () -> Void
    private var metrics = FeedMetrics()

    var body: some View {
        Button(action: action) {
            Label(pill == .newBelow ? "New below" : "Latest", systemImage: "arrow.down")
                .font(Design.Text.pill)
                .foregroundStyle(pill == .newBelow ? Color.brand : Color.textPrimary)
                .padding(.horizontal, Design.Space.s12)
                .frame(minHeight: metrics.pillHeight)
                .quietHitArea()
        }
        .buttonStyle(.plain)
        .floatingPill()
        .accessibilityLabel(pill == .newBelow ? "New messages below" : "Scroll to the latest message")
    }
}

private extension View {
    /// The system's glass on a floating control, on solid `canvas`. Solid `elevated` under Reduce Transparency.
    func floatingPill() -> some View { modifier(FloatingPill()) }
}

private struct FloatingPill: ViewModifier {
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    func body(content: Content) -> some View {
        if reduceTransparency {
            content
                .background(Color.elevated, in: .capsule)
                .overlay { Capsule().strokeBorder(Color.hairline) }     // it may float over an `elevated` block
        } else {
            content
                .glassEffect(.regular.interactive(), in: .capsule)
                .background(Color.canvas, in: .capsule)
        }
    }
}

/// "Chat with <n>": no chat shown (§4.2 `chat` null).
struct EmptyChat: View {
    let instance: String
    var main = false

    var body: some View {
        VStack(spacing: Design.Space.inTurn) {
            ShaftMark()
            Text("Chat with \(instance)").font(.title3.weight(.semibold))
            if main {
                Text("Your conversation with \(instance). What it sends from its cycles lands here too. For a separate topic, start a thread.")
                    .font(.callout).foregroundStyle(Color.textSecondary)
            } else {
                Text("Your message starts a new conversation.").font(.callout).foregroundStyle(Color.textSecondary)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }
}
