import SHAFTKit
import SwiftUI

/// One row of the feed (`FeedRows`): an item, a day, or what follows the conversation.
struct FeedRowView: View {
    let row: FeedRow
    let archived: Bool
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        switch row {
        case .earlier(let count):
            Button { screen.chat.showEarlier() } label: {
                Text("Show the \(count) earlier messages")
                    .font(Design.Text.pill)
                    .foregroundStyle(Color.brand)
                    .padding(.horizontal, Design.Space.s12)
                    .padding(.vertical, Design.Space.s8)
                    .pill()
                    .quietHitArea()
            }
            .buttonStyle(.plain)
            .frame(maxWidth: .infinity)
        case .day(let date):
            DaySeparator(date: date)
        case .item(let item):
            FeedItemView(item: item, archived: archived)
        case .live(let reply):
            LiveReplyView(reply: reply)
        case .queued(let message):
            QueuedRow(message: message, archived: archived)
        case .pendingFeedback(let pending):
            PendingFeedbackRow(pending: pending)
        case .pendingEdits(let paths):
            PendingEditsRow(paths: paths)
        case .unsent(let entry):
            UnsentRow(entry: entry)
        case .end:
            EmptyView()                     // End the conversation lives in the ⋯ menu; FeedView leaves this row out
        }
    }
}

struct DaySeparator: View {
    let date: Date

    var body: some View {
        Text(Stamp.day(date))
            .font(.caption)
            .foregroundStyle(Color.textSecondary)
            .frame(maxWidth: .infinity)
            .accessibilityAddTraits(.isHeader)
    }
}

/// The reply running now (§4.6): flowing type as it streams, under one status line with its clock, what it is doing
/// now and its steps (tap: every step). Stop is the composer's while a reply runs.
struct LiveReplyView: View {
    let reply: Reply
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var steps = false

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.inTurn) {
            if let now = reply.activity.last {
                Button { steps.toggle() } label: { status(now: now) }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Now: \(now). \(reply.activity.count) steps")
                    .accessibilityHint(steps ? "Hides every step" : "Shows every step")
                if steps { ActivityList(activity: reply.activity).transition(.opacity) }
            } else {
                status(now: nil)
            }
            if !reply.answer.isEmpty {
                // Not a live region: VoiceOver would read it again at every refresh. The finished reply is announced.
                MessageMarkdown(source: reply.answer)
                    .environment(\.markdownStreaming, true)
                    .accessibilityAddTraits([])
            }
        }
        .motion(Design.Motion.spring, value: steps, reduce: reduceMotion)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(screen.instance) is replying")
    }

    /// One line, where what it is doing gives way to the clock and the steps; at the accessibility text sizes, where
    /// those alone would not fit, its parts stack.
    private func status(now: String?) -> some View {
        let large = dynamicTypeSize.isAccessibilitySize
        let layout = large ? AnyLayout(VStackLayout(alignment: .leading, spacing: Design.Space.s4))
                           : AnyLayout(HStackLayout(spacing: Design.Space.s4))
        return layout {
            HStack(spacing: Design.Space.s4) {
                Image(systemName: StateIcon.symbol("", replying: true))
                    .foregroundStyle(Color.brand)
                    .symbolEffect(.pulse, isActive: !reduceMotion)
                    .accessibilityHidden(true)
                    .padding(.trailing, 2)
                TimelineView(.periodic(from: reply.started, by: 1)) { context in
                    Text("replying · \(Stamp.elapsed(since: reply.started, now: context.date))")
                }
            }
            .fixedSize()
            if let now {
                Text(large ? now : "· \(now)")
                    .lineLimit(1)
                    .layoutPriority(-1)
                if !large { Spacer(minLength: 0) }
                Text(large ? stepCount : "· \(stepCount)")
                    .foregroundStyle(Color.textTertiary)
                    .fixedSize()
            }
        }
        .font(Design.Text.pill.monospacedDigit())
        .foregroundStyle(Color.textSecondary)
        .frame(maxWidth: .infinity, minHeight: Design.Space.s24 + Design.Space.s4, alignment: .leading)
        .contentShape(Rectangle())
    }

    private var stepCount: String {
        reply.activity.count == 1 ? "1 step" : "\(reply.activity.count) steps"
    }
}

