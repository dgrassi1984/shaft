import SHAFTKit
import SwiftUI

/// One document: rendered, with its comment threads (open first, resolved folded), what a later write dropped of the
/// operator's (Restore yours), and its actions: Edit, Select text (↩ Reply into the composer, 💬 Comment on the
/// selection), 💬 on the whole document, Download, Delete. In an earlier chat it only reads.
struct DocumentDetailView: View {
    let path: String
    @Environment(DocumentsStore.self) private var docs
    @Environment(ChatScreenModel.self) private var screen
    @Environment(FilePreview.self) private var preview
    @Environment(\.dismiss) private var dismiss
    @State private var editing = false
    @State private var selecting = false
    @State private var composing: CommentDraft?
    @State private var deleting = false
    @State private var showResolved = false

    private var doc: DocumentDetail? { docs.open?.path == path ? docs.open : nil }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let doc {
                        if let overwritten = doc.overwritten, !screen.archived {
                            OverwrittenNotice(path: path, overwritten: overwritten, sha: doc.sha)
                        }
                        MarkdownText(source: doc.text)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        threads(doc)
                    } else if let error = docs.error {
                        ContentUnavailableView {
                            Label(error.code == "too_big" ? "Too big to show" : "Could not open it",
                                  systemImage: "doc.questionmark")
                        } description: {
                            Text(error.message)
                        } actions: {
                            Button("Download") { download() }
                        }
                    } else {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
                    }
                }
                .padding(16)
            }
            .onChange(of: screen.openThread) { _, thread in
                guard let thread else { return }
                withAnimation { proxy.scrollTo(thread, anchor: .top) }
                screen.openThread = nil
            }
            .task(id: doc?.sha) {
                if let thread = screen.openThread, doc != nil {
                    try? await Task.sleep(for: .milliseconds(200))
                    proxy.scrollTo(thread, anchor: .top)
                    screen.openThread = nil
                }
            }
        }
        .navigationTitle((path as NSString).lastPathComponent)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar { toolbar }
        .task(id: path) { await docs.load(path) }
        .refreshable { await docs.load(path) }
        .sheet(isPresented: $editing) {
            if let doc { DocumentEditor(path: path, text: doc.text, sha: doc.sha).environment(docs) }
        }
        .sheet(isPresented: $selecting) {
            if let doc {
                DocumentSelectSheet(path: path, text: RenderedText.plain(doc.text)) { anchor in
                    composing = CommentDraft(anchor: anchor)
                }
            }
        }
        .sheet(item: $composing) { draft in
            CommentComposer(draft: draft).environment(docs)
        }
        .confirmationDialog("Delete \(path)?", isPresented: $deleting, titleVisibility: .visible) {
            Button("Delete", role: .destructive) {
                guard let doc else { return }
                Task { if await docs.delete(path: path, sha: doc.sha) { dismiss() } }
            }
        } message: {
            Text("\(screen.instance) hears of it with your next message. Earlier versions stay in its git history.")
        }
        .safeAreaInset(edge: .bottom) {
            if let said = docs.said {
                Text(said).font(.footnote).foregroundStyle(.secondary).padding(8).frame(maxWidth: .infinity)
                    .background(.regularMaterial)
            } else if let error = docs.error, doc != nil {
                Text(error.message).font(.footnote).foregroundStyle(.red).padding(8).frame(maxWidth: .infinity)
                    .background(.regularMaterial)
            }
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            if !screen.archived && doc != nil {
                Button { editing = true } label: { Image(systemName: "pencil") }
                    .accessibilityLabel("Edit")
            }
            Menu {
                if doc != nil {
                    Button { selecting = true } label: { Label("Select text", systemImage: "selection.pin.in.out") }
                    if !screen.archived {
                        Button { composing = CommentDraft(anchor: nil) } label: {
                            Label("Comment on the whole document", systemImage: "text.bubble")
                        }
                    }
                }
                Button { download() } label: { Label("Download", systemImage: "arrow.down.doc") }
                if !screen.archived && doc != nil {
                    Divider()
                    Button(role: .destructive) { deleting = true } label: { Label("Delete…", systemImage: "trash") }
                }
            } label: {
                Image(systemName: "ellipsis.circle").touchTarget()
            }
            .accessibilityLabel("Document actions")
        }
    }

    @ViewBuilder
    private func threads(_ doc: DocumentDetail) -> some View {
        let open = doc.threads.filter { $0.state == "open" }
        let resolved = doc.threads.filter { $0.state != "open" }
        let rendered = RenderedText.plain(doc.text)
        if !open.isEmpty || !resolved.isEmpty {
            Divider()
            Text("Comments").font(.headline).accessibilityAddTraits(.isHeader)
        }
        ForEach(open) { thread in
            CommentThreadView(thread: thread, rendered: rendered, reply: { composing = CommentDraft(thread: thread.id) },
                              edit: { post in composing = CommentDraft(editing: post) })
                .id(thread.id)
        }
        if !resolved.isEmpty {
            DisclosureGroup("Resolved (\(resolved.count))", isExpanded: $showResolved) {
                ForEach(resolved) { thread in
                    CommentThreadView(thread: thread, rendered: rendered,
                                      reply: { composing = CommentDraft(thread: thread.id) },
                                      edit: { post in composing = CommentDraft(editing: post) })
                        .id(thread.id)
                }
            }
            .frame(minHeight: 44)
        }
    }

    private func download() {
        Task {
            guard let file = await docs.download(path) else { return }
            if let url = try? AttachmentLoader.write(file.data, name: file.filename ?? (path as NSString).lastPathComponent,
                                                     key: "doc-\(screen.instance)-\(path)") {
                preview.open(local: url)
            }
        }
    }
}

