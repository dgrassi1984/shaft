import SHAFTKit
import SwiftUI

/// The setup wizard (first launch with no SHAFT installed): the steps in a sidebar, each done, current or still to
/// come, the step on screen beside it, and Back and Continue at the bottom right. Where it is persists in `setup.state`
/// (MacSetup), so reopening the app resumes there. After Install, on the pairing it made: Model, Your instance (created
/// as Continue leaves it), Access, How it works alone, and Say hello, which opens the instance's chat and ends the
/// wizard. The steps' work is SetupSteps'; what Continue waits for is SetupContinue's.
struct SetupWizard: View {
    let setup: MacSetup
    let session: SHAFTSession
    @Environment(AppModel.self) private var model
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
            // The window closed: the reads, the check and a sign-in stop; a write already sent finishes (SetupSteps).
            .onDisappear { setup.steps.stop() }
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
            case .model:
                page { ModelStepView(setup: setup, session: session, steps: setup.steps) }
            case .instance:
                page { InstanceStepView(setup: setup, session: session, steps: setup.steps) }
            case .access:
                page { AccessStepView(setup: setup, session: session, steps: setup.steps) }
            case .alone:
                page { AloneStepView(setup: setup, session: session, steps: setup.steps) }
            case .hello:
                page { HelloStepView(setup: setup, steps: setup.steps, skip: openChat, retry: hello) }
            }
        }
    }

    private func page<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        ScrollView { content().stepPage() }
    }

    private func hello() {
        setup.steps.sayHello(store: session.store, setup: setup, session: session) { name in
            model.openChat = ChatTarget(instance: name)
        }
    }

    /// The chat without the welcome (Say hello failed and the operator goes on).
    private func openChat() {
        if let name = setup.state.createdInstance { model.openChat = ChatTarget(instance: name) }
        setup.finishWizard(session)
    }

    private var footer: some View {
        HStack(spacing: Design.Space.s12) {
            if let error = setup.saveError {
                Text(error).font(.callout).foregroundStyle(Color.warning).lineLimit(2)
            }
            Spacer()
            if setup.state.step == .access, !connecting {
                // Everything on Access is optional: on, granting nothing.
                Button("Skip all") { setup.steps.skipAccess(setup: setup) }
                    .disabled(busy)
            }
            if let back = backAction {
                Button("Back", action: back)
                    .keyboardShortcut(.cancelAction)
                    .disabled(busy)
            }
            if let next = continueAction {
                Button(action: next.run) {
                    HStack(spacing: Design.Space.s8) {
                        if setup.installing || setup.steps.busy {
                            ProgressView().controlSize(.small).tint(Color.onAccent)
                        }
                        Text(next.title)
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
        setup.installing || setup.installer(for: session)?.isRunning == true || setup.steps.busy
    }

    private var backAction: (() -> Void)? {
        if connecting { return { connecting = false } }
        let all = SetupWizardStep.allCases
        guard let index = all.firstIndex(of: setup.state.step), index > 0 else { return nil }
        return { setup.go(to: all[index - 1]) }
    }

    private var continueAction: (title: String, enabled: Bool, run: () -> Void)? {
        guard !connecting else { return nil }
        let steps = setup.steps
        var context = SetupContinue.Context(understood: understood, busy: busy, state: setup.state)
        context.installComplete = setup.installer(for: session)?.isComplete == true
        context.saved = steps.saved
        context.checked = steps.checked
        context.checkPassed = steps.check?.ok == true
        context.current = try? steps.form.request().get()
        context.capText = steps.capText
        context.accessReady = steps.accessReady(setup.state.createdInstance)
        let enabled = SetupContinue.enabled(setup.state.step, context)
        let store = session.store
        switch setup.state.step {
        case .welcome: return ("Continue", enabled, { setup.go(to: .install) })
        case .install: return ("Continue", enabled, { setup.continueInstall(session) })
        case .model: return ("Continue", enabled, { setup.go(to: .instance) })
        case .instance:
            return (setup.state.createdInstance == nil ? "Create" : "Continue", enabled,
                    { steps.createInstance(store: store, setup: setup) })
        case .access: return ("Continue", enabled, { steps.applyAccess(store: store, setup: setup) })
        case .alone: return ("Continue", enabled, { steps.saveAlone(store: store, setup: setup) })
        case .hello: return ("Say hello", enabled, hello)
        }
    }

    /// Resumed past Install without a pairing (it was dropped since): back to Install, which pairs again. A dev run
    /// opened at a later step (SHAFT_DEV_SETUP) pairs at launch on its own and stays where it was opened.
    private func resume() {
        var devRun = false
        #if DEBUG
        devRun = SetupEnvironment.devStep() != nil
        #endif
        if MacSetup.isAfterInstall(setup.state.step), session.store == nil, !devRun { setup.go(to: .install) }
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
