import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

/// One chat on screen: the header (name, state, Documents, New chat, ⋯), the feed and the composer, with the
/// documents beside it or over it. It long-polls while it is visible, says `away` when it
/// leaves, marks the chat seen when the end of the newest item is on screen, announces a new reply, and takes files dropped on it.
struct ChatScreen: View {
    let chat: ChatStore
    /// Open an earlier chat (§5).
    var openArchived: (ChatSummary) -> Void = { _ in }
    /// Back to the current chat from an earlier one.
    var backToCurrent: (() -> Void)?
    @Environment(AppModel.self) private var model
    @Environment(InstancesStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.chatInFront) private var inFront
    @Environment(\.chatCovered) private var externallyCovered
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    /// The shell that draws the documents beside the chat (an inspector outside its navigation stack). Without one,
    /// or on a phone, this screen presents them as a sheet.
    @Environment(ChatShell.self) private var shell: ChatShell?
    @State private var screen: ChatScreenModel
    private var preview: FilePreview { screen.preview }
    @State private var visible = false
    @State private var newestOnScreen = false
    @State private var seenVersion: String?
    @State private var seenTask: Task<Void, Never>?
    @State private var dropTargeted = false
    #if os(macOS)
    @State private var columnWidth: CGFloat = 700
    #endif

    init(chat: ChatStore, openArchived: @escaping (ChatSummary) -> Void = { _ in }, backToCurrent: (() -> Void)? = nil) {
        self.chat = chat
        self.openArchived = openArchived
        self.backToCurrent = backToCurrent
        _screen = State(initialValue: ChatScreenModel(chat: chat))
    }

    private var watched: Bool {
        visible && scenePhase == .active && !externallyCovered && !covered
    }
    private var covered: Bool {
        (screen.showDocuments && covers) || screen.selecting != nil || screen.rating != nil
            || screen.showEarlierChats || screen.showInstanceSettings || preview.showing || preview.error != nil
            || screen.confirmRetry != nil || screen.confirmingNewChat || screen.confirmingEnd || screen.composerCovered
    }
    private var covers: Bool { shell?.documentsCover ?? true }
    private var sheetsDocuments: Bool { shell?.sheetsDocuments ?? true }

