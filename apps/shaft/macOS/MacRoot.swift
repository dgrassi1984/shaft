import AppKit
import SHAFTKit
import SwiftUI

/// The Mac window's state, which the menu commands share with it: the chat shown (and the screen drawing it, through
/// the shell), the store, and how to bring the window back once closed.
@MainActor
@Observable
final class MacState {
    /// The instance whose chat is shown, and which of its conversations (nil: its default, `InstancesStore.resolve`).
    var selection: ChatSelection?
    /// Earlier chats pushed over the current one.
    var path: [ArchivedRoute] = []
    /// The chat screen on display (current or earlier) and the documents beside it.
    let shell = ChatShell()
    /// The paired store, while the window shows it.
    var store: InstancesStore?
    /// What the menu commands may do now.
    let menu = MacMenu()
    @ObservationIgnored var openWindow: OpenWindowAction?

    /// The screen of the chat on display.
    var screen: ChatScreenModel? { shell.screen }

    /// The current chat's screen, not an earlier chat's: what sending, Stop and End act on.
    var currentScreen: ChatScreenModel? {
        guard let screen = shell.screen, !screen.archived else { return nil }
        return screen
    }

    func open(_ name: String) {
        open(ChatSelection(name))
    }

    /// Show `chosen`: an instance's default conversation, or one thread of it.
    func open(_ chosen: ChatSelection) {
        if selection != chosen { path.removeAll() }
        SidebarDisclosure.set(chosen.instance, expanded: true)
        selection = store.map { ChatSelection.settled(chosen.instance, $0.resolve(chosen)) } ?? chosen
        UserDefaults.standard.set(chosen.instance, forKey: "chat.last")
    }

    /// Walk the same visible order the sidebar draws.
    func step(_ by: Int) {
        guard let store else { return }
        var rows: [ChatSelection] = []
        for row in store.instances {
            rows.append(ChatSelection(row.name, store.defaultRef(row.name)))
            if SidebarDisclosure.expanded(row.name) {
                rows += (store.threads(row.name)?.threads ?? []).map { ChatSelection(row.name, .thread($0.id)) }
            }
        }
        guard !rows.isEmpty else { return }
        let index = selection.flatMap { chosen in
            rows.firstIndex(of: chosen) ?? rows.firstIndex { $0.instance == chosen.instance }
        } ?? (by > 0 ? -1 : 0)
        open(rows[(index + by + rows.count) % rows.count])
    }

    func stepInstance(_ by: Int) {
        guard let store, !store.instances.isEmpty else { return }
        let names = store.instances.map(\.name)
        let index = selection.flatMap { chosen in names.firstIndex(of: chosen.instance) } ?? (by > 0 ? -1 : 0)
        open(names[(index + by + names.count) % names.count])
    }

    func moveThread(_ offset: Int?) {
        guard let store, let chosen = selection, case .thread(let id) = chosen.ref,
              let ids = store.threads(chosen.instance)?.threads.map(\.id),
              let index = ids.firstIndex(of: id) else { return }
        let target = offset.map { index + $0 } ?? 0
        guard ids.indices.contains(target), index != target else { return }
        let remaining = ids.filter { $0 != id }
        let after = target == 0 ? nil : remaining[target - 1]
        Task { await store.moveThread(chosen.instance, id: id, after: after) }
    }

    /// New Thread (⇧⌘N on a server with threads): one more beside the instance's others, shown once started.
    func newThread() {
        guard let store, let name = selection?.instance else { return }
        Task {
            await store.newThread(name) { self.open(ChatSelection(name, $0)) }
        }
    }

    /// Focus the composer ("/", New message).
    func focusComposer() {
        guard let screen = currentScreen else { return }
        screen.focusComposerRequest += 1
    }

    /// Reload: the chat list and the shown instance's threads, and the chat's feed asked for again at once.
    func reload() {
        guard let store else { return }
        let name = selection?.instance
        Task {
            await store.refresh()
            if let name { await store.refreshThreads(name) }
        }
        if let chat = screen?.chat, chat.isPolling {
            chat.stop()
            chat.start()
        }
    }

