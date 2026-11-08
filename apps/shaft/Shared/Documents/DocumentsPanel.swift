import SHAFTKit
import SwiftUI

/// The instance's documents beside the chat (an inspector on a wide screen, a sheet over the chat on a phone): the
/// list by folder, a document rendered with its comment threads, edit with a sha-checked save, delete, comments, and
/// ↩ Reply from a selection into the composer (web-and-api.md, "Documents", "Comments on a document").
struct DocumentsPanel: View {
    enum Route: Hashable {
        case folder(String)
        case doc(String)
    }

    @Environment(ChatScreenModel.self) private var screen
    /// The panel's own file preview: as a sheet over the chat, the chat's could not present over it.
    @State private var preview = FilePreview()
    @State private var docs: DocumentsStore
    @State private var path: [Route] = []
    @State private var creating = false

    init(store: InstancesStore, instance: String, cid: String?) {
        _docs = State(initialValue: DocumentsStore(client: store.client, instance: instance, cid: cid))
    }

    var body: some View {
        NavigationStack(path: $path) {
            DocsListView(folder: "", newDocument: { creating = true })
                .navigationDestination(for: Route.self) { route in
                    // Handed on explicitly: a pushed page in the iPad's inspector did not inherit them (a crash).
                    Group {
                        switch route {
                        case .folder(let folder): DocsListView(folder: folder, newDocument: { creating = true })
                        case .doc(let doc): DocumentDetailView(path: doc)
                        }
                    }
                    .environment(docs)
                    .environment(screen)
                    .environment(preview)
                }
        }
        .environment(docs)
        .environment(preview)
        .filePreview(preview)
        .task {
            await docs.refresh()
            // Live while it shows: a document the instance writes, a comment it answers.
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                await docs.follow()
            }
        }
        .onAppear {
            if screen.openDocument == nil, path.isEmpty, let doc = screen.shownDocument {
                open(doc)
            } else {
                openRequested()
            }
        }
        .onChange(of: path) {
            screen.shownDocument = path.reversed().compactMap { route -> String? in
                if case .doc(let doc) = route { doc } else { nil }
            }.first
        }
        .onChange(of: screen.openDocument) { openRequested() }
        .sheet(isPresented: $creating) {
            NewDocumentSheet(folder: currentFolder) { created in path.append(.doc(created)) }
                .environment(docs)
        }
    }

    private var currentFolder: String {
        for route in path.reversed() {
            if case .folder(let folder) = route { return folder }
        }
        return ""
    }

    private func openRequested() {
        guard let doc = screen.openDocument else { return }
        screen.openDocument = nil
        open(doc)
    }

    private func open(_ doc: String) {
        let folder = (doc as NSString).deletingLastPathComponent
        var route: [Route] = []
        if !folder.isEmpty {
            var built = ""
            for part in folder.split(separator: "/") {
                built = built.isEmpty ? String(part) : built + "/" + part
                route.append(.folder(built))
            }
        }
        route.append(.doc(doc))
        path = route
    }
}

/// One folder level: its subfolders (with what they hold) first, then its documents, in the chosen order.
struct DocsListView: View {
    let folder: String
    var newDocument: () -> Void
    @Environment(DocumentsStore.self) private var docs
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.dismiss) private var dismiss
    @AppStorage("docs.sort") private var sort = DocsTree.Sort.modified.rawValue
    @AppStorage("docs.ascending") private var ascending = false

    var body: some View {
        let order = DocsTree.Sort(rawValue: sort) ?? .modified
        let level = DocsTree.level(docs.list?.docs ?? [], in: folder, sort: order, ascending: ascending)
        List {
            if screen.archived {
                Text("Current files — not as they were in this chat")
                    .font(.footnote)
                    .foregroundStyle(Color.textSecondary)
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)
            }
            if let error = docs.error, docs.list == nil {
                Text(error.message)
                    .foregroundStyle(Color.danger)
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)
            }
            #if os(macOS)
            rows(level)     // a section there has a header's room and a line above its first row, empty or not
            #else
            Section { rows(level) }
                .listSectionSeparator(.hidden)
            #endif
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Color.elevated)
        .overlay {
            if docs.list == nil && docs.error == nil {
                ProgressView()
            } else if docs.list != nil && level.folders.isEmpty && level.docs.isEmpty {
                ContentUnavailableView {
                    Label(folder.isEmpty ? "No documents yet." : "This folder is empty.", systemImage: "doc.text")
                } actions: {
                    if !screen.archived {
                        Button("New document", action: newDocument).compactPrimary()
                    }
                }
            }
        }
        .refreshable { await docs.refresh() }
        .navigationTitle(folder.isEmpty ? "Documents" : (folder as NSString).lastPathComponent)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar {
            #if os(macOS)
            if folder.isEmpty {
                InspectorHeader(title: String(localized: "Documents"), symbol: "xmark", label: "Close the documents",
                                action: { screen.showDocuments = false }) { sortMenu(order) }
            } else {
                InspectorHeader(title: (folder as NSString).lastPathComponent, symbol: "chevron.left", label: "Back",
                                action: { dismiss() }) { sortMenu(order) }
            }
            #else
            if folder.isEmpty {
                ToolbarItem(placement: .cancellationAction) {
                    Button { screen.showDocuments = false } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Close the documents")
                }
            }
            ToolbarItem(placement: .primaryAction) { sortMenu(order) }
            #endif
        }
        #if os(macOS)
        .inspectorPage()
        #endif
    }

    @ViewBuilder
    private func rows(_ level: DocsTree.Level) -> some View {
        ForEach(level.folders) { entry in
            NavigationLink(value: DocumentsPanel.Route.folder(entry.path)) {
                DocRow(name: entry.name, folder: true, bytes: entry.bytes, date: entry.latest,
                       inChat: entry.inChat, comments: entry.openComments, count: entry.count, tooBig: false)
            }
            .docRow()
        }
        ForEach(level.docs) { entry in
            NavigationLink(value: DocumentsPanel.Route.doc(entry.path)) {
                DocRow(name: (entry.path as NSString).lastPathComponent, folder: false, bytes: entry.bytes,
                       date: entry.mtime, inChat: entry.inChat, comments: entry.openComments, count: nil,
                       tooBig: entry.tooBig)
            }
            .docRow()
        }
    }

    private func sortMenu(_ order: DocsTree.Sort) -> some View {
        Menu {
            Picker("Sort by", selection: Binding(get: { order }, set: { new in
                if new == order { ascending.toggle() } else {
                    sort = new.rawValue
                    ascending = new.firstAscending
                }
            })) {
                Text("Name").tag(DocsTree.Sort.name)
                Text("Size").tag(DocsTree.Sort.size)
                Text("Modified").tag(DocsTree.Sort.modified)
            }
            Button(ascending ? "Reverse (now ascending)" : "Reverse (now descending)") { ascending.toggle() }
            if !screen.archived {
                Divider()
                Button { newDocument() } label: { Label("New document", systemImage: "doc.badge.plus") }
            }
        } label: {
            Image(systemName: "arrow.up.arrow.down")
        }
        .accessibilityLabel("Sort and new document")
    }
}

