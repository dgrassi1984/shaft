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
                    paired(session, store)
                        .id(ObjectIdentifier(store))
                        .environment(store)
                        .onAppear { if scenePhase == .active { store.start() } }
                        .onChange(of: scenePhase) { _, phase in
                            if phase == .active { store.start() } else { store.stop() }
                        }
                } else {
                    PairingView(session: session)
                }
            } else {
                MarkMessage("SHAFT could not start", message: Text(model.startupError ?? "No reason was given."))
            }
        }
        .environment(model)
        .onChange(of: model.session?.credential, initial: true) { AttachmentLoader.shared.clear() }
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

    var body: some View {
        NavigationStack {
            Form {
                AppearanceSection()
                if let version = AppVersion.label(info: Bundle.main.infoDictionary) {
                    Section("App") {
                        LabeledContent("Version", value: version)
                    }
                    .listRowBackground(Color.elevated)
                }
                if let credential = session.credential {
                    Section("Server") {
                        LabeledContent("Address", value: credential.baseURL.absoluteString)
                        LabeledContent("Tailnet login", value: credential.login)
                        if let version = session.store?.services.serverVersion {
                            LabeledContent("Version", value: version)
                        }
                    }
                    .listRowBackground(Color.elevated)
                    Section("This device") {
                        LabeledContent("Name", value: credential.name)
                        LabeledContent("Device", value: credential.device)
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
                guard session.isPaired else { return }
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

/// A chat in the list: its name and time, its state, its last line. The unread dot sits in the margin, so read and
/// unread rows align; a replying row, and one in a cycle, pulses its symbol.
struct InstanceRowView: View {
    let row: InstanceRow
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: Design.Space.s8) {
                Text(row.name)
                    .font(.headline)
                    .fontWeight(row.unread ? .bold : .semibold)
                    .foregroundStyle(Color.textPrimary)
                    .overlay(alignment: .leading) {
                        if row.unread {
                            Circle()
                                .fill(Color.brand)
                                .frame(width: Design.Space.s8, height: Design.Space.s8)
                                .offset(x: -Design.Space.s12)
                                .accessibilityHidden(true)
                        }
                    }
                Spacer(minLength: Design.Space.s4)
                if let last = row.last {
                    Text(Stamp.short(last)).font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                }
            }
            HStack(spacing: Design.Space.s4) {
                Image(systemName: StateIcon.symbol(row.state, replying: row.replying))
                    .symbolEffect(.pulse, isActive: StateIcon.pulses(row.state, replying: row.replying) && !reduceMotion)
                    .accessibilityHidden(true)
                Text(row.replying ? String(localized: "replying…") : row.state)
                if !row.since.isEmpty && !row.replying { Text("· \(row.since)") }
            }
            .font(.caption)
            .foregroundStyle(row.replying ? Color.brand : Color.textSecondary)
            if !row.line.isEmpty {
                Text(RenderedText.plain(PartialMarkdown.closed(row.line)))
                    .font(.subheadline)
                    .foregroundStyle(Color.textSecondary)
                    .lineLimit(2)
            }
        }
        .padding(.vertical, Design.Space.s4)
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
        .accessibilityValue(row.unread ? String(localized: "unread") : "")
    }
}

/// What the chat list selects: an instance, and the conversation of it. `ref` nil is the instance's default (its most
/// recently active thread, or its one chat on a server before threads), resolved by `InstancesStore.resolve`.
struct ChatSelection: Hashable {
    var instance: String
    var ref: ChatRef?

    init(_ instance: String, _ ref: ChatRef? = nil) {
        self.instance = instance
        self.ref = ref
    }

    /// The selection for `ref` once resolved: a resolved current chat is the instance's own row (nil).
    static func settled(_ instance: String, _ ref: ChatRef) -> ChatSelection {
        ChatSelection(instance, ref == .current ? nil : ref)
    }
}

extension InstancesStore {
    /// The conversation `selection` shows now.
    func resolve(_ selection: ChatSelection) -> ChatRef {
        resolve(selection.instance, selection.ref)
    }

    /// `selection` as it should be held once the threads are known: the default named by its thread, a thread that
    /// ended given way to the default. Nil when it is right as it is.
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

/// The chats, in the rail's order, with the list's own failure. Under each instance with threads, its open threads,
/// "From <instance>" and New thread (§3.6). On an iPhone and iPad it sits on `elevated`.
struct InstancesList: View {
    /// The phone's sheet of chats.
    var switcher = false
    @Binding var selection: ChatSelection?
    var settings: () -> Void
    @Environment(InstancesStore.self) private var store
    /// The thread whose title is being changed (Rename… on its row).
    @State private var renaming: ChatSelection?
    @State private var newTitle = ""

    var body: some View {
        List(selection: $selection) {
            // A failed first load says so once, in the chat area (`InstancesLoadFailure`).
            if store.loaded, let error = store.error {
                Label(error.message, systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(Color.warning)
            }
            ForEach(store.instances) { row in
                InstanceRowView(row: row)
                    .tag(ChatSelection(row.name))
                    .listRowSeparator(.hidden)
                    .listRowBackground(rowFill(selected: selection == ChatSelection(row.name)))
                if let threads = store.threads(row.name) {
                    threadRows(row.name, threads)
                }
            }
            if store.loaded && store.instances.isEmpty {
                Text("No instances yet. They are created from the terminal with `shaft new <name>`.")
                    .foregroundStyle(Color.textSecondary)
            }
        }
        #if os(iOS)
        .scrollContentBackground(.hidden)
        .background(Color.elevated)
        #endif
        // With a keyboard attached the system rings the row in focus in the tint: the fill says where it is already.
        .focusEffectDisabled()
        .tint(.clear)
        #if os(macOS)
        .background(QuietListSelection())
        #endif
        .overlay { if !store.loaded && store.error == nil { ProgressView() } }
        .refreshable {
            await store.refresh()
            await store.refreshMovedThreads(all: true)
        }
        .navigationTitle(title)
        .toolbar {
            #if os(iOS)
            // The sidebar's bar centres an inline title in what its buttons leave: it sat against them, off the rows.
            if !switcher {
                ToolbarItem(placement: .topBarLeading) {
                    Text(title)
                        .font(.headline)
                        .foregroundStyle(Color.textPrimary)
                        .fixedSize()
                        .padding(.leading, Design.Space.s8)
                }
                .sharedBackgroundVisibility(.hidden)
            }
            #endif
            ToolbarItem(placement: .primaryAction) {
                Button(action: settings) { Image(systemName: "gearshape") }.accessibilityLabel("Settings")
            }
        }
        #if os(iOS)
        .toolbar(removing: switcher ? nil : .title)
        #endif
        .alert("Rename the thread", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("Title", text: $newTitle)
            Button("Rename") {
                if let renaming { rename(renaming, to: newTitle) }
            }
            .disabled(newTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            Button("Cancel", role: .cancel) {}
        }
    }

    /// An instance's open threads, its From feed and New thread, under its row.
    @ViewBuilder
    private func threadRows(_ name: String, _ threads: ThreadsResponse) -> some View {
        ForEach(threads.threads) { thread in
            let tag = ChatSelection(name, .thread(thread.id))
            ThreadRowView(thread: thread)
                .tag(tag)
                .listRowSeparator(.hidden)
                .listRowBackground(rowFill(selected: selection == tag))
                .contextMenu {
                    Button {
                        newTitle = thread.title
                        renaming = tag
                    } label: {
                        Label("Rename…", systemImage: "pencil")
                    }
                }
        }
        let from = ChatSelection(name, .from)
        FromRowView(instance: name, from: threads.from)
            .tag(from)
            .listRowSeparator(.hidden)
            .listRowBackground(rowFill(selected: selection == from))
        Button {
            Task {
                if let ref = await store.newThread(name) { selection = ChatSelection(name, ref) }
            }
        } label: {
            Label("New thread", systemImage: "plus")
                .font(.subheadline)
                .foregroundStyle(Color.brand)
                .padding(.leading, ThreadRowView.indent)
                .frame(minHeight: 44, alignment: .leading)
        }
        .buttonStyle(.plain)
        .listRowSeparator(.hidden)
        .accessibilityHint("Starts another conversation with \(name)")
    }

    private func rename(_ thread: ChatSelection, to title: String) {
        guard let ref = thread.ref else { return }
        Task { await store.chat(thread.instance, ref).rename(title) }
    }

    private var title: String { store.badge > 0 ? "SHAFT (\(store.badge))" : "SHAFT" }

    /// The selected row's quiet fill, in place of the system's accent selection (2026-11-04).
    private func rowFill(selected: Bool) -> some View {
        RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous)
            .fill(selected ? Color.rowSelected : Color.clear)
            .padding(.horizontal, Design.Space.s8)
    }
}

/// An open thread under its instance: the title and time, replying or waiting, its last line. The unread dot sits in
/// the margin, as an instance's does.
struct ThreadRowView: View {
    let thread: ThreadRow
    /// How far a thread sits in from its instance.
    static let indent = Design.Space.s16
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: Design.Space.s8) {
                Text(thread.title)
                    .font(.subheadline)
                    .fontWeight(thread.unread ? .bold : .medium)
                    .foregroundStyle(Color.textPrimary)
                    .lineLimit(1)
                    .overlay(alignment: .leading) { UnreadDot(unread: thread.unread) }
                Spacer(minLength: Design.Space.s4)
                if let last = thread.last {
                    Text(Stamp.short(last)).font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                }
            }
            if thread.replying || thread.waiting != nil {
                HStack(spacing: Design.Space.s4) {
                    Image(systemName: "ellipsis.bubble")
                        .symbolEffect(.pulse, isActive: thread.waiting == nil && !reduceMotion)
                        .accessibilityHidden(true)
                    Text(thread.waiting ?? String(localized: "replying…")).lineLimit(1)
                }
                .font(.caption)
                .foregroundStyle(thread.waiting == nil ? Color.brand : Color.textSecondary)
            } else if !thread.line.isEmpty {
                Text(RenderedText.plain(PartialMarkdown.closed(thread.line)))
                    .font(.caption)
                    .foregroundStyle(Color.textSecondary)
                    .lineLimit(1)
            }
        }
        .padding(.leading, Self.indent)
        .padding(.vertical, 2)
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
        .accessibilityValue(thread.unread ? String(localized: "unread") : "")
    }
}

/// "From <instance>": what it sent from its cycles, and where to write for its next one.
struct FromRowView: View {
    let instance: String
    let from: FromRow

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: Design.Space.s8) {
                Label("From \(instance)", systemImage: "tray")
                    .font(.subheadline)
                    .fontWeight(from.unread ? .bold : .medium)
                    .foregroundStyle(Color.textPrimary)
                    .lineLimit(1)
                    .overlay(alignment: .leading) { UnreadDot(unread: from.unread) }
                Spacer(minLength: Design.Space.s4)
                if let last = from.last {
                    Text(Stamp.short(last)).font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                }
            }
            if !from.line.isEmpty {
                Text(RenderedText.plain(PartialMarkdown.closed(from.line)))
                    .font(.caption)
                    .foregroundStyle(Color.textSecondary)
                    .lineLimit(1)
            }
        }
        .padding(.leading, ThreadRowView.indent)
        .padding(.vertical, 2)
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
        .accessibilityValue(from.unread ? String(localized: "unread") : "")
    }
}

/// The unread dot in a row's margin.
private struct UnreadDot: View {
    let unread: Bool

    var body: some View {
        if unread {
            Circle()
                .fill(Color.brand)
                .frame(width: Design.Space.s8, height: Design.Space.s8)
                .offset(x: -Design.Space.s12)
                .accessibilityHidden(true)
        }
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
