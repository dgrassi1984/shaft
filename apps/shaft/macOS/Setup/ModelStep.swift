import AppKit
import SHAFTKit
import SwiftUI

/// The wizard's Model step: three cards (OpenRouter, ChatGPT through Codex, a custom OpenAI-compatible endpoint), the
/// chosen one's form, Save (its key first, through `/setup/secret`, then `/setup/provider`) with what the provider
/// covers and what stays off, and Test (`/setup/provider/check`, one live call per model). Continue waits for a test
/// that passed for what is saved now.
struct ModelStepView: View {
    let setup: MacSetup
    let session: SHAFTSession
    @Bindable var steps: SetupSteps

    static let chatGPTDownload = URL(string: "https://openai.com/chatgpt/desktop/")!
    static let openRouterKeys = URL(string: "https://openrouter.ai/settings/keys")!

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            Text("Choose the model").font(.largeTitle.bold()).foregroundStyle(Color.textPrimary)
            Text("Your instance, the judges that grade its work and SHAFT's own helpers all run on the provider you "
                 + "choose. Keys go into the server's settings on this Mac; instances never see them.")
                .foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(alignment: .top, spacing: Design.Space.s12) {
                ForEach(ProviderKind.allCases) { kind in
                    SetupCard(selected: steps.form.kind == kind, action: { steps.form.kind = kind }) {
                        Text(kind.title).font(.headline).foregroundStyle(Color.textPrimary)
                        Text(kind.subtitle).font(.callout).foregroundStyle(Color.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .disabled(steps.busy)

            form
                .textFieldStyle(.roundedBorder)
                .disabled(steps.saving.running || steps.testing.running)

            HStack(spacing: Design.Space.s12) {
                Button(action: save) {
                    HStack(spacing: Design.Space.s8) {
                        if steps.saving.running { ProgressView().controlSize(.small).tint(Color.onAccent) }
                        Text("Save")
                    }
                }
                .buttonStyle(ProminentAction())
                .disabled(steps.busy)
                Button(action: test) {
                    HStack(spacing: Design.Space.s8) {
                        if steps.testing.running { ProgressView().controlSize(.small) }
                        Text("Test")
                    }
                }
                .disabled(steps.busy || !savedNow)
            }
            if let restarting = steps.restarting {
                // The saved models reach the running services only once they restart: Test waits for it.
                Text(restarting).font(.callout).foregroundStyle(Color.textSecondary)
            }
            if let failure = steps.saving.failure {
                SetupFailure(message: failure, retry: save)
            }
            if let coverage = steps.coverage, steps.saved?.provider == steps.form.kind.rawValue {
                coverageBlock(coverage)
            }
            if let failure = steps.testing.failure {
                SetupFailure(message: failure, retry: test)
            }
            if let check = steps.check, savedNow, steps.checked == steps.saved {
                checkBlock(check)
            }
        }
        .onAppear {
            // Reopened on this step: the card chosen before, saved and tested again (nothing of a check is kept).
            if steps.saved == nil, let kept = setup.state.provider.flatMap(ProviderKind.init(rawValue:)) {
                steps.form.kind = kept
            }
        }
    }

    private var request: ProviderRequest? { try? steps.form.request().get() }
    /// What is saved is what the form says now: Test checks it.
    private var savedNow: Bool { steps.saved != nil && steps.saved == request }

    private func save() {
        steps.saveProvider(store: session.store, setup: setup, installer: setup.installer(for: session),
                           reachable: MacSetup.reachability(of: session))
    }
    private func test() { steps.test(store: session.store) }

    @ViewBuilder
    private var form: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            switch steps.form.kind {
            case .openrouter:
                SecureField("OpenRouter key", text: $steps.form.key)
                Text("Leave it empty to keep the key saved already.").font(.caption).foregroundStyle(Color.textSecondary)
                Link("Get a key at openrouter.ai", destination: Self.openRouterKeys).foregroundStyle(Color.brand)
            case .codex:
                codex
            case .custom:
                TextField("Base URL (https://host/v1)", text: $steps.form.baseURL)
                SecureField("Key (optional: leave it empty to keep the saved one, or for none)", text: $steps.form.key)
                TextField("Model, as the endpoint names it", text: $steps.form.model)
                HStack {
                    TextField("Price in, $ per million tokens", text: $steps.form.priceIn)
                    TextField("Price out, $ per million tokens", text: $steps.form.priceOut)
                }
                Toggle("It reads images", isOn: $steps.form.readsImages)
                    .toggleStyle(.checkbox)
                    .foregroundStyle(Color.textPrimary)
                Text("A second model for the judges (optional): judges grade the instance's work, better with a model "
                     + "other than its own.")
                    .font(.caption).foregroundStyle(Color.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                TextField("Judge model (optional)", text: $steps.form.judgeModel)
                if !steps.form.judgeModel.trimmingCharacters(in: .whitespaces).isEmpty {
                    HStack {
                        TextField("Judge price in", text: $steps.form.judgePriceIn)
                        TextField("Judge price out", text: $steps.form.judgePriceOut)
                    }
                }
                Text("Prices are dollars per million tokens, 0 for a server of your own: SHAFT counts the daily budgets "
                     + "with them.")
                    .font(.caption).foregroundStyle(Color.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: 480, alignment: .leading)
    }

    @ViewBuilder
    private var codex: some View {
        HStack(spacing: Design.Space.s12) {
            if steps.signingIn.running {
                ProgressView().controlSize(.small)
                Text("Waiting for you to sign in…").foregroundStyle(Color.textSecondary)
                Button("Cancel") { steps.cancelSignIn() }
            } else {
                Button(steps.signedIn ? "Sign in again" : "Sign in") {
                    steps.signInToCodex(installer: setup.installer(for: session))
                }
                .disabled(steps.busy)
            }
            if steps.signedIn {
                Label("Signed in", systemImage: "checkmark").foregroundStyle(Color.brand)
            }
        }
        Text("Signing in opens ChatGPT's sign-in page in your browser, where you enter a one-time code shown here. "
             + "Codex keeps its login inside SHAFT's own folder.")
            .font(.caption).foregroundStyle(Color.textSecondary)
            .fixedSize(horizontal: false, vertical: true)
        if steps.signingIn.running, steps.loginCode != nil || steps.loginURL != nil {
            codexCode
        }
        if let failure = steps.signingIn.failure {
            SetupFailure(message: failure) { steps.signInToCodex(installer: setup.installer(for: session)) }
            Link("Get ChatGPT for Mac", destination: Self.chatGPTDownload).foregroundStyle(Color.brand)
        }
    }

    /// The code to enter on ChatGPT's page, copyable, and that page again should its tab have closed.
    private var codexCode: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            if let code = steps.loginCode {
                Text("Enter this code on the page that opened in your browser (it expires in 15 minutes):")
                    .foregroundStyle(Color.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: Design.Space.s12) {
                    Text(code)
                        .font(.system(.title, design: .monospaced).weight(.semibold))
                        .foregroundStyle(Color.textPrimary)
                        .textSelection(.enabled)
                        .accessibilityLabel("Code \(code.map(String.init).joined(separator: " "))")
                    Button("Copy") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(code, forType: .string)
                    }
                }
                Text("Enter it only on a page this sign-in opened: anyone with the code could sign in as you.")
                    .font(.caption).foregroundStyle(Color.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let url = steps.loginURL {
                Link("Open the sign-in page again", destination: url).foregroundStyle(Color.brand)
            }
        }
        .padding(Design.Space.s12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .elevatedBlock()
    }

    private func coverageBlock(_ coverage: ProviderUseResponse) -> some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            Text("Covers: " + (coverage.covers.isEmpty ? "nothing yet" : coverage.covers.map(Self.function).joined(separator: ", ")))
                .foregroundStyle(Color.textPrimary)
            ForEach(coverage.off, id: \.function) { off in
                Text("Off: \(Self.function(off.function)) — \(off.why)")
                    .foregroundStyle(Color.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("A new instance runs on \(coverage.model).").font(.callout).foregroundStyle(Color.textSecondary)
        }
        .textSelection(.enabled)
        .padding(Design.Space.s12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .elevatedBlock()
    }

    private func checkBlock(_ check: ProviderCheckResponse) -> some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            Text(check.ok ? "Every model answered." : "Not every model answered: fix what it says, Save, then Test again.")
                .font(.headline)
                .foregroundStyle(check.ok ? Color.textPrimary : Color.warning)
            ForEach(Array(check.models.enumerated()), id: \.offset) { _, row in
                Label {
                    Text((row.model ?? "no model") + (row.error.map { ": \($0)" } ?? ""))
                        .foregroundStyle(row.ok ? Color.textSecondary : Color.warning)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: row.ok ? "checkmark" : "xmark").foregroundStyle(row.ok ? Color.brand : Color.warning)
                }
                .font(.callout)
            }
        }
        .textSelection(.enabled)
        .padding(Design.Space.s12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .elevatedBlock()
        .onAppear { Announce.say(check.ok ? "Every model answered" : "Not every model answered") }
    }

    /// A covered or switched-off function in the operator's words.
    static func function(_ name: String) -> String {
        ["instances": "the instance", "judge": "judges", "judge2": "the second judge", "coder": "coding",
         "consult": "second opinions", "describe": "reading images", "recap": "recaps", "trial": "trials"][name] ?? name
    }
}

/// A choice among a step's cards: its content on an elevated block, outlined in the accent when chosen.
struct SetupCard<Content: View>: View {
    let selected: Bool
    let action: () -> Void
    @ViewBuilder let content: () -> Content

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: Design.Space.s4) { content() }
                .padding(Design.Space.s12)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .elevatedBlock()
                .overlay {
                    RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous)
                        .strokeBorder(selected ? Color.brand : Color.hairline, lineWidth: selected ? 2 : 1)
                }
                .contentShape(RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// A step's failure: the server's (or the command's) own words, and Try again.
struct SetupFailure: View {
    let message: String
    var retry: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            Text(message)
                .foregroundStyle(Color.warning)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if let retry {
                Button("Try again", action: retry).buttonStyle(ProminentAction())
            }
        }
        .onAppear { Announce.say(message) }
    }
}
