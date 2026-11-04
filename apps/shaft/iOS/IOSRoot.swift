import SHAFTKit
import SwiftUI

/// The iPhone and iPad layouts over the shared views. A phone shows the chat full screen with a chat switcher. An iPad
/// shows the instances in a sidebar beside the chat, at every window size: one layout, so a resize or a turn keeps the
/// chat and the open document where they are (spec §5). The chat last open comes back at launch, and a notification tap
/// opens its chat.
struct IOSRoot: View {
    let model: AppModel

    var body: some View {
        SessionRoot(model: model) { session, store in
            ChatsLayout(session: session, store: store)
        }
    }
}

struct ChatsLayout: View {
    let session: SHAFTSession
    let store: InstancesStore
    @Environment(AppModel.self) private var model
    @AppStorage("chat.last") private var last = ""
    @State private var selection: String?
    @State private var path: [ArchivedRoute] = []
    @State private var switching = false
    @State private var settings = false
    @State private var columns = NavigationSplitViewVisibility.all
    @State private var compactColumn = NavigationSplitViewColumn.detail
    /// The documents, not the operator, put the sidebar away: closing them brings it back.
    @State private var sidebarCollapsed = false
    /// The inspector on screen: a moment after `showDocuments` where the sidebar has to go first.
    @State private var inspector = false
    @State private var sidebarLeaving = false
    @State private var resized = Date.distantPast
    @State private var shell = ChatShell(sheetsDocuments: UIDevice.current.userInterfaceIdiom == .phone)

    var body: some View {
        Group {
            if shell.sheetsDocuments {
                phone
            } else {
                pad
            }
        }
        .environment(shell)
        .environment(\.chatCovered, switching || settings)
        .sheet(isPresented: $settings) { SettingsView(session: session) }
        .onAppear(perform: restore)
        .onChange(of: store.instances.map(\.name)) { restore() }
        .onChange(of: model.openChat) { _, name in
            guard let name else { return }
            openNotification(name)
            model.openChat = nil
        }
    }