    var body: some View {
        @Bindable var screen = screen
        GeometryReader { geometry in
            VStack(spacing: 0) {
                if chat.cid != nil { archivedBanner }
                // The feed fades out before the header and the composer: nothing is legible behind the glass.
                content
                    .scrollEdgeEffectHidden(true, for: .all)
                    .feedEdges(pillVisible: screen.latestPill != nil) {
                        if let pill = screen.latestPill {
                            LatestPillButton(pill: pill) { screen.jumpToLatest += 1 }
                                .transition(.opacity)
                        }
                    }
                    .safeAreaInset(edge: .bottom, spacing: 0) {
                        VStack(spacing: 0) {
                            connectionNotice
                            if let said = chat.actionError?.message ?? chat.said {
                                StatusLine(text: said, isError: chat.actionError != nil) { chat.dismissStatus() }
                                    .padding(.vertical, 2)
                                    .task(id: said) {
                                        guard chat.actionError == nil else { return }
                                        try? await Task.sleep(for: .seconds(5))
                                        if chat.said == said { chat.dismissStatus() }
                                    }
                            }
                            if !screen.archived {
                                ComposerView(maxHeight: max(80, geometry.size.height / 3))
                            }
                        }
                        .feedFloor()
                    }
            }
        }
        .background(Color.canvas.ignoresSafeArea())
        .toolbar { header }
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #else
        .toolbarBackgroundVisibility(.hidden, for: .windowToolbar)     // no lighter band and line at the fade's foot
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { columnWidth = $0 }
        #endif
        .navigationTitle(chat.instance)
        // Over the chat on a phone (a sheet); beside it where there is room, drawn by the shell (ChatShell): an
        // inspector inside the navigation stack took the chat's toolbar away.
        .sheet(isPresented: Binding(get: { screen.showDocuments && sheetsDocuments }, set: { screen.showDocuments = $0 })) {
            SidePanel(store: store, instance: chat.instance, cid: chat.cid)
                .environment(screen)
                .environment(preview)
                .environment(store)
                .environment(model)
        }
        .sheet(isPresented: $screen.showInstanceSettings) {
            InstanceSettingsView(store: store.settings(chat.instance), services: store.services)
                #if os(macOS)
                .frame(minWidth: 480, minHeight: 560)
                #endif
        }
        .sheet(item: $screen.selecting) { selecting in SelectTextSheet(selecting: selecting) }
        .sheet(item: $screen.rating) { target in RatingSheet(target: target) }
        .sheet(isPresented: $screen.showEarlierChats) {
            EarlierChatsSheet(instance: chat.instance, open: openArchived)
        }
        .filePreview(preview)
        .alert(screen.confirmRetry?.confirm ?? "", isPresented: Binding(
            get: { screen.confirmRetry != nil }, set: { if !$0 { screen.confirmRetry = nil } })) {
            Button("Send it again") {
                if let retry = screen.confirmRetry { Task { await chat.retryReply(retry) } }
                screen.confirmRetry = nil
            }
            Button("Cancel", role: .cancel) { screen.confirmRetry = nil }
        }
        .alert(preview.error ?? "", isPresented: Binding(get: { preview.error != nil },
                                                         set: { if !$0 { preview.error = nil } })) {
            Button("OK", role: .cancel) {}
        }
        .onDrop(of: [.item], isTargeted: $dropTargeted) { providers in
            guard !screen.archived else { return false }
            Task { _ = await ItemImport.attach(providers, to: chat) }
            return true
        }
        .overlay {
            if dropTargeted {
                RoundedRectangle(cornerRadius: Design.Radius.chip, style: .continuous)
                    .strokeBorder(Color.brand, lineWidth: 3)
                    .padding(4)
                    .allowsHitTesting(false)
                    .accessibilityHidden(true)
            }
        }
        .environment(screen)
        .environment(preview)
        .onAppear {
            visible = true
            screen.documentsCover = covers
            // The shell's documents follow the chat on display (an earlier chat's are read-only); open stays open.
            if let shell, shell.screen !== screen {
                let open = shell.screen?.showDocuments ?? false
                let panel = shell.screen?.sidePanel ?? .documents
                shell.screen = screen
                screen.sidePanel = panel
                if open { screen.showDocuments = true }
            }
            follow()
        }
        .onDisappear {
            visible = false
            follow()
        }
        .onChange(of: scenePhase) { follow() }
        .onChange(of: watched) { follow() }
        .onChange(of: inFront) { follow() }
        .onChange(of: covers) { _, covers in
            screen.documentsCover = covers
            follow()
        }
        .onChange(of: chat.feed) { old, new in
            guard let new else { return }
            if visible, !FeedRows.arrivals(from: old, to: new).isEmpty {
                Announce.say(String(localized: "\(chat.instance) replied"))
            }
            markSeen()
        }
    }

    // MARK: Parts

