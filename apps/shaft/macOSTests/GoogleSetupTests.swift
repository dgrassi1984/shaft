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
    nonisolated static let labels = #"{"alias": "google", "labels": ["Assistant", "Receipts", "Travel"]}"#
    nonisolated static let calendars = #"""
    {"alias": "google", "calendars": [{"id": "ada@example.com", "title": "Ada", "primary": true, "access": "owner"},
                                      {"id": "team@group.calendar.google.com", "title": "Team", "primary": false, "access": "reader"}]}
    """#

    func until(_ done: () -> Bool) async {
        for _ in 0..<800 where !done() { try? await Task.sleep(for: .milliseconds(5)) }
    }

    func posts(_ f: Fixture) -> [[String: Any]] {
        f.server.requests.filter { $0.method == "POST" }.compactMap(\.json)
    }

    // MARK: - Pickers → grants

    @Test func pickersMapToGrantsInOrder() throws {
        let all: Set<AccessRow> = [.mail, .calendar, .drive, .chat]
        let made = GoogleGrants.requests(alias: "google", chosen: all, labels: ["Assistant", "Receipts", "Travel"],
                                         picked: ["Travel", "Assistant"],
                                         calendars: ["ada@example.com", "team@group.calendar.google.com"],
                                         pickedCalendars: ["team@group.calendar.google.com", "ada@example.com"],
                                         writable: ["ada@example.com"], driveAll: true, chat: true)
        #expect(try made.get() == [.mail(alias: "google", label: "Assistant"), .mail(alias: "google", label: "Travel"),
                                   .calendar(alias: "google", id: "ada@example.com", writable: true),
                                   .calendar(alias: "google", id: "team@group.calendar.google.com", writable: false),
                                   .drive(alias: "google", all: true), .chat(alias: "google")])
        // Only the chosen rows; Drive's own files; Chat turned off in the sheet sends nothing.
        let some = GoogleGrants.requests(alias: "google", chosen: [.drive, .chat], labels: ["Assistant"],
                                         picked: ["Assistant"], calendars: [], pickedCalendars: [], writable: [],
                                         driveAll: false, chat: false)
        #expect(try some.get() == [.drive(alias: "google", all: false)])
        // A chosen row with nothing picked is the operator's to fix, before anything is sent.
        let none = GoogleGrants.requests(alias: "google", chosen: [.mail], labels: ["Assistant"], picked: [],
                                         calendars: [], pickedCalendars: [], writable: [], driveAll: false, chat: true)
        #expect(none == .failure(FormProblem("Pick at least one mail label, or untick Mail labels on Access.")))
        let noCalendar = GoogleGrants.requests(alias: "google", chosen: [.calendar], labels: [], picked: [],
                                               calendars: ["a"], pickedCalendars: ["gone"], writable: [],
                                               driveAll: false, chat: true)
        #expect(noCalendar == .failure(FormProblem("Pick at least one calendar, or untick Calendars on Access.")))
        // The cards list the chosen grants' APIs and scopes.
        #expect(GoogleScopes.apis(for: [.chat, .mail]).map(\.id) == ["gmail.googleapis.com", "chat.googleapis.com"])
        #expect(GoogleScopes.needed(by: [.drive]).map(\.name) == ["drive.readonly"])
    }

    @Test func applySendsInOrderStopsAtTheFirstRefusalAndTryAgainSendsTheRest() async throws {
        let f = try Fixture(step: .access) { state in
            state.createdInstance = "ledger"
            state.google.adopt(signedIn: true)                         // the cards, the client and the sign-in done
        }
        let granted = SetupAccessTests.nothingGranted.replacingOccurrences(
            of: #""drive":[]"#, with: #""drive":[{"account":"google","reach":"own"}]"#)
        let refusing = Answers([:])
        f.server.answer { request in
            switch (request.method, request.path) {
            case ("GET", "/app/v1/google/google/labels"): return .init(200, Self.labels)
            case ("GET", "/app/v1/google/google/calendars"): return .init(200, Self.calendars)
            case ("POST", "/app/v1/access/ledger"):
                if request.json?["grant"] as? String == "calendar", request.json?["what"] as? String == "team@group.calendar.google.com",
                   refusing.answer(.init(method: "GET", path: "team", body: Data())).status == 409 {
                    return refusing.answer(.init(method: "GET", path: "team", body: Data()))
                }
                return .init(200, granted)
            default: return .init(404, #"{"error": {"code": "not_found", "message": "no"}}"#)
            }
        }
        refusing.set("team", .init(409, #"{"error": {"code": "refused", "message": "not granted: Google answered HTTP 403: no access"}}"#))
        let steps = f.setup.steps, google = steps.google
        steps.accessChecked = [.mail, .calendar, .drive, .chat]
        google.loadPickers(chosen: steps.googleChosen, store: f.store)
        await until { google.labels != nil && google.calendars != nil }
        #expect(google.pickedCalendars == ["ada@example.com"])           // the primary, ticked to start
        google.pickedLabels = ["Receipts"]
        google.pickedCalendars.insert("team@group.calendar.google.com")
        google.chatOn = false

        google.apply(chosen: steps.googleChosen, store: f.store, setup: f.setup, steps: steps)
        await until { !google.applying.running }
        #expect(posts(f).map { $0["grant"] as? String ?? "" } == ["mail", "calendar", "calendar"])
        #expect(google.applying == .failed("not granted: Google answered HTTP 403: no access"))
        #expect(google.failed == .calendar(alias: "google", id: "team@group.calendar.google.com", writable: false))
        #expect(!f.setup.state.google.isDone(.grants))

        // Try again: from the refused calendar on; nothing sent twice; Chat off sends nothing and unticks its row.
        refusing.set("team", .init(200, granted))
        google.apply(chosen: steps.googleChosen, store: f.store, setup: f.setup, steps: steps)
        await until { !google.applying.running }
        #expect(posts(f).map { $0["grant"] as? String ?? "" } == ["mail", "calendar", "calendar", "calendar", "drive"])
        #expect(google.applying == .idle)
        #expect(f.setup.state.google.isDone(.grants))
        #expect(f.setup.state.google.resume == nil)
        #expect(steps.isOn(.drive))                                    // the answer reached the Access step
        #expect(!steps.accessChecked.contains(.chat))
        #expect(posts(f).last?["all"] == nil)                           // its own files
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
        google.signIn(address: " ada@example.com ", installer: try runner.installer(home: f.home), setup: f.setup)
        await until { !google.signingIn.running }
        #expect(google.signingIn == .idle)
        #expect(runner.ran.map(\.args) == [["google", "login", "google", "ada@example.com", "--client", "google", "--json"]])
        #expect(f.opened.all == [URL(string: "https://accounts.google.com/o/oauth2/v2/auth?client_id=1&state=s")!])
        #expect(f.setup.state.google.isDone(.signIn))
        #expect(f.setup.state.google.address == "ada@example.com")
        #expect(google.granted == ["gmail.readonly", "calendar.readonly", "drive.readonly"])
        // What a chosen grant needs and was not granted is said; a scope for later grants is not.
        #expect(google.missingNeeded(for: [.mail, .chat]) == ["chat.spaces.readonly"])
    }

    @Test func aSignInThatTimesOutShowsTheServersWords() async throws {
        let timeout = #"{"ok": false, "error": "no answer from the browser in 300 s: nothing saved"}"#
        let runner = ScriptRunner(["google login": ([Self.consent, timeout], 1)])
        let f = try Fixture(step: .access, runner: runner) { $0.createdInstance = "ledger" }
        let google = f.setup.steps.google
        google.signIn(address: "ada@example.com", installer: try runner.installer(home: f.home), setup: f.setup)
        await until { !google.signingIn.running }
        #expect(google.signingIn == .failed("no answer from the browser in 300 s: nothing saved"))
        #expect(!f.setup.state.google.isDone(.signIn))
        // Not an address: nothing runs.
        google.signIn(address: "ada", installer: try runner.installer(home: f.home), setup: f.setup)
        #expect(google.signingIn.failure?.hasPrefix("Type the address") == true)
        #expect(runner.ran.count == 1)
    }

    @Test func cancellingTheSignInEndsTheCommandAndKeepsNothing() async throws {
        let runner = ScriptRunner(["google login": ([Self.consent], nil)])
        let f = try Fixture(step: .access, runner: runner) { $0.createdInstance = "ledger" }
        let google = f.setup.steps.google
        google.signIn(address: "ada@example.com", installer: try runner.installer(home: f.home), setup: f.setup)
        await until { google.loginURL != nil }
        google.cancelSignIn()
        await until { runner.cancelled }
        #expect(runner.cancelled)
        #expect(google.signingIn == .idle)
        #expect(google.loginURL == nil)
        #expect(!f.setup.state.google.isDone(.signIn))
        #expect(!f.setup.steps.busy)
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
                                  with: #""google_accounts":[{"alias":"google","address":"ada@example.com","scopes":[]}]"#).utf8))
        f.setup.steps.google.prepare(access: access, setup: f.setup)
        #expect(f.setup.state.google.resume == .grants)
        #expect(f.setup.state.google.address == "ada@example.com")
        // None signed in: it starts at the first card, the address from the install's email.
        let g = try Fixture(step: .access) { $0.email = "ada@example.com" }
        g.setup.steps.google.prepare(access: nil, setup: g.setup)
        #expect(g.setup.state.google.resume == .project)
        #expect(g.setup.state.google.address == "ada@example.com")
    }
}

/// `shaft google client|login --json` scripted, by its first two words: its lines, then its exit, or held until
/// cancelled (nil). It records each command's args and stdin. `uv python find` answers the test's own Python.
final class ScriptRunner: ProcessRunning, @unchecked Sendable {
    struct Ran { let args: [String]; let stdin: Data? }
    private let lock = NSLock()
    private let script: [String: ([String], Int32?)]
    private var python = ""
    private var _ran: [Ran] = []
    private var _cancelled = false

    init(_ script: [String: ([String], Int32?)]) { self.script = script }

    var ran: [Ran] { lock.withLock { _ran } }
    var cancelled: Bool { lock.withLock { _cancelled } }

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
        guard let (lines, status) = script[args.prefix(2).joined(separator: " ")] else {
            return ProcessResult(status: 2, stdout: Data(), stderr: Data("no script for \(args)".utf8))
        }
        for line in lines {
            onLine?(line)
            try? await Task.sleep(for: .milliseconds(20))
        }
        if let status {
            return ProcessResult(status: status, stdout: Data(lines.map { $0 + "\n" }.joined().utf8), stderr: Data())
        }
        while !Task.isCancelled { try? await Task.sleep(for: .milliseconds(5)) }
        lock.withLock { _cancelled = true }
        throw CancellationError()
    }
}
