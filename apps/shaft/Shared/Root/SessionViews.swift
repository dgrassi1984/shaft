import SHAFTKit
import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// What every platform shell shows first: why SHAFT could not start, the pairing screen, or — paired — `paired` with
/// the instances store in the environment. It polls the chat list while the app is in front.
struct SessionRoot<Paired: View>: View {
    let model: AppModel
    @ViewBuilder var paired: (SHAFTSession, InstancesStore) -> Paired
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if let session = model.session {
                if let store = session.store {
                    if case .needsNewerServer(let server) = store.compatibility {
                        // §19: one clear screen instead of every call failing on an older server.
                        NeedsNewerServer(server: server, store: store)
                    } else {
                        paired(session, store)
                            .id(ObjectIdentifier(store))
                            .environment(store)
                            .onAppear { if scenePhase == .active { store.start() } }
                            .onChange(of: scenePhase) { _, phase in
                                if phase == .active { store.start() } else { store.stop() }
                            }
                    }
                } else {
                    PairingView(session: session)
                }
            } else {
                MarkMessage("SHAFT could not start", message: Text(model.startupError ?? "No reason was given.")) {
                    Button("Try again") { model.retryStart() }.buttonStyle(ProminentAction())
                }
            }
        }
        .environment(model)
        .onChange(of: model.session?.credential, initial: true) { AttachmentLoader.shared.clear() }
        // Once per paired store: is the server new enough for this app (§19)?
        .task(id: model.session?.store.map(ObjectIdentifier.init)) { await model.session?.store?.checkCompatibility() }
    }
}

