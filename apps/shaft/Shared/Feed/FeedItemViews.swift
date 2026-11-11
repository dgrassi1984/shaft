import SHAFTKit
import SwiftUI

// One view per item kind of the contract (§4.4), each with what the HTML feed draws for it (§14). A message with text
// has Copy, Reply (its whole Markdown source) and Select text in its long-press menu (on the Mac: its right-click menu,
// with ↩ Reply on a selection made in place); an answer or a message the instance sent has its thumbs there too, and
// always in its footer.

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
                .font(.footnote).foregroundStyle(Color.textSecondary)
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
    var cancel: (() -> Void)? = nil
    @Environment(ChatScreenModel.self) private var screen

    func body(content: Content) -> some View {
        content
            // The Mac's text view draws its own right-click menu from this (it selects in place: no Select text).
            .environment(\.messageMenu, MessageMenuContext(source: source, rating: rating, k: k, file: file,
                                                           rateable: rateable, cancel: cancel))
            .contextMenu {
                if !source.isEmpty {
                    Button { Clipboard.copy(source) } label: { Label("Copy", systemImage: "doc.on.doc") }
                    if !screen.archived {
                        Button { screen.reply(with: source, file: file) } label: { Label("Reply", systemImage: "arrowshape.turn.up.left") }
                    }
                    #if os(iOS)
                    Button { screen.select(RenderedText.plain(source)) } label: {
                        Label("Select text", systemImage: "selection.pin.in.out")
                    }
                    #endif
                }
                if let cancel {
                    Button("Cancel", action: cancel)
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
    /// A long press lifts text on an opaque surface; cycle cards keep their tint in the preview too.
    func liftsOnCanvas(tint: Color = .clear) -> some View {
        #if os(iOS)
        padding(Design.Space.s8)
            .background {
                Color.canvas.overlay(tint)
                    .clipShape(.rect(cornerRadius: Design.Radius.card, style: .continuous))
            }
            .contentShape(.contextMenuPreview, .rect(cornerRadius: Design.Radius.card, style: .continuous))
            .padding(-Design.Space.s8)
        #else
        self
        #endif
    }

    func messageMenu(_ source: String, rating: Rating? = nil, k: Int? = nil, file: String? = nil,
                     rateable: Bool = false, cancel: (() -> Void)? = nil) -> some View {
        modifier(MessageMenu(source: source, rating: rating, k: k, file: file, rateable: rateable, cancel: cancel))
    }
}

// MARK: - The operator's messages

struct UserItemView: View {
    let turn: UserItem
    @Environment(ChatScreenModel.self) private var screen
    @State private var hovering = false
    private var spoken: Bool { turn.via == "voice" }

    var body: some View {
        ChipColumn {
            VStack(alignment: .trailing, spacing: Design.Space.s4) {
                VStack(alignment: .leading, spacing: Design.Space.inTurn) {
                    if !turn.text.isEmpty { MessageOperatorText(text: turn.text) }
                    AttachmentsView(attachments: turn.attachments)
                    ChipRow(edits: turn.edits, deleted: turn.deleted, comments: turn.comments, feedback: turn.feedback)
                }
                .operatorChip()
                .messageMenu(turn.text)
                HStack(spacing: Design.Space.s4) {
                    #if os(macOS)
                    if !turn.text.isEmpty { HoverActions(source: turn.text, shown: hovering).padding(.trailing, 6) }
                    #endif
                    // Said aloud in voice mode (`via: voice`): its text is the transcript, which may hold
                    // recognition errors.
                    if spoken { Image(systemName: "waveform").imageScale(.small) }
                    Text(Stamp.time(turn.ts))
                    if turn.interjected { Text("· during its reply") }
                }
                .font(Design.Text.metadata)
                .foregroundStyle(Color.textSecondary)
                .accessibilityLabel((spoken ? "Spoken, " : "") + (turn.interjected ? "sent \(Stamp.full(turn.ts)), during its reply"
                                                                                  : "sent \(Stamp.full(turn.ts))"))
            }
        }
        #if os(macOS)
        .onHover { hovering = $0 }
        #endif
        .accessibilityElement(children: .contain)
        .accessibilityLabel("You")
    }
}

/// A message of the operator's: trailing, at most 78% of the column (spec §3), whatever it holds.
struct ChipColumn: Layout {
    private let share = 0.78

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let size = subviews.first?.sizeThatFits(ProposedViewSize(width: proposal.width.map { $0 * share }, height: nil))
            ?? .zero
        return CGSize(width: proposal.width ?? size.width, height: size.height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard let view = subviews.first else { return }
        let width = view.sizeThatFits(ProposedViewSize(width: bounds.width * share, height: nil)).width
        view.place(at: CGPoint(x: bounds.maxX, y: bounds.minY), anchor: .topTrailing,
                   proposal: ProposedViewSize(width: min(width, bounds.width * share), height: nil))
    }
}

extension View {
    /// The operator's own message: `operator` fill, radius 18, the composer's text insets.
    func operatorChip() -> some View {
        padding(.horizontal, Design.Space.s12)
            .padding(.vertical, 9)
            .background(Color.operatorChip, in: RoundedRectangle(cornerRadius: Design.Radius.chip, style: .continuous))
    }

    /// The same chip at reduced emphasis, for what has not reached the instance yet (next cycle, queued).
    func waitingChip() -> some View {
        padding(.horizontal, Design.Space.s12)
            .padding(.vertical, 9)
            .background(Color.canvas, in: RoundedRectangle(cornerRadius: Design.Radius.chip, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: Design.Radius.chip, style: .continuous).strokeBorder(Color.hairline)
            }
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
            FlowLayout {
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
            .font(Design.Text.pill)
            .foregroundStyle(Color.textSecondary)
            .lineLimit(1)
            .padding(.horizontal, Design.Space.s8)
            .padding(.vertical, Design.Space.s4)
            .pill()
            .overlay {          // `elevated` is a hair from `operator`: the line keeps the chip a chip inside a message
                RoundedRectangle(cornerRadius: Design.Radius.pill, style: .continuous).strokeBorder(Color.hairline)
            }
            .quietHitArea()
    }
}

// MARK: - The instance's answers

struct AssistantItemView: View {
    let answer: AssistantItem
    let archived: Bool
    @Environment(ChatScreenModel.self) private var screen
    @Environment(InstancesStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    @State private var steps = false

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.inTurn) {
            if !answer.content.isEmpty {
                // A reply cut short ends mid-text: what it left open is closed, as while it streamed.
                MessageMarkdown(source: answer.incomplete || answer.failure != nil
                                ? PartialMarkdown.closed(answer.content) : answer.content)
                    .liftsOnCanvas()
                    .messageMenu(answer.content, rating: answer.rating, k: answer.k, rateable: answer.failure == nil)
            } else if answer.failure == nil {
                Text("(no answer)").foregroundStyle(Color.textSecondary).italic()
            }
            if answer.incomplete {
                Label("incomplete", systemImage: "exclamationmark.circle").font(.caption).foregroundStyle(Color.warning)
            }
            if let failure = answer.failure { FailureView(failure: failure) }
            ForEach(answer.docs, id: \.path) { doc in
                Button { screen.openDocument(doc.path) } label: {
                    DocCard(path: doc.path, created: doc.created, archived: archived)
                }
                .buttonStyle(.plain)
            }
            if !answer.answered.isEmpty || !answer.opened.isEmpty {
                FlowLayout {
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
            AnswerFooter(ts: answer.ts, source: answer.content, rating: answer.rating, k: answer.k, file: nil,
                         keepActions: answer.last, hovering: hovering, steps: answer.activity.count, stepsOpen: $steps,
                         rateable: !answer.content.isEmpty && answer.failure == nil)
            if steps && !answer.activity.isEmpty {
                ActivityList(activity: answer.activity).transition(.opacity)
            }
        }
        .motion(Design.Motion.spring, value: steps, reduce: reduceMotion)
        .frame(maxWidth: .infinity, alignment: .leading)
        #if os(macOS)
        .onHover { hovering = $0 }
        #endif
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(screen.instance)")
    }
}

/// A message's footer, one 28 pt row: its time, "⚙ 4 steps", then the thumbs on every message with something to rate.
/// On iOS Copy and Reply stand before the thumbs on the last answer, kept in view. On the Mac they follow the thumbs on
/// every message, the last one too, while the pointer is over it, as the web does on a computer. It wraps rather than
/// overlap at the largest text sizes.
struct AnswerFooter: View {
    let ts: Date
    let source: String
    let rating: Rating?
    let k: Int?
    let file: String?
    let keepActions: Bool
    var hovering = false
    /// How many steps the answer took, and their list being open below this row.
    var steps = 0
    var stepsOpen: Binding<Bool> = .constant(false)
    /// A question not answered yet: Reply stands out in `accent`, at the end of the row.
    var asksReply = false
    /// A turn with no answer text, a failure and nothing else, has nothing to rate.
    var rateable = true
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        HStack(spacing: Design.Space.s8) {
            FlowLayout(spacing: 0) {
                Text(Stamp.time(ts))
                    .font(Design.Text.metadata)
                    .foregroundStyle(Color.textTertiary)
                    .padding(.trailing, Design.Space.s8)
                    .accessibilityLabel(Stamp.full(ts))
                if steps > 0 {
                    StepsPill(count: steps, open: stepsOpen).padding(.trailing, Design.Space.s4)
                }
                #if os(iOS)
                if keepActions && !source.isEmpty {
                    CopyButton(text: source)
                    if !screen.archived && !asksReply { ReplyIcon(source: source, file: file) }
                }
                #endif
                if rateable {
                    // Always here, on every message and answer: never only on hover or in a menu (2026-11-04).
                    ThumbButton(thumb: "up", rating: rating, k: k, file: file)
                    ThumbButton(thumb: "down", rating: rating, k: k, file: file)
                    if let rating { RatingState(rating: rating, k: k, file: file).padding(.leading, Design.Space.s4) }
                }
                #if os(macOS)
                // After the thumbs: hidden, they keep their room, which must not open a gap before the thumbs.
                if !source.isEmpty { HoverActions(source: source, file: file, shown: hovering, reply: !asksReply) }
                #endif
            }
            Spacer(minLength: 0)
            if asksReply && !source.isEmpty && !screen.archived {
                Button { screen.reply(with: source, file: file) } label: {
                    Label("Reply", systemImage: "arrowshape.turn.up.left")
                        .font(Design.Text.pill)
                        .foregroundStyle(Color.brand)
                        .frame(minHeight: Design.Space.s24 + Design.Space.s4)
                        .quietHitArea()
                }
                .buttonStyle(.plain)
                .fixedSize()
                .layoutPriority(1)
            }
        }
    }
}

/// Reply for a whole message.
private struct ReplyIcon: View {
    let source: String
    var file: String? = nil
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        Button { screen.reply(with: source, file: file) } label: { QuietIcon(symbol: "arrowshape.turn.up.left") }
            .buttonStyle(.plain)
            .help("Reply")
            .accessibilityLabel("Reply")
    }
}

#if os(macOS)
/// Copy and Reply for a whole message, after the thumbs while the pointer is over it (the web on a computer). Always laid
/// out, so the feed does not move as the pointer passes.
struct HoverActions: View {
    let source: String
    var file: String? = nil
    let shown: Bool
    /// False where the footer's own Reply stands at the row's end (a question).
    var reply = true
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        HStack(spacing: 0) {
            CopyButton(text: source).help("Copy")
            if reply && !screen.archived { ReplyIcon(source: source, file: file) }
        }
        .opacity(shown ? 1 : 0)
        .allowsHitTesting(shown)
    }
}
#endif

struct ThumbButton: View {
    let thumb: String
    let rating: Rating?
    let k: Int?
    let file: String?
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        let lit = rating?.thumb == thumb
        Button { screen.thumb(thumb, k: k, file: file, current: rating) } label: {
            QuietIcon(symbol: "hand.thumbs\(thumb)\(lit ? ".fill" : "")", colour: lit ? .brand : .textTertiary)
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
            Text("sent \(Stamp.time(sent.ts))").font(Design.Text.metadata).foregroundStyle(Color.textTertiary)
        } else if rating.pending {
            Text("goes with your next message").font(Design.Text.metadata).foregroundStyle(Color.textTertiary)
        } else {
            Button { screen.sendRating(rating, k: k, file: file) } label: {
                Text("Send…").font(Design.Text.pill).foregroundStyle(Color.brand).quietHitArea()
            }
            .buttonStyle(.plain)
            .accessibilityHint("Sends this rating to the instance")
        }
    }
}

