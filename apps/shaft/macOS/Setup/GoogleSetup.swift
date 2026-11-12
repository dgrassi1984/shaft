import Foundation
import Observation
import SHAFTKit

/// The Google setup behind Access → "Set up Google…": the operator makes their own OAuth client in Google Cloud,
/// following five cards; drops the Desktop app client's JSON, which `shaft google client google --json` keeps in the
/// server's `.env` (on stdin, never here); signs in with `shaft google login google <address> --client google --json`,
/// whose consent page the app opens; then picks mail labels and calendars and grants Drive and Chat through
/// `POST /access/<n>`. Both commands run as local processes of the installed server (Installer.shaft): the login's
/// loopback listener lives in that process for the minutes the consent takes.
///
/// Where it got is `setup.state.google` (GoogleSetupProgress): a reopened app resumes at the first stage not done.
/// Every task here runs under this setup's generation: `reset()` (the wizard over) drops them all, `stop()` (the window
/// closed) stops the reads and the sign-in; the client's JSON and the grants, once sent, finish.
@MainActor
@Observable
final class GoogleSetup {
    typealias Work = SetupSteps.Work

    /// The account's alias and its OAuth client's name: one Google account from the wizard.
    static let alias = "google"
    static let client = "google"
    /// Google's client JSON is under 1 KB: a larger file is not one (the server refuses past 64 KB too).
    static let maxJSON = 64 * 1024

    private(set) var savingClient: Work = .idle
    private(set) var signingIn: Work = .idle
    /// The consent page the running sign-in relayed (opened once; "Open it again" reopens it).
    private(set) var loginURL: URL?
    /// The last sign-in's scopes, as Google granted them, and those it did not grant.
    private(set) var granted: [String] = []
    private(set) var missing: [String] = []
    private(set) var labels: [String]?
    private(set) var loadingLabels: Work = .idle
    private(set) var calendars: [GoogleCalendars.Calendar]?
    private(set) var loadingCalendars: Work = .idle
    /// The pickers: labels and calendars ticked (by name and id), the calendars it may change, Drive's reach, Chat.
    var pickedLabels: Set<String> = []
    var pickedCalendars: Set<String> = []
    var writableCalendars: Set<String> = []
    var driveAll = false
    var chatOn = true
    private(set) var applying: Work = .idle
    /// The grants this setup sent and the server accepted: Try again never sends one twice.
    private(set) var applied: [AccessGrant] = []
    /// The grant being sent now, and the one the last failure stopped at.
    private(set) var sending: AccessGrant?
    private(set) var failed: AccessGrant?

    @ObservationIgnored private var generation = 0
    /// The sign-in's own identity: Cancel and a new sign-in move it on, so a late line of one given up is dropped.
    @ObservationIgnored private var loginRun = 0
    @ObservationIgnored private var tasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private let openURL: @MainActor (URL) -> Void

    init(openURL: @escaping @MainActor (URL) -> Void) {
        self.openURL = openURL
    }

    var busy: Bool {
        [savingClient, signingIn, loadingLabels, loadingCalendars, applying].contains(.running)
    }

    // MARK: - Lifecycle

    /// The window closed: the reads and the sign-in stop (its command is terminated: nothing is saved). The client's
    /// JSON and a grant already sent finish.
    func stop() {
        for key in ["login", "labels", "calendars"] {
            tasks[key]?.cancel()
            tasks[key] = nil
        }
        if signingIn.running {
            loginRun &+= 1
            signingIn = .idle
            loginURL = nil
        }
        if loadingLabels.running { loadingLabels = .idle }
        if loadingCalendars.running { loadingCalendars = .idle }
    }

    /// The wizard ended or starts over: everything stops, and nothing started before changes anything.
    func reset() {
        generation &+= 1
        loginRun &+= 1
        for task in tasks.values { task.cancel() }
        tasks = [:]
        (savingClient, signingIn, loadingLabels, loadingCalendars, applying) = (.idle, .idle, .idle, .idle, .idle)
        (loginURL, granted, missing, labels, calendars) = (nil, [], [], nil, nil)
        (pickedLabels, pickedCalendars, writableCalendars, driveAll, chatOn) = ([], [], [], false, true)
        (applied, sending, failed) = ([], nil, nil)
    }