/// "Lines you wrote were dropped by a later write of <n>" with the diff and Restore yours (§10.2 `overwritten`).
struct OverwrittenNotice: View {
    let path: String
    let overwritten: Overwritten
    let sha: String
    @Environment(DocumentsStore.self) private var docs
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("\(overwritten.lines) lines you saved at \(Stamp.short(overwritten.ts)) were dropped by a later write.",
                  systemImage: "exclamationmark.triangle")
                .foregroundStyle(.orange)
            DisclosureGroup("What changed", isExpanded: $open) {
                ScrollView(.horizontal) {
                    Text(overwritten.diff).font(.caption.monospaced()).textSelection(.enabled)
                }
            }
            .frame(minHeight: 44)
            Button("Restore yours") {
                Task { _ = await docs.save(path: path, text: overwritten.text, sha: sha) }
            }
            .buttonStyle(.bordered)
        }
        .padding(10)
        .background(Color.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
    }
}

/// Editing a document's Markdown. Save sends the sha it was read at; a stale save shows what changed meanwhile and
/// lets the operator replace it with their text, take the newer version, or keep editing.
struct DocumentEditor: View {
    let path: String
    let original: String
    @State private var text: String
    @State private var sha: String
    @Environment(DocumentsStore.self) private var docs
    @Environment(\.dismiss) private var dismiss
    @State private var conflict: DocumentsStore.Conflict?
    @State private var problem: String?
    @State private var saving = false
    @State private var leaving = false

    init(path: String, text: String, sha: String) {
        self.path = path
        original = text
        _text = State(initialValue: text)
        _sha = State(initialValue: sha)
    }

