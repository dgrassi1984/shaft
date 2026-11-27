import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers
#if os(iOS)
import GameController
#elseif os(macOS)
import AppKit
#endif

/// One chat on screen: the header (name and thread, state, Talk, Documents, New thread or New chat, ⋯), the feed and
/// the composer, with the documents beside it or over it, and voice mode over it (iPhone, iPad) or in the composer's
/// place (Mac). It long-polls while it is visible, says `away` when it leaves, marks the chat seen when the end of the
/// newest item is on screen, announces a new reply, and takes files dropped on it. The chat is one conversation of the
/// instance (`chat.ref`): a thread, its From feed, its one chat on a server before threads, or an earlier chat.
struct ChatScreen: View {
    let chat: ChatStore
    /// Open an earlier chat (§5) by its id: from Earlier chats, or a message's "re: «title»".
    var openArchived: (String) -> Void = { _ in }
    /// Back to the current chat from an earlier one.
    var backToCurrent: (() -> Void)?
    /// Show another conversation of the instance (a thread just started).
    var openThread: (ChatRef) -> Void = { _ in }
    @Environment(AppModel.self) private var model
    @Environment(InstancesStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.chatCovered) private var externallyCovered
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    #if os(iOS)
    @Environment(\.horizontalSizeClass) private var sizeClass
    @State private var recordingBlocksTalk = false
    #elseif os(macOS)
    @Environment(\.openWindow) private var openWindow
    #endif
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
    /// Rename… asks for the thread's new title.
    @State private var renaming = false
    @State private var newTitle = ""
    @State private var columnWidth: CGFloat = 700

    init(chat: ChatStore, openArchived: @escaping (String) -> Void = { _ in }, backToCurrent: (() -> Void)? = nil,
         openThread: @escaping (ChatRef) -> Void = { _ in }) {
        self.chat = chat
        self.openArchived = openArchived
        self.backToCurrent = backToCurrent
        self.openThread = openThread
        _screen = State(initialValue: ChatScreenModel(chat: chat, openEarlier: chat.isEarlier ? nil : openArchived))
    }

    /// The composer takes the keyboard when a chat opens: always on the Mac; on an iPhone or iPad only with a hardware
    /// keyboard attached, since the on-screen one would cover the chat just opened.
    private var focusesComposerOnOpen: Bool {
        #if os(iOS)
        GCKeyboard.coalesced != nil
        #else
        true
        #endif
    }

    private var watched: Bool {
        visible && scenePhase == .active && !externallyCovered && !covered
    }
    private var covered: Bool {
        (screen.showDocuments && covers) || screen.selecting != nil || screen.rating != nil
            || screen.showEarlierChats || screen.showInstanceSettings || preview.showing || preview.error != nil
            || screen.confirmRetry != nil || screen.confirmingNewChat || screen.confirmingEnd || screen.confirmingClear
            || screen.composerCovered
            || renaming || (screen.showVoice && Self.voiceCovers)
    }
    /// Voice mode covers the chat on an iPhone or iPad (a full-screen cover); on the Mac it takes the composer's place
    /// and the feed stays in view.
    #if os(iOS)
    private static let voiceCovers = true
    #else
    private static let voiceCovers = false
    #endif
    /// Talk is available in an open conversation when voice mode is on for the instance.
    private var showsTalk: Bool { store.row(chat.instance)?.voiceMode == "on" }
    private var canTalk: Bool { !chat.isEarlier && !screen.archived && showsTalk }
    private var openMain: (() -> Void)? {
        guard case .thread = chat.ref, !screen.archived else { return nil }
        return { openThread(store.defaultRef(chat.instance)) }
    }
    /// The instance has threads (a server with them): New thread starts one beside the others, and ends nothing.
    private var hasThreads: Bool { store.threads(chat.instance) != nil }
    /// Main uses the instance name; a thread uses its own title.
    private var threadTitle: String? {
        if chat.ref.isFrom { return nil }
        return chat.isEarlier ? nil : chat.title
    }
    /// On an iPhone's width, five header buttons left no room for the instance's name (M3, iPhone Simulator): with Talk
    /// shown, Cycles moves into ⋯.
    private var cyclesInMenu: Bool {
        #if os(iOS)
        sizeClass == .compact
        #else
        false
        #endif
    }
    private var covers: Bool { shell?.documentsCover ?? true }
    private var sheetsDocuments: Bool { shell?.sheetsDocuments ?? true }