extension View {
    /// A row of the panel: on its surface, with the system's chevron, split by a hairline.
    fileprivate func docRow() -> some View {
        listRowBackground(Color.clear)
            .listRowSeparatorTint(Color.hairline)
    }
}

struct DocRow: View {
    let name: String
    let folder: Bool
    let bytes: Int
    let date: Date?
    let inChat: Bool
    let comments: Int
    let count: Int?
    let tooBig: Bool
    @ScaledMetric(relativeTo: .body) private var iconWidth = Design.Space.s24
    @Environment(\.dynamicTypeSize) private var typeSize

    private var details: String {
        var parts: [String] = []
        if let count { parts.append(count == 1 ? String(localized: "1 document") : String(localized: "\(count) documents")) }
        parts.append(DocsTree.size(bytes))
        if let date { parts.append(Stamp.short(date)) }
        if tooBig { parts.append(String(localized: "too big to show")) }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        let large = typeSize.isAccessibilitySize
        HStack(alignment: large ? .firstTextBaseline : .center, spacing: Design.Space.s12) {
            Image(systemName: folder ? "folder" : "doc.text")
                .foregroundStyle(Color.brand)
                .frame(width: iconWidth)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Design.Space.s4) {
                HStack(spacing: Design.Space.s4) {
                    Text(name)
                        .foregroundStyle(Color.textPrimary)
                        .lineLimit(large ? nil : 1)
                        .truncationMode(.middle)
                    if inChat {
                        Circle().fill(Color.brand).frame(width: 7, height: 7)
                            .accessibilityLabel("changed in this chat")
                    }
                }
                Text(details)
                    .font(Design.Text.metadata)
                    .foregroundStyle(Color.textSecondary)
                if large { commentCount }
            }
            Spacer(minLength: 0)
            if !large { commentCount }
        }
        .frame(minHeight: 44)
        // The hairline starts under the name, whatever else the row holds.
        .alignmentGuide(.listRowSeparatorLeading) { $0[.leading] + iconWidth + Design.Space.s12 }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var commentCount: some View {
        if comments > 0 {
            HStack(spacing: Design.Space.s4) {
                Image(systemName: "text.bubble")
                Text("\(comments)")
            }
            .font(Design.Text.pill)
            .foregroundStyle(Color.brand)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(comments) open comments")
        }
    }
}

/// A new document in the folder shown: its name (`q4/plan` makes the subfolder too); it opens once made.
struct NewDocumentSheet: View {
    let folder: String
    var created: (String) -> Void
    @Environment(DocumentsStore.self) private var docs
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var problem: String?
    @State private var busy = false
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Design.Space.s12) {
                TextField(folder.isEmpty ? "Document name" : "Document name in \(folder)/", text: $name)
                    .autocorrectionDisabled()
                    .textFieldStyle(.plain)
                    .foregroundStyle(Color.textPrimary)
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    #endif
                    .focused($focused)
                    .padding(Design.Space.s12)
                    .elevatedBlock()
                if let problem { Text(problem).font(.footnote).foregroundStyle(Color.danger) }
                Spacer(minLength: 0)
            }
            .padding(Design.Space.gutter)
            .navigationTitle("New document")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Create") { create() }.disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || busy)
                }
            }
            .onAppear { focused = true }
        }
        .sheetFrame(minHeight: 220)
        .presentationDetents([.medium])
    }

    private func create() {
        busy = true
        Task {
            let title = (name as NSString).lastPathComponent
            switch await docs.save(path: name, text: "# \(title)\n", sha: "none", dir: folder.isEmpty ? nil : folder) {
            case .saved(let answer):
                dismiss()
                created(answer.path)
            case .stale(let conflict):
                problem = conflict.message
            case .refused(let error):
                problem = error.message
            case .cancelled:
                break
            }
            busy = false
        }
    }
}
