import SHAFTKit
import SwiftUI

/// The iPhone and iPad layouts over the shared views. A phone shows the chat full screen with a chat switcher. An iPad
/// shows the instances in a sidebar beside the chat, at every window size: one layout, so a resize or a turn keeps the
/// chat and the open document where they are (spec §5). The chat last open comes back at launch, and a notification tap
/// opens its chat, its thread when it names one. An instance's threads are listed under it, in the switcher and the
/// sidebar alike.
struct IOSRoot: View {
    let model: AppModel
    let recorder: IOSMeetingRecorder
    @State private var meetings = false

    var body: some View {
        SessionRoot(model: model) { session, store in
            ChatsLayout(session: session, store: store, meetings: $meetings)
                .task { await VoiceReporter.atLaunch(client: store.client) }
        }
        .safeAreaInset(edge: .top, spacing: 0) {
            if recorder.started != nil, !meetings {
                Button {
                    meetings = true
                } label: {
                    Label(recorder.interrupted ? "Recording paused" : "Recording meeting", systemImage: "mic.fill")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, Design.Space.s8)
                }
                .foregroundStyle(Color.danger)
                .background(.regularMaterial)
                .accessibilityHint("Opens Meetings")
            }
        }
        .fullScreenCover(isPresented: $meetings) {
            IOSMeetingsScreen(client: model.session?.store?.client, recorder: recorder)
        }
    }
}