    var body: some View {
        @Bindable var screen = screen
        GeometryReader { geometry in
            VStack(spacing: 0) {
                if chat.isEarlier { archivedBanner }
                // The feed fades out before the header and the composer: nothing is legible behind the glass.
                // One container whatever the feed's state: on the if/else itself, the switch from Loading to the feed
                // made the composer anew and dropped its keyboard.
                ZStack { content }
                    .scrollEdgeEffectHidden(true, for: .all)
                    .feedEdges(pillVisible: screen.latestPill != nil) {
                        if let pill = screen.latestPill {
                            LatestPillButton(pill: pill) { screen.jumpToLatest += 1 }
                                .transition(.opacity)
                        }
                    }
                    // Above the fade, not under it: the feed's own inset sat beneath the header's fade, washed out.
                    .safeAreaInset(edge: .top, spacing: 0) { questionsWaiting }
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
                            if screen.showVoice && !Self.voiceCovers {
                                VoiceScreen(client: store.client, instance: chat.instance, cid: chat.ref.seenID,
                                            style: .panel) {
                                    screen.showVoice = false
                                }
                            } else if !screen.archived {
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
        #endif
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { columnWidth = $0 }
        .navigationTitle(chat.instance)
        // Over the chat on a phone (a sheet); beside it where there is room, drawn by the shell (ChatShell): an
        // inspector inside the navigation stack took the chat's toolbar away.
        .sheet(isPresented: Binding(get: { screen.showDocuments && sheetsDocuments }, set: { screen.showDocuments = $0 })) {
            SidePanel(store: store, instance: chat.instance, cid: chat.ref.cid)
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
        #if os(iOS)
        .alert(IOSAudioUse.meetingInProgress, isPresented: $recordingBlocksTalk) {
            Button("OK", role: .cancel) {}
        }
        .fullScreenCover(isPresented: $screen.showVoice) {
            VoiceScreen(client: store.client, instance: chat.instance, cid: chat.ref.seenID, style: .fullScreen) {
                screen.showVoice = false
            }
        }
        #endif
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
        .alert("Rename the thread", isPresented: $renaming) {
            TextField("Title", text: $newTitle)
            Button("Rename") {
                let title = newTitle
                Task { await chat.rename(title) }
            }
            .disabled(newTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            Button("Cancel", role: .cancel) {}
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
            TransitionClock.mark("shown")
            visible = true
            if focusesComposerOnOpen && !chat.isEarlier && !screen.archived {
                screen.focusComposerOnOpen = true
            }
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
            screen.stopReadAloud()
            visible = false
            follow()
        }
        .onChange(of: screen.showVoice) { _, shown in
            if shown { screen.stopReadAloudForVoice() }
        }
        .onChange(of: scenePhase) { follow() }
        .onChange(of: watched) { follow() }
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
            FeedView(feed: feed, rows: FeedRows.rows(feed, unsent: chat.unsent)) { visible in
                newestOnScreen = visible
                if !visible { stopMarkingSeen() }
                markSeen()
            }
            .onAppear { TransitionClock.mark("loaded") }
        } else if case .stopped(let error) = chat.connection {
            MarkMessage("Could not open the chat", message: Text(error.message)) {
                Button("Try again") { chat.start() }.buttonStyle(ProminentAction())
            }
            .frame(maxHeight: .infinity)
        } else {
            ProgressView("Loading the chat…").frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    /// "N questions waiting": the open questions of this feed; a tap goes to the oldest.
    @ViewBuilder
    private var questionsWaiting: some View {
        let questions = chat.feed.map(FeedRows.waitingQuestions) ?? []
        if let oldest = questions.first {
            Button {
                screen.jumpToQuestion = oldest.key
            } label: {
                Text("\(questions.count) \(questions.count == 1 ? "question" : "questions") waiting")
                    .font(Design.Text.pill)
                    .foregroundStyle(Color.brand)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, Design.Space.gutter)
                    .padding(.vertical, Design.Space.s8)
                    .quietHitArea()
            }
            .buttonStyle(.plain)
            .background(Color.canvas)
            .accessibilityHint("Scrolls to the oldest unanswered question")
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

    #if os(iOS)
    /// Reserve the controls' full width before proposing space to the two title lines.
    private var compactTitleLimit: CGFloat? {
        guard sizeClass == .compact else { return nil }
        let controls: CGFloat = showsTalk ? 4 : 3
        let leading: CGFloat = chat.isEarlier || shell?.sheetsDocuments == false ? 44 : 0
        return max(0, columnWidth - controls * 44 - 64 - leading)
    }
    #endif

    @ToolbarContentBuilder
    private var header: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            ChatTitle(instance: chat.instance, thread: threadTitle, archived: screen.archived, openMain: openMain)
                .environment(screen)
                #if os(iOS)
                .frame(maxWidth: compactTitleLimit)
                #else
                // Meetings, and Talk when shown, are buttons more at the right.
                .titleWidth(limit: MacChrome.titleLimit(column: columnWidth, sidebar: shell?.sidebarWidth ?? 0,
                                                        earlier: chat.isEarlier, extraButtons: showsTalk ? 2 : 1))
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
        if showsTalk {
            Button {
                #if os(iOS)
                if IOSAudioUse.shared.owner == .meeting {
                    recordingBlocksTalk = true
                    return
                }
                #endif
                screen.showVoice = true
            } label: { Image(systemName: "waveform") }
                .disabled(!canTalk)
                .accessibilityLabel("Talk")
                .accessibilityHint("Talk with \(chat.instance) by voice")
                .help("Talk with \(chat.instance) by voice")
        }
        Button { screen.togglePanel(.documents) } label: { Image(systemName: "doc.text") }
            .accessibilityLabel("Documents")
            .help("Documents")
        if !cyclesInMenu {
            Button { screen.togglePanel(.cycles) } label: { Image(systemName: "clock.arrow.circlepath") }
                .accessibilityLabel("Cycles")
                .help("Cycles")
        }
        #if os(macOS)
        Button {
            NSApp.activate()
            openWindow(id: MeetingsWindow.windowID)
        } label: { Image(systemName: "person.2.wave.2") }
            .accessibilityLabel("Meetings")
            .help("Meetings (⌥⌘M)")
        #endif
        if hasThreads {
            // A thread more, beside this one: it ends nothing, so nothing to confirm.
            Button { newThread() } label: { NewThreadIcon(starting: store.creatingThread.contains(chat.instance)) }
                .disabled(screen.archived || chat.isEarlier || store.creatingThread.contains(chat.instance))
                .accessibilityLabel("New thread with \(chat.instance)")
                .accessibilityHint("Starts another conversation with \(chat.instance); this one stays open")
                .help("New thread with \(chat.instance)")
        } else {
            let newChatLabel = store.threadsState(chat.instance) == .unsupported ? "New chat" : "New thread with \(chat.instance)"
            Button { screen.confirmingNewChat = true } label: { Image(systemName: "square.and.pencil") }
                .disabled(chat.isEarlier || store.threadsState(chat.instance) == .unknown || chat.content?.canClear != true)
                .accessibilityLabel(newChatLabel)
                .help(newChatLabel)
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
            if cyclesInMenu {
                Button { screen.togglePanel(.cycles) } label: { Label("Cycles", systemImage: "clock.arrow.circlepath") }
            }
            if case .thread = chat.ref, !screen.archived {
                Button {
                    newTitle = chat.title ?? ""
                    renaming = true
                } label: {
                    Label("Rename thread…", systemImage: "pencil")
                }
            }
            Button { screen.showEarlierChats = true } label: { Label("Earlier chats", systemImage: "clock") }
            if chat.content?.canStop == true {
                Button(role: .destructive) { Task { await chat.stopReply() } } label: {
                    Label("Stop the reply", systemImage: "stop")
                }
            }
            if hasThreads, chat.content?.canClear == true, !screen.archived, !chat.isEarlier {
                Button { screen.confirmingClear = true } label: {
                    Label("Clear conversation…", systemImage: "eraser")
                }
            }
            if chat.content?.canEnd == true {
                Button { screen.confirmingEnd = true } label: {
                    Label("Close thread", systemImage: "flag.checkered")      // the menu hyphenated a longer one
                }
            }
        } label: {
            Image(systemName: "ellipsis")
        }
        .accessibilityLabel("More")
        .help("More")
        .confirmationDialog("End the conversation with \(chat.instance)?",
                            isPresented: Bindable(screen).confirmingEnd, titleVisibility: .visible) {
            Button("End it", role: .destructive) { Task { await chat.end() } }
        } message: {
            hasThreads ? Text("It writes up its journal. Its other threads stay open.")
                       : Text("It writes up its journal. Your next message starts a new conversation.")
        }
        .confirmationDialog("Clear the conversation with \(chat.instance)?",
                            isPresented: Bindable(screen).confirmingClear, titleVisibility: .visible) {
            Button("Clear", role: .destructive) { Task { await chat.clear() } }
        } message: {
            Text("\(chat.instance) starts fresh: it no longer reads what was said so far, and writes up its journal. Nothing is deleted: earlier messages stay under Show earlier messages.")
        }
    }

    /// New thread: shown as soon as the server made it (the thread list is read again behind it).
    private func newThread() {
        Task {
            await store.newThread(chat.instance) { openThread($0) }
        }
    }

    /// What `AppModel.visibleChat` says while this chat is on screen: its instance and thread.
    private var target: ChatTarget { ChatTarget(instance: chat.instance, cid: chat.ref.seenID) }

    // MARK: Lifecycle

    /// The long-poll runs while the chat is visible (§4); leaving says `away` (§7.6).
    private func follow() {
        chat.setWatching(watched)
        if !chat.isEarlier {
            if watched { model.visibleChat = target }
            else if model.visibleChat == target { model.visibleChat = nil }
        }
        if visible && scenePhase == .active {
            chat.start()
            if watched { markSeen() } else { stopMarkingSeen() }
        } else {
            stopMarkingSeen()
            let wasPolling = chat.isPolling
            chat.stop()
            if !chat.isEarlier {
                if model.visibleChat == target { model.visibleChat = nil }
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

    /// The newest item's end is on screen and the chat watched (its window shows it, on the Mac even beside another app's
    /// window in front): mark the version shown seen (§7.5).
    /// Retry failures with the current feed version until seen succeeds or the task is cancelled.
    private func markSeen() {
        guard !chat.isEarlier, watched, newestOnScreen, let version = chat.feed?.version,
              version != seenVersion else { return }
        seenVersion = version
        seenTask?.cancel()
        seenTask = Task {
            guard watched, newestOnScreen else {
                if seenVersion == version { seenVersion = nil }
                return
            }
            if !(await chat.markSeenUntilDone()) && seenVersion == version { seenVersion = nil }
        }
    }
}

/// Two header lines in one capsule. The title opens the switcher on a phone; a thread's second line opens Main.
struct ChatTitle: View {
    let instance: String
    /// The thread title; nil for Main or a server before threads.
    var thread: String? = nil
    let archived: Bool
    var openMain: (() -> Void)? = nil
    @Environment(InstancesStore.self) private var store
    @Environment(ChatScreenModel.self) private var screen
    @State private var showNote = false
    @State private var showPendingNotes = false
    @Environment(\.openConversationSwitcher) private var switchConversations

    var body: some View {
        let row = store.row(instance)
        VStack(spacing: 0) {
            Button {
                if let switchConversations { switchConversations() }
                else if row != nil { showNote = true }
            } label: {
                Text(thread ?? instance)
                    .foregroundStyle(Color.textPrimary)
                    .font(.headline)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint(switchConversations != nil ? "Choose a conversation" : "Shows instance details")
            if let row, !archived { status(row) }
        }
        .dynamicTypeSize(...DynamicTypeSize.xLarge)
        .padding(.horizontal, Design.Header.titleInset)
        .frame(minHeight: Design.Header.height)
        .glassSurface(in: Capsule())
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
                PendingNotesView(instance: instance)
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

    @ViewBuilder
    private func status(_ row: InstanceRow) -> some View {
        let label = ConversationLabels.header(instance: instance, state: row.state, replying: row.replying,
                                              inThread: openMain != nil)
        HStack(spacing: Design.Space.s4) {
            if let openMain {
                Button(action: openMain) {
                    Text(label).lineLimit(1).truncationMode(.middle)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(label). Open Main")
                .help("Open Main with \(instance)")
            } else {
                Text(label).lineLimit(1)
            }
            if (store.pendingNotes[instance]?.count ?? 0) > 0 {
                pendingNotesButton
            }
        }
        .font(Design.Text.metadata)
        .foregroundStyle(Color.textSecondary)
    }

    private var pendingNotesButton: some View {
        let count = store.pendingNotes[instance]?.count ?? 0
        return Button { showPendingNotes = true } label: {
            Label("\(count)", systemImage: "moon").labelStyle(.titleAndIcon).lineLimit(1)
        }
        .buttonStyle(.plain)
        .fixedSize()
        .accessibilityLabel("\(count) notes for next cycle")
        .popover(isPresented: $showPendingNotes) {
            PendingNotesView(instance: instance)
                .padding(Design.Space.s16)
                .frame(idealWidth: 320)
                .presentationCompactAdaptation(.popover)
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

/// Pending notes come from the instance-wide endpoint, including notes outside the feed window.
private struct PendingNotesView: View {
    let instance: String
    @Environment(InstancesStore.self) private var store

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Design.Space.s12) {
                Text("For next cycle").font(.headline)
                if let error = store.notesError[instance] { Text(error.message).foregroundStyle(Color.warning) }
                if (store.pendingNotes[instance] ?? []).isEmpty { Text("No pending notes.").foregroundStyle(Color.textSecondary) }
                ForEach(store.pendingNotes[instance] ?? []) { note in
                    VStack(alignment: .leading, spacing: Design.Space.s4) {
                        Text(note.title ?? instance).font(.subheadline.weight(.semibold))
                        Text(note.text ?? "")
                        if note.cancellable == true {
                            Button("Cancel") { Task { await store.cancelNote(instance, note: note) } }
                        } else {
                            Text("It may already have been read; it cannot be cancelled.")
                                .font(.caption).foregroundStyle(Color.textSecondary)
                        }
                    }
                }
            }
        }
        .frame(maxHeight: 400)
        .task { await store.refreshNotes(instance) }
    }
}

/// The New thread button's face: the pencil, or a small spinner in its place while the server starts the thread (a tap
/// shows at once that it was taken; the thread opens when the server has made it).
struct NewThreadIcon: View {
    let starting: Bool

    var body: some View {
        Image(systemName: "square.and.pencil")
            .opacity(starting ? 0 : 1)
            .overlay { if starting { ProgressView().controlSize(.small) } }
            .accessibilityValue(starting ? Text("Starting") : Text(""))
    }
}