/// Pairing (§2): paste the link `shaft web pair` prints (the Paste button reads the clipboard, which Universal
/// Clipboard fills from the Mac), and the server's own refusal is shown as it says it.
struct PairingView: View {
    let session: SHAFTSession
    @Environment(\.colorScheme) private var colorScheme
    @State private var link = ""
    @State private var pairing = false
    @State private var settings = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Design.Space.s16) {
                ShaftMark()
                    .padding(.top, Design.Space.s24)
                    .padding(.bottom, Design.Space.s8)
                Text("Pair with SHAFT").font(.largeTitle.bold()).foregroundStyle(Color.textPrimary)
                Text("On the Mac, run this and copy the link it prints. It works once, for 10 minutes.")
                    .foregroundStyle(Color.textSecondary)
                Text("shaft web pair --name \(Self.deviceName)")
                    .font(Design.Text.code)
                    .foregroundStyle(Color.textPrimary)
                    .padding(Design.Space.s12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .elevatedBlock()
                    .textSelection(.enabled)
                HStack(alignment: .top, spacing: Design.Space.s8) {
                    TextField("https://…/pair?t=…", text: $link, axis: .vertical)
                        .lineLimit(1...4)
                        .textFieldStyle(.plain)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        #endif
                        .accessibilityLabel("Pairing link")
                        .onSubmit(pair)
                        .padding(.horizontal, Design.Space.s12)
                        .padding(.vertical, Design.Space.s12)
                        .frame(minHeight: 44)
                        .elevatedBlock()
                    // The system's Paste button: it reads the clipboard (Universal Clipboard fills it from the Mac)
                    // without the "allow paste" prompt, and is only enabled when there is text to paste.
                    PasteButton(payloadType: String.self) { strings in
                        if let text = strings.first { link = text.trimmingCharacters(in: .whitespacesAndNewlines) }
                    }
                    .labelStyle(.titleAndIcon)
                    .buttonBorderShape(.capsule)
                    // Its text is the system's white, which the dark accent fails.
                    .tint(colorScheme == .dark ? Color.operatorChip : Color.brand)
                    .frame(minHeight: 44)
                }
                Button(action: pair) {
                    HStack(spacing: Design.Space.s8) {
                        if pairing { ProgressView().controlSize(.small).tint(Color.onAccent) }
                        Text(pairing ? "Pairing…" : "Pair")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(ProminentAction())
                .disabled(link.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || pairing)
                #if os(iOS)
                Button("Try the demo") { session.startDemo() }
                    .buttonStyle(.bordered)
                    .disabled(pairing)
                Text("Explore SHAFT with sample instances. Nothing leaves this device.")
                    .font(.footnote)
                    .foregroundStyle(Color.textSecondary)
                Button("Settings") { settings = true }
                    .foregroundStyle(Color.brand)
                #endif
                if let problem = session.problem {
                    Label(problem, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(Color.danger)
                        .accessibilityAddTraits(.isStaticText)
                }
            }
            .padding(Design.Space.s24)
            .frame(maxWidth: Design.measure)
            .frame(maxWidth: .infinity)
        }
        .background(Color.canvas.ignoresSafeArea())
        #if os(iOS)
        .sheet(isPresented: $settings) { SettingsView(session: session) }
        #endif
        .onChange(of: session.problem) { _, problem in
            if let problem { Announce.say(problem) }
        }
    }

    /// The name the pairing command suggests: what this device is.
    static var deviceName: String {
        #if os(macOS)
        "mac-app"
        #else
        UIDevice.current.userInterfaceIdiom == .pad ? "ipad-app" : "iphone-app"
        #endif
    }

    private func pair() {
        guard !pairing else { return }
        pairing = true
        Task {
            await session.pair(pasted: link)
            pairing = false
        }
    }
}

/// Settings: the server, this device, notifications, and Unpair (§2 `POST /app/v1/unpair`; Forget when the server
/// cannot be reached, then `shaft web unpair` revokes it on the Mac). A sheet on iOS; the Mac's Settings window.
struct SettingsView: View {
    let session: SHAFTSession
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var push: PushStatus?
    @State private var pushProblem: String?
    @State private var unpairing = false
    @State private var forgetting = false
    #if os(macOS)
    @Environment(\.appUpdates) private var updates
    #endif

    var body: some View {
        NavigationStack {
            Form {
                AppearanceSection()
                Section("App") {
                    if let version = AppVersion.label(info: Bundle.main.infoDictionary) {
                        LabeledContent("Version", value: version)
                    }
                    // App Review asks for the privacy policy inside the app as well as on the listing.
                    Link("Privacy policy", destination: Self.privacyPolicy)
                        .foregroundStyle(Color.brand)
                    #if os(macOS)
                    if let updates { AppUpdatesRows(updates: updates) }
                    #endif
                }
                .listRowBackground(Color.elevated)
                if let credential = session.credential {
                    Section("Server") {
                        #if os(iOS)
                        if session.isDemo {
                            Text("Demo (sample data)")
                        } else {
                            serverDetails(credential)
                        }
                        #else
                        serverDetails(credential)
                        #endif
                    }
                    .listRowBackground(Color.elevated)
                    Section("This device") {
                        LabeledContent("Name", value: credential.name)
                        LabeledContent("Device", value: credential.device)
                    }
                    .listRowBackground(Color.elevated)
                }
                if !session.isDemo, let store = session.store, !store.paused.isEmpty {
                    Section {
                        ForEach(store.paused, id: \.name) { instance in
                            HStack {
                                Text(instance.name)
                                Spacer()
                                Button("Resume") { Task { await store.resume(instance.name) } }
                                    .foregroundStyle(Color.brand)
                                    .rowTextButton()
                            }
                        }
                        if let error = store.error {
                            Text(error.message).font(.footnote).foregroundStyle(Color.danger)
                        }
                    } header: {
                        Text("Paused")
                    } footer: {
                        Text("Paused instances are hidden from the sidebar. They still answer if you open their chat.")
                            .foregroundStyle(Color.textSecondary)
                    }
                    .listRowBackground(Color.elevated)
                }
                if session.isPaired {
                    Section("Notifications") {
                        if let push {
                            LabeledContent("Push", value: push.on ? "on (\(push.env ?? "?"))" : "off")
                        }
                        let notes = PushStatus.notes(push, loadProblem: pushProblem, registered: session.pushStatus)
                        ForEach(notes, id: \.self) { note in
                            Text(note).font(.footnote).foregroundStyle(Color.textSecondary)
                        }
                        if push == nil && notes.isEmpty { ProgressView() }
                        if let problem = push?.problem { Text(problem).font(.footnote).foregroundStyle(Color.warning) }
                        if let error = model.pushError { Text(error).font(.footnote).foregroundStyle(Color.warning) }
                    }
                    .listRowBackground(Color.elevated)
                    #if os(iOS)
                    if session.isDemo {
                        Section {
                            Button("Leave the demo") { session.leaveDemo(); dismiss() }
                                .foregroundStyle(Color.brand)
                                .buttonStyle(.plain)
                        }
                        .listRowBackground(Color.elevated)
                    } else {
                        pairingControls
                    }
                    #else
                    pairingControls
                    #endif
                }
                if let problem = session.problem {
                    Text(problem).foregroundStyle(Color.danger)
                }
            }
            .scrollContentBackground(.hidden)
            .background(Color.canvas.ignoresSafeArea())
            .navigationTitle("Settings")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            #else
            // The Mac's Settings window (⌘,) closes as any window does.
            .formStyle(.grouped)
            #endif
            .task {
                guard session.isPaired, !session.isDemo else { return }
                do {
                    push = try await session.store?.client.pushStatus()
                } catch {
                    if let error = APIError.from(error) { pushProblem = error.message }
                }
            }
            .onDisappear {
                if let services = session.store?.services, services.restarting.contains("scheduler"),
                   !services.restarting.contains("api") { services.stop() }
            }
            .confirmationDialog("Unpair this device?", isPresented: $unpairing, titleVisibility: .visible) {
                Button("Unpair", role: .destructive) { Task { await session.unpair() } }
            } message: {
                Text("Its drafts stay here. Pairing again needs a new link from `shaft web pair`.")
            }
            .confirmationDialog("Forget the pairing on this device?", isPresented: $forgetting,
                                titleVisibility: .visible) {
                Button("Forget", role: .destructive) { session.forget() }
            }
        }
    }

    /// The published app's privacy policy (apps/shaft/AppStore/site/privacy.html, served by publish-site.sh).
    static let privacyPolicy = URL(string: "https://dgrassi1984.github.io/shaft-app/privacy.html")!

    @ViewBuilder
    private func serverDetails(_ credential: ServerCredential) -> some View {
        LabeledContent("Address", value: credential.baseURL.absoluteString)
        LabeledContent("Tailnet login", value: credential.login)
        if let version = session.store?.services.serverVersion {
            LabeledContent("Version", value: version)
        }
    }

    @ViewBuilder
    private var pairingControls: some View {
        if let store = session.store {
            ServerSection(store: store.services)
        }
        Section {
            Button("Unpair this device", role: .destructive) { unpairing = true }
                .foregroundStyle(Color.danger)
                .rowTextButton()
            Button("Forget the pairing here only") { forgetting = true }
                .foregroundStyle(Color.brand)
                .rowTextButton()
        } footer: {
            Text("Unpair tells the server; Forget is for when it cannot be reached (then run `shaft web unpair` on the Mac).")
                .foregroundStyle(Color.textSecondary)
        }
        .listRowBackground(Color.elevated)
    }
}

private extension View {
    /// An action that is a form's row: text, as on iOS, where the Mac would draw a tinted pill in the row.
    @ViewBuilder
    func rowTextButton() -> some View {
        #if os(macOS)
        buttonStyle(.plain)
        #else
        self
        #endif
    }
}

/// A failed first load, with recovery available in the chat area on every platform.
struct InstancesLoadFailure: View {
    let error: APIError
    var settings: () -> Void
    @Environment(InstancesStore.self) private var store

    var body: some View {
        MarkMessage("Could not load chats",
                    message: Text("\(error.message)\n\(Text("Trying again every few seconds…"))")) {
            Button("Retry") {
                store.stop()
                store.start()
            }
            .buttonStyle(ProminentAction())
            Button("Settings", action: settings)
        }
    }
}

/// Shared row state slot. Idle reserves alignment without drawing a radio-like ring.
struct ConversationStateMark: View {
    let state: String
    let waiting: String?
    let failed: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            if waiting != nil || state == "waiting" {
                Image(systemName: "clock").foregroundStyle(Color.textSecondary)
            } else if state == "replying" {
                Image(systemName: "circle.fill")
                    .font(.system(size: 7))
                    .foregroundStyle(Color.brand)
                    .symbolEffect(.pulse, isActive: !reduceMotion)
            } else if failed {
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 13)).foregroundStyle(Color.danger)
            } else {
                Color.clear
            }
        }
        .frame(width: 16, height: 16)
        .accessibilityHidden(true)
    }

    static func label(state: String, waiting: String?, failed: Bool) -> String {
        if waiting != nil || state == "waiting" { return "waiting" }
        if state == "replying" { return "replying" }
        return failed ? "reply failed" : "idle"
    }
}