    /// The sheet opened: an account the server has signed in already counts (GoogleSetupProgress.adopt), and the
    /// address field starts from the install's email.
    func prepare(access: AccessResponse?, setup: MacSetup) {
        let signedIn = access?.googleAccounts.contains { $0.alias == Self.alias } == true
        let email = setup.state.email
        setup.edit { state in
            state.google.adopt(signedIn: signedIn)
            if state.google.address.isEmpty { state.google.address = email }
        }
    }

    /// A card marked done (or reopened) by the operator: the app cannot see Google Cloud.
    func mark(_ stage: GoogleSetupStage, done: Bool, setup: MacSetup) {
        setup.edit { done ? $0.google.finish(stage) : $0.google.reopen(stage) }
    }

    // MARK: - The client's JSON

    /// The JSON file dropped or chosen: read here (size checked), sent on the command's stdin, never kept.
    func saveClient(file: URL, installer: Installer?, setup: MacSetup) {
        let data: Data
        do {
            let size = try file.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            guard size <= Self.maxJSON else {
                savingClient = .failed("\(file.lastPathComponent) is \(size) bytes: not the client JSON Google Cloud "
                                       + "downloads (under 1 KB). Choose the file Download JSON saved.")
                return
            }
            data = try Data(contentsOf: file)
        } catch {
            savingClient = .failed("\(file.lastPathComponent) could not be read: \(error.localizedDescription)")
            return
        }
        saveClient(json: data, installer: installer, setup: setup)
    }

    func saveClient(json: Data, installer: Installer?, setup: MacSetup) {
        guard !savingClient.running else { return }
        guard let installer else {
            savingClient = .failed("SHAFT is not installed on this Mac: go back to Install.")
            return
        }
        let mine = generation
        savingClient = .running
        tasks["client"] = Task { [weak self] in
            var outcome: Work = .idle
            do {
                let result = try await installer.shaft(["google", "client", Self.client, "--json"], stdin: json)
                switch GoogleCommand.outcome(result.stdout) {
                case .ok? where result.succeeded:
                    guard let self, self.generation == mine else { return }
                    setup.edit { $0.google.clientSaved() }
                    (self.granted, self.missing) = ([], [])
                case .failed(let why)?: outcome = .failed(why)
                default: outcome = .failed(Self.failure("Saving the client", result))
                }
            } catch is CancellationError {
            } catch {
                outcome = .failed(error.localizedDescription)
            }
            guard let self, self.generation == mine else { return }
            self.savingClient = outcome
            self.tasks["client"] = nil
        }
    }

    // MARK: - Sign in

    /// `shaft google login google <address> --client google --json`: its `{"url"}` line opens the consent page in the
    /// browser (once), then the wait (the server's 300 s) ends with `{"ok": true, granted, missing}` or the real error.
    func signIn(address raw: String, installer: Installer?, setup: MacSetup) {
        guard !signingIn.running else { return }
        let address = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard address.contains("@"), !address.contains(where: \.isWhitespace) else {
            signingIn = .failed("Type the address of the Google account SHAFT will read (name@example.com).")
            return
        }
        guard let installer else {
            signingIn = .failed("SHAFT is not installed on this Mac: go back to Install.")
            return
        }
        setup.edit { $0.google.address = address }
        loginRun &+= 1
        let mine = generation, run = loginRun
        (loginURL, granted, missing) = (nil, [], [])
        signingIn = .running
        let onLine: @Sendable (String) -> Void = { [self] line in
            Task { @MainActor in self.received(line, generation: mine, run: run) }
        }
        func current(_ me: GoogleSetup) -> Bool {
            !Task.isCancelled && me.generation == mine && me.loginRun == run && me.signingIn.running
        }
        tasks["login"] = Task { [weak self] in
            do {
                let result = try await installer.shaft(["google", "login", Self.alias, address, "--client", Self.client,
                                                        "--json"], onLine: onLine)
                guard let self, current(self) else { return }
                switch GoogleCommand.outcome(result.stdout) {
                case .ok(let row)? where result.succeeded:
                    self.granted = row["granted"] as? [String] ?? []
                    self.missing = row["missing"] as? [String] ?? []
                    self.signingIn = .idle
                    self.loginURL = nil
                    (self.labels, self.calendars) = (nil, nil)       // read again with this login
                    setup.edit { $0.google.finish(.signIn) }
                case .failed(let why)?: self.signingIn = .failed(why)
                default: self.signingIn = .failed(Self.failure("The Google sign-in", result))
                }
            } catch {
                guard let self, current(self) else { return }
                self.signingIn = .failed(error.localizedDescription)
            }
            if let self, self.generation == mine, self.loginRun == run { self.tasks["login"] = nil }
        }
    }

