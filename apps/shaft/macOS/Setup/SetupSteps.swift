import AppKit
import Foundation
import Observation
import SHAFTKit

/// The wizard's work after Install, against the server this app just paired with (docs/app-api.md §18): the provider's
/// key, preset and check; the Codex sign-in; the drives; creating the instance; the access granted to it; its choice
/// for working alone and its budget; and the welcome. MacSetup owns it, so a closed window reopened finds it as it was.
///
/// Every action runs in a task kept here, under the wizard's generation. `reset()` (the wizard ended or started over)
/// moves the generation on and cancels them all: work started before changes nothing after its next suspension.
/// `stop()` (the window closed) stops only what is safe to drop, the reads, the check and the Codex sign-in; a write
/// already sent (the key, the provider, the instance, a grant, the choice, the welcome) finishes and is recorded, so the
/// app and the server never disagree on what was done. The Codex sign-in and the grants also have their own Cancel.
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
    /// What `shaft codex login --json` relayed: the sign-in page (opened in the browser) and the code to enter there.
    private(set) var loginURL: URL?
    private(set) var loginCode: String?
    private(set) var signedIn = false

    // MARK: Your instance
    private(set) var drives: [DrivePreset] = []
    private(set) var loadingDrives: Work = .idle
    private(set) var creating: Work = .idle

    // MARK: Access
    /// What the server says is granted (`GET /access/<n>`), refreshed by each grant's answer.
    private(set) var access: AccessResponse?
    /// Where the instance's Telegram stands (`GET /setup/telegram/status/<n>`): the row is on only once its bot is
    /// saved, an account is paired and the service runs.
    private(set) var telegram: TelegramStatus?
    /// While the Telegram row waits for the pairing: the code the operator sends the bot as `/pair <code>`.
    private(set) var telegramCode: String?
    /// How often the Telegram row asks whether the pairing landed (tests shorten it).
    @ObservationIgnored var telegramPoll: Duration = .seconds(2)
    private(set) var loadingAccess: Work = .idle
    /// The instance whose access was read and whose rows were pre-checked: Continue waits for it (a failed read would
    /// otherwise skip the drive's suggestions without a word), and the operator's own ticks are never redone.
    private(set) var accessPrepared: String?
    /// What the chosen drive suggests (tagged "suggested"), and the rows ticked.
    private(set) var accessSuggested: Set<AccessRow> = []
    var accessChecked: Set<AccessRow> = []
    /// The folder chosen (`NSOpenPanel`), the ScrapeBadger key and the bot token as typed: held only until sent.
    var accessFolder = ""
    var webKey = ""
    var telegramToken = ""
    private(set) var granting: Work = .idle
    /// The row being applied now, and the row the last failure stopped at.
    private(set) var grantingRow: AccessRow?
    private(set) var failedRow: AccessRow?
    /// The rows this wizard applied.
    private(set) var accessDone: Set<AccessRow> = []

    // MARK: How it works alone
    private(set) var settings: InstanceSettings?
    private(set) var loadingSettings: Work = .idle
    /// The model's daily cap as typed (empty: the supervisor's default).
    var capText = ""
    private(set) var savingAlone: Work = .idle

    // MARK: Say hello
    private(set) var greeting: Work = .idle

    @ObservationIgnored private var generation = 0
    /// The Codex sign-in's own identity: Cancel and a new sign-in move it on, so a late line of one given up is dropped.
    @ObservationIgnored private var loginRun = 0
    @ObservationIgnored private var tasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private let openURL: @MainActor (URL) -> Void

    /// `openURL` opens the Codex sign-in page; tests give one that records it.
    init(openURL: @escaping @MainActor (URL) -> Void = { NSWorkspace.shared.open($0) }) {
        self.openURL = openURL
    }

    static let unpaired = "This app is not paired with the server on this Mac: go back to Install, which pairs it."

    /// Something of the step on screen runs: Back and Continue wait.
    var busy: Bool {
        [saving, testing, signingIn, loadingDrives, creating, loadingAccess, granting, loadingSettings, savingAlone,
         greeting].contains(.running)
    }

    // MARK: - Lifecycle

    /// The window closed: the reads, the check and the sign-in stop (a reopened step asks again); writes finish.
    func stop() {
        for key in ["check", "login", "drives", "access-read", "settings"] {
            tasks[key]?.cancel()
            tasks[key] = nil
        }
        if testing.running { testing = .idle }
        if signingIn.running {
            loginRun &+= 1
            signingIn = .idle
            (loginURL, loginCode) = (nil, nil)       // that code is dead with its command
        }
        if loadingDrives.running { loadingDrives = .idle }
        if loadingAccess.running { loadingAccess = .idle }
        if loadingSettings.running { loadingSettings = .idle }
    }

    /// The wizard ended or starts over: everything stops, and nothing started before changes anything.
    func reset() {
        generation &+= 1
        for task in tasks.values { task.cancel() }
        tasks = [:]
        form = ProviderForm()
        (saving, testing, signingIn, loadingDrives, creating, loadingAccess, granting, loadingSettings, savingAlone,
         greeting) = (.idle, .idle, .idle, .idle, .idle, .idle, .idle, .idle, .idle, .idle)
        (coverage, saved, check, checked, settings) = (nil, nil, nil, nil, nil)
        (loginURL, loginCode, signedIn, drives, capText) = (nil, nil, false, [], "")
        (access, telegram, telegramCode, accessPrepared, accessSuggested, accessChecked, accessDone) =
            (nil, nil, nil, nil, [], [], [])
        (accessFolder, webKey, telegramToken, grantingRow, failedRow) = ("", "", "", nil, nil)
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
        guard !saving.running else { return }
        // Whatever was tested before no longer counts, whether this save works or not: Continue waits for a new test.
        forgetTest(saved: true)
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
            setup.edit { state in
                state.provider = request.provider
                state.model = answer.model
            }
        }
    }

    /// One live call per configured model of the saved provider.
    func test(store: InstancesStore?) {
        guard let saved, !testing.running else { return }
        forgetTest(saved: false)                     // a failed or stopped retest leaves no earlier success behind
        perform("check", \.testing, store: store) { [weak self] client, current in
            let answer = try await client.checkProvider(saved.provider)
            guard current(), let self else { return }
            self.check = answer
            self.checked = saved
        }
    }

    /// `shaft codex login --json` through the installed server (`codex login --device-auth`, no terminal): its page opens
    /// in the browser as soon as it is relayed, and its code shows here to enter there. Signed in only on its final
    /// `{"ok": true}`; a failure shows Codex's own words. Nothing of it is kept in `setup.state`: a provider is saved only
    /// by Save, so a sign-in given up (Cancel, the window closed, the app quit) leaves the step as it was.
    func signInToCodex(installer: Installer?) {
        guard !signingIn.running else { return }
        guard let installer else {
            signingIn = .failed("SHAFT is not installed on this Mac: go back to Install.")
            return
        }
        loginRun &+= 1
        let mine = generation, run = loginRun
        forgetTest(saved: false)                     // another login: the check is of the one before
        (loginURL, loginCode, signedIn) = (nil, nil, false)
        signingIn = .running
        // Each line hops to the main actor; one from an earlier sign-in (cancelled, or another wizard's) is dropped there.
        let onLine: @Sendable (String) -> Void = { [self] line in
            Task { @MainActor in self.received(line, generation: mine, run: run) }
        }
        func current(_ steps: SetupSteps) -> Bool {
            !Task.isCancelled && steps.generation == mine && steps.loginRun == run && steps.signingIn.running
        }
        tasks["login"] = Task { [weak self] in
            do {
                let result = try await installer.shaft(["codex", "login", "--json"], onLine: onLine)
                guard let self, current(self) else { return }
                // The outcome from all it printed (a line's hop to the main actor may come after this answer).
                switch CodexLogin.outcome(result.stdout) {
                case .ok? where result.succeeded:
                    self.signedIn = true
                    self.signingIn = .idle
                    self.loginCode = nil
                case .failed(let why)?:
                    self.signingIn = .failed(why)
                default:
                    let said = result.said
                    self.signingIn = .failed("The Codex sign-in failed (exit \(result.status))"
                                             + (said.isEmpty ? "." : ": \(said)"))
                }
            } catch {
                guard let self, current(self) else { return }
                self.signingIn = .failed(error.localizedDescription)
            }
            // A sign-in given up was taken off already, and a newer one may hold the key now.
            if let self, self.generation == mine, self.loginRun == run { self.tasks["login"] = nil }
        }
    }

    /// The operator gave up on the sign-in: the command is terminated (and Codex with it), nothing is recorded.
    func cancelSignIn() {
        loginRun &+= 1
        tasks["login"]?.cancel()
        tasks["login"] = nil
        if signingIn.running { signingIn = .idle }
        (loginURL, loginCode) = (nil, nil)
    }

    /// Drop the last check (and with `saved`, what the last save answered): nothing tested before a change counts.
    private func forgetTest(saved alsoSaved: Bool) {
        check = nil
        checked = nil
        if alsoSaved {
            saved = nil
            coverage = nil
        }
    }

    private func received(_ line: String, generation mine: Int, run: Int) {
        guard generation == mine, loginRun == run, signingIn.running else { return }
        switch CodexLogin.event(in: line) {
        case .url(let url)?:
            guard loginURL == nil else { return }    // one page per sign-in
            loginURL = url
            openURL(url)
        case .code(let code)?: loginCode = code
        default: break
        }
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
    /// refusal after `new` succeeded keeps its name (`startedInstance`) and says what was not set; Try again sends
    /// the same request, which the server finishes without a second `new`.
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
                setup.edit { state in
                    state.createdInstance = made.instance
                    state.startedInstance = nil
                }
                await store?.refresh()
                guard current() else { return }
                setup.go(to: .access)
            } catch APIError.server(let status, let body) where body.done?.contains("new") == true {
                guard current() else { return }
                setup.edit { $0.startedInstance = state.instanceName }
                await store?.refresh()
                let missing = ["config": "its name for you and its model", "drive": "its drive"]
                    .filter { !(body.done ?? []).contains($0.key) }.sorted { $0.key < $1.key }.map(\.value)
                throw FormProblem("\(state.instanceName) was created, but \(ListFormatter.localizedString(byJoining: missing)) "
                                  + "could not be set (HTTP \(status)): \(APIError.server(status: status, error: body).message) "
                                  + "Fix it here, then Try again: only what is left runs.")
            }
        }
    }

    // MARK: - Access

    /// The step opened: what is granted already, and (once per instance) the rows the chosen drive suggests ticked,
    /// minus those already on. The drive is the card picked on "Your instance" (`setup.state.drive`).
    func prepareAccess(store: InstancesStore?, setup: MacSetup) {
        guard let name = setup.state.createdInstance else { return }
        let drive = setup.state.drive
        let known = drives
        perform("access-read", \.loadingAccess, store: store) { [weak self] client, current in
            let presets = known.isEmpty ? try await client.setupDrives().drives : known
            let answer = try await client.access(name)
            let telegram = try await client.telegramStatus(name)
            guard current(), let self else { return }
            if self.drives.isEmpty { self.drives = presets }
            (self.access, self.telegram) = (answer, telegram)
            guard self.accessPrepared != name else { return }
            self.accessSuggested = AccessRow.suggested(by: presets.first { $0.id == drive })
            self.accessChecked = self.accessSuggested.subtracting(AccessRow.granted(in: answer, telegram: telegram))
            self.accessPrepared = name
        }
    }

    /// Access was read for this instance and its suggestions ticked: what Continue on Access waits for.
    func accessReady(_ instance: String?) -> Bool {
        instance != nil && accessPrepared == instance
    }

    /// Granted already (by the server's word) or applied by this wizard.
    func isOn(_ row: AccessRow) -> Bool {
        accessDone.contains(row) || AccessRow.granted(in: access, telegram: telegram).contains(row)
    }

    /// A folder chosen in the open panel: it is the folder row's, which it ticks.
    func choose(folder: URL) {
        accessFolder = folder.path
        accessChecked.insert(.folder)
    }

    /// Continue: the ticked rows not on yet, in `AccessRow.applied` order, each through its route. The first failure
    /// stops there with its real words (Try again sends what is left, never a row already applied); all applied, the
    /// wizard goes on. A key or a token is held only until its route has it.
    func applyAccess(store: InstancesStore?, setup: MacSetup) {
        guard let name = setup.state.createdInstance, accessPrepared == name else { return }
        let rows = AccessRow.applied.filter { accessChecked.contains($0) && !isOn($0) }
        failedRow = nil
        perform("access", \.granting, store: store) { [weak self] client, current in
            for row in rows {
                guard let self, current() else { return }
                self.grantingRow = row
                do {
                    try await self.apply(row, to: name, client: client, current: current)
                } catch {
                    if current() {
                        self.failedRow = row
                        self.grantingRow = nil
                    }
                    throw error
                }
                guard current() else { return }
                self.accessDone.insert(row)
            }
            guard current() else { return }
            self?.grantingRow = nil
            setup.go(to: .alone)
        }
    }

    private func apply(_ row: AccessRow, to name: String, client: APIClient,
                       current: @escaping @MainActor () -> Bool) async throws {
        switch row {
        case .folder:
            let path = accessFolder
            guard !path.isEmpty else { throw FormProblem("Choose the folder \(name) may read, or untick it.") }
            let answer = try await client.grantAccess(name, .folder(path))
            if current() { access = answer }
        case .browser:
            let answer = try await client.grantAccess(name, .browser)
            if current() { access = answer }
        case .web:
            let key = webKey.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !key.isEmpty else { throw FormProblem("Paste your ScrapeBadger key, or untick web search.") }
            _ = try await client.setupSecret(name: "SCRAPEBADGER_API_KEY", value: key)
            if current(), webKey.trimmingCharacters(in: .whitespacesAndNewlines) == key { webKey = "" }
        case .telegram:
            try await applyTelegram(to: name, client: client, current: current)
        case .mail, .calendar, .drive, .chat:
            break                                     // the Google setup grants these
        }
    }

    /// Telegram, whole: the bot's token (when none is saved), the Telegram service on (restarted for a new bot, so it
    /// reads it), then, with no account paired yet, a code the operator sends the bot as `/pair <code>`; the row waits
    /// until the pairing lands (asking every `telegramPoll`) or the code expires. Cancel stops the wait.
    private func applyTelegram(to name: String, client: APIClient, current: @escaping @MainActor () -> Bool) async throws {
        var newBot = false
        if telegram?.token != true {
            let token = telegramToken.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !token.isEmpty else { throw FormProblem("Paste the token @BotFather gave you, or untick Telegram.") }
            _ = try await client.setupTelegram(name: name, token: token)
            guard current() else { return }
            if telegramToken.trimmingCharacters(in: .whitespacesAndNewlines) == token { telegramToken = "" }
            newBot = true
        }
        if newBot || telegram?.service != true {
            _ = try await client.telegramService()
            guard current() else { return }
        }
        var status = try await client.telegramStatus(name)
        guard current() else { return }
        telegram = status
        guard !status.paired else { return }
        let pairing = try await client.telegramPair(name)
        guard current() else { return }
        telegram = pairing
        guard !pairing.paired, let code = pairing.code else { return }
        telegramCode = code
        defer { if current() { telegramCode = nil } }
        let until = ContinuousClock.now + .seconds(60 * (pairing.minutes ?? 10))
        while !status.paired {
            try await Task.sleep(for: telegramPoll)
            guard current() else { return }
            guard ContinuousClock.now < until else {
                throw FormProblem("The code expired before it reached \(Self.botName(pairing.bot)): Try again for a "
                                  + "new one.")
            }
            status = try await client.telegramStatus(name)
            guard current() else { return }
            telegram = status
        }
    }

    /// The bot as the operator finds it in Telegram.
    static func botName(_ bot: String?) -> String {
        bot.map { "@" + $0 } ?? "the bot"
    }

    /// "Skip all": on to "How it works alone", granting nothing; a key or a token typed here is dropped.
    func skipAccess(setup: MacSetup) {
        guard !granting.running else { return }
        (webKey, telegramToken, failedRow) = ("", "", nil)
        if granting.failure != nil { granting = .idle }
        setup.go(to: .alone)
    }

    /// Cancel while a row is applied (the browser's install may take minutes): the app stops waiting at once. A
    /// request the server already has finishes there; the step reads what is granted again when it next opens.
    func cancelAccess() {
        tasks["access"]?.cancel()
        tasks["access"] = nil
        if granting.running { granting = .idle }
        (grantingRow, telegramCode) = (nil, nil)
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
