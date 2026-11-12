import AppKit
import Foundation
import Observation
import SHAFTKit

/// The wizard's work after Install, against the server this app just paired with (docs/app-api.md §18): the provider's
/// key, preset and check; the Codex sign-in; the drives; creating the instance; its choice for working alone and its
/// budget; and the welcome. MacSetup owns it, so a closed window reopened finds it as it was.
///
/// Every action runs in a task kept here, under the wizard's generation. `reset()` (the wizard ended or started over)
/// moves the generation on and cancels them all: work started before changes nothing after its next suspension.
/// `stop()` (the window closed) stops only what is safe to drop, the reads, the check and the Codex sign-in; a write
/// already sent (the key, the provider, the instance, the choice, the welcome) finishes and is recorded, so the app and
/// the server never disagree on what was done.
@MainActor
@Observable
final class SetupSteps {
    enum Work: Equatable {
        case idle, running
        case failed(String)

        var running: Bool { self == .running }
        var failure: String? { if case .failed(let message) = self { message } else { nil } }
    }

    // MARK: Model
    var form = ProviderForm()
    private(set) var saving: Work = .idle
    /// What `/setup/provider` answered, and the request it answered.
    private(set) var coverage: ProviderUseResponse?
    private(set) var saved: ProviderRequest?
    private(set) var testing: Work = .idle
    private(set) var check: ProviderCheckResponse?
    private(set) var checked: ProviderRequest?
    private(set) var signingIn: Work = .idle
    /// What `shaft codex login --json` printed, line by line.
    private(set) var loginLines: [String] = []
    private(set) var signedIn = false

    // MARK: Your instance
    private(set) var drives: [DrivePreset] = []
    private(set) var loadingDrives: Work = .idle
    private(set) var creating: Work = .idle

    // MARK: How it works alone
    private(set) var settings: InstanceSettings?
    private(set) var loadingSettings: Work = .idle
    /// The model's daily cap as typed (empty: the supervisor's default).
    var capText = ""
    private(set) var savingAlone: Work = .idle

    // MARK: Say hello
    private(set) var greeting: Work = .idle

    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var tasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private let openURL: @MainActor (URL) -> Void

    /// `openURL` opens the Codex sign-in page; tests give one that records it.
    init(openURL: @escaping @MainActor (URL) -> Void = { NSWorkspace.shared.open($0) }) {
        self.openURL = openURL
    }

    static let unpaired = "This app is not paired with the server on this Mac: go back to Install, which pairs it."

    /// Something of the step on screen runs: Back and Continue wait.
    var busy: Bool {
        [saving, testing, signingIn, loadingDrives, creating, loadingSettings, savingAlone, greeting].contains(.running)
    }

    // MARK: - Lifecycle

    /// The window closed: the reads, the check and the sign-in stop (a reopened step asks again); writes finish.
    func stop() {
        for key in ["check", "login", "drives", "settings"] {
            tasks[key]?.cancel()
            tasks[key] = nil
        }
        if testing.running { testing = .idle }
        if signingIn.running { signingIn = .idle }
        if loadingDrives.running { loadingDrives = .idle }
        if loadingSettings.running { loadingSettings = .idle }
    }

    /// The wizard ended or starts over: everything stops, and nothing started before changes anything.
    func reset() {
        generation &+= 1
        for task in tasks.values { task.cancel() }
        tasks = [:]
        form = ProviderForm()
        (saving, testing, signingIn, loadingDrives, creating, loadingSettings, savingAlone, greeting) =
            (.idle, .idle, .idle, .idle, .idle, .idle, .idle, .idle)
        (coverage, saved, check, checked, settings) = (nil, nil, nil, nil, nil)
        (loginLines, signedIn, drives, capText) = ([], false, [], "")
    }

