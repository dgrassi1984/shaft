import SHAFTKit
import SwiftUI

/// What the comment composer is for: a new thread (on a selection, or the whole document: `anchor` nil), a reply in a
/// thread, or an edit of an unsent post.
struct CommentDraft: Identifiable, Equatable {
    let id = UUID()
    var anchor: CommentAnchor?
    var thread: String?
    var editing: CommentPost?

    init(anchor: CommentAnchor?) { self.anchor = anchor }
    init(thread: String) { self.thread = thread }
    init(editing: CommentPost) { self.editing = editing }
}

/// A comment thread (§10.2): the text it is on (or "text no longer in the document"), its posts with how each went,
/// the turns that answered it without a section, and its actions: one primary (Send for a post not sent yet, otherwise
/// Reply) and a ⋯ menu for the rest.
struct CommentThreadView: View {
    let thread: CommentThread
    let rendered: String
    var reply: () -> Void
    var edit: (CommentPost) -> Void
    @Environment(DocumentsStore.self) private var docs
    @Environment(ChatScreenModel.self) private var screen
    @State private var deleting: CommentPost?

    /// The operator's own posts not delivered yet: only those can be edited, deleted or sent (§10.2).
    private var unsent: [CommentPost] { thread.posts.filter(\.mineUnsent) }

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s12) {
            HStack(alignment: .firstTextBaseline, spacing: Design.Space.s8) {
                Text(thread.state == "open" ? thread.id : "\(thread.id) · \(String(localized: "resolved"))")
                    .font(Design.Text.pill)
                    .foregroundStyle(Color.textSecondary)
                Spacer(minLength: 0)
                Text(Stamp.short(thread.created))
                    .font(Design.Text.metadata)
                    .foregroundStyle(Color.textSecondary)
            }
            VStack(alignment: .leading, spacing: Design.Space.s4) {
                if let anchor = thread.anchor {
                    QuotedPassage(text: anchor.exact)
                    if Anchoring.locate(anchor, in: rendered) == nil {
                        Text("text no longer in the document").font(.caption).foregroundStyle(Color.warning)
                    }
                } else {
                    Text("on the whole document").font(.caption).foregroundStyle(Color.textSecondary)
                }
                if thread.docChanged {
                    Text("the document changed since this comment").font(.caption).foregroundStyle(Color.textSecondary)
                }
            }
            ForEach(thread.posts) { post in
                PostView(post: post, instance: screen.instance)
            }
            if !thread.unsectioned.isEmpty {
                Text("answered in the chat, without a section").font(.caption).foregroundStyle(Color.textSecondary)
            }
            if !screen.archived { actions }
        }
        .padding(Design.Space.s12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .elevatedBlock()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Comment thread \(thread.id)")
    }

    private var actions: some View {
        HStack(spacing: Design.Space.s12) {
            if let first = unsent.first {
                Button("Send") { Task { await docs.sendPost(pid: first.pid, when: .now) } }.compactPrimary()
            } else {
                Button("Reply", action: reply).compactPrimary()
            }
            Spacer(minLength: 0)
            Menu {
                ForEach(unsent) { post in
                    if unsent.count > 1 {
                        Section(String(post.text.prefix(40))) { postActions(post) }
                    } else {
                        postActions(post)
                    }
                }
                if !unsent.isEmpty {
                    Divider()
                    Button(action: reply) { Label("Reply", systemImage: "arrowshape.turn.up.left") }
                }
                Button {
                    Task { await docs.setState(thread: thread.id, resolved: thread.state == "open") }
                } label: {
                    if thread.state == "open" {
                        Label("Resolve", systemImage: "checkmark.circle")
                    } else {
                        Label("Reopen", systemImage: "arrow.uturn.backward")
                    }
                }
            } label: {
                // Drawn at the row's height, as the footers are; the shape keeps the 44 pt target.
                Image(systemName: "ellipsis")
                    .foregroundStyle(Color.textSecondary)
                    .frame(width: 44, height: 28)
                    .contentShape(Rectangle().inset(by: -8))
            }
            .iconMenu()
            .accessibilityLabel("Comment actions")
        }
        .confirmationDialog("Delete this comment?",
                            isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
                            presenting: deleting) { post in
            Button("Delete", role: .destructive) { Task { await docs.deletePost(pid: post.pid) } }
        }
    }

    /// What the menu holds for a post not sent yet. The first one's Send is the primary button.
    @ViewBuilder
    private func postActions(_ post: CommentPost) -> some View {
        if post.pid != unsent.first?.pid {
            Button { Task { await docs.sendPost(pid: post.pid, when: .now) } } label: {
                Label("Send", systemImage: "paperplane")
            }
        }
        Button { Task { await docs.sendPost(pid: post.pid, when: .cycle) } } label: {
            Label("Next cycle", systemImage: "moon")
        }
        Button { edit(post) } label: { Label("Edit", systemImage: "pencil") }
        Button(role: .destructive) { deleting = post } label: { Label("Delete", systemImage: "trash") }
    }
}

extension CommentPost {
    fileprivate var mineUnsent: Bool { unsent && by == "operator" }
}

/// The text a comment is on: a quiet quote with the same accent the page's highlight has.
struct QuotedPassage: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.callout)
            .foregroundStyle(Color.textSecondary)
            .lineLimit(4)
            .padding(.leading, Design.Space.s12)
            .overlay(alignment: .leading) { AccentBar() }
    }
}

/// One post: who, when, how it went, its Markdown.
struct PostView: View {
    let post: CommentPost
    let instance: String