    @ViewBuilder
    private var content: some View {
        if let feed = chat.feed {
            FeedView(feed: feed, rows: FeedRows.rows(feed, unsent: chat.cid == nil ? chat.unsent : [])) { visible in
                newestOnScreen = visible
                if !visible { stopMarkingSeen() }
                markSeen()
            }
        } else if case .stopped(let error) = chat.connection {
            MarkMessage("Could not open the chat", message: Text(error.message)) {
                Button("Try again") { chat.start() }.buttonStyle(ProminentAction())
            }
            .frame(maxHeight: .infinity)
        } else {
            ProgressView("Loading the chat…").frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder
    private var connectionNotice: some View {
        switch chat.connection {
        case .retrying(let error, let retryAt):
            TimelineView(.periodic(from: .now, by: 1)) { context in
                let seconds = max(0, Int(retryAt.timeIntervalSince(context.date)))
                Label(offlineNote(seconds: seconds, error: error), systemImage: "wifi.exclamationmark")
                    .font(.footnote)
                    .foregroundStyle(Color.warning)
                    .lineLimit(2)
                    .padding(.horizontal, Design.Space.gutter)
                    .padding(.vertical, Design.Space.s4)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .accessibilityAddTraits(.updatesFrequently)
        case .stopped(let error) where chat.feed != nil:
            HStack {
                Label(error.message, systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(Color.danger)
                Spacer()
                Button("Try again") { chat.start() }.font(.footnote).frame(minHeight: 44)
            }
            .padding(.horizontal, Design.Space.gutter)
        default:
            EmptyView()
        }
    }

    /// "Offline" is what a network failure's message would say again; anything else keeps its reason.
    private func offlineNote(seconds: Int, error: APIError) -> String {
        guard seconds > 0 else { return String(localized: "Offline — retrying…") }
        if case .transport = error { return String(localized: "Offline — retrying in \(seconds) s") }
        return String(localized: "Offline — retrying in \(seconds) s (\(error.message))")
    }

    private var archivedBanner: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("Earlier chat").font(.footnote.weight(.semibold)).foregroundStyle(Color.textPrimary)
                if let created = chat.content?.chat?.created {
                    Text(Stamp.full(created)).font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                }
            }
            Spacer()
            if let backToCurrent {
                Button("Back to the current chat", action: backToCurrent).font(.footnote).frame(minHeight: 44)
            }
        }
        .padding(.horizontal, Design.Space.gutter)
        .padding(.vertical, Design.Space.s4)
        .background(Color.elevated)
    }

    @ToolbarContentBuilder
    private var header: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            ChatTitle(instance: chat.instance, archived: screen.archived)
                .environment(screen)
                #if os(macOS)
                .titleWidth(limit: MacChrome.titleLimit(column: columnWidth, sidebar: shell?.sidebarWidth ?? 0,
                                                        earlier: chat.cid != nil))
                #endif
        }
        .sharedBackgroundVisibility(.hidden)     // its own capsule: the toolbar's drew a short name as a circle
        if reduceTransparency {
            // The system's fallback for its glass is pure white or black: the group draws its own solid `elevated`.
            ToolbarItem(placement: .primaryAction) {
                HStack(spacing: 0) { headerButtons }
                    .frame(height: Design.Header.height)
                    .glassSurface(in: Capsule())
            }
            .sharedBackgroundVisibility(.hidden)
        } else {
            ToolbarItemGroup(placement: .primaryAction) { headerButtons }
        }
    }

    @ViewBuilder
    private var headerButtons: some View {
        Button { screen.togglePanel(.documents) } label: { Image(systemName: "doc.text") }
            .accessibilityLabel("Documents")
        Button { screen.togglePanel(.cycles) } label: { Image(systemName: "clock.arrow.circlepath") }
            .accessibilityLabel("Cycles")
        if chat.cid == nil {
            Button { screen.confirmingNewChat = true } label: { Image(systemName: "square.and.pencil") }
                .disabled(chat.content?.canClear != true)
                .accessibilityLabel("New chat")
                .confirmationDialog("Start a new chat with \(chat.instance)?",
                                    isPresented: Bindable(screen).confirmingNewChat, titleVisibility: .visible) {
                    Button("New chat") { Task { await chat.clear() } }
                } message: {
                    Text("An open conversation is ended first. This one stays in Earlier chats.")
                }
        }
        Menu {
            if !screen.archived {
                Button { screen.showInstanceSettings = true } label: {
                    Label("Settings for \(chat.instance)", systemImage: "gearshape")
                }
            }
            Button { screen.showEarlierChats = true } label: { Label("Earlier chats", systemImage: "clock") }
            if chat.content?.canStop == true {
                Button(role: .destructive) { Task { await chat.stopReply() } } label: {
                    Label("Stop the reply", systemImage: "stop")
                }
            }
            if chat.content?.canEnd == true {
                Button { screen.confirmingEnd = true } label: {
                    Label("End conversation", systemImage: "flag.checkered")      // the menu hyphenated a longer one
                }
            }
        } label: {
            Image(systemName: "ellipsis")
        }
        .accessibilityLabel("More")
        .confirmationDialog("End the conversation with \(chat.instance)?",
                            isPresented: Bindable(screen).confirmingEnd, titleVisibility: .visible) {
            Button("End it", role: .destructive) { Task { await chat.end() } }
        } message: {
            Text("It writes up its journal. Your next message starts a new conversation.")
        }
    }

    // MARK: Lifecycle

    /// The long-poll runs while the chat is visible (§4); leaving says `away` (§7.6).
    private func follow() {
        chat.setWatching(watched)
        if chat.cid == nil {
            if watched { model.visibleChat = chat.instance }
            else if model.visibleChat == chat.instance { model.visibleChat = nil }
        }
        if visible && scenePhase == .active {
            chat.start()
            if watched && inFront { markSeen() } else { stopMarkingSeen() }
        } else {
            stopMarkingSeen()
            let wasPolling = chat.isPolling
            chat.stop()
            if chat.cid == nil {
                if model.visibleChat == chat.instance { model.visibleChat = nil }
                if wasPolling { Task { await chat.away() } }
            }
        }
    }

    /// Cancelled, this version is free to be sent again at once: the task itself only notices later, and the end of the
    /// newest item may be back on screen before it does.
    private func stopMarkingSeen() {
        seenTask?.cancel()
        seenTask = nil
        seenVersion = nil
    }

    /// The newest item's end is on screen and the app (on the Mac: its window) in front: mark the version shown seen (§7.5).
    /// Retry failures with the current feed version until seen succeeds or the task is cancelled.
    private func markSeen() {
        guard chat.cid == nil, watched, inFront, newestOnScreen, let version = chat.feed?.version,
              version != seenVersion else { return }
        seenVersion = version
        seenTask?.cancel()
        seenTask = Task {
            guard watched, inFront, newestOnScreen else {
                if seenVersion == version { seenVersion = nil }
                return
            }
            if !(await chat.markSeenUntilDone()) && seenVersion == version { seenVersion = nil }
        }
    }
}

