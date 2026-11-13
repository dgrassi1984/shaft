import SHAFTKit
import SwiftUI

/// The wizard's Install step: the operator's name (SHAFT signs its commits with it), then one row per install step
/// with its state, what it found, and for a failure the real error with Try again. The configure row lists the server's
/// own steps (`shaft install --json`) as they arrive. Continue (the wizard's footer) opens the session on the install's
/// pairing once every row is done.
struct InstallStepView: View {
    let setup: MacSetup
    let session: SHAFTSession
    let installer: Installer
    @State private var name = ""
    @State private var email = ""

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            Text("Install SHAFT").font(.largeTitle.bold()).foregroundStyle(Color.textPrimary)
            Text("SHAFT goes into \(server). Each row checks first and skips what is already done, so closing the app "
                 + "halfway and opening it again resumes here.")
                .foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            VStack(alignment: .leading, spacing: Design.Space.s8) {
                TextField("Your name", text: $name)
                    .textContentType(.name)
                    .accessibilityHint("SHAFT signs its commits with it, in this repository only")
                TextField("Email (optional)", text: $email)
                    .textContentType(.emailAddress)
                Text("Your name and email sign SHAFT's commits in \(server) only, never your global git settings.")
                    .font(.caption)
                    .foregroundStyle(Color.textSecondary)
            }
            .textFieldStyle(.roundedBorder)
            .disabled(installer.isRunning || installer.isComplete)
            .frame(maxWidth: 360, alignment: .leading)

            if showsStart {
                Button(startTitle) { setup.install(session) }
                    .buttonStyle(ProminentAction())
                    .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }

            VStack(alignment: .leading, spacing: Design.Space.s16) {
                ForEach(InstallStep.allCases, id: \.self) { step in
                    row(step)
                }
            }
            .padding(Design.Space.s16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .elevatedBlock()

            if let error = setup.installError {
                Text(error).foregroundStyle(Color.warning).textSelection(.enabled)
                Button("Try again") { setup.continueInstall(session) }
                    .buttonStyle(ProminentAction())
            }
        }
        .onAppear {
            name = setup.state.operatorName
            email = setup.state.email
        }
        .onChange(of: name) { setup.setOperator(name: name, email: email) }
        .onChange(of: email) { setup.setOperator(name: name, email: email) }
        // The window closed: the running command stops (a rerun resumes), and what it prints after changes nothing.
        .onDisappear { setup.stopInstall() }
        .onChange(of: failure) { _, now in if let now { Announce.say(now) } }
    }

    private var server: String {
        (installer.configuration.server.path as NSString).abbreviatingWithTildeInPath
    }

    private var failed: Bool {
        InstallStep.allCases.contains { if case .failed = installer.steps[$0] { true } else { false } }
    }

    private var failure: String? {
        for step in InstallStep.allCases { if case .failed(let message) = installer.steps[step] { return message } }
        return nil
    }

    private var showsStart: Bool { !installer.isRunning && !installer.isComplete && !failed }

    private var startTitle: String {
        InstallStep.allCases.allSatisfy { installer.steps[$0] == .pending } ? "Install" : "Continue the install"
    }

    @ViewBuilder
    private func row(_ step: InstallStep) -> some View {
        let state = installer.steps[step] ?? .pending
        HStack(alignment: .firstTextBaseline, spacing: Design.Space.s12) {
            icon(state).frame(width: 18)
            VStack(alignment: .leading, spacing: Design.Space.s4) {
                Text(Self.title(step)).font(.body.weight(.semibold)).foregroundStyle(Color.textPrimary)
                if step == .configure, !installer.subRows.isEmpty {
                    ForEach(installer.subRows) { sub in
                        Label {
                            Text("\(sub.step): \(sub.ok ? sub.detail : sub.error ?? sub.detail)")
                                .foregroundStyle(sub.ok ? Color.textSecondary : Color.warning)
                        } icon: {
                            Image(systemName: sub.ok ? "checkmark" : "xmark")
                                .foregroundStyle(sub.ok ? Color.brand : Color.warning)
                        }
                        .font(.callout)
                    }
                } else if let detail = installer.details[step], state != .pending {
                    Text(detail).font(.callout).foregroundStyle(Color.textSecondary).textSelection(.enabled)
                } else {
                    Text(Self.subtitle(step, server: server)).font(.callout).foregroundStyle(Color.textSecondary)
                }
                if case .failed(let message) = state {
                    Text(message).font(.callout).foregroundStyle(Color.warning).textSelection(.enabled)
                    Button("Try again") { setup.install(session, retrying: step) }
                        .buttonStyle(.plain)
                        .foregroundStyle(Color.brand)
                        .disabled(installer.isRunning || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder
    private func icon(_ state: StepState) -> some View {
        switch state {
        case .pending: Image(systemName: "circle").foregroundStyle(Color.textTertiary).accessibilityLabel("to do")
        case .running: ProgressView().controlSize(.small).accessibilityLabel("running")
        case .done: Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.brand).accessibilityLabel("done")
        case .failed: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Color.warning).accessibilityLabel("failed")
        }
    }

    static func title(_ step: InstallStep) -> String {
        switch step {
        case .git: "Command Line Tools"
        case .server: "SHAFT"
        case .runtime: "Python 3.12"
        case .configure: "Services"
        case .pair: "This app"
        }
    }

    static func subtitle(_ step: InstallStep, server: String) -> String {
        switch step {
        case .git: "git, from Apple: macOS asks to install it when it is missing"
        case .server: "The newest release, into \(server)"
        case .runtime: "uv and Python 3.12, inside \(server)"
        case .configure: "Your git identity, the API, the web service and the scheduler"
        case .pair: "Paired with the server on this Mac, no link to paste"
        }
    }
}
