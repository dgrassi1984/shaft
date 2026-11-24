#if os(macOS)
import SwiftUI

struct ServerKeysSection: View {
    let keys: ServerKeys

    var body: some View {
        Section {
            if let message = keys.loadError {
                Text(message).font(.footnote).foregroundStyle(Color.danger)
                Button("Try again") { Task { await keys.load() } }
                    .foregroundStyle(Color.brand)
            }
            ForEach(ServerKeys.rows.indices, id: \.self) { index in
                let row = ServerKeys.rows[index]
                ServerKeyRow(keys: keys, name: row.name, label: row.label, help: row.help, link: row.link)
            }
        } header: {
            Text("Server keys")
        } footer: {
            Text("Kept in the server's .env on this Mac. The app never shows a saved key.")
                .foregroundStyle(Color.textSecondary)
        }
        .listRowBackground(Color.elevated)
        .task { await keys.load() }
    }
}

private struct ServerKeyRow: View {
    let keys: ServerKeys
    let name: String
    let label: String
    let help: String
    let link: URL?
    @State private var value = ""

    var body: some View {
        let isSet = keys.set.contains(name)
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            LabeledContent(label, value: isSet ? "Set" : "Not set")
            Text(help).font(.footnote).foregroundStyle(Color.textSecondary)
            if let link {
                Link("Create a token", destination: link).foregroundStyle(Color.brand)
            }
            HStack(spacing: Design.Space.s8) {
                SecureField(isSet ? "Paste a new key" : "Paste the key", text: $value)
                    .accessibilityLabel(label + " key")
                    .disabled(keys.saving == name)
                Button("Save") {
                    Task {
                        await keys.save(name: name, value: value)
                        if keys.outcome[name] == .saved { value = "" }
                    }
                }
                .foregroundStyle(Color.brand)
                .disabled(value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || keys.saving != nil)
                if keys.saving == name { ProgressView().controlSize(.small) }
            }
            switch keys.outcome[name] {
            case .saved:
                Text("Saved").font(.footnote).foregroundStyle(Color.textSecondary)
            case .failed(let message):
                Text(message).font(.footnote).foregroundStyle(Color.danger)
            case nil:
                EmptyView()
            }
        }
    }
}
#endif
