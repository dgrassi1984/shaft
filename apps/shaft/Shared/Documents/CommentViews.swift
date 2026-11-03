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
/// the turns that answered it without a section, and its actions.
struct CommentThreadView: View {
    let thread: CommentThread
    let rendered: String
    var reply: () -> Void
    var edit: (CommentPost) -> Void
    @Environment(DocumentsStore.self) private var docs
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(thread.id).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                if thread.state != "open" {
                    Text("resolved").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Text(Stamp.short(thread.created)).font(.caption2).foregroundStyle(.secondary)
            }
            if let anchor = thread.anchor {
                Text(anchor.exact)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(4)
                    .padding(.leading, 8)
                    .overlay(alignment: .leading) { Rectangle().fill(Color.yellow.opacity(0.7)).frame(width: 3) }
                if Anchoring.locate(anchor, in: rendered) == nil {
                    Text("text no longer in the document").font(.caption).foregroundStyle(.orange)
                }
            } else {
                Text("on the whole document").font(.caption).foregroundStyle(.secondary)
            }
            if thread.docChanged {
                Text("the document changed since this comment").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(thread.posts) { post in
                PostView(post: post, instance: screen.instance, edit: { edit(post) })
            }
            if !thread.unsectioned.isEmpty {
                Text("answered in the chat, without a section").font(.caption).foregroundStyle(.secondary)
            }
            if !screen.archived {
                HStack(spacing: 16) {
                    Button("Reply", action: reply)
                    Button(thread.state == "open" ? "Resolve" : "Reopen") {
                        Task { await docs.setState(thread: thread.id, resolved: thread.state == "open") }
                    }
                }
                .font(.footnote)
                .frame(minHeight: 44)
            }
        }
        .padding(10)
        .background(Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Comment thread \(thread.id)")
    }
}

/// One post: who, when, its Markdown, how it went; an unsent one can be edited, deleted or sent.
struct PostView: View {
    let post: CommentPost
    let instance: String
    var edit: () -> Void
    @Environment(DocumentsStore.self) private var docs
    @Environment(ChatScreenModel.self) private var screen
    @State private var deleting = false

    var status: String? {
        if post.unsent { return String(localized: "not sent") }
        if let sent = post.sent {
            if sent.hasPrefix("inbox/") { return String(localized: "for its next cycle") }
            if sent.hasPrefix("message:") { return String(localized: "in a message") }
            return String(localized: "sent in the chat")
        }
        if post.seen != nil { return String(localized: "answered from its copy") }
        return nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(post.by == "operator" ? String(localized: "You") : post.by).font(.caption.weight(.semibold))
                Text(Stamp.short(post.ts)).font(.caption2).foregroundStyle(.secondary)
                if let status {
                    Text("· \(status)").font(.caption2).foregroundStyle(post.unsent ? Color.orange : Color.secondary)
                }
            }
            MarkdownText(source: post.text)
            if post.unsent && !screen.archived {
                HStack(spacing: 14) {
                    Button("Edit", action: edit)
                    Button("Delete", role: .destructive) { deleting = true }
                    Button("Send") { Task { await docs.sendPost(pid: post.pid, when: .now) } }
                    Button("Next cycle") { Task { await docs.sendPost(pid: post.pid, when: .cycle) } }
                }
                .font(.footnote)
                .frame(minHeight: 44)
                .confirmationDialog("Delete this comment?", isPresented: $deleting) {
                    Button("Delete", role: .destructive) { Task { await docs.deletePost(pid: post.pid) } }
                }
            }
        }
        .accessibilityElement(children: .contain)
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
    @State private var delivery = DocumentsStore.Delivery.now
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
        switch delivery {
        case .now: return String(localized: "Send comment")
        case .cycle: return String(localized: "Send for its next cycle")
        case .keep: return String(localized: "Save without sending")
        }
    }

    var body: some View {
        NavigationStack {
            Form {
                if let anchor = draft.anchor {
                    Section { Text(anchor.exact).foregroundStyle(.secondary).lineLimit(4) }
                }
                Section {
                    TextField("Your comment", text: $text, axis: .vertical)
                        .lineLimit(3...12)
                        .focused($focused)
                }
                if draft.editing == nil {
                    Section("Send to \(screen.instance)") {
                        Picker("Send", selection: $delivery) {
                            Text("now").tag(DocumentsStore.Delivery.now)
                            Text("in its next cycle").tag(DocumentsStore.Delivery.cycle)
                            Text("don't send").tag(DocumentsStore.Delivery.keep)
                        }
                        .pickerStyle(.inline)
                        .labelsHidden()
                    }
                }
                if let problem { Text(problem).foregroundStyle(.red) }
            }
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
    }

    private func submit() {
        busy = true
        Task {
            let ok: Bool
            if let post = draft.editing {
                ok = await docs.edit(pid: post.pid, text: text)
            } else if let thread = draft.thread {
                ok = await docs.reply(thread: thread, text: text, send: delivery)
            } else {
                ok = await docs.comment(anchor: draft.anchor, text: text, send: delivery)
            }
            busy = false
            if ok { dismiss() } else { problem = docs.error?.message }
        }
    }
}