private struct InstanceStateMark: View {
    let row: InstanceRow
    let main: FromRow?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let state = ConversationRowState(row: row, main: main)
        Group {
            if state.waiting != nil || state.state == "waiting" || state.state == "replying" || state.failed {
                ConversationStateMark(state: state.state, waiting: state.waiting, failed: state.failed)
                    .font(.system(size: 13))
                    .scaleEffect(0.85)
            } else if row.state.hasPrefix("in a cycle") || row.state.contains("running") {
                Image(systemName: "circle.fill").font(.system(size: 7)).foregroundStyle(Color.warning)
                    .symbolEffect(.pulse, isActive: !reduceMotion)
            } else if row.state.hasPrefix("sleeping") {
                Image(systemName: "bed.double").font(.system(size: 11)).foregroundStyle(Color.textSecondary)
            } else if row.state.contains("paused") {
                Image(systemName: "pause.circle").font(.system(size: 11)).foregroundStyle(Color.textSecondary)
            } else {
                Color.clear
            }
        }
        .frame(width: SidebarMetrics.state, height: 16)
        .accessibilityHidden(true)
    }
}

private enum SidebarMetrics {
    static let stateGap: CGFloat = 6
    static let nameIndent: CGFloat = state + stateGap
    #if os(macOS)
    static let state: CGFloat = 12
    static let chevron: CGFloat = 14
    static let compose: CGFloat = 20
    static let controlHeight: CGFloat = 24
    static let rowInsets = EdgeInsets(top: 2, leading: 8, bottom: 2, trailing: 8)
    /// A thread row ends where its instance's buttons start, so the times form one column.
    static let threadTrailing: CGFloat = compose + chevron + 8
    #else
    static let state: CGFloat = 14
    static let chevron: CGFloat = 44
    static let compose: CGFloat = 44
    static let controlHeight: CGFloat = 44
    static let rowInsets = EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16)
    /// A thread row has no buttons: reserving the instance row's two 44 pt buttons left an iPad sidebar's thread title
    /// four letters (2026-11-15), so its time sits at the row's end instead.
    static let threadTrailing: CGFloat = 8
    #endif
}

