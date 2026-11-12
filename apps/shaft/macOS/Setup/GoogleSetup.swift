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
/// closed) stops the reads and the sign-in; the client's JSON and the grants, once sent, finish. The client and the
/// sign-in never overlap: a client sent while a sign-in waits ends that sign-in first (its consent would belong to the
/// earlier client), and no sign-in starts while a client is being saved.
///
/// The pickers show what the server has granted now (`sync`), and Apply sends the difference between what they say and
/// the grants the server holds at that moment (`GoogleGrants.plan`, read again before each Apply): a control cleared is
/// a revocation, a calendar's "can change events" cleared is a downgrade, and nothing already in force is sent again.
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
    /// The account the server has signed in, by its own word (the login's answer, or `google_accounts`): never the
    /// address typed in the field.
    private(set) var signedInAs: String?
    /// The last sign-in's scopes, as Google granted them, and those it did not grant.
    private(set) var granted: [String] = []
    private(set) var missing: [String] = []
    private(set) var labels: [String]?
    private(set) var loadingLabels: Work = .idle
    private(set) var calendars: [GoogleCalendars.Calendar]?
    private(set) var loadingCalendars: Work = .idle
    /// The pickers, from the server's grants (`sync`): labels and calendars ticked (by name and id), the calendars it
    /// may change, Drive's reach, Chat.
    var pickedLabels: Set<String> = []
    var pickedCalendars: Set<String> = []
    var writableCalendars: Set<String> = []
    var driveAll = false
    var chatOn = false
    private(set) var applying: Work = .idle
    /// The change being sent now, and the one the last failure stopped at.
    private(set) var sending: AccessGrant?
    private(set) var failed: AccessGrant?

    @ObservationIgnored private var generation = 0
    /// The sign-in's own identity: Cancel, a client sent and a new sign-in move it on, so the answer or a late line of
    /// one given up is dropped.
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
        for key in ["labels", "calendars"] {
            tasks[key]?.cancel()
            tasks[key] = nil
        }
        cancelSignIn()
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
        (loginURL, signedInAs, granted, missing, labels, calendars) = (nil, nil, [], [], nil, nil)
        (pickedLabels, pickedCalendars, writableCalendars, driveAll, chatOn) = ([], [], [], false, false)
        (sending, failed) = (nil, nil)
    }

    /// The sheet opened: an account the server has signed in already counts (GoogleSetupProgress.adopt) and is shown
    /// as the server names it; the address field starts from the install's email; the pickers show what is granted.
    func prepare(access: AccessResponse?, setup: MacSetup) {
        let account = access?.googleAccounts.first { $0.alias == Self.alias }
        if let account { signedInAs = account.address }
        let email = setup.state.email
        setup.edit { state in
            state.google.adopt(signedIn: account != nil)
            if state.google.address.isEmpty { state.google.address = email }
        }
        sync(access)
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

    /// `shaft google client google --json`, the JSON on stdin. A sign-in still waiting is ended first: its consent is
    /// for the client this replaces, and its answer, should it still come, is dropped (never marked signed in).
    func saveClient(json: Data, installer: Installer?, setup: MacSetup) {
        guard !savingClient.running else { return }
        guard let installer else {
            savingClient = .failed("SHAFT is not installed on this Mac: go back to Install.")
            return
        }
        cancelSignIn()
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
    /// browser (once), then the wait (the server's 300 s) ends with `{"ok": true, address, granted, missing}` or the
    /// real error. Each sign-in that succeeds reads the pickers' lists again with it (`chosen`, `store`), whether or not
    /// an earlier one had.
    func signIn(address raw: String, installer: Installer?, setup: MacSetup, chosen: Set<AccessRow>,
                store: InstancesStore?) {
        guard !signingIn.running else { return }
        guard !savingClient.running else {
            signingIn = .failed("The client is still being saved: sign in once it is.")
            return
        }
        if let problem = GoogleScopes.problem(chosen: chosen, signedIn: setup.state.google.isDone(.signIn)) {
            signingIn = .failed(problem)
            return
        }
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
            me.generation == mine && me.loginRun == run && me.signingIn.running
        }
        tasks["login"] = Task { [weak self] in
            do {
                let result = try await installer.shaft(["google", "login", Self.alias, address, "--client", Self.client,
                                                        "--json"], onLine: onLine)
                guard let self, current(self) else { return }
                switch GoogleCommand.outcome(result.stdout) {
                case .ok(let row)? where result.succeeded:
                    self.signedInAs = row["address"] as? String ?? address
                    self.granted = row["granted"] as? [String] ?? []
                    self.missing = row["missing"] as? [String] ?? []
                    self.signingIn = .idle
                    self.loginURL = nil
                    setup.edit { $0.google.finish(.signIn) }
                    self.reloadPickers(chosen: chosen, store: store)       // with this login, every time
                case .failed(let why)?: self.signingIn = .failed(why)
                default: self.signingIn = .failed(Self.failure("The Google sign-in", result))
                }
            } catch {
                guard let self, current(self) else { return }
                self.signingIn = APIError.isCancellation(error) ? .idle : .failed(error.localizedDescription)
            }
            if let self, self.generation == mine, self.loginRun == run { self.tasks["login"] = nil }
        }
    }

    /// The operator gave up (or a client replaces the one it used): the command is terminated (its listener with it),
    /// nothing is saved, and whatever it still answers is dropped.
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
                // Nothing granted yet: the primary calendar starts ticked.
                if me.pickedCalendars.isEmpty, let primary = answer.calendars.first(where: \.primary) {
                    me.pickedCalendars = [primary.id]
                }
            }
        }
    }

    /// Read the lists again: after a sign-in, and the Try again of a refused read. A read still running from before
    /// is replaced.
    func reloadPickers(chosen: Set<AccessRow>, store: InstancesStore?) {
        for key in ["labels", "calendars"] {
            tasks[key]?.cancel()
            tasks[key] = nil
        }
        (loadingLabels, loadingCalendars) = (.idle, .idle)
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

    /// The pickers as the server's grants for the account say now, on opening and after an Apply: a control is on
    /// only for a grant in force (Chat among them). The one default is the primary calendar, ticked when the lists
    /// arrive and no calendar is granted yet.
    func sync(_ access: AccessResponse?) {
        guard let access else { return }
        let mine = { (account: String) in account == Self.alias }
        pickedLabels = Set(access.mail.filter { mine($0.account) }.map(\.label))
        let cals = access.calendar.filter { mine($0.account) }
        pickedCalendars = Set(cals.map(\.calendar))
        writableCalendars = Set(cals.filter(\.write).map(\.calendar))
        driveAll = access.drive.first { mine($0.account) }?.reach == "all"
        chatOn = access.chat.contains { mine($0.account) }
    }

    // MARK: - Apply

    /// Apply: the grants the server holds now are read, and only the difference with the pickers is sent, in order
    /// (GoogleGrants.plan): grants, upgrades, downgrades and revocations. The first refusal stops there with the server's
    /// words; Try again reads the grants again, so nothing in force is sent twice and nothing left out is skipped. All
    /// sent, the grants stage is done, and each answer (the fresh access) reached the Access step on the way.
    func apply(chosen: Set<AccessRow>, store: InstancesStore?, setup: MacSetup, steps: SetupSteps) {
        guard !applying.running, let name = setup.state.createdInstance else { return }
        guard let store else {
            applying = .failed(SetupSteps.unpaired)
            return
        }
        let mine = generation
        let client = store.client
        let picks = GoogleGrants.Picks(labels: labels ?? [], picked: pickedLabels,
                                       calendars: (calendars ?? []).map(\.id), pickedCalendars: pickedCalendars,
                                       writable: writableCalendars, driveAll: driveAll, chat: chatOn)
        failed = nil
        applying = .running
        tasks["apply"] = Task { [weak self] in
            var outcome: Work = .idle
            var latest: AccessResponse?
            do {
                latest = try await client.access(name)
                guard let self, self.generation == mine, let now = latest else { return }
                steps.received(access: now)
                for change in GoogleGrants.plan(alias: Self.alias, chosen: chosen, current: now, picks: picks) {
                    self.sending = change
                    do {
                        let answer = try await client.grantAccess(name, change)
                        guard self.generation == mine else { return }
                        latest = answer
                        steps.received(access: answer)
                    } catch {
                        guard self.generation == mine else { return }
                        self.failed = change
                        outcome = .failed(APIError.from(error)?.message ?? error.localizedDescription)
                        break
                    }
                }
            } catch {
                outcome = .failed(APIError.from(error)?.message ?? error.localizedDescription)
            }
            guard let self, self.generation == mine else { return }
            self.sending = nil
            if outcome == .idle {
                // A row whose every grant was taken back is no longer ticked on Access: Continue does not wait for it.
                let on = AccessRow.granted(in: latest)
                for row in AccessRow.google where chosen.contains(row) && !on.contains(row) {
                    steps.accessChecked.remove(row)
                }
                self.sync(latest)
                setup.edit { $0.google.finish(.grants) }
            }
            self.applying = outcome
            self.tasks["apply"] = nil
        }
    }

    /// "Change the grants": the grants stage open again.
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

/// The APIs and Data Access scopes of the Google grants (supervisor/google.py SCOPES, through the `google-scopes`
/// fixture the server's tests write).
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

    /// Every scope the sign-in asks for (`shaft google login` asks for all of google.SCOPES whatever was chosen), in
    /// its order.
    static var requested: [Scope] { AccessRow.google.flatMap(scopes) }

    /// The Data Access card: every scope the sign-in asks for, each said needed or not by the grants chosen.
    static func card(for chosen: Set<AccessRow>) -> [(scope: Scope, chosen: Bool)] {
        let needed = Set(self.needed(by: chosen).map(\.url))
        return requested.map { ($0, needed.contains($0.url)) }
    }

    /// The chosen grants' scopes, in the order the Access step shows the rows.
    static func needed(by chosen: Set<AccessRow>) -> [Scope] {
        AccessRow.google.filter(chosen.contains).flatMap(scopes)
    }

    static func apis(for chosen: Set<AccessRow>) -> [API] {
        AccessRow.google.filter(chosen.contains).compactMap(api)
    }

    /// Why the chosen grants cannot be set up as they are, or nil. The sign-in confirms which account signed in by
    /// reading Gmail, Calendar or Drive (supervisor/google.py `_signed_in_as`), so Chat alone cannot sign in.
    static func problem(chosen: Set<AccessRow>, signedIn: Bool) -> String? {
        guard !signedIn, chosen.contains(.chat), chosen.isDisjoint(with: [.mail, .calendar, .drive]) else { return nil }
        return "Google Chat cannot be set up alone: the sign-in confirms which Google account it is through Gmail, "
            + "Calendar or Drive. Tick one of those too."
    }
}

