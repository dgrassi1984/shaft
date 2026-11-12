import SHAFTKit
import SwiftUI

/// The setup wizard (first launch with no SHAFT installed): the steps in a sidebar, each done, current or still to
/// come, the step on screen beside it, and Back and Continue at the bottom right. Where it is persists in `setup.state`
/// (MacSetup), so reopening the app resumes there. Welcome and Install are built; the steps after Install come with
/// the next part of the setup: a Release build ends the wizard at Install's pairing, a Debug build shows Model's slot.
struct SetupWizard: View {
    let setup: MacSetup
    let session: SHAFTSession
    @State private var understood = false
    /// Welcome's "Connect to an existing SHAFT": the pasted-link pairing in place of the steps.
    @State private var connecting = false

    var body: some View {
        if let error = setup.stateError {
            MarkMessage("The setup cannot resume", message: Text(error).foregroundStyle(Color.warning)) {
                Button("Start over") { setup.startOver() }.buttonStyle(ProminentAction())
            }
        } else {
            HStack(spacing: 0) {
                SetupSidebar(current: setup.state.step)
                    .frame(width: 240)
                    .background(Color.elevated.ignoresSafeArea())
                Rectangle().fill(Color.hairline).frame(width: 1).ignoresSafeArea()
                VStack(spacing: 0) {
                    content
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    footer
                }
            }
            .background(Color.canvas.ignoresSafeArea())
            .onAppear(perform: resume)
        }
    }

    @ViewBuilder
    private var content: some View {
        if connecting {
            PairingView(session: session)
        } else {
            switch setup.state.step {
            case .welcome:
                ScrollView {
                    WelcomeStepView(understood: $understood, connect: { connecting = true })
                        .stepPage()
                }
            case .install:
                if let installer = setup.installer(for: session) {
                    ScrollView {
                        InstallStepView(setup: setup, session: session, installer: installer)
                            .stepPage()
                    }
                }
            case .model, .instance, .access, .alone, .hello:
                after
            }
        }
    }

    /// The steps after Install, which the next part of the setup builds.
    @ViewBuilder
    private var after: some View {
        #if DEBUG
        ContentUnavailableView {
            Label("Model setup", systemImage: "cpu")
        } description: {
            Text("Debug build: SHAFT is installed and this app is paired. The model step is not built yet; Open SHAFT ends the setup here.")
        } actions: {
            Button("Open SHAFT") { setup.finishWizard(session) }.buttonStyle(ProminentAction())
        }
        #else
        Color.canvas.onAppear { setup.finishWizard(session) }
        #endif
    }

    private var footer: some View {
        HStack(spacing: Design.Space.s12) {
            if let error = setup.saveError {
                Text(error).font(.callout).foregroundStyle(Color.warning).lineLimit(2)
            }
            Spacer()
            if let back = backAction {
                Button("Back", action: back)
                    .keyboardShortcut(.cancelAction)
                    .disabled(busy)
            }
            if let next = continueAction {
                Button(action: next.run) {
                    HStack(spacing: Design.Space.s8) {
                        if setup.installing { ProgressView().controlSize(.small).tint(Color.onAccent) }
                        Text("Continue")
                    }
                }
                .buttonStyle(ProminentAction())
                .keyboardShortcut(.defaultAction)
                .disabled(!next.enabled)
            }
        }
        .padding(.horizontal, Design.Space.s24)
        .padding(.vertical, Design.Space.s16)
        .overlay(alignment: .top) { Rectangle().fill(Color.hairline).frame(height: 1) }
    }

    private var busy: Bool {
        setup.installing || setup.installer(for: session)?.isRunning == true
    }

    private var backAction: (() -> Void)? {
        if connecting { return { connecting = false } }
        switch setup.state.step {
        case .welcome: return nil
        case .install: return { setup.go(to: .welcome) }
        case .model, .instance, .access, .alone, .hello: return { setup.go(to: .install) }
        }
    }

    private var continueAction: (enabled: Bool, run: () -> Void)? {
        guard !connecting else { return nil }
        switch setup.state.step {
        case .welcome:
            return (SetupGate.welcomeCanContinue(understood: understood), { setup.go(to: .install) })
        case .install:
            let done = setup.installer(for: session)?.isComplete == true
            return (done && !busy, { Task { await setup.completeInstall(session) } })
        case .model, .instance, .access, .alone, .hello:
            return nil
        }
    }

    /// Resumed past Install without a pairing (it was dropped since): back to Install, which pairs again.
    private func resume() {
        if MacSetup.isAfterInstall(setup.state.step), session.store == nil { setup.go(to: .install) }
        if setup.state.step != .welcome { understood = true }   // Welcome was passed once already
    }
}

/// The wizard's steps, in order, each with a short line about it.
struct SetupSidebar: View {
    let current: SetupWizardStep

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            Text("Set up SHAFT")
                .font(.headline)
                .foregroundStyle(Color.textPrimary)
                .padding(.bottom, Design.Space.s4)
            ForEach(Array(SetupWizardStep.allCases.enumerated()), id: \.element) { index, step in
                row(step, number: index + 1)
            }
            Spacer()
        }
        .padding(Design.Space.s20)
        .padding(.top, Design.Space.s24)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func row(_ step: SetupWizardStep, number: Int) -> some View {
        let status = SetupGate.status(of: step, current: current)
        return HStack(alignment: .firstTextBaseline, spacing: Design.Space.s12) {
            Image(systemName: status == .done ? "checkmark.circle.fill" : status == .current ? "circle.inset.filled" : "circle")
                .foregroundStyle(status == .pending ? Color.textTertiary : Color.brand)
            VStack(alignment: .leading, spacing: 2) {
                Text(Self.title(step))
                    .font(.body.weight(status == .current ? .semibold : .regular))
                    .foregroundStyle(status == .pending ? Color.textSecondary : Color.textPrimary)
                Text(Self.subtitle(step))
                    .font(.caption)
                    .foregroundStyle(Color.textSecondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Step \(number) of \(SetupWizardStep.allCases.count), \(Self.title(step)), "
                            + (status == .done ? "done" : status == .current ? "current" : "to come"))
    }

    static func title(_ step: SetupWizardStep) -> String {
        switch step {
        case .welcome: "Welcome"
        case .install: "Install"
        case .model: "Model"
        case .instance: "Your instance"
        case .access: "Access"
        case .alone: "How it works alone"
        case .hello: "Say hello"
        }
    }

    static func subtitle(_ step: SetupWizardStep) -> String {
        switch step {
        case .welcome: "What SHAFT does, and its risks"
        case .install: "SHAFT and its services on this Mac"
        case .model: "The AI it runs on"
        case .instance: "Its name and drive"
        case .access: "What it may read"
        case .alone: "Working on its own, and budgets"
        case .hello: "Your first chat"
        }
    }
}

private extension View {
    /// A step's page: at reading width, from the top left of its column.
    func stepPage() -> some View {
        padding(Design.Space.s24)
            .padding(.top, Design.Space.s16)
            .frame(maxWidth: Design.measure, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}
