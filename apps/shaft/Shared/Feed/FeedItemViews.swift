import SHAFTKit
import SwiftUI

// One view per item kind of the contract (§4.4), each with what the HTML feed draws for it (§14). A message with text
// has Copy, Reply (its whole Markdown source) and Select text in its long-press menu (on the Mac: its right-click menu,
// with ↩ Reply on a selection made in place), and an answer or a message the instance sent has its thumbs there too.

struct FeedItemView: View {
    let item: FeedItem
    let archived: Bool

    var body: some View {
        switch item {
        case .user(let turn): UserItemView(turn: turn)
        case .assistant(let answer): AssistantItemView(answer: answer, archived: archived)
        case .retried(let retried): RetriedView(retried: retried)
        case .sent(let message): SentItemView(message: message)
        case .later(let message): LaterItemView(message: message)
        case .ended(let ended): EndedView(ended: ended, archived: archived)
        case .unknown(_, let type):
            Text("This app does not show “\(type)” items yet: update it.")
                .font(.footnote).foregroundStyle(.secondary)
        }
    }
}

/// The long-press menu of a message with text: Copy, Reply, Select text, and the thumbs where they apply.
struct MessageMenu: ViewModifier {
    let source: String
    var rating: Rating?
    var k: Int?
    var file: String?
    var rateable = false
    @Environment(ChatScreenModel.self) private var screen

    func body(content: Content) -> some View {
        content
            // The Mac's text view draws its own right-click menu from this (it selects in place: no Select text).
            .environment(\.messageMenu, MessageMenuContext(source: source, rating: rating, k: k, file: file,
                                                           rateable: rateable))
            .contextMenu {
                if !source.isEmpty {
                    Button { Clipboard.copy(source) } label: { Label("Copy", systemImage: "doc.on.doc") }
                    if !screen.archived {
                        Button { screen.reply(with: source) } label: { Label("Reply", systemImage: "arrowshape.turn.up.left") }
                    }
                    #if os(iOS)
                    Button { screen.select(RenderedText.plain(source)) } label: {
                        Label("Select text", systemImage: "selection.pin.in.out")
                    }
                    #endif
                }
                if rateable {
                    Divider()
                    Button { screen.thumb("up", k: k, file: file, current: rating) } label: {
                        Label(rating?.thumb == "up" ? "Remove thumbs up" : "Thumbs up",
                              systemImage: rating?.thumb == "up" ? "hand.thumbsup.fill" : "hand.thumbsup")
                    }
                    Button { screen.thumb("down", k: k, file: file, current: rating) } label: {
                        Label(rating?.thumb == "down" ? "Remove thumbs down" : "Thumbs down",
                              systemImage: rating?.thumb == "down" ? "hand.thumbsdown.fill" : "hand.thumbsdown")
                    }
                }
            }
    }
}

extension View {
    func messageMenu(_ source: String, rating: Rating? = nil, k: Int? = nil, file: String? = nil,
                     rateable: Bool = false) -> some View {
        modifier(MessageMenu(source: source, rating: rating, k: k, file: file, rateable: rateable))
    }
}

// MARK: - The operator's messages

struct UserItemView: View {
    let turn: UserItem
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            VStack(alignment: .leading, spacing: 8) {
                if !turn.text.isEmpty { MessageOperatorText(text: turn.text) }
                AttachmentsView(attachments: turn.attachments)
                ChipRow(edits: turn.edits, deleted: turn.deleted, comments: turn.comments, feedback: turn.feedback)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(Color.accentColor.opacity(0.16), in: RoundedRectangle(cornerRadius: 16))
            .messageMenu(turn.text)
            HStack(spacing: 4) {
                Text(Stamp.time(turn.ts))
                if turn.interjected { Text("· during its reply") }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
            .accessibilityLabel(turn.interjected ? "Sent \(Stamp.full(turn.ts)), during its reply"
                                                 : "Sent \(Stamp.full(turn.ts))")
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 40)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("You")
    }
}

/// "✎ edited <path>", "🗑 deleted <path>", "💬 c3", "👎 feedback": what went with an operator's message.
struct ChipRow: View {
    var edits: [String] = []
    var deleted: [String] = []
    var comments: [CommentRef] = []
    var feedback: [String] = []
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        if !(edits.isEmpty && deleted.isEmpty && comments.isEmpty && feedback.isEmpty) {
            FlowLayout(spacing: 6) {
                ForEach(edits, id: \.self) { path in
                    Button { screen.openDocument(path) } label: { Chip(symbol: "pencil", text: "edited \(path)") }
                        .buttonStyle(.plain)
                }
                ForEach(deleted, id: \.self) { path in
                    Chip(symbol: "trash", text: "deleted \(path)")
                }
                ForEach(comments, id: \.id) { ref in
                    Button { screen.openDocument(ref.doc, thread: ref.id) } label: { Chip(symbol: "text.bubble", text: ref.id) }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Comment \(ref.id) on \(ref.doc)")
                }
                if !feedback.isEmpty { Chip(symbol: "hand.thumbsdown", text: "feedback") }
            }
        }
    }
}

