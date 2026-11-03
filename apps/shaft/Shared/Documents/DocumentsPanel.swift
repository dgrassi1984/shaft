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
                    switch route {
                    case .folder(let folder): DocsListView(folder: folder, newDocument: { creating = true })
                    case .doc(let doc): DocumentDetailView(path: doc)
                    }
                }
        }
        .environment(docs)
        .task { await docs.refresh() }
        .onAppear(perform: openRequested)
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
                Section {
                    Text("Current files — not as they were in this chat").font(.footnote).foregroundStyle(.secondary)
                }
            }
            if let error = docs.error, docs.list == nil {
                Text(error.message).foregroundStyle(.red)
            }
            ForEach(level.folders) { entry in
                NavigationLink(value: DocumentsPanel.Route.folder(entry.path)) {
                    DocRow(name: entry.name, folder: true, bytes: entry.bytes, date: entry.latest, inChat: entry.inChat,
                           comments: entry.openComments, count: entry.count, tooBig: false)
                }
            }
            ForEach(level.docs) { entry in
                NavigationLink(value: DocumentsPanel.Route.doc(entry.path)) {
                    DocRow(name: (entry.path as NSString).lastPathComponent, folder: false, bytes: entry.bytes,
                           date: entry.mtime, inChat: entry.inChat, comments: entry.openComments, count: nil,
                           tooBig: entry.tooBig)
                }
            }
            if docs.list != nil && level.folders.isEmpty && level.docs.isEmpty {
                Text(folder.isEmpty ? "No documents yet." : "This folder is empty.").foregroundStyle(.secondary)
            }
        }
        .overlay { if docs.list == nil && docs.error == nil { ProgressView() } }
        .refreshable { await docs.refresh() }
        .navigationTitle(folder.isEmpty ? "Documents" : (folder as NSString).lastPathComponent)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar {
            if folder.isEmpty {
                ToolbarItem(placement: .cancellationAction) {
                    Button { screen.showDocuments = false } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Close the documents")
                }
            }
            ToolbarItem(placement: .primaryAction) {
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
                    Image(systemName: "ellipsis.circle").touchTarget()
                }
                .accessibilityLabel("Sort and new document")
            }
        }
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

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: folder ? "folder" : "doc.text").foregroundStyle(folder ? Color.accentColor : .secondary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(name).lineLimit(1)
                    if inChat {
                        Circle().fill(Color.accentColor).frame(width: 7, height: 7)
                            .accessibilityLabel("changed in this chat")
                    }
                }
                HStack(spacing: 6) {
                    if let count { Text("\(count) documents") }
                    Text(DocsTree.size(bytes))
                    if let date { Text(Stamp.short(date)) }
                    if tooBig { Text("too big to show") }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            if comments > 0 {
                Label("\(comments)", systemImage: "text.bubble").font(.caption).foregroundStyle(.secondary)
                    .accessibilityLabel("\(comments) open comments")
            }
        }
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
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

    var body: some View {
        NavigationStack {
            Form {
                TextField(folder.isEmpty ? "Document name" : "Document name in \(folder)/", text: $name)
                    .autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    #endif
                if let problem { Text(problem).foregroundStyle(.red) }
            }
            .navigationTitle("New document")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Create") { create() }.disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || busy)
                }
            }
        }
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
            }
            busy = false
        }
    }
}
