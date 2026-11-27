import SHAFTKit
import SwiftUI

struct InstanceSettingsView: View {
    let store: InstanceSettingsStore
    let services: ServicesStore
    @Environment(InstancesStore.self) private var instances
    @Environment(\.dismiss) private var dismiss
    @State private var confirmingPause = false
    @State private var pausing = false

    var body: some View {
        NavigationStack {
            Group {
                if store.unsupported {
                    VStack(spacing: Design.Space.s8) {
                        Text("This needs a newer SHAFT server.")
                        Text("Update the server to change settings here.").foregroundStyle(Color.textSecondary)
                    }
                } else if let error = store.loadError {
                    VStack(spacing: Design.Space.s8) {
                        Text(error.message).foregroundStyle(Color.warning)
                        Button("Try again") { Task { await store.load() } }.foregroundStyle(Color.brand)
                    }
                } else if let settings = store.settings {
                    settingsForm(settings)
                } else {
                    ProgressView("Loading settings…")
                }
            }
            .navigationTitle(store.instance)
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .background(Color.canvas.ignoresSafeArea())
        }
        .task { await store.load() }
        .onDisappear {
            // stop() cancels every watcher, including the API's bounded confirmation watch.
            if services.restarting.contains("scheduler"), !services.restarting.contains("api") { services.stop() }
        }
        .confirmationDialog("Pause \(store.instance)?", isPresented: $confirmingPause, titleVisibility: .visible) {
            Button("Pause") { setPaused(true) }
        } message: {
            Text("A running cycle is asked to stop at its next turn.")
        }
    }

    private func settingsForm(_ settings: InstanceSettings) -> some View {
        Form {
            // Only when there is a message: an always-present empty Section here made macOS draw the next
            // section's header ("Model") as a row inside its box instead of above it like the others.
            if let message = store.saveError ?? store.notice {
                Section {} footer: {
                    Text(message).foregroundStyle(store.saveError == nil ? Color.textSecondary : Color.warning)
                }
            }
            Section("Model") {
                savingRow(.model) {
                    Picker("Model", selection: stringBinding(.model, read: { store.settings?.model.value ?? "" })) {
                        ForEach(settings.model.choices, id: \.model) { choice in
                            Text(choice.label).tag(choice.model)
                        }
                    }
                }
                if let choices = settings.effort.choices {
                    savingRow(.effort) {
                        Picker("Effort", selection: Binding<String?>(
                            get: { store.settings?.effort.value },
                            set: { value in Task { await store.save(.effort, value: value.map(SettingValue.string) ?? .null) } }
                        )) {
                            if let value = settings.effort.value, !choices.contains(value) {
                                Text(value).tag(Optional(value))
                            }
                            if settings.effort.value == nil { Text("Not set").tag(String?.none) }
                            ForEach(choices, id: \.self) { Text($0).tag(Optional($0)) }
                        }
                    }
                } else {
                    // Unknown levels (the model's listing could not be read): show the value, never hide the row
                    VStack(alignment: .leading, spacing: Design.Space.s4) {
                        LabeledContent("Effort", value: settings.effort.value ?? "Not set")
                        Text("Effort choices are unavailable.").font(.footnote).foregroundStyle(Color.textSecondary)
                    }
                }
            }
            .listRowBackground(Color.elevated)
            VoiceSettingsSection(store: store, client: instances.client, settings: settings)
            if SettingField.watches.contains(where: { settings.watch($0) != nil }) {
                Section {
                    ForEach(SettingField.watches, id: \.self) { field in
                        if let watch = settings.watch(field) {
                            savingRow(field) {
                                Picker(Self.watchLabel(field), selection: stringBinding(field, read: {
                                    store.settings?.watch(field)?.value ?? "off"
                                })) {
                                    if !watch.choices.contains(watch.value) { Text(watch.value).tag(watch.value) }
                                    ForEach(watch.choices, id: \.self) { Text(Self.watchChoice($0)).tag($0) }
                                }
                            }
                        }
                    }
                } header: { Text("Watches") } footer: {
                    Text("What \(store.instance) hears about by itself on what you granted it: nothing, a note in its "
                         + "inbox, or a note that also wakes it. A meeting is sent once, when its transcript is first ready.")
                        .foregroundStyle(Color.textSecondary)
                }
                .listRowBackground(Color.elevated)
            }
            InstanceAccessSection(client: instances.client, instance: store.instance)
            Section {
                HStack {
                    Toggle("Paused", isOn: Binding(
                        get: { store.settings?.paused ?? false },
                        set: { if $0 { confirmingPause = true } else { setPaused(false) } }
                    ))
                    if pausing { ProgressView().controlSize(.small) }
                }
                SettingTextRow(store: store, field: .wakeInterval, label: "Wake interval (minutes)")
            } header: { Text("Autonomy") } footer: {
                Text(wakeFooter(settings)).foregroundStyle(Color.textSecondary)
            }
            .listRowBackground(Color.elevated)
            Section {
                SettingTextRow(store: store, field: .cloudCap, label: "Daily cloud cap ($)")
                LabeledContent("Spent today", value: String(format: "$%.2f", settings.cloudCap.spentToday))
            } header: { Text("Limits") } footer: {
                Text("Empty removes this instance's override. The default cap still applies.")
                    .foregroundStyle(Color.textSecondary)
            }
            .listRowBackground(Color.elevated)
            Section {
                savingRow(.channel) {
                    Picker("Channel", selection: stringBinding(.channel, read: { store.settings?.channel.value ?? "auto" })) {
                        Text("Automatic").tag("auto")
                        Text("App").tag("web")
                        Text("Telegram").tag("telegram")
                    }
                }
            } header: { Text("Notifications") } footer: {
                Text("While a conversation is open, messages arrive there anyway.").foregroundStyle(Color.textSecondary)
            }
            .listRowBackground(Color.elevated)
            Section("You") {
                SettingTextRow(store: store, field: .operatorName, label: "Your name")
            }
            .listRowBackground(Color.elevated)
            ServerSection(store: services)
        }
        .disabled(store.saving != nil || pausing)
        .scrollContentBackground(.hidden)
        #if os(macOS)
        .formStyle(.grouped)
        #endif
    }