struct ChatsLayout: View {
    let session: SHAFTSession
    let store: InstancesStore
    @Binding var meetings: Bool
    @Environment(AppModel.self) private var model
    @AppStorage("chat.last") private var last = ""
    @State private var selection: ChatSelection?
    @State private var path: [ArchivedRoute] = []
    @State private var switching = false
    @State private var settings = false
    @State private var meetingsAfterSwitcher = false
    @State private var columns = NavigationSplitViewVisibility.all
    @State private var compactColumn = NavigationSplitViewColumn.detail
    /// The documents, not the operator, put the sidebar away: closing them brings it back.
    @State private var sidebarCollapsed = false
    /// The inspector on screen: a moment after `showDocuments` where the sidebar has to go first.
    @State private var inspector = false
    @State private var inspectorRestore: Task<Void, Never>?
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
        .environment(\.chatCovered, switching || settings || meetings)
        .sheet(isPresented: $settings) { SettingsView(session: session) }
        .onAppear(perform: restore)
        .onDisappear { inspectorRestore?.cancel() }
        .onChange(of: shell.screen.map(ObjectIdentifier.init)) {
            inspectorRestore?.cancel()
            fitColumns()
        }
        .onChange(of: shell.screen?.openDocument) { inspectorRestore?.cancel() }
        .onChange(of: shell.screen?.showDocuments) { inspectorRestore?.cancel() }
        .onChange(of: store.instances.map(\.name)) { restore() }
        .onChange(of: model.openChat) { _, target in
            guard let target else { return }
            openNotification(target)
            model.openChat = nil
        }
        // Threads read: the default named by its thread, an ended thread given way.
        .onChange(of: store.threadStates) {
            if let settled = store.settle(selection) { selection = settled }
        }
    }

    private var phone: some View {
        NavigationStack(path: $path) {
            chat
                .navigationDestination(for: ArchivedRoute.self) { route in
                    ArchivedChatView(route: route) { path.removeAll() }
                }
        }
        .environment(\.openConversationSwitcher, { switching = true })
        .sheet(isPresented: $switching, onDismiss: {
            if meetingsAfterSwitcher {
                meetingsAfterSwitcher = false
                meetings = true
            }
        }) {
            NavigationStack {
                InstancesList(switcher: true, selection: Binding(get: { selection }, set: { chosen in
                    if let chosen { open(chosen); switching = false }
                }), settings: {
                    switching = false
                    settings = true
                }, meetings: session.isDemo ? nil : {
                    meetingsAfterSwitcher = true
                    switching = false
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

    private var pad: some View {
        NavigationSplitView(columnVisibility: $columns, preferredCompactColumn: $compactColumn) {
            InstancesList(selection: Binding(get: { selection }, set: { chosen in if let chosen { open(chosen) } }),
                          settings: { settings = true },
                          meetings: session.isDemo ? nil : { meetings = true })
                .navigationSplitViewColumnWidth(min: 260, ideal: 300, max: 380)
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
            .background(SidebarTiles(overlays: documentsTakeSidebar))
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
        .onChange(of: columns) { columnsChanged() }
    }

    /// The sidebar's toggle stays in the header while the documents have its room: there the operator's tap lays the
    /// sidebar over the chat (`SidebarTiles`), for a moment, and that is left alone. A change the resize made is asked
    /// again: leaving compact width shows the sidebar.
    private func columnsChanged() {
        if documentsTakeSidebar && inspector && !sidebarLeaving && columns != .detailOnly
            && Date.now.timeIntervalSince(resized) > 0.5 { return }
        fitColumns()
    }

    /// The system's own changes to the inspector. In a resize that turns it between column and sheet, it closes it and
    /// opens it again with what it held: that close is not the operator's, and acted on it would lose the open document.
    /// It also opens it unasked, restoring a window whose documents were open or reopening them after a turn of the
    /// device: left alone that is an empty column, so the documents follow.
    private func inspectorChanged(_ open: Bool) {
        inspectorRestore?.cancel()
        if !open {
            if Date.now.timeIntervalSince(resized) > 0.5 { shell.screen?.showDocuments = false }
        } else if let screen = shell.screen {
            inspectorRestore = Task { @MainActor in
                do { try await Task.sleep(for: .seconds(0.3)) }
                catch { return }
                guard !Task.isCancelled, shell.screen === screen else { return }
                // A restored screen may already have documents open. Reconcile its columns just as a manual open does.
                screen.showDocuments = true
                fitColumns()
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
            let chat = store.chat(selection)
            ChatScreen(chat: chat, openArchived: { cid in
                path.append(ArchivedRoute(instance: selection.instance, cid: cid))
            }, openThread: { ref in open(ChatSelection(selection.instance, ref)) })
            .id(ChatSelection(selection.instance, chat.ref))
        } else if store.loaded {
            MarkMessage("No chat open", message: Text("Choose an instance to chat with."))
        } else {
            ProgressView()
        }
    }

    private func openNotification(_ target: ChatTarget) {
        shell.screen?.dismissPresentations()
        switching = false
        settings = false
        meetingsAfterSwitcher = false
        meetings = false
        path.removeAll()
        open(ChatSelection(target.instance, target.ref))
    }

    private func open(_ chosen: ChatSelection) {
        inspectorRestore?.cancel()
        if selection != chosen { path.removeAll() }
        SidebarDisclosure.set(chosen.instance, expanded: true)
        selection = ChatSelection.settled(chosen.instance, store.resolve(chosen))
        last = chosen.instance
        compactColumn = .detail
        if documentsTakeSidebar { columns = .detailOnly }       // the sidebar laid over the chat goes with the choice
    }

    /// The chat open last time, else the first instance; a tap's chat wins.
    private func restore() {
        if let target = model.openChat {
            openNotification(target)
            model.openChat = nil
            return
        }
        guard selection == nil, !store.instances.isEmpty else { return }
        let names = store.instances.map(\.name)
        open(ChatSelection(names.contains(last) ? last : names[0]))
    }
}

/// The sidebar beside the chat at every width that holds both. `.balanced` alone left it to the system at launch, which
/// by window shape often laid the sidebar over the chat, dimmed. With `overlays` (the documents have its room) it is laid
/// over the chat when asked for. Asked at each layout: earlier, the split view resets it.
private struct SidebarTiles: UIViewRepresentable {
    let overlays: Bool

    func makeUIView(context: Context) -> Finder { Finder() }

    func updateUIView(_ view: Finder, context: Context) {
        view.behavior = overlays ? .overlay : .tile
        view.apply()
    }

    final class Finder: UIView {
        var behavior = UISplitViewController.SplitBehavior.tile

        override func layoutSubviews() {
            super.layoutSubviews()
            apply()
        }

        func apply() {
            var next: UIResponder? = self
            while let responder = next {
                if let split = responder as? UISplitViewController {
                    if split.preferredSplitBehavior != behavior { split.preferredSplitBehavior = behavior }
                    return
                }
                next = responder.next
            }
        }
    }
}