/// "⚙ 4 steps" beside an answer's time: opens what it did below the row.
struct StepsPill: View {
    let count: Int
    @Binding var open: Bool

    var body: some View {
        Button { open.toggle() } label: {
            HStack(spacing: Design.Space.s4) {
                Image(systemName: "gearshape").accessibilityHidden(true)
                Text(count == 1 ? "1 step" : "\(count) steps")
            }
            .font(Design.Text.pill)
            .foregroundStyle(Color.textTertiary)
            .padding(.horizontal, Design.Space.s8)
            .padding(.vertical, Design.Space.s4)
            .pill()
            .quietHitArea()
        }
        .buttonStyle(.plain)
        .accessibilityLabel("What it did, \(count) steps")
        .accessibilityValue(open ? "Shown" : "Hidden")
        .accessibilityHint(open ? "Hides every step" : "Shows every step")
    }
}

/// What a reply did, step by step.
struct ActivityList: View {
    let activity: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            ForEach(Array(activity.enumerated()), id: \.offset) { _, line in
                Text(line).font(.caption.monospaced()).foregroundStyle(Color.textSecondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

/// A small text that opens what it holds below it: a block's detail, kept out of the reading.
struct QuietDisclosure<Content: View>: View {
    let title: LocalizedStringKey
    @ViewBuilder var content: () -> Content
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            Button { open.toggle() } label: {
                HStack(spacing: Design.Space.s4) {
                    Text(title)
                    Image(systemName: "chevron.right")
                        .imageScale(.small)
                        .rotationEffect(.degrees(open ? 90 : 0))
                        .accessibilityHidden(true)
                }
                .font(.caption)
                .foregroundStyle(Color.textSecondary)
                .quietHitArea()
            }
            .buttonStyle(.plain)
            #if os(macOS)
            .padding(.vertical, -Design.Footer.slack)      // the Mac's 28 pt target takes no layout: a bar stops at the text
            #endif
            .accessibilityValue(open ? "Shown" : "Hidden")
            if open { content().transition(.opacity) }
        }
        .motion(Design.Motion.spring, value: open, reduce: reduceMotion)
    }
}

/// A document an answer made or changed: opens it. In an earlier chat it opens the current file.
struct DocCard: View {
    let path: String
    let created: Bool
    let archived: Bool