    /// Run `body` as the task `key`, its state in `status`: running, then idle, or the real error. `current()` says
    /// whether the work may still change anything (not cancelled, not from an earlier wizard).
    private func perform(_ key: String, _ status: ReferenceWritableKeyPath<SetupSteps, Work>, store: InstancesStore?,
                         _ body: @escaping @MainActor (_ client: APIClient, _ current: @escaping @MainActor () -> Bool) async throws -> Void) {
        guard !self[keyPath: status].running else { return }
        guard let store else {
            self[keyPath: status] = .failed(Self.unpaired)
            return
        }
        let mine = generation
        let client = store.client
        self[keyPath: status] = .running
        tasks[key] = Task { [weak self] in
            let current = { @MainActor [weak self] in !Task.isCancelled && self?.generation == mine }
            var outcome: Work = .idle
            do {
                try await body(client, current)
            } catch {
                // A cancellation is work given up on purpose (stop, reset): nothing to show.
                outcome = APIError.from(error).map { .failed(Self.said($0, error)) } ?? .idle
            }
            guard let self, current() else { return }
            if self[keyPath: status].running { self[keyPath: status] = outcome }
            self.tasks[key] = nil
        }
    }

    /// What a failure says on screen: a step's own problem as it is, the server's words otherwise.
    private static func said(_ api: APIError, _ error: any Error) -> String {
        (error as? FormProblem)?.message ?? api.message
    }

    // MARK: - Model

    /// The key first (when one was typed), then the provider's preset. The key field clears once the key is saved.
    func saveProvider(store: InstancesStore?, setup: MacSetup) {
        let request: ProviderRequest
        switch form.request() {
        case .failure(let problem):
            saving = .failed(problem.message)
            return
        case .success(let value): request = value
        }
        let secret = form.secret
        perform("provider", \.saving, store: store) { [weak self] client, current in
            if let secret {
                _ = try await client.setupSecret(name: secret.name, value: secret.value)
                guard current(), let self else { return }
                if self.form.key.trimmingCharacters(in: .whitespacesAndNewlines) == secret.value { self.form.key = "" }
            }
            let answer = try await client.setupProvider(request)
            guard current(), let self else { return }
            self.coverage = answer
            self.saved = request
            self.check = nil
            self.checked = nil
            setup.edit { state in
                state.provider = request.provider
                state.model = answer.model
            }
        }
    }

    /// One live call per configured model of the saved provider.
    func test(store: InstancesStore?) {
        guard let saved else { return }
        perform("check", \.testing, store: store) { [weak self] client, current in
            let answer = try await client.checkProvider(saved.provider)
            guard current(), let self else { return }
            self.check = answer
            self.checked = saved
        }
    }

    /// `shaft codex login --json` through the installed server, its lines shown as they come; a `{"url": …}` line
    /// opens that page in the browser.
    func signInToCodex(installer: Installer?) {
        guard !signingIn.running else { return }
        guard let installer else {
            signingIn = .failed("SHAFT is not installed on this Mac: go back to Install.")
            return
        }
        let mine = generation
        loginLines = []
        signedIn = false
        signingIn = .running
        // Each line hops to the main actor; one from an earlier sign-in (another generation) is dropped there.
        let onLine: @Sendable (String) -> Void = { [self] line in
            Task { @MainActor in self.received(line, generation: mine) }
        }
        tasks["login"] = Task { [weak self] in
            do {
                let result = try await installer.shaft(["codex", "login", "--json"], onLine: onLine)
                guard let self, !Task.isCancelled, self.generation == mine else { return }
                if result.succeeded {
                    self.signedIn = true
                    self.signingIn = .idle
                } else {
                    let said = result.said
                    self.signingIn = .failed("shaft codex login failed (exit \(result.status))" + (said.isEmpty ? "." : ": \(said)"))
                }
            } catch {
                guard let self, !Task.isCancelled, self.generation == mine else { return }
                self.signingIn = .failed(error.localizedDescription)
            }
            if let self, self.generation == mine { self.tasks["login"] = nil }
        }
    }

    private func received(_ line: String, generation mine: Int) {
        guard generation == mine, signingIn.running else { return }
        loginLines.append(line)
        if let url = CodexLogin.url(in: line) { openURL(url) }
    }

    // MARK: - Your instance