    private func stringBinding(_ field: SettingField, read: @escaping () -> String) -> Binding<String> {
        Binding(get: read, set: { value in Task { await store.save(field, value: .string(value)) } })
    }

    private func savingRow<Content: View>(_ field: SettingField, @ViewBuilder content: () -> Content) -> some View {
        HStack {
            content()
            if store.saving == field { ProgressView().controlSize(.small) }
        }
    }

    private func setPaused(_ value: Bool) {
        pausing = true
        Task {
            await store.setPaused(value)
            pausing = false
        }
    }

    static func watchLabel(_ field: SettingField) -> String {
        switch field {
        case .watchMeetings: "Meetings"
        case .watchMail: "Mail"
        case .watchCalendar: "Calendar"
        case .watchGchat: "Google Chat"
        default: field.rawValue
        }
    }

    static func watchChoice(_ value: String) -> String {
        switch value {
        case "off": "Off"
        case "inbox": "Inbox note"
        case "wake": "Inbox note and wake"
        default: value
        }
    }

    private func wakeFooter(_ settings: InstanceSettings) -> String {
        let hint = "0 means no scheduled wakes."
        return settings.wakeInterval.effective == settings.wakeInterval.value ? hint :
            hint + " Runs every \(settings.wakeInterval.effective) minutes because of the floor."
    }
}