    /// The operator gave up: the command is terminated (its listener with it), nothing is saved.
    func cancelSignIn() {
        loginRun &+= 1
        tasks["login"]?.cancel()
        tasks["login"] = nil
        if signingIn.running { signingIn = .idle }
        loginURL = nil
    }

    /// The consent page again (the browser tab was closed).
    func openLoginAgain() {
        if let loginURL { openURL(loginURL) }
    }

    private func received(_ line: String, generation mine: Int, run: Int) {
        guard generation == mine, loginRun == run, signingIn.running,
              case .url(let url)? = GoogleCommand.event(in: line), loginURL == nil else { return }
        loginURL = url
        openURL(url)
    }

    /// The scopes a chosen grant needs that the sign-in did not get: said on the sign-in card.
    func missingNeeded(for chosen: Set<AccessRow>) -> [String] {
        let needed = Set(GoogleScopes.needed(by: chosen).filter(\.required).map(\.name))
        return missing.filter(needed.contains)
    }

    // MARK: - Pickers

    /// What the grants may name, read live with the signed-in account: the labels for Mail, the calendars for
    /// Calendar. Each read has its own failure (a scope not granted, an API not enabled: the server's words).
    func loadPickers(chosen: Set<AccessRow>, store: InstancesStore?) {
        if chosen.contains(.mail), labels == nil {
            read("labels", \.loadingLabels, store: store) { client, me in
                let answer = try await client.googleLabels(Self.alias)
                me.labels = answer.labels
            }
        }
        if chosen.contains(.calendar), calendars == nil {
            read("calendars", \.loadingCalendars, store: store) { client, me in
                let answer = try await client.googleCalendars(Self.alias)
                me.calendars = answer.calendars
                if me.pickedCalendars.isEmpty, let primary = answer.calendars.first(where: \.primary) {
                    me.pickedCalendars = [primary.id]
                }
            }
        }
    }

    /// Read the lists again (the Try again of a refused read).
    func reloadPickers(chosen: Set<AccessRow>, store: InstancesStore?) {
        (labels, calendars) = (nil, nil)
        loadPickers(chosen: chosen, store: store)
    }

    private func read(_ key: String, _ status: ReferenceWritableKeyPath<GoogleSetup, Work>, store: InstancesStore?,
                      _ body: @escaping @MainActor (APIClient, GoogleSetup) async throws -> Void) {
        guard !self[keyPath: status].running else { return }
        guard let store else {
            self[keyPath: status] = .failed(SetupSteps.unpaired)
            return
        }
        let mine = generation
        let client = store.client
        self[keyPath: status] = .running
        tasks[key] = Task { [weak self] in
            var outcome: Work = .idle
            do {
                guard let self else { return }
                try await body(client, self)
            } catch {
                outcome = APIError.from(error).map { .failed($0.message) } ?? .idle
            }
            guard let self, !Task.isCancelled, self.generation == mine else { return }
            if self[keyPath: status].running { self[keyPath: status] = outcome }
            self.tasks[key] = nil
        }
    }