/// The pickers against the grants the server holds now, as the changes that make them agree, in order: the labels (as
/// listed), the calendars (as listed: a grant, an upgrade to "can change events", a downgrade, or a revocation), Drive's
/// reach, Chat. Only the chosen rows; a label or calendar not in the lists (renamed, removed) is left as it is. Nothing
/// picked and nothing held is a state already reached (no change); nothing picked with grants held revokes them all.
enum GoogleGrants {
    struct Picks: Equatable {
        var labels: [String]
        var picked: Set<String>
        var calendars: [String]
        var pickedCalendars: Set<String>
        var writable: Set<String>
        var driveAll: Bool
        var chat: Bool
    }

    static func plan(alias: String, chosen: Set<AccessRow>, current: AccessResponse,
                     picks p: Picks) -> [AccessGrant] {
        var out: [AccessGrant] = []
        if chosen.contains(.mail) {
            let held = Set(current.mail.filter { $0.account == alias }.map(\.label))
            let wanted = Set(p.labels.filter(p.picked.contains))
            for label in p.labels {
                if wanted.contains(label), !held.contains(label) { out.append(.mail(alias: alias, label: label)) }
                if !wanted.contains(label), held.contains(label) {
                    out.append(.mail(alias: alias, label: label, action: "revoke"))
                }
            }
        }
        if chosen.contains(.calendar) {
            var held: [String: Bool] = [:]
            for g in current.calendar where g.account == alias { held[g.calendar] = g.write }
            let wanted = Set(p.calendars.filter(p.pickedCalendars.contains))
            for id in p.calendars {
                let write = p.writable.contains(id)
                switch (wanted.contains(id), held[id]) {
                case (true, nil): out.append(.calendar(alias: alias, id: id, writable: write))
                case (true, false?) where write: out.append(.calendar(alias: alias, id: id, writable: true))
                case (true, true?) where !write: out.append(.calendarWriteRevoked(alias: alias, id: id))
                case (false, _?): out.append(.calendar(alias: alias, id: id, writable: false, action: "revoke"))
                default: break
                }
            }
        }
        if chosen.contains(.drive) {
            let reach = current.drive.first { $0.account == alias }?.reach
            if reach != (p.driveAll ? "all" : "own") { out.append(.drive(alias: alias, all: p.driveAll)) }
        }
        if chosen.contains(.chat) {
            let held = current.chat.contains { $0.account == alias }
            if p.chat, !held { out.append(.chat(alias: alias)) }
            if !p.chat, held { out.append(.chat(alias: alias, action: "revoke")) }
        }
        return out
    }
}