    /// Bring the window to the front, opening it again when it was closed.
    func bringForward() {
        NSApp.activate()
        if let window = NSApp.windows.first(where: { $0.identifier?.rawValue.hasPrefix(MacRoot.windowID) == true }) {
            if window.isMiniaturized { window.deminiaturize(nil) }
            window.makeKeyAndOrderFront(nil)
        } else {
            openWindow?(id: MacRoot.windowID)
        }
    }
}

/// The Mac window: the instances in the sidebar, the chat, and the chat's documents as an inspector. Before a
/// session, the setup gate (MacSetup): the setup wizard when SHAFT is not installed on this Mac, the pairing with it
/// (no link) when it is, and the pasted-link pairing from the wizard's Welcome.
struct MacRoot: View {
    static let windowID = "main"
    /// The window's least: with the sidebar collapsed it holds the chat beside the documents at up to 360 pt
    /// (ChatShell.inspectorMaximum).
    static let minimumWidth: CGFloat = 400 + ChatShell.chatMinimum
    /// The chat column's minimum as the split views take it. They count an open inspector twice (the chat runs under it,
    /// and its width is added again on the way out): what the window must hold is sidebar's minimum + this + 2 ×
    /// inspector, and over the window's width all three columns ran off its edges. So this is the 380 the chat keeps
    /// (ChatShell.chatMinimum) less the inspector's own 320, which leaves the sum at what the columns really take. The
    /// chat keeps its 380 by the sidebar giving way and the inspector's maximum (ChatShell). It must stay a fixed number
    /// (crash of 2026-11-04, below).
    static let splitChatMinimum: CGFloat = ChatShell.chatMinimum - ChatShell.inspectorWidth
    let model: AppModel
    let state: MacState
    let setup: MacSetup
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Group {
            if let error = setup.configurationError {
                MarkMessage("SHAFT could not start", message: Text(error)) {
                    Button("Try again") {
                        setup.loadConfiguration()
                        setup.update(model.session)
                    }
                    .buttonStyle(ProminentAction())
                }
            } else if let session = model.session, setup.route == .wizard {
                SetupWizard(setup: setup, session: session)
            } else if let session = model.session, setup.route == .localPairing {
                LocalPairingView(setup: setup, session: session)
            } else {
                SessionRoot(model: model) { session, store in
                    MacChats(session: session, store: store, state: state)
                        .task { await VoiceReporter.atLaunch(client: store.client) }
                }
            }
        }
        .environment(model)
        .environment(\.serverUpdater, model.session.flatMap { setup.updater(for: $0) })
        .environment(\.phoneAccess, model.session.flatMap { setup.phoneAccess(for: $0) })
        // The gate decides again when the session opens or changes, and when a pairing comes or goes.
        .onChange(of: model.session.map(ObjectIdentifier.init), initial: true) { setup.update(model.session) }
        .onChange(of: model.session?.store.map(ObjectIdentifier.init)) { setup.update(model.session) }
        .frame(minWidth: Self.minimumWidth, minHeight: 480)
        // The reading surface behind everything, the title bar's area too.
        .containerBackground(Color.canvas, for: .window)
        .onAppear { state.openWindow = openWindow }
        // shaft://chat/<instance>[/<cid>]: the chat a notification click opens, from a link too.
        .onOpenURL { url in
            let target = AppLinks.target(in: url)
            model.notificationTapped(instance: target?.instance, cid: target?.cid)
        }
    }
}

struct MacChats: View {
    let session: SHAFTSession
    let store: InstancesStore
    let state: MacState
    @Environment(AppModel.self) private var model
    @Environment(\.openSettings) private var openSettings
    @State private var presence = MacPresence()
    @State private var columns = NavigationSplitViewVisibility.automatic
    /// The sidebar made room for the documents, and comes back when they go.
    @State private var sidebarYielded = false
    /// The sidebar's width as last shown: it can be dragged wider than its usual 260.
    @State private var sidebarWidth: CGFloat = 260

