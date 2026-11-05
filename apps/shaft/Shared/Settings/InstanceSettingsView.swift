import SHAFTKit
import SwiftUI

struct InstanceSettingsView: View {
    let store: InstanceSettingsStore
    let services: ServicesStore
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
            Section {} footer: {
                if let message = store.saveError ?? store.notice {
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
                }
            }
            .listRowBackground(Color.elevated)
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

    private func wakeFooter(_ settings: InstanceSettings) -> String {
        let hint = "0 means no scheduled wakes."
        return settings.wakeInterval.effective == settings.wakeInterval.value ? hint :
            hint + " Runs every \(settings.wakeInterval.effective) minutes because of the floor."
    }
}

/// Keeps only the unfinished edit locally. Every submit returns to the server's value, including a refused save.
private struct SettingTextRow: View {
    let store: InstanceSettingsStore
    let field: SettingField
    let label: String
    @State private var draft: String?
    @State private var inputError: String?

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
                    TextField(label, text: Binding(get: { draft ?? serverText }, set: { draft = $0; inputError = nil }))
                        .multilineTextAlignment(.trailing)
                        .onSubmit(submit)
                }
                if store.saving == field { ProgressView().controlSize(.small) }
            }
            if let inputError { Text(inputError).font(.footnote).foregroundStyle(Color.warning) }
        }
        .onChange(of: serverText) { draft = nil }
    }

    private func submit() {
        guard store.saving == nil else { return }
        let text = draft ?? serverText
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
            await store.save(field, value: value)
            draft = nil
        }
    }
}