    var status: String? {
        if post.mineUnsent { return String(localized: "not sent") }
        if let sent = post.sent {
            if sent.hasPrefix("inbox/") { return String(localized: "for its next cycle") }
            if sent.hasPrefix("message:") { return String(localized: "in a message") }
            return String(localized: "sent in the chat")
        }
        if post.seen != nil { return String(localized: "answered from its copy") }
        return nil
    }

    private var author: Text {
        Text(post.by == "operator" ? String(localized: "You") : post.by)
            .font(Design.Text.pill)
            .foregroundStyle(Color.textPrimary)
    }

    private var time: Text {
        Text(Stamp.short(post.ts))
            .font(Design.Text.metadata)
            .foregroundStyle(Color.textSecondary)
    }

    private var state: Text {
        Text(status.map { " · \($0)" } ?? "")
            .font(Design.Text.metadata)
            .foregroundStyle(post.mineUnsent ? Color.warning : Color.textSecondary)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            // One Text, so at the largest sizes it wraps as a line rather than as columns.
            Text("\(author) \(time)\(state)")
            MarkdownText(source: post.text)
        }
        .accessibilityElement(children: .contain)
    }
}

/// The thread (or threads) on a passage tapped in the document, over it so the reading place stays. Each thread is
/// read from the store, so what its actions change shows at once.
struct CommentThreadsSheet: View {
    let ids: [String]
    let rendered: String
    @Environment(DocumentsStore.self) private var docs
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.dismiss) private var dismiss
    @State private var composing: CommentDraft?

    private var threads: [CommentThread] { (docs.open?.threads ?? []).filter { ids.contains($0.id) } }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: Design.Space.s12) {
                    ForEach(threads) { thread in
                        CommentThreadView(thread: thread, rendered: rendered,
                                          reply: { composing = CommentDraft(thread: thread.id) },
                                          edit: { post in composing = CommentDraft(editing: post) })
                    }
                }
                .readingColumn()
                .padding(.vertical, Design.Space.s8)
            }
            .background(Color.canvas.ignoresSafeArea())
            .navigationTitle(ids.count == 1 ? "Comment" : "Comments")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
            }
            .safeAreaBar(edge: .bottom) {
                if let error = docs.error { DocumentStatus(text: error.message, color: Color.danger) }
            }
        }
        .sheetFrame()
        .presentationDetents([.medium, .large])
        .sheet(item: $composing) { draft in
            CommentComposer(draft: draft).environment(docs).environment(screen)
        }
    }
}

/// Writing a comment: its text and how it goes — now (the default: a message of its own, or an interjection while it
/// replies), in its next cycle, or not at all (it can read it); editing an unsent post saves it as a note.
struct CommentComposer: View {
    let draft: CommentDraft
    @Environment(DocumentsStore.self) private var docs
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var sending = DocumentsStore.Delivery.now
    @State private var busy = false
    @State private var problem: String?
    @FocusState private var focused: Bool

    private var title: String {
        if draft.editing != nil { return String(localized: "Edit comment") }
        if draft.thread != nil { return String(localized: "Reply") }
        return draft.anchor == nil ? String(localized: "Comment on the document") : String(localized: "Comment")
    }

    private var action: String {
        if draft.editing != nil { return String(localized: "Save note") }
        switch sending {
        case .now: return String(localized: "Send comment")
        case .cycle: return String(localized: "Send for its next cycle")
        case .keep: return String(localized: "Save without sending")
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Design.Space.s16) {
                    if let anchor = draft.anchor { QuotedPassage(text: anchor.exact) }
                    TextField("Your comment", text: $text, axis: .vertical)
                        .lineLimit(3...12)
                        .focused($focused)
                        .padding(Design.Space.s12)
                        .elevatedBlock()
                    if draft.editing == nil {
                        VStack(alignment: .leading, spacing: Design.Space.s8) {
                            Text("Send to \(screen.instance)")
                                .font(Design.Text.pill)
                                .foregroundStyle(Color.textSecondary)
                            delivery
                        }
                    }
                    if let problem { Text(problem).font(.footnote).foregroundStyle(Color.danger) }
                }
                .readingColumn()
                .padding(.vertical, Design.Space.s8)
            }
            .scrollBounceBehavior(.basedOnSize)
            .navigationTitle(title)
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(action) { submit() }
                        .disabled(busy || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .onAppear {
                text = draft.editing?.text ?? ""
                focused = true
            }
        }
        .sheetFrame()
        .presentationDetents([.medium, .large])
    }

    /// The three ways it can go, side by side; a menu at accessibility sizes, where the segments would cut their words.
    @ViewBuilder
    private var delivery: some View {
        let picker = Picker("Send", selection: $sending) {
            Text("now").tag(DocumentsStore.Delivery.now)
            Text("in its next cycle").tag(DocumentsStore.Delivery.cycle)
            Text("don't send").tag(DocumentsStore.Delivery.keep)
        }
        .labelsHidden()
        if typeSize.isAccessibilitySize {
            picker.pickerStyle(.menu)
        } else {
            picker.pickerStyle(.segmented)
        }
    }

    private func submit() {
        busy = true
        Task {
            let ok: Bool
            if let post = draft.editing {
                ok = await docs.edit(pid: post.pid, text: text)
            } else if let thread = draft.thread {
                ok = await docs.reply(thread: thread, text: text, send: sending)
            } else {
                ok = await docs.comment(anchor: draft.anchor, text: text, send: sending)
            }
            busy = false
            if ok { dismiss() } else { problem = docs.error?.message }
        }
    }
}
