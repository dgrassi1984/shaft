import QuickLook
import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

/// One chat on screen: the header (name, state, Documents, New chat, ⋯), the feed and the composer, with the
/// documents beside it or over it. It long-polls only while it is visible and the app is in front, says `away` when it
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
    #if os(iOS)
    @Environment(\.horizontalSizeClass) private var sizeClass
    #endif
    /// The shell that draws the documents beside the chat (an inspector outside its navigation stack); nil where
    /// they cover it (a phone): then this screen presents them as a sheet.
    @Environment(ChatShell.self) private var shell: ChatShell?
    @State private var screen: ChatScreenModel
    private var preview: FilePreview { screen.preview }
    @State private var visible = false
    @State private var newestOnScreen = false
    @State private var seenVersion: String?
    @State private var dropTargeted = false
    @State private var clearing = false

    init(chat: ChatStore, openArchived: @escaping (ChatSummary) -> Void = { _ in }, backToCurrent: (() -> Void)? = nil) {
        self.chat = chat
        self.openArchived = openArchived
        self.backToCurrent = backToCurrent
        _screen = State(initialValue: ChatScreenModel(chat: chat))
    }

    private var active: Bool { visible && scenePhase == .active }
    private var covers: Bool {
        #if os(iOS)
        shell == nil || sizeClass == .compact
        #else
        shell == nil
        #endif
    }

    var body: some View {
        @Bindable var screen = screen
        GeometryReader { geometry in
            VStack(spacing: 0) {
                if chat.cid != nil { archivedBanner }
                content
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
                    Divider()
                    ComposerView(maxHeight: max(80, geometry.size.height / 3))
                }
            }
        }
        .toolbar { header }
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .navigationTitle(chat.instance)
        // Over the chat on a phone (a sheet); beside it where there is room, drawn by the shell (ChatShell): an
        // inspector inside the navigation stack took the chat's toolbar away.
        .sheet(isPresented: Binding(get: { screen.showDocuments && covers }, set: { screen.showDocuments = $0 })) {
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
        .confirmationDialog("Start a new chat with \(chat.instance)?", isPresented: $clearing, titleVisibility: .visible) {
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
                RoundedRectangle(cornerRadius: 16).strokeBorder(Color.accentColor, lineWidth: 3)
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
            if chat.cid == nil || shell?.screen?.chat.instance != chat.instance { shell?.screen = screen }
            follow()
        }
        .onDisappear {
            visible = false
            follow()
        }
        .onChange(of: scenePhase) { follow() }
        .onChange(of: screen.showDocuments) { _, open in chat.setWatching(!(open && covers)) }
        .onChange(of: covers) { _, covers in
            screen.documentsCover = covers
            chat.setWatching(!(screen.showDocuments && covers))
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
            ContentUnavailableView {
                Label("Could not open the chat", systemImage: "exclamationmark.triangle")
            } description: {
                Text(error.message)
            } actions: {
                Button("Try again") { chat.start() }
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
                    .foregroundStyle(.orange)
                    .lineLimit(2)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 4)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .accessibilityAddTraits(.updatesFrequently)
        case .stopped(let error) where chat.feed != nil:
            HStack {
                Label(error.message, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(.red)
                Spacer()
                Button("Try again") { chat.start() }.font(.footnote).frame(minHeight: 44)
            }
            .padding(.horizontal, 12)
        default:
            EmptyView()
        }
    }

    private var archivedBanner: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("Earlier chat").font(.footnote.weight(.semibold))
                if let created = chat.content?.chat?.created {
                    Text(Stamp.full(created)).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if let backToCurrent {
                Button("Back to the current chat", action: backToCurrent).font(.footnote).frame(minHeight: 44)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Color.secondary.opacity(0.1))
    }

    @ToolbarContentBuilder
    private var header: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            ChatTitle(instance: chat.instance, archived: chat.cid != nil)
        }
        ToolbarItemGroup(placement: .primaryAction) {
            Button { screen.showDocuments.toggle() } label: { Image(systemName: "doc.text") }
                .accessibilityLabel("Documents")
            if chat.cid == nil {
                Button { clearing = true } label: { Image(systemName: "square.and.pencil") }
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
                Image(systemName: "ellipsis.circle")
            }
            .accessibilityLabel("More")
        }
    }

    // MARK: Lifecycle

    /// The long-poll runs only while the chat is visible and the app in front (§4); leaving says `away` (§7.6).
    private func follow() {
        if active {
            chat.start()
            if chat.cid == nil { model.visibleChat = chat.instance }
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

    /// The newest item is on screen and the app in front: mark the version shown seen (§7.5). A version the server
    /// no longer has (409) is tried again when the new one is drawn.
    private func markSeen() {
        guard chat.cid == nil, active, newestOnScreen, let version = chat.feed?.version, version != seenVersion else {
            return
        }
        seenVersion = version
        Task {
            if !(await chat.markSeen()) && seenVersion == version { seenVersion = nil }
        }
    }
}

/// The header's title: the name, then its state and model (with what a message does now behind a tap when it is busy
/// and not replying).
struct ChatTitle: View {
    let instance: String
    let archived: Bool
    @Environment(InstancesStore.self) private var store
    @State private var showNote = false

    var body: some View {
        let row = store.row(instance)
        Button { if row != nil { showNote = true } } label: {
            VStack(spacing: 0) {
                Text(instance).font(.headline)
                if let row {
                    HStack(spacing: 4) {
                        Image(systemName: StateIcon.symbol(row.state, replying: row.replying)).accessibilityHidden(true)
                        Text(row.replying ? String(localized: "replying") : row.state)
                        if !row.since.isEmpty && !row.replying { Text("· \(row.since)") }
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityHint(row?.busyNote != nil ? "Says what a message does now" : "Shows its model")
        .popover(isPresented: $showNote) {
            VStack(alignment: .leading, spacing: 6) {
                if let note = row?.busyNote { Text(note) }
                if let model = row?.model, !model.isEmpty {
                    Text("Model: \(model)").font(.footnote).foregroundStyle(.secondary)
                }
                if let usd = row?.usdToday, usd > 0 {
                    Text("Cloud spend today: $\(usd, specifier: "%.2f")").font(.footnote).foregroundStyle(.secondary)
                }
            }
            .padding()
            .frame(maxWidth: 320)
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
}