struct InstanceRowView: View {
    let row: InstanceRow
    var main: FromRow? = nil
    var collapsed = false
    var notes = 0
    private var unread: Bool { main?.unread ?? row.unreadMain ?? row.unread }
    private var subline: String {
        RenderedText.plain(PartialMarkdown.closed(ConversationLabels.mainSubline(row, main: main, pendingNotes: notes)))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: SidebarMetrics.stateGap) {
                InstanceStateMark(row: row, main: main)
                Text(row.name)
                    .font(.headline)
                    .fontWeight(unread ? .bold : .semibold)
                    .lineLimit(1)
                    .layoutPriority(1)
                Spacer(minLength: 4)
                #if os(iOS)
                if let last = main?.last ?? row.last {
                    // Never squeezed: the title's layout priority would wrap it one character per line.
                    Text(SidebarStamp.text(last)).font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                        .fixedSize()
                }
                #endif
                if unread { Circle().fill(Color.brand).frame(width: 6, height: 6) }
                if collapsed, let count = row.unreadThreads, count > 0 {
                    Text("\(count)").font(.caption).padding(.horizontal, 6)
                        .background(Color.rowSelected, in: Capsule())
                }
                #if os(macOS)
                if let last = main?.last ?? row.last {
                    Text(SidebarStamp.text(last)).font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                        .fixedSize()
                }
                #endif
            }
            if !subline.isEmpty {
                Text(subline)
                    .font(.subheadline).foregroundStyle(Color.textSecondary)
                    .lineLimit(1)
                    .padding(.leading, SidebarMetrics.nameIndent)
            }
        }
        .foregroundStyle(Color.textPrimary)
        #if os(macOS)
        .frame(minHeight: subline.isEmpty ? 24 : 36)
        #else
        .frame(minHeight: 44)
        #endif
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(SidebarRowLabels.instance(row, main: main, collapsed: collapsed, subline: subline))
    }
}