    var body: some View {
        HStack(spacing: Design.Space.s12) {
            Image(systemName: "doc.richtext").foregroundStyle(Color.brand).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: Design.Space.s8) {
                    Text(path).lineLimit(1).truncationMode(.middle)
                    if !archived {
                        Text(created ? "created" : "changed")
                            .font(.caption)
                            .foregroundStyle(Color.textSecondary)
                            .fixedSize()
                    }
                }
                if archived {
                    Text("Open in the current chat to edit or comment")
                        .font(.caption).foregroundStyle(Color.textSecondary)
                }
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(Color.textTertiary)
                .accessibilityHidden(true)
        }
        .padding(.horizontal, Design.Space.s12)
        .frame(minHeight: Design.Space.s24 + Design.Space.s20)
        .elevatedBlock()
        .contentShape(.rect(cornerRadius: Design.Radius.card, style: .continuous))
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

    private var stopped: Bool { failure.kind == "stopped" }

    var body: some View {
        BarBlock(colour: stopped ? .hairline : .danger) {
            VStack(alignment: .leading, spacing: Design.Space.s4) {
                Text(failure.why)
                    .foregroundStyle(stopped ? Color.textSecondary : Color.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                if let action = failure.action {
                    Text(action).font(.subheadline).foregroundStyle(Color.textSecondary)
                }
                if failure.retry != nil || !failure.links.isEmpty {
                    FlowLayout(spacing: Design.Space.s12) {
                        if let retry = failure.retry {
                            Button("Retry") { screen.retry(retry) }
                                .buttonStyle(CompactAccentButton())
                        }
                        ForEach(failure.links, id: \.url) { link in
                            Button {
                                if let url = store.client.credential.webURL(link.url) { openURL(url) }
                            } label: {
                                Text(link.label).font(Design.Text.pill).foregroundStyle(Color.brand).quietHitArea()
                            }
                            .buttonStyle(.plain)
                            .accessibilityHint("Opens the web page")
                        }
                    }
                    .padding(.top, Design.Space.s4)
                }
                if !failure.detail.isEmpty {
                    QuietDisclosure(title: "Technical details") {
                        Text(failure.detail)
                            .font(.caption.monospaced())
                            .foregroundStyle(Color.textSecondary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }
}

/// A block on a 3 pt bar at its left: what needs the operator or what failed (spec §3).
struct BarBlock<Content: View>: View {
    var colour: Color = .brand
    @ViewBuilder var content: () -> Content

    var body: some View {
        content()
            .padding(.leading, Design.Space.s12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(alignment: .leading) { AccentBar(color: colour).clipShape(.capsule) }
    }
}

/// A small filled button in `accent`, for the one action a block offers.
struct CompactAccentButton: ButtonStyle {
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Design.Text.pill)
            .foregroundStyle(enabled ? Color.onAccent : Color.textTertiary)
            .padding(.horizontal, Design.Space.s12)
            .padding(.vertical, 5)
            .background(enabled ? Color.brand.opacity(configuration.isPressed ? 0.75 : 1) : Color.hairline,
                        in: RoundedRectangle(cornerRadius: Design.Radius.pill, style: .continuous))
            .quietHitArea()
    }
}

struct RetriedView: View {
    let retried: RetriedItem

    var body: some View {
        Label("The first try failed and was sent again (\(Stamp.time(retried.ts))).",
              systemImage: "arrow.clockwise")
            .font(.footnote)
            .foregroundStyle(Color.textSecondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Messages the instance sent, Next-cycle messages, the end

struct SentItemView: View {
    let message: SentItem
    @Environment(ChatScreenModel.self) private var screen
    @State private var hovering = false

    var heading: String {
        if message.kind == "question" {
            return message.read ? "\(screen.instance) asked you a question (answered)" : "\(screen.instance) asks you"
        }
        return "\(screen.instance) sent you a message"
    }

    /// A question still waiting for its answer: the loudest thing in the feed.
    private var asking: Bool { message.kind == "question" && !message.read }

    /// The heading, and beside it where the message came from: "⏾ from its cycle" for a cycle's answer to work left in
    /// this thread; in From, "re: «title»" for the ended thread it answers, which opens that earlier chat.
    @ViewBuilder private var header: some View {
        let title = Text(heading)
            .font(Design.Text.pill)
            .foregroundStyle(asking ? Color.brand : Color.textSecondary)
        if message.fromCycle || message.re != nil {
            FlowLayout(spacing: Design.Space.s8) {
                title
                if message.fromCycle {
                    Label("from its cycle", systemImage: "moon")
                        .font(Design.Text.metadata)
                        .foregroundStyle(Color.textSecondary)
                        .help("Written later, in its cycle, for what was left in this thread")
                }
                if let re = message.re { link(re) }
            }
        } else {
            title
        }
    }

    @ViewBuilder private func link(_ re: ThreadLink) -> some View {
        let label = Text("re: «\(re.title)»").font(Design.Text.pill).lineLimit(1)
        if let open = screen.openEarlier {
            Button { open(re.cid) } label: {
                label.foregroundStyle(Color.brand).quietHitArea()
            }
            .buttonStyle(.plain)
            .help("It answers this ended thread")
            .accessibilityHint("Opens that earlier chat")
        } else {
            label.foregroundStyle(Color.textSecondary)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.inTurn) {
            header
            if !message.text.isEmpty {
                MessageMarkdown(source: message.text)
                    .liftsOnCanvas(tint: Color.brand.opacity(0.06))
                    .messageMenu(message.text, rating: message.rating, file: message.file, rateable: true)
            }
            AttachmentsView(attachments: message.attachments)
            AnswerFooter(ts: message.ts, source: message.text, rating: message.rating, k: nil, file: message.file,
                         keepActions: asking, hovering: hovering, asksReply: !screen.archived)
                .padding(.bottom, -Design.Footer.slack)     // keep the footer target within the card padding
        }
        .padding(Design.Space.s12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.brand.opacity(0.06), in: RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous))
        .accessibilityElement(children: .contain)
        #if os(macOS)
        .onHover { hovering = $0 }
        #endif
    }
}

struct LaterItemView: View {
    let message: LaterItem
    @Environment(ChatScreenModel.self) private var screen

    private var canCancel: Bool {
        message.cancellable == true && message.cancelled == nil && !screen.archived
    }

    private func cancel() {
        Task { await screen.chat.uncycle(message) }
    }

    /// Where the note stands: taken back, read in a cycle, or still in its inbox.
    private var status: String {
        if message.cancelled != nil { return String(localized: "cancelled") }
        return message.read ? String(localized: "read") : String(localized: "waiting")
    }

    /// Cancel's action while the note can still be taken back; none once it cannot.
    private var cancelAction: (() -> Void)? {
        guard canCancel else { return nil }
        return { cancel() }
    }

    @ViewBuilder private var noteText: some View {
        if message.text.isEmpty {
            EmptyView()
        } else if message.cancelled != nil {
            Text(message.text).strikethrough().foregroundStyle(Color.textSecondary)
        } else {
            MessageOperatorText(text: message.text)
        }
    }

    private var footer: some View {
        HStack(spacing: Design.Space.s12) {
            Label("next cycle · \(status) · \(Stamp.time(message.ts))", systemImage: "moon")
                .font(Design.Text.metadata)
                .foregroundStyle(Color.textSecondary)
            if canCancel {
                Button { cancel() } label: {
                    Text("Cancel").font(Design.Text.pill).foregroundStyle(Color.brand).quietHitArea()
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Cancel the next-cycle note")
            }
        }
    }

    var body: some View {
        ChipColumn {
            VStack(alignment: .trailing, spacing: Design.Space.s4) {
                VStack(alignment: .leading, spacing: Design.Space.inTurn) {
                    noteText
                    AttachmentsView(attachments: message.attachments)
                    ChipRow(comments: message.comments)
                }
                .padding(.horizontal, Design.Space.s12)
                .padding(.vertical, 9)
                .background(Color.canvas, in: RoundedRectangle(cornerRadius: Design.Radius.chip, style: .continuous))
                .overlay {
                    if !message.read && message.cancelled == nil {
                        RoundedRectangle(cornerRadius: Design.Radius.chip, style: .continuous)
                            .strokeBorder(Color.textTertiary, style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
                    }
                }
                .messageMenu(message.text, cancel: cancelAction)
                footer
            }
        }
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
            .foregroundStyle(Color.textSecondary)
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.vertical, Design.Space.s4)
    }
}

/// Lays small things out in lines, each centred on its line, wrapping as text does.
struct FlowLayout: Layout {
    var spacing: CGFloat = Design.Space.s8

    private struct Line {
        var views: [(view: LayoutSubview, size: CGSize, x: CGFloat)] = []
        var height: CGFloat = 0
    }

    private func lines(_ subviews: Subviews, width: CGFloat) -> [Line] {
        var lines = [Line()]
        var x: CGFloat = 0
        for view in subviews {
            var size = view.sizeThatFits(.unspecified)
            if size.width > width { size = view.sizeThatFits(ProposedViewSize(width: width, height: nil)) }
            if x > 0 && x + size.width > width {
                lines.append(Line())
                x = 0
            }
            lines[lines.count - 1].views.append((view, size, x))
            lines[lines.count - 1].height = max(lines[lines.count - 1].height, size.height)
            x += size.width + spacing
        }
        return lines
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        let lines = lines(subviews, width: width)
        let widest = lines.compactMap { $0.views.last.map { $0.x + $0.size.width } }.max() ?? 0
        return CGSize(width: min(widest, width),
                      height: lines.map(\.height).reduce(0, +) + spacing * CGFloat(lines.count - 1))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for line in lines(subviews, width: bounds.width) {
            for item in line.views {
                item.view.place(at: CGPoint(x: bounds.minX + item.x, y: y + (line.height - item.size.height) / 2),
                                proposal: ProposedViewSize(item.size))
            }
            y += line.height + spacing
        }
    }
}
