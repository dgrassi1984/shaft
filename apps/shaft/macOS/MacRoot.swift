import AppKit
import SHAFTKit
import SwiftUI

/// The Mac window's state, which the menu commands share with it: the chat shown (and the screen drawing it, through
/// the shell), the store, and how to bring the window back once closed.
@MainActor
@Observable
final class MacState {
    /// The instance whose chat is shown.
    var selection: String?
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
        if selection != name { path.removeAll() }
        selection = name
        UserDefaults.standard.set(name, forKey: "chat.last")
    }

    /// Next (`by` 1) or previous (-1) chat in the list's order (Option-↓/↑).
    func step(_ by: Int) {
        guard let names = store?.instances.map(\.name), !names.isEmpty else { return }
        guard let selection, let index = names.firstIndex(of: selection) else {
            open(names[0])
            return
        }
        open(names[(index + by + names.count) % names.count])
    }

    /// Focus the composer ("/", New message).
    func focusComposer() {
        guard let screen = currentScreen else { return }
        screen.focusComposerRequest += 1
    }

    /// Reload: the chat list, and the chat's feed asked for again at once.
    func reload() {
        guard let store else { return }
        Task { await store.refresh() }
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

/// The Mac window: the instances in the sidebar, the chat, and the chat's documents as an inspector.
struct MacRoot: View {
    static let windowID = "main"
    let model: AppModel
    let state: MacState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        SessionRoot(model: model) { session, store in
            MacChats(session: session, store: store, state: state)
        }
        .frame(minWidth: 720, minHeight: 480)
        .onAppear { state.openWindow = openWindow }
        .onChange(of: model.session?.isPaired ?? false) { _, paired in if !paired { DockBadge.clear() } }
        // shaft://chat/<instance>: the chat a notification click opens, from a link too.
        .onOpenURL { url in model.notificationTapped(instance: AppLinks.instance(in: url)) }
    }
}

struct MacChats: View {
    let session: SHAFTSession
    let store: InstancesStore
    let state: MacState
    @Environment(AppModel.self) private var model
    @Environment(\.openSettings) private var openSettings
    @State private var presence = MacPresence()

    var body: some View {
        @Bindable var state = state
        NavigationSplitView {
            InstancesList(selection: Binding(get: { state.selection }, set: { name in if let name { state.open(name) } }),
                          settings: { openSettings() })
                .navigationSplitViewColumnWidth(min: 220, ideal: 270, max: 360)
        } detail: {
            NavigationStack(path: $state.path) {
                chat
                    .navigationDestination(for: ArchivedRoute.self) { route in
                        // The stack's environment does not reach its pushed views: an earlier chat's screen needs the
                        // shell too, or the documents and the menus stay on the current chat's.
                        ArchivedChatView(route: route) { state.path.removeAll() }
                            .environment(state.shell)
                    }
            }
            .environment(state.shell)
            // The chat long-polls while this window shows it, and is marked seen only while the window is in front
            // (MacPresence): the scene's own phase says neither.
            .environment(\.scenePhase, presence.phase == .background ? .background : .active)
            .environment(\.chatInFront, presence.phase == .active)
        }
        // The documents beside the chat, outside its navigation stack (ChatShell).
        .documentsInspector(state.shell, store: store)
        .background(WindowReader { window in presence.track(window) })
        .onAppear {
            state.store = store
            presence.recheck()                   // a window opened again is the same window, its views kept
            restore()
            DockBadge.follow(store)
        }
        .onDisappear {
            if state.store === store { state.store = nil }
            state.menu.publish(MacMenu.Snapshot())
        }
        .onChange(of: MacMenu.snapshot(state), initial: true) { _, now in state.menu.publish(now) }
        .onChange(of: store.instances.map(\.name)) { restore() }
        .onChange(of: model.openChat) { restore() }
    }

    @ViewBuilder
    private var chat: some View {
        if let selection = state.selection {
            ChatScreen(chat: store.chat(selection), openArchived: { summary in
                state.path.append(ArchivedRoute(instance: selection, cid: summary.id))
            })
            .id(selection)
            .toolbar(removing: .title)           // the header's own title is the name with its state
        } else if store.loaded {
            ContentUnavailableView("No chat open", systemImage: "bubble.left.and.bubble.right",
                                   description: Text("Choose an instance to chat with."))
        } else {
            ProgressView()
        }
    }

    /// A notification's chat first, then the chat open last time, else the first instance.
    private func restore() {
        if let name = model.openChat {
            model.openChat = nil
            state.open(name)
            state.bringForward()
            return
        }
        guard state.selection == nil, !store.instances.isEmpty else { return }
        let names = store.instances.map(\.name)
        let last = UserDefaults.standard.string(forKey: "chat.last") ?? ""
        state.open(names.contains(last) ? last : names[0])
    }
}

/// Whether the window is in front: the app active, the window its key or main window, not minimised and not covered
/// whole. The chat's `scenePhase` follows it: `.active` in front, `.inactive` behind, `.background` hidden.
@MainActor
@Observable
final class MacPresence {
    private(set) var phase: ScenePhase = .active
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

/// The Dock icon's badge: the server's count of chats with something unseen (§12), as the push's badge sets it. It
/// follows the store's count with the window closed too (the chat list keeps polling then), for as long as that store
/// is the session's.
@MainActor
enum DockBadge {
    private static weak var followed: InstancesStore?

    static func follow(_ store: InstancesStore) {
        guard followed !== store else { return }
        followed = store
        track(store)
    }

    /// Unpaired: no count to show.
    static func clear() {
        followed = nil
        NSApp.dockTile.badgeLabel = nil
    }

    private static func track(_ store: InstancesStore) {
        guard followed === store else { return }
        withObservationTracking {
            NSApp.dockTile.badgeLabel = store.badge > 0 ? "\(store.badge)" : nil
        } onChange: { [weak store] in
            Task { @MainActor in if let store { track(store) } }
        }
    }
}
