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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var editing = false
    @State private var selecting = false
    @State private var composing: CommentDraft?
    @State private var deleting = false
    @State private var showResolved = false
    /// The threads of a passage tapped in the document.
    @State private var tapped: TappedThreads?
    /// The answer of a write made on this page, while it shows.
    @State private var said: String?
    @State private var rendered = ""

    private var doc: DocumentDetail? { docs.open?.path == path ? docs.open : nil }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: Design.Space.turn) {
                    if let doc {
                        if let overwritten = doc.overwritten, !screen.archived {
                            OverwrittenNotice(path: path, overwritten: overwritten, sha: doc.sha)
                        }
                        DocumentMarkdown(doc: doc) { tapped = TappedThreads(ids: $0) }
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
                        ProgressView().frame(maxWidth: .infinity).padding(.top, Design.Space.s24)
                    }
                }
                .readingColumn()
                .padding(.vertical, Design.Space.s16)
            }
            #if os(macOS)
            .background(Color.canvas.ignoresSafeArea())          // under the toolbar too: no seam at its foot
            #else
            .background(Color.canvas)
            #endif
            .onChange(of: screen.openThread) { _, thread in
                guard let thread else { return }
                reveal(thread)
                withAnimation(reduceMotion ? nil : Design.Motion.spring) { proxy.scrollTo(thread, anchor: .top) }
                screen.openThread = nil
            }
            .task(id: doc?.sha) {
                if let thread = screen.openThread, doc != nil {
                    reveal(thread)
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
        #if os(macOS)
        .inspectorPage()
        #endif
        .task(id: path) { await docs.load(path) }
        .onChange(of: doc?.text, initial: true) { _, text in
            rendered = RenderedText.plain(text ?? "")
        }
        .refreshable { await docs.load(path) }
        .sheet(isPresented: $editing) {
            if let doc { DocumentEditor(path: path, text: doc.text, sha: doc.sha).environment(docs) }
        }
        .sheet(isPresented: $selecting) {
            if doc != nil {
                DocumentSelectSheet(path: path, text: rendered) { anchor in
                    composing = CommentDraft(anchor: anchor)
                }
            }
        }
        .sheet(item: $composing) { draft in
            CommentComposer(draft: draft).environment(docs)
        }
        .sheet(item: $tapped) { tapped in
            CommentThreadsSheet(ids: tapped.ids, rendered: rendered)
                .environment(docs)
                .environment(screen)
        }
        .confirmationDialog("Delete \(path)?", isPresented: $deleting, titleVisibility: .visible) {
            Button("Delete", role: .destructive) {
                guard let doc else { return }
                Task { if await docs.delete(path: path, sha: doc.sha) { dismiss() } }
            }
        } message: {
            Text("\(screen.instance) hears of it with your next message. Earlier versions stay in its git history.")
        }
        .safeAreaBar(edge: .bottom) {
            if let said {
                DocumentStatus(text: said, color: Color.textSecondary)
            } else if let error = docs.error, doc != nil {
                DocumentStatus(text: error.message, color: Color.danger)
            }
        }
        .onChange(of: docs.said) { _, new in said = new }
        .task(id: said) {
            // A write's answer shows for a moment: left up it hid the page, and any failure after it.
            guard said != nil else { return }
            try? await Task.sleep(for: .seconds(6))
            if !Task.isCancelled { said = nil }
        }
    }

    /// A resolved thread sits in its fold: opening it from the chat opens the fold too.
    private func reveal(_ thread: String) {
        if doc?.threads.contains(where: { $0.id == thread && $0.state != "open" }) == true { showResolved = true }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        #if os(macOS)
        InspectorHeader(title: (path as NSString).lastPathComponent, symbol: "chevron.left", label: "Back",
                        action: { dismiss() }) { toolbarActions }
        #else
        ToolbarItemGroup(placement: .primaryAction) { toolbarActions }
        #endif
    }

    @ViewBuilder
    private var toolbarActions: some View {
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
            Image(systemName: "ellipsis")
        }
        .accessibilityLabel("Document actions")
    }

    @ViewBuilder
    private func threads(_ doc: DocumentDetail) -> some View {
        let open = doc.threads.filter { $0.state == "open" }
        let resolved = doc.threads.filter { $0.state != "open" }
        if !open.isEmpty || !resolved.isEmpty {
            VStack(alignment: .leading, spacing: Design.Space.s12) {
                Rectangle().fill(Color.hairline).frame(height: 1)
                Text("Comments")
                    .font(Design.Text.heading(2))
                    .foregroundStyle(Color.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                ForEach(open) { thread in
                    CommentThreadView(thread: thread, rendered: rendered,
                                      reply: { composing = CommentDraft(thread: thread.id) },
                                      edit: { post in composing = CommentDraft(editing: post) })
                        .id(thread.id)
                }
                if !resolved.isEmpty {
                    DisclosureGroup(isExpanded: $showResolved) {
                        VStack(spacing: Design.Space.s12) {
                            ForEach(resolved) { thread in
                                CommentThreadView(thread: thread, rendered: rendered,
                                                  reply: { composing = CommentDraft(thread: thread.id) },
                                                  edit: { post in composing = CommentDraft(editing: post) })
                                    .id(thread.id)
                            }
                        }
                        .padding(.top, Design.Space.s8)
                    } label: {
                        Text("Resolved (\(resolved.count))").font(.subheadline).foregroundStyle(Color.textSecondary)
                    }
                    .tint(Color.textSecondary)
                    .frame(minHeight: 44)
                }
            }
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
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            Text(overwritten.lines == 1
                 ? "1 line you saved at \(Stamp.short(overwritten.ts)) was dropped by a later write."
                 : "\(overwritten.lines) lines you saved at \(Stamp.short(overwritten.ts)) were dropped by a later write.")
                .foregroundStyle(Color.textPrimary)
            Button("Restore yours") {
                Task { _ = await docs.save(path: path, text: overwritten.text, sha: sha) }
            }
            .compactPrimary()
            DisclosureGroup(isExpanded: $open) {
                ScrollView(.horizontal) {
                    Text(overwritten.diff)
                        .font(Design.Text.code)
                        .foregroundStyle(Color.textPrimary)
                        .textSelection(.enabled)
                        .padding(Design.Space.s12)
                }
                .sidewaysFade()
                .elevatedBlock()
                .clipShape(.rect(cornerRadius: Design.Radius.card, style: .continuous))
                .padding(.top, Design.Space.s8)
            } label: {
                Text("What changed").font(.footnote).foregroundStyle(Color.textSecondary)
            }
            .tint(Color.textSecondary)
            .frame(minHeight: 44)
        }
        .padding(.leading, Design.Space.s12)
        .overlay(alignment: .leading) { AccentBar(color: .warning) }
        .accessibilityElement(children: .contain)
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
                .font(Design.Text.code)
                .lineSpacing(Design.Text.lineSpacing)
                .foregroundStyle(Color.textPrimary)
                .scrollContentBackground(.hidden)
                .frame(maxWidth: Design.measure)
                .padding(.horizontal, Design.Space.s12)
                .frame(maxWidth: .infinity)
                .background(Color.canvas)
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
                .safeAreaBar(edge: .bottom) {
                    if let problem { DocumentStatus(text: problem, color: Color.danger) }
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
    /// What the sheet needs: its text, the bar over it and the sheet's own insets.
    @State private var fullHeight: CGFloat?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Design.Space.s16) {
                    Text(conflict.message).foregroundStyle(Color.textPrimary)
                    if let diff = conflict.diff, !diff.isEmpty {
                        VStack(alignment: .leading, spacing: Design.Space.s8) {
                            Text("What it has now that your text has not:")
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(Color.textSecondary)
                            ScrollView(.horizontal) {
                                CodeColours().highlightCode(diff, language: "diff")
                                    .font(Design.Text.code)
                                    .foregroundStyle(Color.textPrimary)
                                    .textSelection(.enabled)
                                    .padding(Design.Space.s12)
                            }
                            .sidewaysFade()
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .elevatedBlock()
                            .clipShape(.rect(cornerRadius: Design.Radius.card, style: .continuous))
                        }
                    }
                }
                .readingColumn()
                .padding(.vertical, Design.Space.s16)
            }
            .background(Color.canvas.ignoresSafeArea())
            .onScrollGeometryChange(for: CGFloat.self) { geometry in
                geometry.contentSize.height + geometry.contentInsets.top + geometry.contentInsets.bottom
            } action: { _, height in
                fullHeight = height
            }
            .navigationTitle("It changed meanwhile")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .safeAreaBar(edge: .bottom) {
                VStack(spacing: Design.Space.s8) {
                    Button {
                        dismiss()
                        replace()
                    } label: {
                        Text("Replace it with my text").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.glassProminent)
                    .tint(Color.brand)
                    .foregroundStyle(Color.onAccent)
                    Button(role: .destructive) {
                        dismiss()
                        takeTheirs()
                    } label: {
                        Text("Take its version (drop my edit)").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.glass)
                    .foregroundStyle(Color.danger)
                    Button {
                        dismiss()
                    } label: {
                        Text("Keep editing").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.glass)
                    .foregroundStyle(Color.brand)
                }
                .controlSize(.large)
                .frame(maxWidth: Design.measure)
                .padding(Design.Space.s16)
                .frame(maxWidth: .infinity)
            }
        }
        .sheetFrame()
        .presentationDetents(fullHeight.map { [.height($0)] } ?? [.medium])
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
                .background(Color.canvas)
                #if os(iOS)
                .navigationTitle("Select text")
                .navigationBarTitleDisplayMode(.inline)
                #else
                .sheetTitle("Select text")
                #endif
                #if os(iOS)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                    if !screen.archived {
                        ToolbarItemGroup(placement: .primaryAction) {
                            commentButton
                            replyButton
                        }
                    }
                }
                #else
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    SheetFooter {
                        Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
                        if !screen.archived {
                            commentButton
                            replyButton.compactPrimary().keyboardShortcut(.defaultAction)
                        }
                    }
                }
                #endif
        }
        .sheetFrame()
    }

    private var commentButton: some View {
        Button { commentOn(selection) } label: { Image(systemName: "text.bubble") }
            .disabled(selection.length == 0)
            .accessibilityLabel("Comment on the selection")
    }

    private var replyButton: some View {
        Button("\u{21A9}\u{FE0E} Reply") { reply((text as NSString).substring(with: selection)) }
            .disabled(selection.length == 0)
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

/// The threads a tap on a commented passage opens.
struct TappedThreads: Identifiable {
    let ids: [String]
    var id: String { ids.joined(separator: ",") }
}

/// What a write answered, or how it failed, floating over the bottom of the page.
struct DocumentStatus: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(color)
            .multilineTextAlignment(.center)
            .padding(.horizontal, Design.Space.s12)
            .padding(.vertical, Design.Space.s8)
            .glassEffect(.regular, in: .rect(cornerRadius: Design.Radius.chip, style: .continuous))
            .padding(.horizontal, Design.Space.s16)
            .padding(.bottom, Design.Space.s8)
            .frame(maxWidth: .infinity)
            .accessibilityAddTraits(.updatesFrequently)
    }
}

extension View {
    /// The document's column (spec §5): at most `Design.measure` wide and centred, with the screen's gutter, or the
    /// phone's in the narrow inspector, where a wider one would take a sixth of the text.
    func readingColumn() -> some View {
        modifier(ReadingColumn())
    }

    /// A thread's or a notice's one primary action: a small accent button with text that keeps its contrast.
    func compactPrimary() -> some View {
        buttonStyle(CompactAccentButton())
    }
}

private struct ReadingColumn: ViewModifier {
    @State private var width: CGFloat = 0

    func body(content: Content) -> some View {
        content
            .frame(maxWidth: Design.measure, alignment: .leading)
            .padding(.horizontal, width > 0 && width < 400 ? Design.Space.s16 : Design.Space.gutter)
            .frame(maxWidth: .infinity)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
    }
}