private struct InstanceAccessSection: View {
    let client: APIClient
    let instance: String
    @State private var access: AccessResponse?
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        Group {
            #if os(macOS)
            accessSection
            #else
            if access == nil || access?.claudeNotes != nil { accessSection }
            #endif
        }
        .task { await load() }
    }

    private var accessSection: some View {
        Section {
            if let access {
                if access.claudeNotes != nil {
                    Toggle("Notes from Claude", isOn: Binding(
                        get: { self.access?.claudeNotes == true },
                        set: { setGrant(AccessGrant(grant: "claude", action: $0 ? "grant" : "revoke")) }
                    ))
                    .disabled(saving)
                }
                #if os(macOS)
                ForEach(access.googleAccounts, id: \.alias) { account in
                    Toggle("Meetings (\(account.alias))", isOn: Binding(
                        get: { self.access?.meetings?.contains { $0.account == account.alias } == true },
                        set: { setGrant(.meetings(alias: account.alias, action: $0 ? "grant" : "revoke")) }
                    ))
                    .disabled(saving)
                }
                if access.googleAccounts.isEmpty {
                    Text("No Google accounts configured.").foregroundStyle(Color.textSecondary)
                }
                if let available = access.shortcutsAvailable {
                    ForEach(shortcutRows(available, granted: access.shortcuts ?? []), id: \.id) { shortcut in
                        Toggle(shortcut.gone ? "Shortcut \(shortcut.name) (no longer on the Mac)"
                                             : "Shortcut \(shortcut.name)", isOn: Binding(
                            get: { self.access?.shortcuts?.contains { $0.id == shortcut.id } == true },
                            set: { setGrant(.shortcut(id: shortcut.id, action: $0 ? "grant" : "revoke")) }
                        ))
                        .disabled(saving)
                    }
                    if let problem = access.shortcutsProblem {
                        Text("Shortcuts: \(problem)").foregroundStyle(Color.textSecondary)
                    }
                }
                #endif
                if saving { ProgressView().controlSize(.small) }
            } else if error == nil {
                ProgressView("Loading access…")
            }
            if let error {
                Text(error).foregroundStyle(Color.warning)
                if access == nil {
                    Button("Try again") { Task { await load() } }
                }
            }
        } header: {
            Text("Access")
        } footer: {
            VStack(alignment: .leading, spacing: Design.Space.s4) {
                if access?.claudeNotes != nil {
                    Text("Your Claude sessions (Claude Desktop, Claude Code) can leave notes in its inbox; it reads them at its next wake.")
                }
                #if os(macOS)
                Text("A Meetings grant lets the instance read all ready transcripts filed under that account.")
                if access?.shortcutsAvailable != nil {
                    Text("A Shortcut grant lets the instance run that shortcut on the server Mac, as you, whenever it chooses.")
                }
                #endif
            }
            .foregroundStyle(Color.textSecondary)
        }
        .listRowBackground(Color.elevated)
    }

    /// The server Mac's shortcuts, then any granted one not among them (so it can still be revoked): `gone` only when the
    /// server says it is no longer on the Mac, not when the list could not be read.
    private func shortcutRows(_ available: [AccessResponse.Shortcut],
                              granted: [AccessResponse.ShortcutGrant]) -> [(id: String, name: String, gone: Bool)] {
        let here = Set(available.map(\.id))
        return available.map { ($0.id, $0.name, false) } + granted.filter { !here.contains($0.id) }.map { ($0.id, $0.name, $0.present == false) }
    }

    private func load() async {
        error = nil
        do {
            access = try await client.access(instance)
        } catch {
            self.error = APIError.from(error)?.message
        }
    }

    private func setGrant(_ grant: AccessGrant) {
        guard !saving else { return }
        saving = true
        error = nil
        Task {
            defer { saving = false }
            do {
                access = try await client.grantAccess(instance, grant)
            } catch {
                self.error = APIError.from(error)?.message
            }
        }
    }
}

/// Keep unfinished edits until the server accepts them.
private struct SettingTextRow: View {
    let store: InstanceSettingsStore
    let field: SettingField
    let label: String
    @State private var draft: String?
    @State private var inputError: String?
    @FocusState private var focused: Bool