    var body: some View {
        NavigationStack {
            TextEditor(text: $text)
                .font(.system(.body, design: .monospaced))
                .padding(.horizontal, 8)
                .accessibilityLabel("Markdown of \(path)")
                .navigationTitle(path)
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                #endif
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") { if text == original { dismiss() } else { leaving = true } }
                    }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Save") { save(over: sha) }.disabled(saving || text == original)
                    }
                }
                .safeAreaInset(edge: .bottom) {
                    if let problem {
                        Text(problem).font(.footnote).foregroundStyle(.red).padding(8).frame(maxWidth: .infinity)
                            .background(.regularMaterial)
                    }
                }
                .confirmationDialog("Drop your changes to \(path)?", isPresented: $leaving, titleVisibility: .visible) {
                    Button("Discard changes", role: .destructive) { dismiss() }
                }
                .sheet(item: $conflict) { conflict in
                    StaleSaveSheet(conflict: conflict,
                                   replace: { save(over: conflict.sha ?? "none") },
                                   takeTheirs: {
                                       Task {
                                           await docs.load(path)
                                           dismiss()
                                       }
                                   })
                }
        }
        .sheetFrame()
        .interactiveDismissDisabled(text != original)
    }

    private func save(over sha: String) {
        saving = true
        Task {
            switch await docs.save(path: path, text: text, sha: sha) {
            case .saved:
                dismiss()
            case .stale(let stale):
                conflict = stale
            case .refused(let error):
                problem = error.message
            }
            saving = false
        }
    }
}

extension DocumentsStore.Conflict: @retroactive Identifiable {
    public var id: String { (sha ?? "new") + path }
}

/// A save refused because the document changed since it was opened: what it has now that the edit has not.
struct StaleSaveSheet: View {
    let conflict: DocumentsStore.Conflict
    var replace: () -> Void
    var takeTheirs: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(conflict.message)
                    if let diff = conflict.diff, !diff.isEmpty {
                        Text("What it has now that your text has not:").font(.subheadline.weight(.semibold))
                        ScrollView(.horizontal) {
                            Text(diff).font(.caption.monospaced()).textSelection(.enabled).padding(8)
                        }
                        .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
                    }
                }
                .padding()
            }
            .navigationTitle("It changed meanwhile")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .safeAreaInset(edge: .bottom) {
                VStack(spacing: 8) {
                    Button("Replace it with my text") {
                        dismiss()
                        replace()
                    }
                    .buttonStyle(.borderedProminent)
                    Button("Take its version (drop my edit)", role: .destructive) {
                        dismiss()
                        takeTheirs()
                    }
                    Button("Keep editing") { dismiss() }
                }
                .frame(maxWidth: .infinity)
                .padding()
                .background(.regularMaterial)
            }
        }
        .sheetFrame()
    }
}

/// Select text in a document: ↩ Reply quotes it into the composer (from notes/docs/<path>) and closes the documents
/// so the composer shows; 💬 Comment opens a comment on the selection.
struct DocumentSelectSheet: View {
    let path: String
    let text: String
    var comment: (CommentAnchor) -> Void
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.dismiss) private var dismiss
    @State private var selection = NSRange(location: 0, length: 0)

    var body: some View {
        NavigationStack {
            SelectableTextView(text: text, selection: $selection,
                               quoteTitle: screen.chat.draft.quotes.isEmpty ? String(localized: "\u{21A9}\u{FE0E} Reply")
                                                                            : String(localized: "+ Quote"),
                               onQuote: reply,
                               onComment: screen.archived ? nil : { _, range in commentOn(range) })
                .navigationTitle("Select text")
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                #endif
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                    if !screen.archived {
                        ToolbarItemGroup(placement: .sheetAction) {
                            Button { commentOn(selection) } label: { Image(systemName: "text.bubble") }
                                .disabled(selection.length == 0)
                                .accessibilityLabel("Comment on the selection")
                            Button("\u{21A9}\u{FE0E} Reply") { reply((text as NSString).substring(with: selection)) }
                                .disabled(selection.length == 0)
                        }
                    }
                }
        }
        .sheetFrame()
    }

    private func reply(_ selected: String) {
        screen.quote(selected, doc: path)
        dismiss()
        if screen.documentsCover { screen.showDocuments = false }
    }

    private func commentOn(_ range: NSRange) {
        guard let anchor = Anchoring.anchor(in: text, range: range) else { return }
        dismiss()
        comment(anchor)
    }
}