/// A message queued for after the reply (§4.7), with Cancel.
struct QueuedRow: View {
    let message: QueuedMessage
    let archived: Bool
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        ChipColumn {
            VStack(alignment: .trailing, spacing: Design.Space.s4) {
                VStack(alignment: .leading, spacing: Design.Space.inTurn) {
                    if !message.text.isEmpty { OperatorTextView(text: message.text) }
                    ForEach(Array(message.attachments.enumerated()), id: \.offset) { _, file in
                        FileChip(name: file.name, bytes: file.bytes)
                    }
                }
                .waitingChip()
                HStack(spacing: Design.Space.s12) {
                    Label("queued — sent when this reply ends", systemImage: "clock")
                        .font(Design.Text.metadata)
                        .foregroundStyle(Color.textSecondary)
                    if !archived {
                        Button { Task { await screen.chat.unqueue(message.mid) } } label: {
                            Text("Cancel").font(Design.Text.pill).foregroundStyle(Color.brand).quietHitArea()
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Cancel the queued message")
                    }
                }
            }
        }
    }
}

/// "👎 on its reply of 14:06 goes with your next message", × (§4.8).
struct PendingFeedbackRow: View {
    let pending: PendingFeedback
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "hand.thumbsdown").accessibilityHidden(true)
            Text("on its reply of \(Stamp.time(pending.replyTs)) goes with your next message")
            Spacer(minLength: 0)
            Button { Task { await screen.chat.unpend(pending.id) } } label: { QuietIcon(symbol: "xmark") }
                .buttonStyle(.plain)
                .accessibilityLabel("Keep it, but not with the next message")
        }
        .font(.footnote)
        .foregroundStyle(Color.textSecondary)
        .padding(.leading, Design.Space.s12)
        .padding(.trailing, Design.Space.s4)
        .padding(.vertical, Design.Space.s4)
        .elevatedBlock()
    }
}

/// "✎ Your changes to … go to <n> with your next message".
struct PendingEditsRow: View {
    let paths: [String]
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        Label("Your changes to \(paths.joined(separator: ", ")) go to \(screen.instance) with your next message",
              systemImage: "pencil")
            .font(.footnote)
            .foregroundStyle(Color.textSecondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A message on its way, or not sent (the outbox): Retry with its `mid`, Edit (back into the composer), Discard.
struct UnsentRow: View {
    let entry: OutboxEntry
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        ChipColumn {
            VStack(alignment: .trailing, spacing: Design.Space.s4) {
                VStack(alignment: .leading, spacing: Design.Space.inTurn) {
                    let text = QuoteText.compose(entry.text, quotes: entry.quotes)
                    if !text.isEmpty { OperatorTextView(text: text) }
                    ForEach(entry.files) { file in FileChip(name: file.name, bytes: file.bytes) }
                }
                .operatorChip()
                if entry.state == .sending {
                    Label(mode, systemImage: "arrow.up.circle")
                        .font(Design.Text.metadata)
                        .foregroundStyle(Color.textSecondary)
                } else {
                    Text("not sent: \(entry.reason ?? "unknown")")
                        .font(Design.Text.metadata)
                        .foregroundStyle(Color.warning)
                        .multilineTextAlignment(.trailing)
                    HStack(spacing: Design.Space.s16) {
                        textButton("Retry", colour: .brand) { Task { await screen.chat.retry(entry.mid) } }
                        textButton("Edit", colour: .brand) { screen.chat.editUnsent(entry.mid) }
                        textButton("Discard", colour: .danger) { screen.chat.discardUnsent(entry.mid) }
                    }
                    .frame(minHeight: Design.Space.s24 + Design.Space.s4)
                }
            }
        }
        .accessibilityElement(children: .contain)
    }

    private func textButton(_ title: LocalizedStringKey, colour: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(Design.Text.pill).foregroundStyle(colour).quietHitArea()
        }
        .buttonStyle(.plain)
    }

    var mode: String {
        switch entry.mode {
        case .now: "sending…"
        case .queue: "queueing…"
        case .background: "sending for its next cycle…"
        }
    }
}