struct Chip: View {
    let symbol: String
    let text: String

    var body: some View {
        Label(text, systemImage: symbol)
            .font(.caption)
            .lineLimit(1)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .frame(minHeight: 30)
            .background(Color.secondary.opacity(0.14), in: Capsule())
    }
}

// MARK: - The instance's answers

struct AssistantItemView: View {
    let answer: AssistantItem
    let archived: Bool
    @Environment(ChatScreenModel.self) private var screen
    @Environment(InstancesStore.self) private var store

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !answer.content.isEmpty {
                MessageMarkdown(source: answer.content)
                    .messageMenu(answer.content, rating: answer.rating, k: answer.k, rateable: true)
            } else if answer.failure == nil {
                Text("(no answer)").foregroundStyle(.secondary).italic()
            }
            if answer.incomplete {
                Label("incomplete", systemImage: "exclamationmark.circle").font(.caption).foregroundStyle(.orange)
            }
            if let failure = answer.failure { FailureView(failure: failure) }
            ForEach(answer.docs, id: \.path) { doc in
                Button { screen.openDocument(doc.path) } label: {
                    DocCard(path: doc.path, created: doc.created, archived: archived)
                }
                .buttonStyle(.plain)
            }
            if !answer.answered.isEmpty || !answer.opened.isEmpty {
                FlowLayout(spacing: 6) {
                    ForEach(answer.answered, id: \.id) { ref in
                        Button { screen.openDocument(ref.doc, thread: ref.id) } label: {
                            Chip(symbol: "text.bubble", text: "answered \(ref.id)")
                        }
                        .buttonStyle(.plain)
                    }
                    ForEach(answer.opened, id: \.id) { ref in
                        Button { screen.openDocument(ref.doc, thread: ref.id) } label: {
                            Chip(symbol: "text.bubble", text: "opened \(ref.id)")
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            if !answer.activity.isEmpty { ActivityDisclosure(activity: answer.activity) }
            AnswerFooter(ts: answer.ts, source: answer.content, rating: answer.rating, k: answer.k, file: nil,
                         keepActions: answer.last)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(screen.instance)")
    }
}

/// An answer's time, its rating, and (on the last answer, kept in view) Copy, Reply and the thumbs.
struct AnswerFooter: View {
    let ts: Date
    let source: String
    let rating: Rating?
    let k: Int?
    let file: String?
    let keepActions: Bool
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        HStack(spacing: 2) {
            Text(Stamp.time(ts))
                .font(.caption2)
                .foregroundStyle(.secondary)
                .accessibilityLabel(Stamp.full(ts))
            if keepActions && !source.isEmpty {
                CopyButton(text: source)
                if !screen.archived {
                    Button { screen.reply(with: source) } label: {
                        Image(systemName: "arrowshape.turn.up.left").font(.footnote).foregroundStyle(.secondary)
                            .touchTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Reply")
                }
            }
            if keepActions || rating != nil {
                ThumbButton(thumb: "up", rating: rating, k: k, file: file)
                ThumbButton(thumb: "down", rating: rating, k: k, file: file)
            }
            if let rating { RatingState(rating: rating, k: k, file: file) }
            Spacer(minLength: 0)
        }
    }
}

struct ThumbButton: View {
    let thumb: String
    let rating: Rating?
    let k: Int?
    let file: String?
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        let lit = rating?.thumb == thumb
        Button { screen.thumb(thumb, k: k, file: file, current: rating) } label: {
            Image(systemName: "hand.thumbs\(thumb)\(lit ? ".fill" : "")")
                .font(.footnote)
                .foregroundStyle(lit ? Color.accentColor : Color.secondary)
                .touchTarget()
        }
        .buttonStyle(.plain)
        .accessibilityLabel(thumb == "up" ? "Thumbs up" : "Thumbs down")
        .accessibilityValue(lit ? (rating?.reason.isEmpty == false ? "On: \(rating!.reason)" : "On") : "Off")
        .accessibilityHint(lit ? "Removes the rating" : "")
    }
}

/// "sent 14:06", "waiting for your next message", or Send… for a rating kept and not sent.
struct RatingState: View {
    let rating: Rating
    let k: Int?
    let file: String?
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        if let sent = rating.sent {
            Text("sent \(Stamp.time(sent.ts))").font(.caption2).foregroundStyle(.secondary)
        } else if rating.pending {
            Text("goes with your next message").font(.caption2).foregroundStyle(.secondary)
        } else {
            Button("Send…") { screen.sendRating(rating, k: k, file: file) }
                .font(.caption)
                .frame(minHeight: 44)
                .accessibilityHint("Sends this rating to the instance")
        }
    }
}

/// "what it did (n)": the reply's activity, folded.
struct ActivityDisclosure: View {
    let activity: [String]
    @State private var open = false

