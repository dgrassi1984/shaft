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
                Text("Show the \(count) earlier messages").frame(maxWidth: .infinity).frame(minHeight: 44)
            }
            .buttonStyle(.bordered)
        case .day(let date):
            DaySeparator(date: date)
        case .item(let item):
            FeedItemView(item: item, archived: archived)
        case .live(let reply):
            LiveReplyView(reply: reply, canStop: screen.chat.content?.canStop == true)
        case .queued(let message):
            QueuedRow(message: message, archived: archived)
        case .pendingFeedback(let pending):
            PendingFeedbackRow(pending: pending)
        case .pendingEdits(let paths):
            PendingEditsRow(paths: paths)
        case .unsent(let entry):
            UnsentRow(entry: entry)
        case .end:
            EndRow()
        }
    }
}

struct DaySeparator: View {
    let date: Date

    var body: some View {
        HStack(spacing: 8) {
            Rectangle().fill(Color.secondary.opacity(0.25)).frame(height: 1)
            Text(Stamp.day(date)).font(.caption.weight(.medium)).foregroundStyle(.secondary).fixedSize()
            Rectangle().fill(Color.secondary.opacity(0.25)).frame(height: 1)
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// The reply running now (§4.6): "replying since 14:05" with its clock, what it is doing now (tap: every step),
/// the dots before the first word, then the answer as it streams, and Stop.
struct LiveReplyView: View {
    let reply: Reply
    let canStop: Bool
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var steps = false
    @State private var stopping = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                TimelineView(.periodic(from: reply.started, by: 1)) { context in
                    Text("replying since \(Stamp.time(reply.started)) · \(Stamp.elapsed(since: reply.started, now: context.date))")
                        .font(.footnote.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                if canStop {
                    Button(role: .destructive) {
                        stopping = true
                        Task {
                            await screen.chat.stopReply()
                            stopping = false
                        }
                    } label: {
                        Label("Stop", systemImage: "stop.fill").frame(minHeight: 44)
                    }
                    .buttonStyle(.bordered)
                    .disabled(stopping)
                    .accessibilityLabel("Stop the reply")
                }
            }
            if let now = reply.activity.last {
                Button { steps.toggle() } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Image(systemName: "gearshape").accessibilityHidden(true)
                        Text(now).lineLimit(steps ? nil : 1)
                        Text("· \(reply.activity.count) steps").foregroundStyle(.tertiary)
                    }
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Now: \(now). \(reply.activity.count) steps")
                .accessibilityHint(steps ? "Hides every step" : "Shows every step")
                if steps {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(reply.activity.dropLast().enumerated()), id: \.offset) { _, line in
                            Text(line).font(.caption.monospaced()).foregroundStyle(.tertiary)
                        }
                    }
                }
            }
            if reply.answer.isEmpty {
                TypingDots(animated: !reduceMotion)
            } else {
                // Not a live region: VoiceOver would read it again at every refresh. The finished reply is announced.
                MessageMarkdown(source: reply.answer)
                    .accessibilityAddTraits([])
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 12))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(screen.instance) is replying")
    }
}

struct TypingDots: View {
    let animated: Bool
    @State private var phase = 0

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3) { index in
                Circle()
                    .fill(Color.secondary)
                    .frame(width: 7, height: 7)
                    .opacity(animated ? (phase == index ? 1 : 0.35) : 0.6)
            }
        }
        .padding(.vertical, 6)
        .accessibilityLabel("Writing")
        .task(id: animated) {
            guard animated else { return }
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(350))
                phase = (phase + 1) % 3
            }
        }
    }
}

/// A message queued for after the reply (§4.7), with Cancel.
struct QueuedRow: View {
    let message: QueuedMessage
    let archived: Bool
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            VStack(alignment: .leading, spacing: 6) {
                if !message.text.isEmpty { OperatorTextView(text: message.text) }
                ForEach(Array(message.attachments.enumerated()), id: \.offset) { _, file in
                    FileChip(name: file.name, bytes: file.bytes)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 16))
            HStack(spacing: 8) {
                Text("queued — sent when this reply ends").font(.caption2).foregroundStyle(.secondary)
                if !archived {
                    Button("Cancel") { Task { await screen.chat.unqueue(message.mid) } }
                        .font(.caption)
                        .frame(minHeight: 44)
                        .accessibilityLabel("Cancel the queued message")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 40)
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
            Button { Task { await screen.chat.unpend(pending.id) } } label: {
                Image(systemName: "xmark").touchTarget()
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Keep it, but not with the next message")
        }
        .font(.footnote)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 10)
        .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
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
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A message on its way, or not sent (the outbox): Retry with its `mid`, Edit (back into the composer), Discard.
struct UnsentRow: View {
    let entry: OutboxEntry
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            VStack(alignment: .leading, spacing: 6) {
                let text = QuoteText.compose(entry.text, quotes: entry.quotes)
                if !text.isEmpty { OperatorTextView(text: text) }
                ForEach(entry.files) { file in FileChip(name: file.name, bytes: file.bytes) }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(Color.accentColor.opacity(entry.state == .sending ? 0.1 : 0.05),
                        in: RoundedRectangle(cornerRadius: 16))
            .overlay {
                if entry.state == .notSent {
                    RoundedRectangle(cornerRadius: 16).strokeBorder(Color.red.opacity(0.6))
                }
            }
            if entry.state == .sending {
                Label(mode, systemImage: "arrow.up.circle").font(.caption2).foregroundStyle(.secondary)
            } else {
                Text("not sent: \(entry.reason ?? "unknown")").font(.caption2).foregroundStyle(.red)
                    .multilineTextAlignment(.trailing)
                HStack(spacing: 14) {
                    Button("Discard", role: .destructive) { screen.chat.discardUnsent(entry.mid) }
                    Button("Edit") { screen.chat.editUnsent(entry.mid) }
                    Button("Retry") { Task { await screen.chat.retry(entry.mid) } }.bold()
                }
                .font(.footnote)
                .frame(minHeight: 44)
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 40)
        .accessibilityElement(children: .contain)
    }

    var mode: String {
        switch entry.mode {
        case .now: "sending…"
        case .queue: "queueing…"
        case .background: "sending for its next cycle…"
        }
    }
}

/// End the conversation (§7.3): offered when the chat is open, idle and has turns.
struct EndRow: View {
    @Environment(ChatScreenModel.self) private var screen
    @State private var asking = false

    var body: some View {
        Button("End the conversation") { asking = true }
            .font(.footnote)
            .frame(maxWidth: .infinity, minHeight: 44)
            .confirmationDialog("End the conversation with \(screen.instance)?", isPresented: $asking,
                                titleVisibility: .visible) {
                Button("End it") { Task { await screen.chat.end() } }
            } message: {
                Text("It writes up its journal. Your next message starts a new conversation.")
            }
    }
}
