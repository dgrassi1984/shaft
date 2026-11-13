import Foundation
import SHAFTKit
import Testing

/// Access → "Set up Google…" (macOS/Setup/GoogleSetup.swift): the client's JSON on `shaft google client`'s stdin, the
/// sign-in through `shaft google login --json`, the pickers as grants, and Apply in order, stopping at the first
/// refusal. Against the stub server and scripted commands: no real ~/SHAFT, Keychain, browser, Google or network.
@MainActor
struct GoogleSetupTests {
    typealias Fixture = SetupStepsTests.Fixture

    nonisolated static let consent = #"{"url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=1&state=s"}"#
    nonisolated static let signedIn = #"""
    {"ok": true, "alias": "google", "address": "ada@example.com", "client": "google", "granted": ["gmail.readonly", "calendar.readonly", "drive.readonly"], "missing": ["gmail.compose", "chat.spaces.readonly"]}
    """#
    nonisolated static let desktopJSON = Data(#"{"installed": {"client_id": "1.apps.googleusercontent.com", "client_secret": "GOCSPX-never-kept"}}"#.utf8)

    func until(_ done: () -> Bool) async {
        for _ in 0..<800 where !done() { try? await Task.sleep(for: .milliseconds(5)) }
    }

    nonisolated static let me = "ada@example.com"

    /// Paired, the instance made, the cards, the client and the sign-in done: the grants card.
    func signedInFixture(runner: any ProcessRunning = NoProcesses()) throws -> Fixture {
        try Fixture(step: .access, runner: runner) { state in
            state.createdInstance = "ledger"
            state.google.adopt(signedIn: true)
        }
    }

    // MARK: - The Data Access card

    /// The card lists every scope the sign-in asks for (supervisor/google.py SCOPES, through the file the server's tests
    /// check: supervisor/tests/google_scopes.json), whatever single service was chosen; the chosen one's are marked.
    @Test func theScopesCardListsEveryRequestedScopeForEachChoice() throws {
        let file = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: "supervisor/tests/google_scopes.json")
        let object = try #require(try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
        let requested = try #require(object["scopes"] as? [String])
        #expect(requested.count == 9)
        for row in AccessRow.google {
            let card = GoogleScopes.card(for: [row])
            #expect(card.map(\.scope.url) == requested, "\(row)")
            #expect(card.filter(\.chosen).map(\.scope.url) == GoogleScopes.scopes(row).map(\.url), "\(row)")
        }
        #expect(GoogleScopes.card(for: []).map(\.scope.url) == requested)
        #expect(GoogleScopes.apis(for: [.chat, .mail]).map(\.id) == ["gmail.googleapis.com", "chat.googleapis.com"])
    }

    // MARK: - Chat alone

    @Test func chatAloneIsExplainedAndCannotSignIn() async throws {
        let said = "Google Chat cannot be set up alone: the sign-in confirms which Google account it is through Gmail, "
            + "Calendar or Drive. Tick one of those too."
        #expect(GoogleScopes.problem(chosen: [.chat], signedIn: false) == said)
        #expect(GoogleScopes.problem(chosen: [.chat, .drive], signedIn: false) == nil)
        #expect(GoogleScopes.problem(chosen: [.mail], signedIn: false) == nil)
        #expect(GoogleScopes.problem(chosen: [.chat], signedIn: true) == nil)     // signed in already: Chat can change
        let runner = ScriptRunner(["google login": ([Self.consent, Self.signedIn], 0)])
        let f = try Fixture(step: .access, runner: runner) { $0.createdInstance = "ledger" }
        f.setup.steps.google.signIn(address: "ada@example.com", installer: try runner.installer(home: f.home),
                                    setup: f.setup, chosen: [.chat], store: f.store)
        #expect(f.setup.steps.google.signingIn == .failed(said))
        #expect(runner.ran.isEmpty)
    }

    // MARK: - Pickers against the server's grants

    @Test func thePlanIsTheDifferenceWithWhatTheServerHolds() throws {
        let current = try GrantsFake(mail: ["Receipts"], calendars: ["a@x": true, "team": false], drive: "all",
                                     chat: true).access()
        let picks = GoogleGrants.Picks(labels: ["Assistant", "Receipts", "Travel"], picked: ["Assistant"],
                                       calendars: ["a@x", "team", "home"], pickedCalendars: ["a@x", "home"],
                                       writable: ["home"], driveAll: false, chat: false)
        let plan = GoogleGrants.plan(alias: "google", chosen: [.mail, .calendar, .drive, .chat], current: current,
                                     picks: picks)
        #expect(plan == [.mail(alias: "google", label: "Assistant"),
                         .mail(alias: "google", label: "Receipts", action: "revoke"),
                         .calendarWriteRevoked(alias: "google", id: "a@x"),
                         .calendar(alias: "google", id: "team", writable: false, action: "revoke"),
                         .calendar(alias: "google", id: "home", writable: true),
                         .drive(alias: "google", all: false),
                         .chat(alias: "google", action: "revoke")])
        // What the server holds already is never sent again; a row not chosen is left alone.
        let same = GoogleGrants.Picks(labels: ["Receipts"], picked: ["Receipts"], calendars: ["a@x", "team"],
                                      pickedCalendars: ["a@x", "team"], writable: ["a@x"], driveAll: true, chat: true)
        #expect(GoogleGrants.plan(alias: "google", chosen: [.mail, .calendar, .drive, .chat], current: current,
                                  picks: same).isEmpty)
        #expect(GoogleGrants.plan(alias: "google", chosen: [.drive], current: current, picks: picks)
                == [.drive(alias: "google", all: false)])
        // Fix round 2: nothing picked and nothing held is reached already (no change), never a refusal; nothing picked
        // with labels held revokes them all.
        let nothing = GoogleGrants.Picks(labels: ["Assistant", "Receipts"], picked: [], calendars: ["a@x"],
                                         pickedCalendars: [], writable: [], driveAll: false, chat: false)
        #expect(GoogleGrants.plan(alias: "google", chosen: [.mail, .calendar], current: try GrantsFake().access(),
                                  picks: nothing).isEmpty)
        #expect(GoogleGrants.plan(alias: "google", chosen: [.mail], current: current, picks: nothing)
                == [.mail(alias: "google", label: "Receipts", action: "revoke")])
    }

    /// Fix round 2: after the last label was taken back, a calendar refusal made every Try again fail ("Pick at least
    /// one mail label"): the plan refused an empty choice with nothing held. Now that is no change, and the retry
    /// sends only the calendar.
    @Test func clearingTheLastLabelThenARefusalRetriesOnlyWhatIsLeft() async throws {
        let f = try signedInFixture()
        let fake = GrantsFake(mail: ["Receipts"], calendars: [Self.me: false])
        fake.refuse = ["calendar team"]
        f.server.answer { fake.handle($0) }
        let steps = f.setup.steps, google = steps.google
        steps.received(access: try fake.access())
        steps.accessChecked = [.mail, .calendar]
        google.prepare(access: try fake.access(), setup: f.setup)
        google.loadPickers(chosen: steps.googleChosen, store: f.store)
        await until { google.labels != nil && google.calendars != nil }
        google.pickedLabels = []                                         // the last label cleared
        google.pickedCalendars.insert("team")
        google.apply(chosen: steps.googleChosen, store: f.store, setup: f.setup, steps: steps)
        await until { !google.applying.running }
        #expect(google.applying == .failed("not granted: Google answered HTTP 403: no access"))
        #expect(fake.mail.isEmpty)                                       // the revoke went through first
        #expect(fake.posts.map { $0["grant"] ?? "" } == ["mail", "calendar"])

        fake.refuse = []
        google.apply(chosen: steps.googleChosen, store: f.store, setup: f.setup, steps: steps)
        await until { !google.applying.running }
        #expect(google.applying == .idle)
        #expect(fake.posts.map { $0["grant"] ?? "" } == ["mail", "calendar", "calendar"])   // no Mail change again
        #expect(fake.calendars == [Self.me: false, "team": false])
        #expect(f.setup.state.google.isDone(.grants))
        #expect(!steps.accessChecked.contains(.mail))                    // no label left: off on Access
    }

    /// Fix round 2: Chat showed on at opening without a Chat grant. Every control starts from the server's grants.
    @Test func chatStartsAsTheServerHasIt() async throws {
        let f = try signedInFixture()
        let google = f.setup.steps.google
        google.prepare(access: try GrantsFake(drive: "own").access(), setup: f.setup)
        #expect(!google.chatOn)
        #expect(!google.driveAll)
        #expect(google.pickedLabels.isEmpty)
        google.prepare(access: try GrantsFake(drive: "all", chat: true).access(), setup: f.setup)
        #expect(google.chatOn)
        #expect(google.driveAll)
    }

    /// Fix round 1: Apply remembered what it had sent, so Drive own → all → own left "all" in force while saying done.
    @Test func driveOwnAllOwnEndsAsOwn() async throws {
        let f = try signedInFixture()
        let fake = GrantsFake()
        f.server.answer { fake.handle($0) }
        let steps = f.setup.steps, google = steps.google
        steps.accessChecked = [.drive]
        google.prepare(access: try fake.access(), setup: f.setup)
        for all in [false, true, false] {
            google.grantMore(setup: f.setup)
            google.driveAll = all
            google.apply(chosen: steps.googleChosen, store: f.store, setup: f.setup, steps: steps)
            await until { !google.applying.running }
            #expect(google.applying == .idle)
            #expect(fake.drive == (all ? "all" : "own"))
        }
        #expect(f.setup.state.google.isDone(.grants))
        #expect(!google.driveAll)
    }

    /// Fix round 1: clearing a control sent nothing (Chat off), or a plain read grant the server kept as a write
    /// (calendar). Now every control starts from the server's grants and a cleared one is a revocation or a downgrade.
    @Test func clearingAControlRevokesOrDowngrades() async throws {
        let f = try signedInFixture()
        let fake = GrantsFake(mail: ["Receipts", "Travel"], calendars: [Self.me: true], drive: "own", chat: true)
        fake.labels = ["Assistant", "Receipts", "Travel"]
        f.server.answer { fake.handle($0) }
        let steps = f.setup.steps, google = steps.google
        steps.received(access: try fake.access())
        #expect(steps.googleChosen == [.mail, .calendar, .drive, .chat])     // on already, so shown
        google.prepare(access: try fake.access(), setup: f.setup)
        #expect(google.pickedLabels == ["Receipts", "Travel"])
        #expect(google.pickedCalendars == [Self.me])
        #expect(google.writableCalendars == [Self.me])
        #expect(google.chatOn)
        google.loadPickers(chosen: steps.googleChosen, store: f.store)
        await until { google.labels != nil && google.calendars != nil }

        google.pickedLabels.remove("Travel")
        google.writableCalendars = []
        google.chatOn = false
        google.apply(chosen: steps.googleChosen, store: f.store, setup: f.setup, steps: steps)
        await until { !google.applying.running }
        #expect(google.applying == .idle)
        #expect(fake.mail == ["Receipts"])
        #expect(fake.calendars == [Self.me: false])                     // still read, no longer written
        #expect(fake.chat == false)
        #expect(fake.drive == "own")
        #expect(fake.posts == [["grant": "mail", "action": "revoke", "alias": "google", "what": "Travel"],
                               ["grant": "calendar", "action": "revoke", "alias": "google", "what": Self.me,
                                "writable": "true"],
                               ["grant": "chat", "action": "revoke", "alias": "google"]])
        #expect(!steps.accessChecked.contains(.chat))                   // nothing left of it: off on Access
        #expect(!steps.isOn(.chat))
        #expect(!google.chatOn)                                          // the controls say what the server holds
    }

    @Test func applyStopsAtTheFirstRefusalAndTryAgainSendsOnlyWhatIsLeft() async throws {
        let f = try signedInFixture()
        let fake = GrantsFake()
        fake.refuse = ["calendar team"]
        f.server.answer { fake.handle($0) }
        let steps = f.setup.steps, google = steps.google
        steps.accessChecked = [.mail, .calendar, .drive, .chat]
        google.prepare(access: try fake.access(), setup: f.setup)
        google.loadPickers(chosen: steps.googleChosen, store: f.store)
        await until { google.labels != nil && google.calendars != nil }
        #expect(google.pickedCalendars == [Self.me])                    // nothing granted: the primary to start
        google.pickedLabels = ["Receipts"]
        google.pickedCalendars.insert("team")
        google.chatOn = true                                             // ticked in the sheet (off to start)

        google.apply(chosen: steps.googleChosen, store: f.store, setup: f.setup, steps: steps)
        await until { !google.applying.running }
        #expect(google.applying == .failed("not granted: Google answered HTTP 403: no access"))
        #expect(google.failed == .calendar(alias: "google", id: "team", writable: false))
        #expect(fake.posts.map { $0["grant"] ?? "" } == ["mail", "calendar", "calendar"])
        #expect(!f.setup.state.google.isDone(.grants))

        fake.refuse = []                                                // Try again: read again, send what is left
        google.apply(chosen: steps.googleChosen, store: f.store, setup: f.setup, steps: steps)
        await until { !google.applying.running }
        #expect(google.applying == .idle)
        #expect(fake.posts.map { $0["grant"] ?? "" } == ["mail", "calendar", "calendar", "calendar", "drive", "chat"])
        #expect(fake.mail == ["Receipts"])
        #expect(fake.calendars == [Self.me: false, "team": false])
        #expect((fake.drive, fake.chat) == ("own", true))
        #expect(f.setup.state.google.isDone(.grants))
        #expect(steps.isOn(.drive))                                      // each answer reached the Access step
    }

    // MARK: - The client's JSON

    @Test func theClientJSONGoesOnStdinAndIsNeverKept() async throws {
        let runner = ScriptRunner(["google client": ([#"{"ok": true, "client": "google", "login_again": []}"#], 0)])
        let f = try Fixture(step: .access, runner: runner) { $0.createdInstance = "ledger" }
        let installer = try runner.installer(home: f.home)
        let google = f.setup.steps.google
        f.setup.edit { $0.google.finish(.signIn) }                     // an earlier sign-in, with another client
        google.saveClient(json: Self.desktopJSON, installer: installer, setup: f.setup)
        await until { !google.savingClient.running }
        #expect(google.savingClient == .idle)
        #expect(runner.ran.map(\.args) == [["google", "client", "google", "--json"]])
        #expect(runner.ran.first?.stdin == Self.desktopJSON)
        #expect(f.setup.state.google.isDone(.json))
        #expect(f.setup.state.google.resume == .project)               // the cards were never marked
        #expect(!f.setup.state.google.isDone(.signIn))                 // a new client: sign in again
        let kept = String(decoding: try #require(f.defaults.data(forKey: SetupStateStore.key)), as: UTF8.self)
        #expect(!kept.contains("GOCSPX"))
        #expect(!kept.contains("apps.googleusercontent.com"))
    }

    @Test func aWebClientJSONShowsTheServersFix() async throws {
        let refused = #"{"ok": false, "error": "not saved: This is a Web application client: create a Desktop app client instead"}"#
        let runner = ScriptRunner(["google client": ([refused], 1)])
        let f = try Fixture(step: .access, runner: runner) { $0.createdInstance = "ledger" }
        let google = f.setup.steps.google
        google.saveClient(json: Data("{}".utf8), installer: try runner.installer(home: f.home), setup: f.setup)
        await until { !google.savingClient.running }
        #expect(google.savingClient == .failed("not saved: This is a Web application client: create a Desktop app client instead"))
        #expect(!f.setup.state.google.isDone(.json))
    }

    // MARK: - Sign in

    @Test func signInOpensGooglesPageOnceAndRecordsWhatWasGranted() async throws {
        let runner = ScriptRunner(["google login": ([Self.consent, Self.consent, Self.signedIn], 0)])
        let f = try Fixture(step: .access, runner: runner) { $0.createdInstance = "ledger" }
        let google = f.setup.steps.google
        google.signIn(address: " ada@example.com ", installer: try runner.installer(home: f.home), setup: f.setup,
                      chosen: [.drive], store: f.store)
        await until { !google.signingIn.running }
        #expect(google.signingIn == .idle)
        #expect(runner.ran.map(\.args) == [["google", "login", "google", "ada@example.com", "--client", "google", "--json"]])
        #expect(f.opened.all == [URL(string: "https://accounts.google.com/o/oauth2/v2/auth?client_id=1&state=s")!])
        #expect(f.setup.state.google.isDone(.signIn))
        #expect(f.setup.state.google.address == "ada@example.com")
        #expect(google.granted == ["gmail.readonly", "calendar.readonly", "drive.readonly"])
        #expect(google.signedInAs == "ada@example.com")
        // What a chosen grant needs and was not granted is said; a scope for later grants is not.
        #expect(google.missingNeeded(for: [.mail, .chat]) == ["chat.spaces.readonly"])
    }

    @Test func aSignInThatTimesOutShowsTheServersWords() async throws {
        let timeout = #"{"ok": false, "error": "no answer from the browser in 300 s: nothing saved"}"#
        let runner = ScriptRunner(["google login": ([Self.consent, timeout], 1)])
        let f = try Fixture(step: .access, runner: runner) { $0.createdInstance = "ledger" }
        let google = f.setup.steps.google
        google.signIn(address: "ada@example.com", installer: try runner.installer(home: f.home), setup: f.setup,
                      chosen: [.drive], store: f.store)
        await until { !google.signingIn.running }
        #expect(google.signingIn == .failed("no answer from the browser in 300 s: nothing saved"))
        #expect(!f.setup.state.google.isDone(.signIn))
        // Not an address: nothing runs.
        google.signIn(address: "ada", installer: try runner.installer(home: f.home), setup: f.setup, chosen: [.drive],
                      store: f.store)
        #expect(google.signingIn.failure?.hasPrefix("Type the address") == true)
        #expect(runner.ran.count == 1)
    }

    @Test func cancellingTheSignInEndsTheCommandAndKeepsNothing() async throws {
        let runner = ScriptRunner(["google login": ([Self.consent], nil)])
        let f = try Fixture(step: .access, runner: runner) { $0.createdInstance = "ledger" }
        let google = f.setup.steps.google
        google.signIn(address: "ada@example.com", installer: try runner.installer(home: f.home), setup: f.setup,
                      chosen: [.drive], store: f.store)
        await until { google.loginURL != nil }
        google.cancelSignIn()
        await until { runner.cancelled }
        #expect(runner.cancelled)
        #expect(google.signingIn == .idle)
        #expect(google.loginURL == nil)
        #expect(!f.setup.state.google.isDone(.signIn))
        #expect(!f.setup.steps.busy)
    }

    /// Fix round 1: "Sign in again" dropped the lists but nothing read them again while the grants card was already
    /// showing. Every successful sign-in reads them again itself.
    @Test func everySignInReadsThePickersAgain() async throws {
        let runner = ScriptRunner(["google login": ([Self.consent, Self.signedIn], 0)])
        let f = try signedInFixture(runner: runner)
        let fake = GrantsFake()
        f.server.answer { fake.handle($0) }
        let google = f.setup.steps.google
        let installer = try runner.installer(home: f.home)
        google.loadPickers(chosen: [.mail], store: f.store)
        await until { google.labels != nil }
        #expect(google.labels == ["Assistant", "Receipts"])
        fake.labels = ["Assistant", "Receipts", "Travel"]                // the same card on screen, signed in again
        for expected in [3, 3] {
            google.signIn(address: Self.me, installer: installer, setup: f.setup, chosen: [.mail], store: f.store)
            await until { !google.signingIn.running && !google.loadingLabels.running && google.labels != nil }
            #expect(google.labels?.count == expected)
        }
        #expect(fake.labelReads == 3)
    }

    /// Fix round 1: a client sent while a sign-in waited let that sign-in, made with the earlier client, finish as
    /// signed in. Now the client ends it first and its late answer is dropped.
    @Test func aClientSentDuringASignInEndsItAndItsAnswerIsDropped() async throws {
        let gate = Gate()
        let runner = ScriptRunner(["google login": ([Self.consent, Self.signedIn], 0),
                                   "google client": ([#"{"ok": true, "client": "google", "login_again": []}"#], 0)],
                                  held: ["google login": gate])
        let f = try Fixture(step: .access, runner: runner) { state in
            state.createdInstance = "ledger"
            state.google.adopt(signedIn: true)
            state.google.clientSaved()                                     // client A saved, its sign-in to come
        }
        let google = f.setup.steps.google
        let installer = try runner.installer(home: f.home)
        google.signIn(address: Self.me, installer: installer, setup: f.setup, chosen: [.drive], store: f.store)
        await until { google.loginURL != nil }
        google.saveClient(json: Self.desktopJSON, installer: installer, setup: f.setup)     // client B
        #expect(!google.signingIn.running)
        await until { !google.savingClient.running }
        gate.open()                                                         // A's consent finishes anyway
        await until { runner.finished("google login") }
        try await Task.sleep(for: .milliseconds(100))
        #expect(!f.setup.state.google.isDone(.signIn))
        #expect(google.signedInAs == nil)
        #expect(f.setup.state.google.resume == .signIn)
        // And no sign-in starts while a client is being saved.
        let slow = Gate()
        let second = ScriptRunner(["google client": ([#"{"ok": true, "client": "google", "login_again": []}"#], 0)],
                                  held: ["google client": slow])
        let g = try Fixture(step: .access, runner: second) { $0.createdInstance = "ledger" }
        let other = try second.installer(home: g.home)
        g.setup.steps.google.saveClient(json: Self.desktopJSON, installer: other, setup: g.setup)
        g.setup.steps.google.signIn(address: Self.me, installer: other, setup: g.setup, chosen: [.drive], store: g.store)
        #expect(g.setup.steps.google.signingIn == .failed("The client is still being saved: sign in once it is."))
        slow.open()
        await until { !g.setup.steps.google.savingClient.running }
    }

    @Test func onlyGooglesConsentPageIsEverOpened() {
        if case .url(let url)? = GoogleCommand.event(in: Self.consent) {
            #expect(url.host == "accounts.google.com")
        } else {
            Issue.record("the consent line did not parse")
        }
        #expect(GoogleCommand.event(in: #"{"url": "https://evil.example/o/oauth2"}"#) == nil)
        #expect(GoogleCommand.event(in: #"{"url": "http://accounts.google.com/x"}"#) == nil)
        #expect(GoogleCommand.event(in: #"{"url": "file:///etc/passwd"}"#) == nil)
        #expect(GoogleCommand.event(in: "Sign in as ada@example.com in the browser") == nil)
    }

    // MARK: - Access and the Google setup

    @Test func aTickedGoogleRowStillOffStopsContinueSayingWhere() async throws {
        let f = try Fixture(step: .access) { state in
            state.createdInstance = "ledger"
            state.drive = "assistant"
        }
        f.server.answer { SetupAccessTests.read($0) ?? SetupAccessTests.notFound }
        let steps = f.setup.steps
        steps.prepareAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(steps.accessChecked == [.mail, .calendar, .drive])
        #expect(steps.googleChosen == [.mail, .calendar, .drive])
        steps.applyAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(steps.failedRow == .mail)
        #expect(steps.granting == .failed("Mail labels is granted through Set up Google… above: finish it there, or "
                                          + "untick Mail labels."))
        #expect(f.setup.state.step == .access)
        #expect(f.server.requests.allSatisfy { $0.method == "GET" })     // nothing sent for them

        // Unticked, Continue goes on.
        steps.accessChecked = []
        steps.applyAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(f.setup.state.step == .alone)
    }

    @Test func anAccountTheServerHasSignedInSkipsToTheGrants() throws {
        let f = try Fixture(step: .access) { state in
            state.createdInstance = "ledger"
            state.email = "ada@example.com"
        }
        let access = try SHAFTJSON.decoder().decode(AccessResponse.self, from: Data(SetupAccessTests.nothingGranted
            .replacingOccurrences(of: #""google_accounts":[]"#,
                                  with: #""google_accounts":[{"alias":"google","address":"ada.work@example.com","scopes":[]}]"#).utf8))
        f.setup.steps.google.prepare(access: access, setup: f.setup)
        #expect(f.setup.state.google.resume == .grants)
        // The identity is the server's account; the field keeps what was typed (here the install's email, different).
        #expect(f.setup.steps.google.signedInAs == "ada.work@example.com")
        #expect(f.setup.state.google.address == "ada@example.com")
        // None signed in: it starts at the first card, the address from the install's email.
        let g = try Fixture(step: .access) { $0.email = "ada@example.com" }
        g.setup.steps.google.prepare(access: nil, setup: g.setup)
        #expect(g.setup.state.google.resume == .project)
        #expect(g.setup.steps.google.signedInAs == nil)
        #expect(g.setup.state.google.address == "ada@example.com")
    }
}

/// `shaft google client|login --json` scripted, by its first two words: its lines, then its exit, or held until
/// cancelled (nil). `held`: a command that waits for its gate before it ends, cancelled or not (a process whose consent
/// still finishes). It records each command's args and stdin. `uv python find` answers the test's own Python.
final class ScriptRunner: ProcessRunning, @unchecked Sendable {
    struct Ran { let args: [String]; let stdin: Data? }
    private let lock = NSLock()
    private let script: [String: ([String], Int32?)]
    private let held: [String: Gate]
    private var python = ""
    private var _ran: [Ran] = []
    private var _finished: [String] = []
    private var _cancelled = false

    init(_ script: [String: ([String], Int32?)], held: [String: Gate] = [:]) {
        self.script = script
        self.held = held
    }

    var ran: [Ran] { lock.withLock { _ran } }
    var cancelled: Bool { lock.withLock { _cancelled } }
    func finished(_ command: String) -> Bool { lock.withLock { _finished.contains(command) } }

    @MainActor func installer(home: URL) throws -> Installer {
        let configuration = Installer.Configuration(home: home)
        let runtime = configuration.runtime
        let bin = runtime.appending(path: "bin", directoryHint: .isDirectory)
        let pythonBin = runtime.appending(path: "python/cpython-3.12/bin", directoryHint: .isDirectory)
        for folder in [bin, pythonBin] { try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true) }
        FileManager.default.createFile(atPath: bin.appending(path: "uv").path, contents: Data())
        let python = pythonBin.appending(path: "python3.12")
        FileManager.default.createFile(atPath: python.path, contents: Data())
        lock.withLock { self.python = python.path }
        return Installer(runner: self, store: MemoryCredentialStore(), configuration: configuration)
    }

    func run(_ exe: URL, _ args: [String], env: [String: String], stdin: Data?,
             onLine: (@Sendable (String) -> Void)?) async throws -> ProcessResult {
        if args.first == "python" {
            return ProcessResult(status: 0, stdout: Data(lock.withLock { python }.utf8), stderr: Data())
        }
        lock.withLock { _ran.append(Ran(args: args, stdin: stdin)) }
        let command = args.prefix(2).joined(separator: " ")
        guard let (lines, status) = script[command] else {
            return ProcessResult(status: 2, stdout: Data(), stderr: Data("no script for \(args)".utf8))
        }
        let gate = held[command]
        for line in lines.dropLast(gate == nil ? 0 : 1) {
            onLine?(line)
            try? await Task.sleep(for: .milliseconds(20))
        }
        if let gate {
            await gate.wait()                                   // ignores cancellation, as a consent that completes
            if let last = lines.last { onLine?(last) }
            lock.withLock { _finished.append(command) }
            return ProcessResult(status: status ?? 0, stdout: Data(lines.map { $0 + "\n" }.joined().utf8), stderr: Data())
        }
        if let status {
            lock.withLock { _finished.append(command) }
            return ProcessResult(status: status, stdout: Data(lines.map { $0 + "\n" }.joined().utf8), stderr: Data())
        }
        while !Task.isCancelled { try? await Task.sleep(for: .milliseconds(5)) }
        lock.withLock { _cancelled = true }
        throw CancellationError()
    }
}

/// The server's grants for the instance `ledger` and the account `google`, kept as the verbs keep them (a calendar
/// grant keeps a write already there, a Drive grant replaces the reach): what `GET|POST /access/ledger` answer, and
/// the labels and calendars the pickers read. `refuse`: "<grant> <what>" refused with Google's words.
final class GrantsFake: @unchecked Sendable {
    private let lock = NSLock()
    private var _mail: [String]
    private var _calendars: [String: Bool]
    private var _drive: String?
    private var _chat: Bool
    private var _posts: [[String: String]] = []
    private var _labels = ["Assistant", "Receipts"]
    private var _labelReads = 0
    private var _refuse: Set<String> = []

    init(mail: [String] = [], calendars: [String: Bool] = [:], drive: String? = nil, chat: Bool = false) {
        (_mail, _calendars, _drive, _chat) = (mail, calendars, drive, chat)
    }

    var mail: [String] { lock.withLock { _mail } }
    var calendars: [String: Bool] { lock.withLock { _calendars } }
    var drive: String? { lock.withLock { _drive } }
    var chat: Bool { lock.withLock { _chat } }
    var posts: [[String: String]] { lock.withLock { _posts } }
    var labelReads: Int { lock.withLock { _labelReads } }
    var labels: [String] {
        get { lock.withLock { _labels } }
        set { lock.withLock { _labels = newValue } }
    }
    var refuse: Set<String> {
        get { lock.withLock { _refuse } }
        set { lock.withLock { _refuse = newValue } }
    }

    func json() -> String {
        lock.withLock {
            let mail = _mail.map { #"{"account":"google","label":"\#($0)","draft":false}"# }.joined(separator: ",")
            let cals = _calendars.keys.sorted().map {
                #"{"account":"google","calendar":"\#($0)","name":"\#($0)","write":\#(_calendars[$0]!)}"#
            }.joined(separator: ",")
            let drive = _drive.map { #"{"account":"google","reach":"\#($0)"}"# } ?? ""
            return #"{"instance":"ledger","folder":null,"browser":{"installed":null,"run":true},"web_search":false,"#
                + #""telegram":false,"mail":[\#(mail)],"calendar":[\#(cals)],"drive":[\#(drive)],"#
                + #""chat":[\#(_chat ? #"{"account":"google"}"# : "")],"#
                + #""google_accounts":[{"alias":"google","address":"ada@example.com","scopes":[]}]}"#
        }
    }

    func access() throws -> AccessResponse {
        try SHAFTJSON.decoder().decode(AccessResponse.self, from: Data(json().utf8))
    }

    func handle(_ request: SetupStub.Request) -> SetupStub.Answer {
        switch (request.method, request.path) {
        case ("GET", "/app/v1/access/ledger"): return .init(200, json())
        case ("GET", "/app/v1/google/google/labels"):
            let names = lock.withLock { () -> [String] in
                _labelReads += 1
                return _labels
            }
            return .init(200, #"{"alias":"google","labels":[\#(names.map { "\"\($0)\"" }.joined(separator: ","))]}"#)
        case ("GET", "/app/v1/google/google/calendars"):
            return .init(200, #"{"alias":"google","calendars":[{"id":"ada@example.com","title":"Ada","primary":true,"access":"owner"},"#
                         + #"{"id":"team","title":"Team","primary":false,"access":"reader"}]}"#)
        case ("POST", "/app/v1/access/ledger"):
            let body = (request.json ?? [:]).mapValues { "\($0)" == "1" ? "true" : "\($0)" }
            let grant = body["grant"] ?? "", action = body["action"] ?? "", what = body["what"] ?? ""
            let refused = lock.withLock { () -> Bool in
                _posts.append(body)
                return _refuse.contains("\(grant) \(what)")
            }
            if refused {
                return .init(409, #"{"error": {"code": "refused", "message": "not granted: Google answered HTTP 403: no access"}}"#)
            }
            lock.withLock {
                let grantIt = action == "grant", write = body["writable"] == "true"
                switch grant {
                case "mail":
                    _mail.removeAll { $0 == what }
                    if grantIt { _mail.append(what) }
                case "calendar":
                    if grantIt { _calendars[what] = (_calendars[what] ?? false) || write }
                    else if write { if _calendars[what] != nil { _calendars[what] = false } }
                    else { _calendars[what] = nil }
                case "drive": _drive = grantIt ? (body["all"] == "true" ? "all" : "own") : nil
                case "chat": _chat = grantIt
                default: break
                }
            }
            return .init(200, json())
        default: return .init(404, #"{"error": {"code": "not_found", "message": "no"}}"#)
        }
    }
}