    // MARK: - Apply

    /// The grants the pickers say, in order (GoogleGrants.requests).
    func requests(chosen: Set<AccessRow>) -> Result<[AccessGrant], FormProblem> {
        GoogleGrants.requests(alias: Self.alias, chosen: chosen, labels: labels ?? [], picked: pickedLabels,
                              calendars: (calendars ?? []).map(\.id), pickedCalendars: pickedCalendars,
                              writable: writableCalendars, driveAll: driveAll, chat: chatOn)
    }

    /// Apply: each grant not sent yet, in order, through `POST /access/<n>`; the first refusal stops there with the
    /// server's words (Try again sends what is left). All accepted, the grants stage is done and the sheet may close.
    /// Each answer is the fresh access, handed to the Access step so its rows show what is on.
    func apply(chosen: Set<AccessRow>, store: InstancesStore?, setup: MacSetup, steps: SetupSteps) {
        guard !applying.running, let name = setup.state.createdInstance else { return }
        let all: [AccessGrant]
        switch requests(chosen: chosen) {
        case .failure(let problem):
            applying = .failed(problem.message)
            return
        case .success(let value): all = value
        }
        guard let store else {
            applying = .failed(SetupSteps.unpaired)
            return
        }
        let left = all.filter { !applied.contains($0) }
        let mine = generation
        let client = store.client
        failed = nil
        applying = .running
        tasks["apply"] = Task { [weak self] in
            var outcome: Work = .idle
            for grant in left {
                guard let self, self.generation == mine else { return }
                self.sending = grant
                do {
                    let answer = try await client.grantAccess(name, grant)
                    guard self.generation == mine else { return }
                    self.applied.append(grant)
                    steps.received(access: answer)
                } catch {
                    guard self.generation == mine else { return }
                    self.failed = grant
                    outcome = .failed(APIError.from(error)?.message ?? error.localizedDescription)
                    break
                }
            }
            guard let self, self.generation == mine else { return }
            self.sending = nil
            if outcome == .idle {
                // Chat turned off in the sheet: its row on Access is unticked, so Continue does not wait for it.
                if !self.chatOn { steps.accessChecked.remove(.chat) }
                setup.edit { $0.google.finish(.grants) }
            }
            self.applying = outcome
            self.tasks["apply"] = nil
        }
    }

    /// "Grant more": the grants stage open again, what was sent kept (never sent twice).
    func grantMore(setup: MacSetup) {
        setup.edit { $0.google.reopen(.grants) }
        if applying.failure != nil { applying = .idle }
    }

    private static func failure(_ what: String, _ result: ProcessResult) -> String {
        let said = result.said
        return "\(what) failed (exit \(result.status))" + (said.isEmpty ? "." : ": \(said)")
    }
}

/// What `shaft google client|login --json` print, one JSON object a line: login's consent page first (`url`), then
/// `{"ok": true, …}` or `{"ok": false, "error"}`.
enum GoogleCommand {
    enum Event {
        case url(URL)
        case ok([String: Any])
        case failed(String)
    }

    static func event(in line: String) -> Event? {
        guard let object = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any] else { return nil }
        if let text = object["url"] as? String {
            // Google's consent page only: nothing else is ever opened.
            guard let url = URL(string: text), url.scheme?.lowercased() == "https",
                  url.host?.lowercased() == "accounts.google.com" else { return nil }
            return .url(url)
        }
        if let ok = object["ok"] as? Bool {
            return ok ? .ok(object) : .failed((object["error"] as? String) ?? "The command failed, without a reason.")
        }
        return nil
    }

    /// How the command ended, from everything it printed: its last `ok` line, nil when it printed none.
    static func outcome(_ stdout: Data) -> Event? {
        String(decoding: stdout, as: UTF8.self).split(whereSeparator: \.isNewline).compactMap { line -> Event? in
            switch event(in: String(line)) {
            case .ok(let row)?: .ok(row)
            case .failed(let why)?: .failed(why)
            default: nil
            }
        }.last
    }
}