/// The sidebar's canonical identity: Main is `.from`, or `.current` on a confirmed legacy server.
struct ChatSelection: Hashable {
    var instance: String
    var ref: ChatRef?

    init(_ instance: String, _ ref: ChatRef? = nil) {
        self.instance = instance
        self.ref = ref ?? .from
    }

    /// Keep exactly the ref used by the row tag.
    static func settled(_ instance: String, _ ref: ChatRef) -> ChatSelection {
        ChatSelection(instance, ref)
    }
}

extension InstancesStore {
    /// The conversation `selection` shows now.
    func resolve(_ selection: ChatSelection) -> ChatRef {
        resolve(selection.instance, selection.ref)
    }

    /// Resolve capability or lifecycle changes without changing Main's identity.
    func settle(_ selection: ChatSelection?) -> ChatSelection? {
        guard let selection else { return nil }
        let resolved = resolve(selection)
        if selection.ref == nil, resolved == .current { return nil }
        let settled = ChatSelection.settled(selection.instance, resolved)
        return settled == selection ? nil : settled
    }

    /// The store of the conversation `selection` shows.
    func chat(_ selection: ChatSelection) -> ChatStore {
        chat(selection.instance, resolve(selection))
    }
}

/// Device-local disclosure state, shared with the Mac's visible-row keyboard navigation.
enum SidebarDisclosure {
    static let key = "chat.expandedInstances"
    static func values(_ raw: String) -> [String: Bool] {
        guard let data = raw.data(using: .utf8) else { return [:] }
        return (try? JSONDecoder().decode([String: Bool].self, from: data)) ?? [:]
    }
    static func expanded(_ name: String) -> Bool {
        values(UserDefaults.standard.string(forKey: key) ?? "")[name] ?? false
    }
    static func set(_ name: String, expanded: Bool) {
        var values = values(UserDefaults.standard.string(forKey: key) ?? "")
        values[name] = expanded
        if let data = try? JSONEncoder().encode(values), let raw = String(data: data, encoding: .utf8) {
            UserDefaults.standard.set(raw, forKey: key)
        }
    }
}

struct InstancesList: View {
    var switcher = false
    @Binding var selection: ChatSelection?
    var settings: () -> Void
    @Environment(InstancesStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AppStorage(SidebarDisclosure.key) private var expanded = ""
    @State private var closing: ChatSelection?
    @State private var renaming: ChatSelection?
    @State private var newTitle = ""
    @State private var hovering: String?
    @FocusState private var composeFocus: String?

    var body: some View {
        List(selection: $selection) {
            if store.loaded, let error = store.error {
                Label(error.message, systemImage: "exclamationmark.triangle")
                    .font(.footnote).foregroundStyle(Color.warning)
            }
            #if os(macOS)
            Section {
                instanceRows
            } header: {
                Text("Chats").font(.caption.weight(.semibold)).foregroundStyle(Color.textSecondary)
            }
            #else
            instanceRows
            #endif
            if store.loaded && store.instances.isEmpty {
                Text("No instances yet. They are created from the terminal with `shaft new <name>`.")
                    .foregroundStyle(Color.textSecondary)
            }
        }
        .tint(Color.brand)
        #if os(iOS)
        .scrollContentBackground(.hidden)
        .listSectionSpacing(0)
        .background(Color.elevated)
        #else
        .listStyle(.sidebar)
        .environment(\.defaultMinListRowHeight, 28)
        .background(QuietListSelection())
        #endif
        .overlay { if !store.loaded && store.error == nil { ProgressView() } }
        .refreshable {
            await store.refresh()
            await store.refreshMovedThreads(all: true)
        }
        .onChange(of: selection) { _, chosen in
            if chosen != nil { TransitionClock.beginIfIdle("open chat") }
            if let chosen { SidebarDisclosure.set(chosen.instance, expanded: true) }
        }
        .navigationTitle(title)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button(action: settings) { Image(systemName: "gearshape") }.accessibilityLabel("Settings")
            }
        }
        .confirmationDialog(closeTitle, isPresented: Binding(get: { closing != nil }, set: { if !$0 { closing = nil } }),
                            titleVisibility: .visible) {
            Button("End it", role: .destructive) {
                if let closing, let ref = closing.ref {
                    Task { await store.chat(closing.instance, ref).end() }
                }
                closing = nil
            }
            Button("Cancel", role: .cancel) { closing = nil }
        } message: {
            Text("It writes up its journal. Its other threads stay open.")
        }
        .alert("Rename the thread", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("Title", text: $newTitle)
            Button("Rename") {
                if let renaming, let ref = renaming.ref {
                    Task { await store.chat(renaming.instance, ref).rename(newTitle) }
                }
            }
            .disabled(newTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            Button("Cancel", role: .cancel) {}
        }
    }