/// The header's title, in a glass capsule: the name, then one status line, which drops its optional parts (the model,
/// then the time, at the narrowest its icon) rather than crowd the capsule's curve; behind a tap, what a message does now
/// when it is busy, its model and its spend. Text stops growing at the largest size two lines fit in the capsule.
struct ChatTitle: View {
    let instance: String
    let archived: Bool
    @Environment(InstancesStore.self) private var store
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showNote = false

    var body: some View {
        let row = store.row(instance)
        Button { if row != nil { showNote = true } } label: {
            VStack(spacing: 0) {
                Text(instance)
                    .font(.headline)
                    .foregroundStyle(Color.textPrimary)
                    .lineLimit(1)
                if let row, !archived {
                    ViewThatFits(in: .horizontal) {
                        status(row, time: true, model: true)
                        status(row, time: true, model: false)
                        status(row, time: false, model: false)
                        status(row, time: false, model: false, icon: false)
                    }
                    .font(Design.Text.metadata)
                    .foregroundStyle(Color.textSecondary)
                }
            }
            .dynamicTypeSize(...DynamicTypeSize.xLarge)
            .padding(.horizontal, Design.Header.titleInset)
            .frame(minHeight: Design.Header.height)
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        // A capsule of its own, as tall as the header's buttons: the name is never bare over the chat (2026-11-04).
        .glassSurface(in: Capsule())
        .accessibilityElement(children: .combine)
        .accessibilityHint(row?.busyNote != nil ? "Says what a message does now" : "Shows its model")
        .popover(isPresented: $showNote) {
            VStack(alignment: .leading, spacing: Design.Space.s8) {
                if let note = row?.busyNote {
                    Text(note)
                        .foregroundStyle(Color.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)   // a popover would cut it to one line
                }
                if let model = row?.model, !model.isEmpty {
                    Text("Model: \(model)").font(.footnote).foregroundStyle(Color.textSecondary)
                }
                if let usd = row?.usdToday, usd > 0 {
                    Text("Cloud spend today: $\(usd, specifier: "%.2f")").font(.footnote).foregroundStyle(Color.textSecondary)
                }
                if !screen.archived {
                    Button("Settings for \(instance)") {
                        showNote = false
                        screen.showInstanceSettings = true
                    }
                    .foregroundStyle(Color.brand)
                }
            }
            .padding(Design.Space.s16)
            .frame(maxWidth: 320)
            .presentationCompactAdaptation(.popover)
        }
    }

    private func status(_ row: InstanceRow, time: Bool, model: Bool, icon: Bool = true) -> some View {
        var parts = [row.replying ? String(localized: "replying") : row.state]
        if time && !row.since.isEmpty && !row.replying { parts.append(row.since) }
        if model && !row.model.isEmpty { parts.append(row.model) }
        return HStack(spacing: Design.Space.s4) {
            if icon {
                Image(systemName: StateIcon.symbol(row.state, replying: row.replying))
                    .symbolEffect(.pulse, isActive: StateIcon.pulses(row.state, replying: row.replying) && !reduceMotion)
                    .accessibilityHidden(true)
            }
            Text(parts.joined(separator: " · ")).lineLimit(1)
        }
    }
}

/// The state's icon: working, asleep, paused by you, waiting for you, replying.
enum StateIcon {
    static func symbol(_ state: String, replying: Bool) -> String {
        if replying { return "ellipsis.bubble" }
        if state.hasPrefix("in a cycle") || state.contains("running") { return "gearshape.2" }
        if state.hasPrefix("sleeping") { return "moon.zzz" }
        if state.contains("paused") { return "pause.circle" }
        if state.contains("waiting for you") { return "person.wave.2" }
        if state.hasPrefix("in a conversation") { return "bubble.left.and.bubble.right" }
        return "circle"
    }

    /// The only pulse in the app (spec §2.4): replying, and in a cycle.
    static func pulses(_ state: String, replying: Bool) -> Bool {
        replying || state.hasPrefix("in a cycle")
    }
}