    var body: some View {
        @Bindable var state = state
        NavigationSplitView(columnVisibility: $columns) {
            InstancesList(selection: Binding(get: { state.selection }, set: { chosen in
                              if let chosen { state.open(chosen) }
                          }), settings: { openSettings() })
                .scrollContentBackground(.hidden)
                .background(Color.elevated.ignoresSafeArea())
                .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { sidebarWidth = $0 }
                .navigationSplitViewColumnWidth(min: ChatShell.sidebarMinimum, ideal: 260, max: 360)
        } detail: {
            NavigationStack(path: $state.path) {
                chat
                    .navigationDestination(for: ArchivedRoute.self) { route in
                        // The stack's environment does not reach its pushed views: an earlier chat's screen needs the
                        // shell too, or the documents and the menus stay on the current chat's.
                        ArchivedChatView(route: route) { state.path.removeAll() }
                            .environment(state.shell)
                            .toolbar(removing: .title)   // the header's own title is the name
                    }
            }
            .environment(state.shell)
            // The chat long-polls and is marked seen while this window shows it (MacPresence: visible, not hidden,
            // minimized or fully covered, in front or beside another app's window): the scene's own phase says neither.
            .environment(\.scenePhase, presence.phase == .background ? .background : .active)
            // A column width of its own: left to its content, the chat's minimum moved with what it shows while the
            // documents opened, and each new minimum asked the window for another constraints pass, until AppKit's
            // guard ended the app (crash of 2026-11-04, a document opened from the chat).
            .navigationSplitViewColumnWidth(min: MacRoot.splitChatMinimum, ideal: 700)
        }
        // The documents beside the chat, outside its navigation stack (ChatShell).
        .documentsInspector(state.shell, store: store)
        .background(WindowReader { window in
            presence.track(window)
            window?.titlebarSeparatorStyle = .none   // the feed fades under the header (spec §2.4), not at a line
        })
        .onChange(of: presence.width) { fitColumns() }
        .onChange(of: sidebarWidth) { fitColumns() }
        .onChange(of: state.screen?.showDocuments ?? false) { fitColumns() }
        .onChange(of: columns) { sidebarAsked() }
        .onAppear {
            state.store = store
            presence.recheck()                   // a window opened again is the same window, its views kept
            fitColumns()
            restore()
        }
        .onDisappear {
            if state.store === store { state.store = nil }
            state.menu.publish(MacMenu.Snapshot())
        }
        .onChange(of: MacMenu.snapshot(state), initial: true) { _, now in state.menu.publish(now) }
        .onChange(of: store.instances.map(\.name)) { restore() }
        .onChange(of: model.openChat) { restore() }
        // Threads read: the default named by its thread, an ended thread given way.
        .onChange(of: store.threadStates) { settle() }
    }

    @ViewBuilder
    private var chat: some View {
        if !store.loaded, let error = store.error {
            InstancesLoadFailure(error: error, settings: { openSettings() })
        } else if let selection = state.selection {
            let chat = store.chat(selection)
            ChatScreen(chat: chat, openArchived: { cid in
                state.path.append(ArchivedRoute(instance: selection.instance, cid: cid))
            }, openThread: { ref in state.open(ChatSelection(selection.instance, ref)) })
            .id(ChatSelection(selection.instance, chat.ref))
            .toolbar(removing: .title)           // the header's own title is the name with its state
        } else if store.loaded {
            MarkMessage("No chat open", message: Text("Choose an instance to chat with."))
        } else {
            ProgressView()
        }
    }

    /// Whether the sidebar has to leave room for the documents in this window.
    private var tight: Bool {
        (state.screen?.showDocuments ?? false) && presence.width < ChatShell.threeColumnWidth(sidebar: sidebarWidth)
    }

    /// Sidebar, chat and documents need the window's width together: in a narrow one the sidebar gives way while the
    /// documents are open, and only then (spec §5's rule, as on an iPad). Left to the split view, three columns that do
    /// not fit relaid themselves without end. The width is the window's: the split view's own swings with its columns.
    private func fitColumns() {
        state.shell.width = presence.width
        state.shell.sidebarWidth = columns == .detailOnly ? 0 : sidebarWidth
        if tight, !sidebarYielded, columns != .detailOnly {
            sidebarYielded = true
            columns = .detailOnly
        } else if !tight, sidebarYielded {
            sidebarYielded = false
            columns = .automatic
        }
    }