    private var instanceRows: some View {
        ForEach(store.instances) { row in
            #if os(iOS)
            Section { instanceContents(row) }
            #else
            instanceContents(row)
            #endif
        }
    }

    @ViewBuilder private func instanceContents(_ row: InstanceRow) -> some View {
        instanceRow(row)
        if isExpanded(row.name), let threads = store.threads(row.name) {
            threadRows(row.name, threads)
        }
    }

    private var closeTitle: String { "End the conversation with \(closing?.instance ?? "")?" }

    private func isExpanded(_ name: String) -> Bool { SidebarDisclosure.values(expanded)[name] ?? false }

    private func composeVisible(_ name: String) -> Bool {
        selection?.instance == name || hovering == name || composeFocus == name
    }

    private func instanceRow(_ row: InstanceRow) -> some View {
        let tag = ChatSelection(row.name, store.defaultRef(row.name))
        let hasThreads = store.threads(row.name)?.threads.isEmpty == false
        return HStack(spacing: 4) {
            InstanceRowView(row: row, main: store.threads(row.name)?.from, collapsed: !isExpanded(row.name),
                            notes: store.pendingNotes[row.name]?.count ?? 0)
                .contentShape(Rectangle())
                .onTapGesture {
                    SidebarDisclosure.set(row.name, expanded: true)
                    selection = tag
                }
            #if os(macOS)
            composeButton(row.name)
                .frame(width: SidebarMetrics.compose, height: 24)
                .opacity(composeVisible(row.name) ? 1 : 0)
                .allowsHitTesting(composeVisible(row.name))
                .accessibilityHidden(!composeVisible(row.name))
            #else
            Group {
                if composeVisible(row.name) { composeButton(row.name) }
                else { Color.clear.accessibilityHidden(true) }
            }
            .frame(width: SidebarMetrics.compose, height: SidebarMetrics.controlHeight)
            #endif
            if hasThreads {
                Button { SidebarDisclosure.set(row.name, expanded: !isExpanded(row.name)) } label: {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Color.textSecondary)
                        .rotationEffect(.degrees(isExpanded(row.name) ? 90 : 0))
                        .animation(reduceMotion ? nil : .easeInOut(duration: 0.2), value: isExpanded(row.name))
                        .frame(width: SidebarMetrics.chevron, height: SidebarMetrics.controlHeight)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.borderless)
                .accessibilityLabel(SidebarRowLabels.disclosure(row.name, expanded: isExpanded(row.name)))
            } else {
                Color.clear.frame(width: SidebarMetrics.chevron, height: SidebarMetrics.controlHeight)
                    .accessibilityHidden(true)
            }
        }
        .tag(tag)
        .onHover { hovering = $0 ? row.name : nil }
        .listRowInsets(SidebarMetrics.rowInsets)
        .listRowSeparator(.hidden)
        .listRowBackground(rowFill(selected: selection == tag))
        .contextMenu {
            Button("New thread") { newThread(row.name) }.disabled(store.threads(row.name) == nil)
            moveInstanceMenu(row.name)
        }
        .accessibilityAction(named: "New thread") { newThread(row.name) }
        .accessibilityActions {
            moveInstanceMenu(row.name)
        }
        #if os(iOS)
        .swipeActions(edge: .leading, allowsFullSwipe: false) {
            Button { newThread(row.name) } label: { Label("New thread", systemImage: "square.and.pencil") }
                .tint(Color.brand).disabled(store.threads(row.name) == nil)
        }
        #endif
    }

    private func composeButton(_ name: String) -> some View {
        Button { newThread(name) } label: {
            NewThreadIcon(starting: store.creatingThread.contains(name))
                #if os(iOS)
                .frame(width: SidebarMetrics.compose, height: SidebarMetrics.controlHeight)
                .contentShape(Rectangle())
                #endif
        }
        .buttonStyle(.borderless)
        .focused($composeFocus, equals: name)
        .disabled(store.threads(name) == nil || store.creatingThread.contains(name))
        .accessibilityLabel("New thread with \(name)")
    }

    private func threadRows(_ name: String, _ threads: ThreadsResponse) -> some View {
        ForEach(threads.threads) { thread in
            threadRow(name, thread)
        }
        .onMove { source, destination in
            moveRows(threads.threads.map(\.id), source: source, destination: destination) { id, after in
                Task { await store.moveThread(name, id: id, after: after) }
            }
        }
    }

    private func threadRow(_ name: String, _ thread: ThreadRow) -> some View {
        let tag = ChatSelection(name, .thread(thread.id))
        return ThreadRowView(thread: thread)
            .tag(tag)
            .help(thread.title)
            .listRowInsets(SidebarMetrics.rowInsets)
            .listRowSeparator(.hidden)
            .listRowBackground(rowFill(selected: selection == tag))
            .contextMenu {
                Text(thread.title)
                moveThreadMenu(name, thread.id)
                Button("Rename…") { newTitle = thread.title; renaming = tag }
                Button("Close Thread", role: .destructive) { closeThread(name, thread.id) }
            } preview: {
                Text(thread.title).padding().fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityActions { moveThreadMenu(name, thread.id) }
            .accessibilityAction(named: "Rename") { newTitle = thread.title; renaming = tag }
            .accessibilityAction(named: "Close Thread") { closeThread(name, thread.id) }
            #if os(iOS)
            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                Button("Close", role: .destructive) { closeThread(name, thread.id) }
            }
            #endif
    }

    @ViewBuilder private func moveInstanceMenu(_ name: String) -> some View {
        let moves = SidebarMoveAvailability(id: name, ids: store.instances.map(\.name))
        if moves.up {
            Button("Move to Top") { moveInstance(name, offset: nil) }
            Button("Move Up") { moveInstance(name, offset: -1) }
        }
        if moves.down { Button("Move Down") { moveInstance(name, offset: 1) } }
    }

    @ViewBuilder private func moveThreadMenu(_ name: String, _ id: String) -> some View {
        let moves = SidebarMoveAvailability(id: id, ids: store.threads(name)?.threads.map(\.id) ?? [])
        if moves.up {
            Button("Move to Top") { moveThread(name, id, offset: nil) }
            Button("Move Up") { moveThread(name, id, offset: -1) }
        }
        if moves.down { Button("Move Down") { moveThread(name, id, offset: 1) } }
    }

    private func newThread(_ name: String) {
        Task { await store.newThread(name) { selection = ChatSelection(name, $0) } }
    }

    private func closeThread(_ name: String, _ id: String) {
        closing = ChatSelection(name, .thread(id))
    }

    private func moveThread(_ name: String, _ id: String, offset: Int?) {
        let ids = store.threads(name)?.threads.map(\.id) ?? []
        moveItem(id, ids: ids, offset: offset) { id, after in
            Task { await store.moveThread(name, id: id, after: after) }
        }
    }

    private func moveInstance(_ name: String, offset: Int?) {
        moveItem(name, ids: store.instances.map(\.name), offset: offset) { id, after in
            Task { await store.moveInstance(id, after: after) }
        }
    }

    private func moveItem(_ id: String, ids: [String], offset: Int?, apply: (String, String?) -> Void) {
        guard let index = ids.firstIndex(of: id) else { return }
        let target = offset.map { index + $0 } ?? 0
        guard ids.indices.contains(target), target != index else { return }
        let remaining = ids.filter { $0 != id }
        apply(id, target == 0 ? nil : remaining[target - 1])
        selectionHaptic()
    }

    private func moveRows(_ ids: [String], source: IndexSet, destination: Int, apply: (String, String?) -> Void) {
        guard source.count == 1, let index = source.first, ids.indices.contains(index) else { return }
        var reordered = ids
        let id = reordered.remove(at: index)
        let target = destination > index ? destination - 1 : destination
        guard target >= 0, target <= reordered.count, target != index else { return }
        reordered.insert(id, at: target)
        apply(id, target == 0 ? nil : reordered[target - 1])
        selectionHaptic()
    }

    private func selectionHaptic() {
        #if os(iOS)
        UISelectionFeedbackGenerator().selectionChanged()
        #endif
    }

    private var title: String { store.badge > 0 ? "SHAFT (\(store.badge))" : "SHAFT" }

    private func rowFill(selected: Bool) -> some View {
        #if os(macOS)
        RoundedRectangle(cornerRadius: Design.Radius.pill, style: .continuous)
            .fill(selected ? Color.rowSelected : Color.clear)
        #else
        RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous)
            .fill(selected ? Color.rowSelected : Color.clear)
            .padding(.horizontal, Design.Space.s8)
        #endif
    }
}