    private var serverText: String {
        guard let settings = store.settings else { return "" }
        switch field {
        case .wakeInterval: return String(settings.wakeInterval.value)
        case .cloudCap: return settings.cloudCap.value.map { String($0) } ?? ""
        case .operatorName: return settings.operatorName.value
        default: return ""
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            HStack {
                LabeledContent(label) {
                    TextField(label, text: Binding(get: { draft ?? serverText }, set: { draft = $0; inputError = nil }), prompt: Text(field == .cloudCap ? "Default" : field == .operatorName ? "Not set" : label))
                        .labelsHidden()
                        .multilineTextAlignment(.trailing)
                        .focused($focused)
                        #if os(macOS)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 220)
                        #endif
                        #if os(iOS)
                        .keyboardType(field == .wakeInterval ? .numberPad : field == .cloudCap ? .decimalPad : .default)
                        #endif
                        .onSubmit(submit)
                }
                if store.saving == field { ProgressView().controlSize(.small) }
            }
            if let inputError { Text(inputError).font(.footnote).foregroundStyle(Color.warning) }
        }
        .onChange(of: focused) { if !focused { submit() } }
        .onDisappear(perform: submit)
    }

    private func submit() {
        guard let text = draft, text != serverText else { return }
        let value: SettingValue
        switch field {
        case .wakeInterval:
            guard let minutes = Int(text.trimmingCharacters(in: .whitespaces)), minutes >= 0 else {
                inputError = "Enter a whole number of minutes, 0 or more."
                return
            }
            value = .int(minutes)
        case .cloudCap:
            let amount = text.trimmingCharacters(in: .whitespaces)
            if amount.isEmpty { value = .null }
            else {
                guard let dollars = Double(amount), dollars.isFinite else {
                    inputError = "Enter a dollar amount."
                    return
                }
                value = .double(dollars)
            }
        default: value = .string(text)
        }
        Task {
            if await store.save(field, value: value), draft == text { draft = nil }
        }
    }
}

/// The shared voice preferences and proposals, with failures kept beside the action that failed.
private struct VoiceSettingsSection: View {
    let store: InstanceSettingsStore
    let client: APIClient
    let settings: InstanceSettings
    @State private var voices: VoicesResponse?
    @State private var loadError: String?
    @State private var rowErrors: [String: String] = [:]
    @State private var pending: String?
    @State private var picking: VoiceLanguage?
    @State private var rejecting: String?
    @State private var reason = ""

    var body: some View {
        Section("Voice") {
            VStack(alignment: .leading, spacing: Design.Space.s4) {
                Toggle("Voice mode", isOn: Binding(
                    get: { store.settings?.voiceMode.value == "on" },
                    set: { save(.voiceMode, value: $0 ? "on" : "off") }
                ))
                errorRow(SettingField.voiceMode.rawValue)
            }
            VStack(alignment: .leading, spacing: Design.Space.s4) {
                if let choices = settings.voiceEffort.choices {
                    Picker("Voice-turn effort", selection: Binding(
                        get: { store.settings?.voiceEffort.value ?? "" },
                        set: { save(.voiceEffort, value: $0) }
                    )) {
                        if !choices.contains(settings.voiceEffort.value) {
                            Text(effortLabel(settings.voiceEffort.value)).tag(settings.voiceEffort.value)
                        }
                        ForEach(choices, id: \.self) { Text(effortLabel($0)).tag($0) }
                    }
                } else {
                    LabeledContent("Voice-turn effort", value: effortLabel(settings.voiceEffort.value))
                    Text("Effort choices are unavailable.").font(.footnote).foregroundStyle(Color.textSecondary)
                }
                errorRow(SettingField.voiceEffort.rawValue)
            }
            if let voices {
                ForEach(VoiceLanguage.allCases, id: \.self) { language in
                    languageRow(language, voices: voices)
                }
                ForEach(voices.proposals.filter { $0.state == "open" }, id: \.id) { proposal in
                    proposalRow(proposal)
                }
            } else if let loadError {
                Text(loadError).foregroundStyle(Color.warning)
                Button("Retry voices") { Task { await load() } }
            } else {
                ProgressView("Loading voices…")
            }
        }
        .listRowBackground(Color.elevated)
        .disabled(pending != nil)
        .task {
            await VoiceReporter.report(client: client)
            await load()
        }
        .sheet(isPresented: Binding(get: { picking != nil }, set: { if !$0 { picking = nil } })) {
            if let language = picking {
                VoicePicker(client: client, instance: store.instance, language: language) { voices = $0 }
                    .sheetFrame()
            }
        }
    }