/// The APIs and Data Access scopes each Google grant needs (supervisor/google.py SCOPES; a test there checks that
/// every scope the login asks for is listed here).
enum GoogleScopes {
    struct Scope: Equatable {
        let url: String
        /// Required by the grant the wizard makes; the others serve later grants (mail drafts, calendar writes).
        let required: Bool
        var name: String { String(url.split(separator: "/").last ?? "") }
    }

    struct API: Equatable {
        let title: String
        /// Its id in the API Library (console.cloud.google.com/apis/library/<id>).
        let id: String
        var library: URL { URL(string: "https://console.cloud.google.com/apis/library/\(id)")! }
    }

    static func scopes(_ row: AccessRow) -> [Scope] {
        switch row {
        case .mail:
            [Scope(url: "https://www.googleapis.com/auth/gmail.readonly", required: true),
             Scope(url: "https://www.googleapis.com/auth/gmail.compose", required: false),
             Scope(url: "https://www.googleapis.com/auth/gmail.modify", required: false)]
        case .calendar:
            [Scope(url: "https://www.googleapis.com/auth/calendar.readonly", required: true),
             Scope(url: "https://www.googleapis.com/auth/calendar.events", required: false)]
        case .drive:
            [Scope(url: "https://www.googleapis.com/auth/drive.readonly", required: true)]
        case .chat:
            [Scope(url: "https://www.googleapis.com/auth/chat.spaces.readonly", required: true),
             Scope(url: "https://www.googleapis.com/auth/chat.messages.readonly", required: true),
             Scope(url: "https://www.googleapis.com/auth/chat.memberships.readonly", required: true)]
        case .folder, .browser, .web, .telegram: []
        }
    }

    static func api(_ row: AccessRow) -> API? {
        switch row {
        case .mail: API(title: "Gmail API", id: "gmail.googleapis.com")
        case .calendar: API(title: "Google Calendar API", id: "calendar-json.googleapis.com")
        case .drive: API(title: "Google Drive API", id: "drive.googleapis.com")
        case .chat: API(title: "Google Chat API", id: "chat.googleapis.com")
        case .folder, .browser, .web, .telegram: nil
        }
    }

    /// The chosen grants' scopes and APIs, in the order the Access step shows the rows.
    static func needed(by chosen: Set<AccessRow>) -> [Scope] {
        AccessRow.google.filter(chosen.contains).flatMap(scopes)
    }

    static func apis(for chosen: Set<AccessRow>) -> [API] {
        AccessRow.google.filter(chosen.contains).compactMap(api)
    }
}

/// The pickers as the grants they send, in order: the labels (as listed), the calendars (as listed, each writable or
/// not), Drive's reach, Chat. Only the chosen rows; a chosen row with nothing picked is the operator's to fix.
enum GoogleGrants {
    static func requests(alias: String, chosen: Set<AccessRow>, labels: [String], picked: Set<String>,
                         calendars: [String], pickedCalendars: Set<String>, writable: Set<String>,
                         driveAll: Bool, chat: Bool) -> Result<[AccessGrant], FormProblem> {
        var out: [AccessGrant] = []
        if chosen.contains(.mail) {
            let names = labels.filter(picked.contains)
            guard !names.isEmpty else {
                return .failure(FormProblem("Pick at least one mail label, or untick Mail labels on Access."))
            }
            out += names.map { AccessGrant.mail(alias: alias, label: $0) }
        }
        if chosen.contains(.calendar) {
            let ids = calendars.filter(pickedCalendars.contains)
            guard !ids.isEmpty else {
                return .failure(FormProblem("Pick at least one calendar, or untick Calendars on Access."))
            }
            out += ids.map { AccessGrant.calendar(alias: alias, id: $0, writable: writable.contains($0)) }
        }
        if chosen.contains(.drive) { out.append(.drive(alias: alias, all: driveAll)) }
        if chosen.contains(.chat), chat { out.append(.chat(alias: alias)) }
        return .success(out)
    }
}
