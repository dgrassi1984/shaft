import SHAFTKit
import SwiftUI

/// The wizard's "Your instance" step: its name (checked as `shaft new` checks it, as it is typed), your name, the model
/// the Model step chose, and its drive: four cards that fill an editable text, which becomes its `drive.md`. Continue
/// creates it (`/setup/instance`); once created, the step shows it and never creates a second.
struct InstanceStepView: View {
    let setup: MacSetup
    let session: SHAFTSession
    let steps: SetupSteps

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            Text("Your instance").font(.largeTitle.bold()).foregroundStyle(Color.textPrimary)
            if let created = setup.state.createdInstance {
                createdView(created)
            } else {
                editor
            }
        }
        .onAppear { steps.loadDrives(store: session.store, setup: setup) }
    }

    private func createdView(_ name: String) -> some View {
        VStack(alignment: .leading, spacing: Design.Space.s12) {
            LabeledContent("Name") { Text(name).foregroundStyle(Color.textPrimary).textSelection(.enabled) }
            LabeledContent("Your name") { Text(setup.state.operatorName).foregroundStyle(Color.textPrimary) }
            LabeledContent("Model") { Text(setup.state.model).foregroundStyle(Color.textPrimary).textSelection(.enabled) }
            Text("\(name) is created, paused until you answer its welcome. Change its model and your name in Settings, "
                 + "and its drive in its documents (drive.md).")
                .foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if let failure = steps.creating.failure {
                SetupFailure(message: failure)
            }
        }
    }

    private var editor: some View {
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            VStack(alignment: .leading, spacing: Design.Space.s8) {
                TextField("Name (lowercase, e.g. ledger)", text: binding(\.instanceName))
                    .accessibilityHint("Lowercase letters, digits and -, starting with a letter")
                if !setup.state.instanceName.isEmpty, let problem = InstanceName.problem(setup.state.instanceName) {
                    Text(problem).font(.callout).foregroundStyle(Color.warning)
                }
                TextField("Your name", text: binding(\.operatorName))
                    .textContentType(.name)
                if setup.state.provider == ProviderKind.custom.rawValue {
                    TextField("Model", text: binding(\.model))
                } else {
                    LabeledContent("Model") {
                        Text(setup.state.model.isEmpty ? "None yet: choose one in the Model step" : setup.state.model)
                            .foregroundStyle(setup.state.model.isEmpty ? Color.warning : Color.textPrimary)
                            .textSelection(.enabled)
                    }
                }
            }
            .textFieldStyle(.roundedBorder)
            .frame(maxWidth: 420, alignment: .leading)
            .disabled(steps.creating.running)

            Text("Its drive").font(.headline).foregroundStyle(Color.textPrimary)
            Text("What it is for, in its own words. Pick a starting point, then make it yours: it becomes its drive.md.")
                .foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if let failure = steps.loadingDrives.failure {
                SetupFailure(message: failure) { steps.loadDrives(store: session.store, setup: setup) }
            } else if steps.loadingDrives.running {
                ProgressView().controlSize(.small)
            }
            LazyVGrid(columns: [GridItem(.flexible(), spacing: Design.Space.s12, alignment: .top),
                                GridItem(.flexible(), spacing: Design.Space.s12, alignment: .top)],
                      alignment: .leading, spacing: Design.Space.s12) {
                ForEach(steps.drives) { preset in
                    SetupCard(selected: setup.state.drive == preset.id, action: {
                        setup.edit { DriveText.choose(preset, into: &$0) }
                    }) {
                        Text(preset.title).font(.headline).foregroundStyle(Color.textPrimary)
                        Text(DriveText.suggests(preset)).font(.caption).foregroundStyle(Color.textSecondary)
                    }
                }
            }
            .disabled(steps.creating.running)
            TextEditor(text: binding(\.driveText))
                .font(.body)
                .scrollContentBackground(.hidden)
                .padding(Design.Space.s8)
                .frame(minHeight: 180)
                .elevatedBlock()
                .disabled(steps.creating.running)
                .accessibilityLabel("Its drive")
            HStack {
                if let problem = DriveText.problem(setup.state.driveText) {
                    Text(problem).font(.callout).foregroundStyle(Color.warning)
                }
                Spacer()
                Text("\(setup.state.driveText.unicodeScalars.count) / \(DriveText.maxLength)")
                    .font(Design.Text.metadata).foregroundStyle(Color.textTertiary)
            }
            if let failure = steps.creating.failure {
                SetupFailure(message: failure) { steps.createInstance(store: session.store, setup: setup) }
            }
        }
    }

    private func binding(_ field: WritableKeyPath<SetupState, String>) -> Binding<String> {
        Binding(get: { setup.state[keyPath: field] }, set: { value in setup.edit { $0[keyPath: field] = value } })
    }
}