struct ThreadRowView: View {
    let thread: ThreadRow

    var body: some View {
        HStack(spacing: SidebarMetrics.stateGap) {
            ConversationStateMark(state: thread.state, waiting: thread.waiting, failed: thread.failed == true)
                .font(.system(size: 13))
                .scaleEffect(0.85)
                .frame(width: SidebarMetrics.state)
            Text(thread.title).font(.subheadline).fontWeight(thread.unread ? .bold : .medium).lineLimit(1)
                .layoutPriority(1)
            Spacer(minLength: 4)
            #if os(macOS)
            if thread.unread { Circle().fill(Color.brand).frame(width: 6, height: 6) }
            #endif
            if let last = thread.last {
                // Never squeezed, on iOS too: the title's layout priority wrapped it one character per line on iPad.
                Text(SidebarStamp.text(last)).font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                    .fixedSize()
            }
            #if os(iOS)
            if thread.unread { Circle().fill(Color.brand).frame(width: 6, height: 6) }
            #endif
        }
        .foregroundStyle(Color.textPrimary)
        .padding(.leading, SidebarMetrics.nameIndent)
        .padding(.trailing, SidebarMetrics.threadTrailing)
        #if os(macOS)
        .frame(minHeight: 24)
        #else
        .frame(minHeight: 44)
        #endif
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(SidebarRowLabels.thread(thread))
    }
}

#if os(macOS)
/// A Mac sidebar paints the selected row itself, in the accent, with its text turned to the selection's colours, and no
/// SwiftUI modifier turns that off: its table keeps the selection and the arrow keys, and `rowFill` is all that shows.
private struct QuietListSelection: NSViewRepresentable {
    func makeNSView(context: Context) -> Finder { Finder() }
    func updateNSView(_ view: Finder, context: Context) { view.apply() }

    final class Finder: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            apply()
            DispatchQueue.main.async { [weak self] in self?.apply() }       // the table may be added after this view
        }

        func apply() {
            var ancestor = superview
            while let view = ancestor {
                if let table = Self.table(in: view) {
                    table.selectionHighlightStyle = .none
                    return
                }
                ancestor = view.superview
            }
        }

        private static func table(in view: NSView) -> NSTableView? {
            if let table = view as? NSTableView { return table }
            for child in view.subviews {
                if let table = table(in: child) { return table }
            }
            return nil
        }
    }
}
#endif