    /// The sidebar asked for where it does not fit beside the documents (its button, ⌃⌘S; only that shows it here, the
    /// window's width having hidden it): the documents give way to it. They come back from the toolbar, and the sidebar
    /// yields again then.
    private func sidebarAsked() {
        state.shell.sidebarWidth = columns == .detailOnly ? 0 : sidebarWidth
        guard columns != .detailOnly, tight else { return }
        sidebarYielded = false
        state.screen?.showDocuments = false
    }

    /// A notification's chat (its thread when it names one) first, then the chat open last time, else the first
    /// instance.
    private func restore() {
        if let target = model.openChat {
            model.openChat = nil
            state.screen?.dismissPresentations()
            state.path.removeAll()
            state.open(ChatSelection(target.instance, target.ref))
            state.bringForward()
            return
        }
        guard state.selection == nil, !store.instances.isEmpty else { return }
        let names = store.instances.map(\.name)
        let last = UserDefaults.standard.string(forKey: "chat.last") ?? ""
        state.open(names.contains(last) ? last : names[0])
    }

    private func settle() {
        if let settled = store.settle(state.selection) { state.selection = settled }
    }
}

/// Whether the window shows the chat: `.active` in front (the app active, the window its key or main window),
/// `.inactive` shown behind another app's window, `.background` closed, minimised, hidden or covered whole. The chat's
/// `scenePhase` is `.active` for both shown phases (it long-polls and is marked seen), `.background` otherwise.
@MainActor
@Observable
final class MacPresence {
    private(set) var phase: ScenePhase = .active
    /// The window's width.
    private(set) var width: CGFloat = .infinity
    @ObservationIgnored private weak var window: NSWindow?
    @ObservationIgnored private var observers: [NSObjectProtocol] = []

    func track(_ window: NSWindow?) {
        guard let window else { return }
        // A window closed and opened again is the same window: its observers stay, its state is read again.
        defer { update(closing: false) }
        guard window !== self.window else { return }
        self.window = window
        observers.forEach(NotificationCenter.default.removeObserver)
        let center = NotificationCenter.default
        let names: [(Notification.Name, AnyObject?)] = [
            (NSApplication.didBecomeActiveNotification, nil), (NSApplication.didResignActiveNotification, nil),
            (NSApplication.didHideNotification, nil), (NSApplication.didUnhideNotification, nil),
            (NSWindow.didBecomeKeyNotification, window), (NSWindow.didResignKeyNotification, window),
            (NSWindow.didMiniaturizeNotification, window), (NSWindow.didDeminiaturizeNotification, window),
            (NSWindow.didChangeOcclusionStateNotification, window), (NSWindow.willCloseNotification, window),
            (NSWindow.didResizeNotification, window),
        ]
        observers = names.map { name, object in
            center.addObserver(forName: name, object: object, queue: .main) { [weak self] note in
                let closing = note.name == NSWindow.willCloseNotification
                MainActor.assumeIsolated { self?.update(closing: closing) }
            }
        }
    }

    /// Read the window's state again (it was opened again: no notification says so when the app is behind).
    func recheck() { update(closing: false) }

    private func update(closing: Bool) {
        if let window { width = window.frame.width }
        #if DEBUG
        // A dev run on a locked or sleeping screen (MacDevSession): every window there counts as covered.
        switch MacDevSession.directory != nil ? ProcessInfo.processInfo.environment["SHAFT_DEV_PRESENCE"] : nil {
        case "front": phase = closing ? .background : .active; return
        case "shown": phase = closing ? .background : .inactive; return
        default: break
        }
        #endif
        guard let window, !closing, window.isVisible, !window.isMiniaturized, !NSApp.isHidden,
              window.occlusionState.contains(.visible) else {
            phase = .background
            return
        }
        phase = NSApp.isActive && (window.isKeyWindow || window.isMainWindow) ? .active : .inactive
    }
}

/// Hands the window a view is in to `found` (for `MacPresence`).
struct WindowReader: NSViewRepresentable {
    var found: (NSWindow?) -> Void

    func makeNSView(context: Context) -> NSView {
        let view = WindowReaderView()
        view.found = found
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {
        (view as? WindowReaderView)?.found = found
    }

    final class WindowReaderView: NSView {
        var found: ((NSWindow?) -> Void)?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            found?(window)
        }
    }
}
