import SHAFTKit
import SwiftUI

/// The wizard's "How it works alone" step: whether the instance works on its own after you answer its welcome, or
/// only when you talk to it (`/setup/autonomy`), and its daily budgets: the model's, which Settings changes too
/// (`cloud_cap`), and the others with the supervisor setting that holds them.
struct AloneStepView: View {
    let setup: MacSetup
    let session: SHAFTSession
    @Bindable var steps: SetupSteps

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            Text("How it works alone").font(.largeTitle.bold()).foregroundStyle(Color.textPrimary)
            Picker("How it works alone", selection: choice) {
                ForEach([AloneChoice.talk, .own]) { option in
                    Text(option.title(minutes: steps.settings?.wakeInterval.effective)).tag(option)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(maxWidth: 480)
            .disabled(steps.savingAlone.running)
            Text(explanation)
                .foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            budgets
            if let failure = steps.savingAlone.failure {
                SetupFailure(message: failure) { steps.saveAlone(store: session.store, setup: setup) }
            }
        }
        .onAppear { load() }
    }

    private var name: String { setup.state.createdInstance ?? "it" }

    private var choice: Binding<AloneChoice> {
        Binding(get: { AloneChoice(autonomy: setup.state.autonomy) },
                set: { value in setup.edit { $0.autonomy = value.rawValue } })
    }

    private var explanation: String {
        switch AloneChoice(autonomy: setup.state.autonomy) {
        case .own:
            "Once you have answered its welcome, \(name) wakes on its own: it works on its goals between your messages, "
                + "and sleeps each night. Until then it waits for you."
        case .talk:
            "\(name) stays paused: it answers when you write to it and does nothing on its own. Resume it in Settings "
                + "whenever you want it to work alone."
        }
    }

    private func load() {
        if let name = setup.state.createdInstance { steps.loadSettings(store: session.store, name: name) }
    }

    private var budgets: some View {
        VStack(alignment: .leading, spacing: Design.Space.s12) {
            Text("Daily budgets").font(.headline).foregroundStyle(Color.textPrimary)
            if let failure = steps.loadingSettings.failure {
                SetupFailure(message: failure, retry: load)
            }
            HStack(alignment: .firstTextBaseline, spacing: Design.Space.s12) {
                Text("Model").foregroundStyle(Color.textPrimary).frame(width: 130, alignment: .leading)
                Text("$").foregroundStyle(Color.textSecondary)
                TextField("default", text: $steps.capText)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 90)
                    .disabled(steps.settings == nil || steps.savingAlone.running)
                    .accessibilityLabel("The model's daily budget in dollars")
                Text(spent).font(.callout).foregroundStyle(Color.textSecondary)
            }
            if case .failure(let problem) = Budget.cloudCap(steps.capText) {
                Text(problem.message).font(.callout).foregroundStyle(Color.warning)
            }
            Text("Empty: the supervisor's default (SHAFT_CLOUD_DAILY_USD, $8 unless you changed it). Settings changes "
                 + "it later too.")
                .font(.caption).foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(Budget.fixed, id: \.setting) { budget in
                HStack(alignment: .firstTextBaseline, spacing: Design.Space.s12) {
                    Text(budget.what).foregroundStyle(Color.textPrimary).frame(width: 130, alignment: .leading)
                    Text("$\(budget.dollars) by default").foregroundStyle(Color.textSecondary)
                    Text(budget.setting).font(Design.Text.metadata).foregroundStyle(Color.textTertiary).textSelection(.enabled)
                }
            }
            HStack(alignment: .firstTextBaseline, spacing: Design.Space.s12) {
                Text("Nightly sleep").foregroundStyle(Color.textPrimary).frame(width: 130, alignment: .leading)
                Text("from 04:00, every instance").foregroundStyle(Color.textSecondary)
            }
            Text("Those three are supervisor settings for every instance, kept at their defaults by this setup; the "
                 + "sleep time is fixed in this version.")
                .font(.caption).foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Label("Budgets are checks before work starts, not billing limits.", systemImage: "dollarsign.circle")
                .foregroundStyle(Color.textPrimary)
            if setup.state.provider == ProviderKind.codex.rawValue {
                Text("With ChatGPT (Codex), the models on your plan spend its weekly share, not dollars: these budgets "
                     + "count only the calls that cost money.")
                    .foregroundStyle(Color.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(Design.Space.s16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .elevatedBlock()
    }

    private var spent: String {
        guard let settings = steps.settings else { return steps.loadingSettings.running ? "reading…" : "" }
        return String(format: "$%.2f spent today", settings.cloudCap.spentToday)
    }
}