    var body: some View {
        DisclosureGroup(isExpanded: $open) {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(activity.enumerated()), id: \.offset) { _, line in
                    Text(line).font(.caption.monospaced()).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(.top, 4)
        } label: {
            Text("what it did (\(activity.count))").font(.caption).foregroundStyle(.secondary)
        }
        .frame(minHeight: 44)
    }
}

/// A document an answer made or changed: opens it. In an earlier chat it opens the current file.
struct DocCard: View {
    let path: String
    let created: Bool
    let archived: Bool

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "doc.richtext").foregroundStyle(Color.accentColor).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(path).lineLimit(1).truncationMode(.middle)
                Text(archived ? "Open in the current chat to edit or comment" : (created ? "created" : "changed"))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary).accessibilityHidden(true)
        }
        .padding(10)
        .frame(minHeight: 44)
        .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
        .accessibilityElement(children: .combine)
        .accessibilityHint("Opens the document")
    }
}

/// Why a reply failed and what to do (§4.4 `failure`): Retry (asking first when it must), the pages to check, and
/// the raw note under Technical details.
struct FailureView: View {
    let failure: Failure
    @Environment(ChatScreenModel.self) private var screen
    @Environment(InstancesStore.self) private var store
    @Environment(\.openURL) private var openURL
    @State private var details = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(failure.why, systemImage: failure.kind == "stopped" ? "stop.circle" : "exclamationmark.triangle")
                .foregroundStyle(failure.kind == "stopped" ? Color.secondary : Color.orange)
            if let action = failure.action {
                Text(action).font(.callout).foregroundStyle(.secondary)
            }
            HStack(spacing: 12) {
                if let retry = failure.retry {
                    Button("Retry") { screen.retry(retry) }
                        .buttonStyle(.borderedProminent)
                        .frame(minHeight: 44)
                }
                ForEach(failure.links, id: \.url) { link in
                    Button(link.label) {
                        if let url = store.client.credential.webURL(link.url) { openURL(url) }
                    }
                    .frame(minHeight: 44)
                    .accessibilityHint("Opens the web page")
                }
            }
            if !failure.detail.isEmpty {
                DisclosureGroup("Technical details", isExpanded: $details) {
                    Text(failure.detail)
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .font(.caption)
                .frame(minHeight: 44)
            }
        }
        .padding(10)
        .background(Color.orange.opacity(failure.kind == "stopped" ? 0.05 : 0.1), in: RoundedRectangle(cornerRadius: 10))
    }
}

struct RetriedView: View {
    let retried: RetriedItem

    var body: some View {
        Label("The first try failed and was sent again (\(Stamp.time(retried.ts))).",
              systemImage: "arrow.clockwise")
            .font(.footnote)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Messages the instance sent, Next-cycle messages, the end

struct SentItemView: View {
    let message: SentItem
    @Environment(ChatScreenModel.self) private var screen

    var heading: String {
        if message.kind == "question" {
            return message.read ? "\(screen.instance) asked you a question (answered)" : "\(screen.instance) asks you"
        }
        return "\(screen.instance) sent you a message"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(heading, systemImage: message.kind == "question" ? "questionmark.bubble" : "envelope")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(message.kind == "question" && !message.read ? Color.accentColor : Color.secondary)
            if !message.text.isEmpty {
                MessageMarkdown(source: message.text)
                    .messageMenu(message.text, rating: message.rating, file: message.file, rateable: true)
            }
            AttachmentsView(attachments: message.attachments)
            AnswerFooter(ts: message.ts, source: message.text, rating: message.rating, k: nil, file: message.file,
                         keepActions: message.kind == "question" && !message.read)
        }
        .padding(12)
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.secondary.opacity(0.35)))
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
    }
}

struct LaterItemView: View {
    let message: LaterItem

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            VStack(alignment: .leading, spacing: 8) {
                if !message.text.isEmpty { MessageOperatorText(text: message.text) }
                AttachmentsView(attachments: message.attachments)
                ChipRow(comments: message.comments)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.accentColor.opacity(0.5),
                                                                     style: StrokeStyle(lineWidth: 1, dash: [4, 3])))
            .messageMenu(message.text)
            Label("next cycle · \(message.read ? "read" : "waiting") · \(Stamp.time(message.ts))", systemImage: "moon")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 40)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("You, for its next cycle")
    }
}

struct EndedView: View {
    let ended: EndedItem
    let archived: Bool

    var body: some View {
        Text(archived ? "This conversation ended on \(Stamp.full(ended.ts))."
                      : "Ended \(Stamp.short(ended.ts)): journal write-up requested. Your next message starts a new conversation.")
            .font(.footnote)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 6)
    }
}

/// Lays chips out in lines, wrapping as text does.
struct FlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, line: CGFloat = 0, widest: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width {
                y += line + spacing
                x = 0
                line = 0
            }
            x += size.width + spacing
            line = max(line, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: min(widest, width), height: y + line)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, line: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX && x + size.width > bounds.maxX {
                y += line + spacing
                x = bounds.minX
                line = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            line = max(line, size.height)
        }
    }
}
