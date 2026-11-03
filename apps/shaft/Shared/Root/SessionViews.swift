import SHAFTKit
import SwiftUI

/// What every platform shell shows first: why SHAFT could not start, the pairing screen, or — paired — `paired` with
/// the instances store in the environment. It keeps the app's badge on the server's count (§12) and polls the chat
/// list while the app is in front.
struct SessionRoot<Paired: View>: View {
    let model: AppModel
    @ViewBuilder var paired: (SHAFTSession, InstancesStore) -> Paired
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if let session = model.session {
                if let store = session.store {
                    paired(session, store)
                        .environment(store)
                        .onAppear { if scenePhase == .active { store.start() } }
                        .onChange(of: scenePhase) { _, phase in
                            if phase == .active { store.start() } else { store.stop() }
                        }
                        .onChange(of: store.badge) { _, badge in model.setBadge(badge) }
                } else {
                    PairingView(session: session)
                }
            } else {
                ContentUnavailableView("SHAFT could not start", systemImage: "exclamationmark.octagon",
                                       description: Text(model.startupError ?? "No reason was given."))
            }
        }
        .environment(model)
    }
}

/// Pairing (§2): paste the link `shaft web pair` prints (the Paste button reads the clipboard, which Universal
/// Clipboard fills from the Mac), and the server's own refusal is shown as it says it.
struct PairingView: View {
    let session: SHAFTSession
    @State private var link = ""
    @State private var pairing = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Image(systemName: "link.badge.plus").font(.system(size: 44)).foregroundStyle(Color.accentColor)
                    .accessibilityHidden(true)
                Text("Pair with SHAFT").font(.largeTitle.bold())
                Text("On the Mac, run this and copy the link it prints. It works once, for 10 minutes.")
                    .foregroundStyle(.secondary)
                Text("shaft web pair --name iphone-app")
                    .font(.callout.monospaced())
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                    .textSelection(.enabled)
                HStack(spacing: 8) {
                    TextField("https://…/pair?t=…", text: $link, axis: .vertical)
                        .lineLimit(1...4)
                        .textFieldStyle(.roundedBorder)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        #endif
                        .accessibilityLabel("Pairing link")
                        .onSubmit(pair)
                    // The system's Paste button: it reads the clipboard (Universal Clipboard fills it from the Mac)
                    // without the "allow paste" prompt, and is only enabled when there is text to paste.
                    PasteButton(payloadType: String.self) { strings in
                        if let text = strings.first { link = text.trimmingCharacters(in: .whitespacesAndNewlines) }
                    }
                    .labelStyle(.titleAndIcon)
                    .frame(minHeight: 44)
                }
                Button(action: pair) {
                    HStack {
                        if pairing { ProgressView().controlSize(.small) }
                        Text(pairing ? "Pairing…" : "Pair").frame(maxWidth: .infinity)
                    }
                    .frame(minHeight: 44)
                }
                .buttonStyle(.borderedProminent)
                .disabled(link.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || pairing)
                if let problem = session.problem {
                    Label(problem, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.red)
                        .accessibilityAddTraits(.isStaticText)
                }
            }
            .padding(24)
            .frame(maxWidth: 560)
            .frame(maxWidth: .infinity)
        }
        .onChange(of: session.problem) { _, problem in
            if let problem { Announce.say(problem) }
        }
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
/// cannot be reached, then `shaft web unpair` revokes it on the Mac).
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
                if let credential = session.credential {
                    Section("Server") {
                        LabeledContent("Address", value: credential.baseURL.absoluteString)
                        LabeledContent("Tailnet login", value: credential.login)
                    }
                    Section("This device") {
                        LabeledContent("Name", value: credential.name)
                        LabeledContent("Device", value: credential.device)
                    }
                }
                Section("Notifications") {
                    if let push {
                        LabeledContent("Push", value: push.on ? "on (\(push.env ?? "?"))" : "off")
                        if !push.reason.isEmpty { Text(push.reason).font(.footnote).foregroundStyle(.secondary) }
                        if let problem = push.problem { Text(problem).font(.footnote).foregroundStyle(.orange) }
                    } else if let pushProblem {
                        Text(pushProblem).font(.footnote).foregroundStyle(.secondary)
                    } else {
                        ProgressView()
                    }
                    if let said = session.pushStatus { Text(said).font(.footnote).foregroundStyle(.secondary) }
                    if let error = model.pushError { Text(error).font(.footnote).foregroundStyle(.orange) }
                }
                Section {
                    Button("Unpair this device", role: .destructive) { unpairing = true }
                    Button("Forget the pairing here only") { forgetting = true }
                } footer: {
                    Text("Unpair tells the server; Forget is for when it cannot be reached (then run `shaft web unpair` on the Mac).")
                }
                if let problem = session.problem {
                    Text(problem).foregroundStyle(.red)
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .task {
                do {
                    push = try await session.store?.client.pushStatus()
                } catch {
                    pushProblem = APIError.from(error).message
                }
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

/// A chat in the list: its name, state, last line, the unread mark and whether it is replying.
struct InstanceRowView: View {
    let row: InstanceRow
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Circle()
                .fill(row.unread ? Color.accentColor : Color.clear)
                .frame(width: 9, height: 9)
                .padding(.top, 6)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(row.name).font(.headline).fontWeight(row.unread ? .bold : .semibold)
                    Spacer(minLength: 4)
                    if let last = row.last {
                        Text(Stamp.short(last)).font(.caption2).foregroundStyle(.secondary)
                    }
                }
                HStack(spacing: 4) {
                    Image(systemName: StateIcon.symbol(row.state, replying: row.replying))
                        .symbolEffect(.pulse, isActive: row.replying && !reduceMotion)
                        .accessibilityHidden(true)
                    Text(row.replying ? String(localized: "replying…") : row.state)
                    if !row.since.isEmpty && !row.replying { Text("· \(row.since)") }
                }
                .font(.caption)
                .foregroundStyle(row.replying ? Color.accentColor : Color.secondary)
                if !row.line.isEmpty {
                    Text(row.line).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                }
            }
        }
        .padding(.vertical, 4)
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
        .accessibilityValue(row.unread ? String(localized: "unread") : "")
    }
}

/// The chats, in the rail's order, with the list's own failure.
struct InstancesList: View {
    @Binding var selection: String?
    var settings: () -> Void
    @Environment(InstancesStore.self) private var store

    var body: some View {
        List(selection: $selection) {
            if let error = store.error {
                Label(error.message, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(.orange)
            }
            ForEach(store.instances) { row in
                InstanceRowView(row: row).tag(row.name)
            }
            if store.loaded && store.instances.isEmpty {
                Text("No instances yet. They are created from the terminal with `shaft new <name>`.")
                    .foregroundStyle(.secondary)
            }
        }
        .overlay { if !store.loaded && store.error == nil { ProgressView() } }
        .refreshable { await store.refresh() }
        .navigationTitle(store.badge > 0 ? "SHAFT (\(store.badge))" : "SHAFT")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button(action: settings) { Image(systemName: "gearshape") }.accessibilityLabel("Settings")
            }
        }
    }
}