    /// The four drive cards; with none picked yet, the last ("Just improve yourself", today's default) fills the text.
    func loadDrives(store: InstancesStore?, setup: MacSetup) {
        guard drives.isEmpty else { return }
        perform("drives", \.loadingDrives, store: store) { [weak self] client, current in
            let answer = try await client.setupDrives()
            guard current(), let self else { return }
            self.drives = answer.drives
            if setup.state.drive == nil, setup.state.driveText.isEmpty, let improve = answer.drives.last {
                setup.edit { DriveText.choose(improve, into: &$0) }
            }
        }
    }

    /// `POST /setup/instance` as the operator leaves "Your instance": created paused, working on its own after the
    /// first answered reply (step 6 may change that). Once created, never again: the step shows it read-only. A
    /// refusal after `new` succeeded keeps the instance as created and says what was not done.
    func createInstance(store: InstancesStore?, setup: MacSetup) {
        let state = setup.state
        if state.createdInstance != nil {
            setup.go(to: .access)
            return
        }
        perform("create", \.creating, store: store) { client, current in
            do {
                let made = try await client.setupInstance(name: state.instanceName, operator: state.operatorName,
                                                          model: state.model, drive: state.driveText, autonomy: "own")
                guard current() else { return }
                setup.edit { $0.createdInstance = made.instance }
                await store?.refresh()
                guard current() else { return }
                setup.go(to: .access)
            } catch APIError.server(let status, let body) where body.done?.contains("new") == true {
                guard current() else { return }
                setup.edit { $0.createdInstance = state.instanceName }
                await store?.refresh()
                let missing = ["config": "its name for you and its model", "drive": "its drive"]
                    .filter { !(body.done ?? []).contains($0.key) }.sorted { $0.key < $1.key }.map(\.value)
                throw FormProblem("\(state.instanceName) was created, but \(ListFormatter.localizedString(byJoining: missing)) "
                                  + "could not be set (HTTP \(status)): \(APIError.server(status: status, error: body).message) "
                                  + "Continue goes on with it as it is; fix it in Settings, or run shaft delete "
                                  + "\(state.instanceName) and set it up again.")
            }
        }
    }

    // MARK: - How it works alone

    func loadSettings(store: InstancesStore?, name: String) {
        guard settings?.instance != name else { return }
        perform("settings", \.loadingSettings, store: store) { [weak self] client, current in
            let answer = try await client.settings(name)
            guard current(), let self else { return }
            self.settings = answer
            self.capText = Budget.text(answer.cloudCap.value)
        }
    }

    /// The choice (`/setup/autonomy`), then the model's cap when it changed (`/settings`, with what was shown as
    /// `expect`), then Say hello.
    func saveAlone(store: InstancesStore?, setup: MacSetup) {
        guard let name = setup.state.createdInstance else { return }
        let cap: Double?
        switch Budget.cloudCap(capText) {
        case .failure(let problem):
            savingAlone = .failed(problem.message)
            return
        case .success(let value): cap = value
        }
        let then = AloneChoice(autonomy: setup.state.autonomy).then
        perform("alone", \.savingAlone, store: store) { [weak self] client, current in
            _ = try await client.setupAutonomy(name, then: then)
            guard current(), let self else { return }
            if let shown = self.settings, shown.cloudCap.value != cap {
                let answer = try await client.saveSetting(name, field: .cloudCap, value: cap.map(SettingValue.double) ?? .null,
                                                          expect: shown.value(for: .cloudCap))
                guard current() else { return }
                self.settings = answer.settings
                self.capText = Budget.text(answer.settings.cloudCap.value)
            }
            setup.go(to: .hello)
        }
    }

    // MARK: - Say hello

    /// The hidden opening; then the instance's chat opens (`open`) and the wizard ends.
    func sayHello(store: InstancesStore?, setup: MacSetup, session: SHAFTSession, open: @escaping @MainActor (String) -> Void) {
        guard let name = setup.state.createdInstance else { return }
        perform("hello", \.greeting, store: store) { client, current in
            _ = try await client.setupHello(name)
            guard current() else { return }
            await store?.refresh()
            guard current() else { return }
            open(name)
            setup.finishWizard(session)
        }
    }
}