    private func effortLabel(_ value: String) -> String {
        value.isEmpty ? "Same as typed (\(settings.effort.value ?? "Not set"))" : value
    }

    private func languageRow(_ language: VoiceLanguage, voices: VoicesResponse) -> some View {
        let voice = voices.voices[language.rawValue].flatMap { $0 }
        return VStack(alignment: .leading, spacing: Design.Space.s4) {
            Text(language == .en ? "English" : "Italian").font(.headline)
            Text(voice?.name ?? "System voice")
            if let voice {
                Text(voice.by == "operator" ? "chosen by you" : "chosen by \(store.instance)")
                    .font(.footnote).foregroundStyle(Color.textSecondary)
            }
            HStack {
                Button { picking = language } label: { Text("Change").quietHitArea() }
                Button { clear(language) } label: { Text("Clear").quietHitArea() }
                    .disabled(voice == nil)
            }
            .buttonStyle(.borderless)
            errorRow(language.rawValue)
        }
    }

    private func proposalRow(_ proposal: VoicesResponse.Proposal) -> some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            Text("\(proposal.name) · \(proposal.language == "en" ? "English" : proposal.language == "it" ? "Italian" : proposal.language)")
                .font(.headline)
            Text("Proposed by \(store.instance)").font(.footnote).foregroundStyle(Color.textSecondary)
            Text(proposal.why)
            if rejecting == proposal.id {
                TextField("Reason (optional)", text: $reason)
                HStack {
                    Button { decide(proposal, accept: false) } label: { Text("Reject proposal").quietHitArea() }
                    Button { rejecting = nil; reason = "" } label: { Text("Cancel").quietHitArea() }
                }
            } else {
                HStack {
                    Button { decide(proposal, accept: true) } label: { Text("Accept").quietHitArea() }
                    Button { rejecting = proposal.id; reason = "" } label: { Text("Reject…").quietHitArea() }
                }
            }
            errorRow(proposal.id)
        }
        .buttonStyle(.borderless)
    }

    @ViewBuilder
    private func errorRow(_ key: String) -> some View {
        if let error = rowErrors[key] { Text(error).font(.footnote).foregroundStyle(Color.warning) }
        if pending == key { ProgressView().controlSize(.small) }
    }

    private func save(_ field: SettingField, value: String) {
        pending = field.rawValue
        rowErrors[field.rawValue] = nil
        Task {
            defer { pending = nil }
            if !(await store.save(field, value: .string(value))) {
                rowErrors[field.rawValue] = store.saveError
            }
        }
    }

    private func load() async {
        do {
            voices = try await client.voices(store.instance)
            loadError = nil
        } catch { loadError = APIError.from(error)?.message }
    }

    private func clear(_ language: VoiceLanguage) {
        pending = language.rawValue
        rowErrors[language.rawValue] = nil
        Task {
            defer { pending = nil }
            do { voices = try await client.clearVoice(store.instance, language: language.rawValue) }
            catch { rowErrors[language.rawValue] = APIError.from(error)?.message }
        }
    }

    private func decide(_ proposal: VoicesResponse.Proposal, accept: Bool) {
        pending = proposal.id
        rowErrors[proposal.id] = nil
        let rejectionReason = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            defer { pending = nil }
            do {
                voices = try await client.decideVoiceProposal(store.instance, id: proposal.id, accept: accept,
                    reason: accept || rejectionReason.isEmpty ? nil : rejectionReason)
                rejecting = nil
                reason = ""
            } catch { rowErrors[proposal.id] = APIError.from(error)?.message }
        }
    }
}