    private var phone: some View {
        NavigationStack(path: $path) {
            chat
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) { chatsButton }
                }
                .navigationDestination(for: ArchivedRoute.self) { route in
                    ArchivedChatView(route: route) { path.removeAll() }
                }
        }
        .sheet(isPresented: $switching) {
            NavigationStack {
                InstancesList(switcher: true, selection: Binding(get: { selection }, set: { name in
                    if let name { open(name) }
                    switching = false
                }), settings: {
                    switching = false
                    settings = true
                })
                .listStyle(.plain)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Close") { switching = false } }
                }
            }
            .environment(store)
            .presentationDetents([.medium, .large])
            .presentationBackground(Color.elevated)
        }
    }

    private var chatsButton: some View {
        Button { switching = true } label: {
            Image(systemName: "list.bullet")
                .overlay(alignment: .topTrailing) {
                    if store.badge > 0 {
                        Circle()
                            .fill(Color.brand)
                            .frame(width: Design.Space.s8, height: Design.Space.s8)
                            .offset(x: Design.Space.s4, y: -Design.Space.s4)
                            .accessibilityHidden(true)
                    }
                }
        }
        .accessibilityLabel("Chats")
        .accessibilityValue(store.badge > 0 ? "\(store.badge) unread" : "")
    }

    private var pad: some View {
        NavigationSplitView(columnVisibility: $columns, preferredCompactColumn: $compactColumn) {
            InstancesList(selection: Binding(get: { selection }, set: { name in if let name { open(name) } }),
                          settings: { settings = true })
                .navigationSplitViewColumnWidth(min: 260, ideal: 300, max: 380)
                .toolbar(removing: documentsTakeSidebar ? .sidebarToggle : nil)
        } detail: {
            NavigationStack(path: $path) {
                chat
                    .navigationDestination(for: ArchivedRoute.self) { route in
                        // The stack's environment does not reach its pushed views (found on the Mac): an
                        // earlier chat's screen needs the shell for its own documents.
                        ArchivedChatView(route: route) { path.removeAll() }
                            .environment(shell)
                    }
            }
            .environment(shell)
            .background(SidebarTiles())
        }
        .navigationSplitViewStyle(.balanced)
        // The documents beside the chat, outside its navigation stack (ChatShell): inside it, their own pages
        // were pushed onto the chat's stack.
        .documentsInspector(shell, store: store,
                            isPresented: Binding(get: { inspector }, set: inspectorChanged))
        // The system turns an inspector into a sheet at compact width, keeping what it shows. It would stay beside the
        // chat down to about 680 pt: under 700 there is no room for both (spec §5), so that band reads as compact.
        .environment(\.horizontalSizeClass, shell.documentsCover ? .compact : .regular)
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width in
            shell.width = width
            resized = .now
            fitColumns()
        }
        .onChange(of: shell.screen?.showDocuments) { fitColumns() }
        .onChange(of: columns) { fitColumns() }
    }

    /// The system's own changes to the inspector. In a resize that turns it between column and sheet, it closes it and
    /// opens it again with what it held: that close is not the operator's, and acted on it would lose the open document.
    /// It also opens it unasked, restoring a window whose documents were open or reopening them after a turn of the
    /// device: left alone that is an empty column, so the documents follow.
    private func inspectorChanged(_ open: Bool) {
        if !open {
            if Date.now.timeIntervalSince(resized) > 0.5 { shell.screen?.showDocuments = false }
        } else if shell.screen?.showDocuments != true {
            Task {
                try? await Task.sleep(for: .seconds(0.3))        // a restored window's chat is not on screen yet
                shell.screen?.showDocuments = true
            }
        }
    }

    /// Under 1000 pt, sidebar, chat and documents do not fit together.
    private var documentsTakeSidebar: Bool {
        shell.screen?.showDocuments == true && shell.collapsesSidebar
    }

    /// Open documents put the sidebar away where all three do not fit; it comes back when they close or the window has
    /// room again. Asked again when the split view changes its columns (leaving compact width shows the sidebar).
    /// A new inspector waits for that collapse to end: presented during it, the split view drops it unseen.
    private func fitColumns() {
        if documentsTakeSidebar {
            if columns != .detailOnly {
                sidebarCollapsed = true
                columns = .detailOnly
                sidebarLeaving = true
                Task {
                    try? await Task.sleep(for: .seconds(0.4))
                    sidebarLeaving = false
                    fitColumns()
                }
            }
        } else if sidebarCollapsed {
            sidebarCollapsed = false
            columns = .all
        }
        let open = shell.screen?.showDocuments == true
        if !open || !sidebarLeaving { inspector = open }
    }

    @ViewBuilder
    private var chat: some View {
        if !store.loaded, let error = store.error {
            InstancesLoadFailure(error: error, settings: { settings = true })
        } else if let selection {
            ChatScreen(chat: store.chat(selection), openArchived: { summary in
                path.append(ArchivedRoute(instance: selection, cid: summary.id))
            })
            .id(selection)
        } else if store.loaded {
            MarkMessage("No chat open", message: Text("Choose an instance to chat with."))
        } else {
            ProgressView()
        }
    }

    private func openNotification(_ name: String) {
        shell.screen?.dismissPresentations()
        switching = false
        settings = false
        path.removeAll()
        open(name)
    }

    private func open(_ name: String) {
        if selection != name { path.removeAll() }
        selection = name
        last = name
        compactColumn = .detail
    }

    /// The chat open last time, else the first instance; a tap's chat wins.
    private func restore() {
        if let name = model.openChat {
            openNotification(name)
            model.openChat = nil
            return
        }
        guard selection == nil, !store.instances.isEmpty else { return }
        let names = store.instances.map(\.name)
        open(names.contains(last) ? last : names[0])
    }
}

/// The sidebar beside the chat at every width that holds both. `.balanced` alone left it to the system at launch, which
/// by window shape often laid the sidebar over the chat, dimmed. Asked at each layout: earlier, the split view resets it.
private struct SidebarTiles: UIViewRepresentable {
    func makeUIView(context: Context) -> Finder { Finder() }
    func updateUIView(_ view: Finder, context: Context) {}

    final class Finder: UIView {
        override func layoutSubviews() {
            super.layoutSubviews()
            var next: UIResponder? = self
            while let responder = next {
                if let split = responder as? UISplitViewController {
                    if split.preferredSplitBehavior != .tile { split.preferredSplitBehavior = .tile }
                    return
                }
                next = responder.next
            }
        }
    }
}
