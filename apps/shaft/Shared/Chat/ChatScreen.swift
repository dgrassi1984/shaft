import QuickLook
import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

/// One chat on screen: the header (name, state, Documents, New chat, ⋯), the feed and the composer, with the
/// documents beside it or over it. It long-polls while it is visible, says `away` when it
/// leaves, marks the chat seen when the newest item is on screen, announces a new reply, and takes files dropped on it.
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
    /// The shell that draws the documents beside the chat (an inspector outside its navigation stack). Without one,
    /// or on a phone, this screen presents them as a sheet.
    @Environment(ChatShell.self) private var shell: ChatShell?
    @State private var screen: ChatScreenModel
    private var preview: FilePreview { screen.preview }
    @State private var visible = false
    @State private var newestOnScreen = false
    @State private var seenVersion: String?
    @State private var dropTargeted = false

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
            || screen.showEarlierChats || preview.url != nil || preview.error != nil
            || screen.confirmRetry != nil || screen.confirmingNewChat || screen.composerCovered
    }
    private var covers: Bool { shell?.documentsCover ?? true }
    private var sheetsDocuments: Bool { shell?.sheetsDocuments ?? true }

    var body: some View {
        @Bindable var screen = screen
        GeometryReader { geometry in
            VStack(spacing: 0) {
                if chat.cid != nil { archivedBanner }
                // The composer floats: the feed runs under it and fades there, as under the header.
                content
                    .scrollEdgeEffectStyle(.soft, for: .all)
                    .safeAreaBar(edge: .bottom, spacing: 0) {
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
                    }
            }
        }
        .background(Color.canvas.ignoresSafeArea())
        .toolbar { header }
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .navigationTitle(chat.instance)
        // Over the chat on a phone (a sheet); beside it where there is room, drawn by the shell (ChatShell): an
        // inspector inside the navigation stack took the chat's toolbar away.
        .sheet(isPresented: Binding(get: { screen.showDocuments && sheetsDocuments }, set: { screen.showDocuments = $0 })) {
            DocumentsPanel(store: store, instance: chat.instance, cid: chat.cid)
                .environment(screen)
                .environment(preview)
                .environment(store)
                .environment(model)
        }
        .sheet(item: $screen.selecting) { selecting in SelectTextSheet(selecting: selecting) }
        .sheet(item: $screen.rating) { target in RatingSheet(target: target) }
        .sheet(isPresented: $screen.showEarlierChats) {
            EarlierChatsSheet(instance: chat.instance, open: openArchived)
        }
        .quickLookPreview(Binding(get: { preview.url }, set: { preview.url = $0 }))
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
        .confirmationDialog("Start a new chat with \(chat.instance)?", isPresented: $screen.confirmingNewChat, titleVisibility: .visible) {
            Button("New chat") { Task { await chat.clear() } }
        } message: {
            Text("An open conversation is ended first. This one stays in Earlier chats.")
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
                shell.screen = screen
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
                Label(seconds > 0 ? "Offline — retrying in \(seconds) s (\(error.message))" : "Offline — retrying…",
                      systemImage: "wifi.exclamationmark")
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
            ChatTitle(instance: chat.instance, archived: chat.cid != nil)
                #if os(macOS)
                .fixedSize()                     // the Mac's toolbar squeezed a short name's state out of it
                #endif
        }
        #if os(macOS)
        .sharedBackgroundVisibility(.hidden)     // its glass drew a short name as a circle, clipping the state
        #endif
        ToolbarItemGroup(placement: .primaryAction) {
            Button { screen.showDocuments.toggle() } label: { Image(systemName: "doc.text") }
                .accessibilityLabel("Documents")
            if chat.cid == nil {
                Button { screen.confirmingNewChat = true } label: { Image(systemName: "square.and.pencil") }
                    .disabled(chat.content?.canClear != true)
                    .accessibilityLabel("New chat")
            }
            Menu {
                Button { screen.showEarlierChats = true } label: { Label("Earlier chats", systemImage: "clock") }
                if chat.content?.canStop == true {
                    Button(role: .destructive) { Task { await chat.stopReply() } } label: {
                        Label("Stop the reply", systemImage: "stop")
                    }
                }
                if chat.content?.canEnd == true {
                    Button { Task { await chat.end() } } label: {
                        Label("End the conversation", systemImage: "flag.checkered")
                    }
                }
            } label: {
                Image(systemName: "ellipsis")
            }
            .accessibilityLabel("More")
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
            markSeen()
        } else {
            let wasPolling = chat.isPolling
            chat.stop()
            if chat.cid == nil {
                if model.visibleChat == chat.instance { model.visibleChat = nil }
                if wasPolling { Task { await chat.away() } }
            }
        }
    }

    /// The newest item is on screen and the app (on the Mac: its window) in front: mark the version shown seen (§7.5).
    /// A version the server no longer has (409) is tried again when the new one is drawn.
    private func markSeen() {
        guard chat.cid == nil, watched, inFront, newestOnScreen, let version = chat.feed?.version,
              version != seenVersion else { return }
        seenVersion = version
        Task {
            guard watched, inFront, newestOnScreen else {
                if seenVersion == version { seenVersion = nil }
                return
            }
            if !(await chat.markSeen()) && seenVersion == version { seenVersion = nil }
        }
    }
}

/// The header's title: the name, then one status line, which drops its optional parts (the model, then the time)
/// rather than truncate; behind a tap, what a message does now when it is busy, its model and its spend. Text stops
/// growing at the largest size two lines fit in the bar.
struct ChatTitle: View {
    let instance: String
    let archived: Bool
    @Environment(InstancesStore.self) private var store
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
                if let row {
                    ViewThatFits(in: .horizontal) {
                        status(row, time: true, model: true)
                        status(row, time: true, model: false)
                        status(row, time: false, model: false)
                    }
                    .font(Design.Text.metadata)
                    .foregroundStyle(Color.textSecondary)
                }
            }
            .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
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
            }
            .padding(Design.Space.s16)
            .frame(maxWidth: 320)
            .presentationCompactAdaptation(.popover)
        }
    }

    private func status(_ row: InstanceRow, time: Bool, model: Bool) -> some View {
        var parts = [row.replying ? String(localized: "replying") : row.state]
        if time && !row.since.isEmpty && !row.replying { parts.append(row.since) }
        if model && !row.model.isEmpty { parts.append(row.model) }
        return HStack(spacing: Design.Space.s4) {
            Image(systemName: StateIcon.symbol(row.state, replying: row.replying))
                .symbolEffect(.pulse, isActive: StateIcon.pulses(row.state, replying: row.replying) && !reduceMotion)
                .accessibilityHidden(true)
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
