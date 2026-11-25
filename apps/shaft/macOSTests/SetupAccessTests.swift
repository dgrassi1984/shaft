import Foundation
import SHAFTKit
import Testing

/// The wizard's Access step (macOS/Setup/AccessStep.swift, SetupSteps' Access work) and the Codex sign-in without a
/// terminal, against the stub server and a scripted `shaft codex login --json`. Nothing here touches the real ~/SHAFT,
/// the Keychain, launchd, a browser or a network.
@MainActor
struct SetupAccessTests {
    typealias Fixture = SetupStepsTests.Fixture

    nonisolated static let nothingGranted = #"""
    {"instance":"ledger","folder":null,"browser":{"installed":null,"run":true},"web_search":false,"telegram":false,
     "mail":[],"calendar":[],"drive":[],"chat":[],"google_accounts":[]}
    """#
    nonisolated static let drives = #"""
    {"drives":[{"id":"assistant","title":"Personal assistant","text":"a","suggests":["mail","calendar","drive"]},
               {"id":"research","title":"Research partner","text":"r","suggests":["web","drive"]},
               {"id":"builder","title":"Builder","text":"b","suggests":["web","folder"]},
               {"id":"improve","title":"Just improve yourself","text":"i","suggests":[]}]}
    """#
    nonisolated static let notFound = SetupStub.Answer(404, #"{"error": {"code": "not_found", "message": "no"}}"#)

    /// `GET /setup/telegram/status/ledger` as the server answers it.
    nonisolated static func telegram(token: Bool = false, paired: Bool = false, service: Bool = false,
                                     problem: String? = nil, code: String? = nil, minutes: Int = 10) -> String {
        #"{"instance":"ledger","token":\#(token),"bot":\#(token ? #""ledger_bot""# : "null"),"paired":\#(paired),"#
            + #""account":\#(paired ? #""ada""# : "null"),"service":\#(service),"pairing":\#(code != nil),"#
            + #""problem":\#(problem.map { #""\#($0)""# } ?? "null")"#
            + (code.map { #","code":"\#($0)","minutes":\#(minutes)"# } ?? "") + "}"
    }

    /// The step's reads (the drives, the access, the Telegram status), or nil for any other request.
    nonisolated static func read(_ request: SetupStub.Request, access: String = nothingGranted,
                                 telegram: String = telegram()) -> SetupStub.Answer? {
        guard request.method == "GET" else { return nil }
        switch request.path {
        case "/app/v1/setup/drives": return .init(200, drives)
        case "/app/v1/access/ledger": return .init(200, access)
        case "/app/v1/setup/telegram/status/ledger": return .init(200, telegram)
        default: return nil
        }
    }

    func until(_ done: () -> Bool) async {
        for _ in 0..<800 where !done() { try? await Task.sleep(for: .milliseconds(5)) }
    }

    /// The step opened and read what is granted: Continue may run.
    func prepared(_ f: Fixture) async {
        f.setup.steps.prepareAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(f.setup.steps.accessReady("ledger"))
    }

    func posts(_ f: Fixture) -> [String] {
        f.server.requests.filter { $0.method == "POST" }.map { $0.json?["grant"] as? String ?? $0.path }
    }

    // MARK: - Access

    @Test func accessSuggestionsArePrechecked() async throws {
        let presets = try JSONDecoder().decode(DrivePresets.self, from: Data(Self.drives.utf8)).drives
        #expect(AccessRow.suggested(by: presets[0]) == [.mail, .calendar, .drive])
        #expect(AccessRow.suggested(by: presets[2]) == [.web, .folder])
        #expect(AccessRow.suggested(by: presets[3]).isEmpty)
        #expect(AccessRow.suggested(by: nil).isEmpty)

        // The builder drive, its folder granted already: web search comes ticked, the folder shows as on.
        let f = try Fixture(step: .access) { state in
            state.createdInstance = "ledger"
            state.drive = "builder"
        }
        let folderOn = Self.nothingGranted.replacingOccurrences(of: #""folder":null"#, with: #""folder":"/Users/ada/Notes""#)
        f.server.answer { Self.read($0, access: folderOn) ?? Self.notFound }
        await prepared(f)
        #expect(f.setup.steps.accessSuggested == [.web, .folder])
        #expect(f.setup.steps.accessChecked == [.web])
        #expect(f.setup.steps.isOn(.folder))
        #expect(f.server.requests.allSatisfy { $0.method == "GET" })     // reading grants nothing

        // The operator's own ticks stay when the step opens again.
        f.setup.steps.accessChecked = [.telegram]
        f.setup.steps.prepareAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(f.setup.steps.accessChecked == [.telegram])
    }

    /// Fix round 1: a failed first read left Continue on, which then applied nothing and moved on, skipping the drive's
    /// suggestions without a word. Now Continue waits for a read that worked; Try again reads again; Skip all remains.
    @Test func aFailedReadKeepsContinueOffUntilTryAgainReadsIt() async throws {
        let f = try Fixture(step: .access) { state in
            state.createdInstance = "ledger"
            state.drive = "research"
        }
        let failing = Answers([:])
        f.server.answer { request in
            request.path == "/app/v1/access/ledger" && failing.answer(request).status == 409
                ? failing.answer(request) : Self.read(request) ?? Self.notFound
        }
        failing.set("/app/v1/access/ledger", .init(409, #"{"error": {"code": "refused", "message": "cannot read the tool settings"}}"#))
        let steps = f.setup.steps
        func canContinue() -> Bool {
            SetupContinue.enabled(.access, SetupContinue.Context(state: f.setup.state,
                                                                 accessReady: steps.accessReady(f.setup.state.createdInstance)))
        }
        steps.prepareAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(steps.loadingAccess == .failed("cannot read the tool settings"))
        #expect(!canContinue())
        #expect(steps.accessChecked.isEmpty)
        steps.applyAccess(store: f.store, setup: f.setup)          // even if called: nothing is sent, nothing moves
        await f.settle()
        #expect(posts(f).isEmpty)
        #expect(f.setup.state.step == .access)

        failing.set("/app/v1/access/ledger", .init(200, Self.nothingGranted))    // Try again
        steps.prepareAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(steps.loadingAccess == .idle)
        #expect(canContinue())
        #expect(steps.accessChecked == [.web, .drive])
    }

    @Test func accessAppliesTheTickedRowsInOrderAndStopsAtTheFirstFailure() async throws {
        let f = try Fixture(step: .access) { $0.createdInstance = "ledger" }
        let browserInstalled = Self.nothingGranted.replacingOccurrences(of: #""installed":null"#, with: #""installed":"1.64.0""#)
        let answers = Answers([
            "/app/v1/access/ledger": .init(409, #"{"error": {"code": "refused", "message": "browser install: Playwright could not download Chromium"}}"#),
            "/app/v1/setup/secret": .init(200, #"{"said": "SCRAPEBADGER_API_KEY saved"}"#),
            "/app/v1/setup/telegram": .init(200, #"{"said": "saved"}"#),
            "/app/v1/setup/telegram/service": .init(200, #"{"said": "telegram installed"}"#),
        ])
        let telegram = Answers(["status": .init(200, Self.telegram())])
        f.server.answer { request in
            if request.path == "/app/v1/setup/telegram/status/ledger" { return telegram.answer(.init(method: "GET", path: "status", body: Data())) }
            if let read = Self.read(request) { return read }
            // The folder is granted; the browser's install is refused until the test changes it.
            if request.path == "/app/v1/access/ledger", request.json?["grant"] as? String == "folder" {
                return .init(200, Self.nothingGranted.replacingOccurrences(of: #""folder":null"#, with: #""folder":"/tmp/notes""#))
            }
            return answers.answer(request)
        }
        await prepared(f)
        let steps = f.setup.steps
        steps.accessChecked = [.telegram, .web, .browser, .folder]   // ticked in any order
        steps.choose(folder: URL(fileURLWithPath: "/tmp/notes"))
        steps.webKey = "sb-key-never-kept"
        steps.telegramToken = "123:bot-token-never-kept"
        steps.applyAccess(store: f.store, setup: f.setup)
        await f.settle()
        var sent = f.server.requests.filter { $0.method == "POST" }
        #expect(posts(f) == ["folder", "browser"])
        #expect(sent[0].json as? [String: String] == ["grant": "folder", "action": "grant", "what": "/tmp/notes"])
        #expect(steps.granting == .failed("browser install: Playwright could not download Chromium"))
        #expect(steps.failedRow == .browser)
        #expect(steps.accessDone == [.folder])
        #expect(f.setup.state.step == .access)
        #expect(steps.webKey == "sb-key-never-kept")          // not sent yet: kept for Try again

        // Try again: what is left, from the browser on; the folder is not granted twice. The account is paired
        // already (another bot's pairing): Telegram needs its token and the service, no code.
        answers.set("/app/v1/access/ledger", .init(200, browserInstalled))
        telegram.set("status", .init(200, Self.telegram(token: true, paired: true, service: true)))
        steps.applyAccess(store: f.store, setup: f.setup)
        await f.settle()
        sent = f.server.requests.filter { $0.method == "POST" }
        #expect(posts(f) == ["folder", "browser", "browser", "/app/v1/setup/secret", "/app/v1/setup/telegram",
                             "/app/v1/setup/telegram/service"])
        #expect(sent[2].json as? [String: String] == ["grant": "browser", "action": "grant"])
        #expect(sent[3].json as? [String: String] == ["name": "SCRAPEBADGER_API_KEY", "value": "sb-key-never-kept"])
        #expect(sent[4].json as? [String: String] == ["name": "ledger", "token": "123:bot-token-never-kept"])
        #expect(steps.granting == .idle)
        #expect(steps.failedRow == nil)
        #expect(steps.accessDone == [.folder, .browser, .web, .telegram])
        #expect((steps.webKey, steps.telegramToken) == ("", ""))
        #expect(f.setup.state.step == .alone)
        let kept = String(decoding: try #require(f.defaults.data(forKey: SetupStateStore.key)), as: UTF8.self)
        #expect(!kept.contains("never-kept"))
    }

    @Test func aTickedRowWithoutItsInputStopsThereAndSendsNothing() async throws {
        let f = try Fixture(step: .access) { $0.createdInstance = "ledger" }
        f.server.answer { Self.read($0) ?? Self.notFound }
        await prepared(f)
        f.setup.steps.accessChecked = [.folder, .web]
        f.setup.steps.applyAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(posts(f).isEmpty)
        #expect(f.setup.steps.failedRow == .folder)
        #expect(f.setup.steps.granting == .failed("Choose the folder ledger may read, or untick it."))
        #expect(f.setup.state.step == .access)
    }

    @Test func cancellingTheBrowserGrantStopsWaitingAndRecordsNothing() async throws {
        let f = try Fixture(step: .access) { $0.createdInstance = "ledger" }
        let gate = Gate()
        let installed = Self.nothingGranted.replacingOccurrences(of: #""installed":null"#, with: #""installed":"1.64.0""#)
        f.server.answer { Self.read($0) ?? .init(200, installed, hold: { await gate.wait() }) }
        await prepared(f)
        let steps = f.setup.steps
        let before = steps.access
        steps.accessChecked = [.browser]
        steps.applyAccess(store: f.store, setup: f.setup)
        await until { !posts(f).isEmpty }
        #expect(steps.grantingRow == .browser)
        #expect(steps.busy)
        steps.cancelAccess()
        #expect(steps.granting == .idle)
        #expect(steps.grantingRow == nil)
        #expect(!steps.busy)
        gate.open()                                            // the server's answer comes after all
        try await Task.sleep(for: .milliseconds(150))
        #expect(steps.accessDone.isEmpty)
        #expect(steps.access == before)
        #expect(steps.granting == .idle)
        #expect(f.setup.state.step == .access)
        #expect(posts(f).count == 1)
    }

    @Test func skipAllGrantsNothingAndDropsWhatWasTyped() async throws {
        let f = try Fixture(step: .access) { $0.createdInstance = "ledger" }
        f.server.answer { _ in Self.notFound }
        f.setup.steps.accessChecked = [.web, .telegram]
        f.setup.steps.webKey = "sb-key"
        f.setup.steps.telegramToken = "1:t"
        f.setup.steps.skipAccess(setup: f.setup)           // no read needed: Skip all is the way past a failed one
        #expect(f.server.requests.isEmpty)
        #expect(f.setup.state.step == .alone)
        #expect((f.setup.steps.webKey, f.setup.steps.telegramToken) == ("", ""))
    }

    // MARK: - Telegram, whole

    /// Fix round 1: the row saved the bot and told the operator to finish in a terminal. Now it saves the bot, turns
    /// the service on, shows the code to send the bot, and waits until the pairing lands.
    @Test func telegramSavesTheBotTurnsItOnAndWaitsForThePairing() async throws {
        let f = try Fixture(step: .access) { $0.createdInstance = "ledger" }
        let server = TelegramServer(pairedAfterPolls: 3)
        f.server.answer { server.answer($0) }
        await prepared(f)
        let steps = f.setup.steps
        steps.telegramPoll = .milliseconds(10)
        #expect(!steps.isOn(.telegram))
        steps.accessChecked = [.telegram]
        steps.telegramToken = "123:bot-token"
        steps.applyAccess(store: f.store, setup: f.setup)
        await until { steps.telegramCode != nil }
        #expect(steps.telegramCode == "CAFE0001")             // shown while the row waits
        #expect(steps.grantingRow == .telegram)
        #expect(steps.telegram?.bot == "ledger_bot")
        await f.settle()
        #expect(posts(f) == ["/app/v1/setup/telegram", "/app/v1/setup/telegram/service",
                             "/app/v1/setup/telegram/pair/ledger"])
        #expect(server.polls >= 3)
        #expect(steps.granting == .idle)
        #expect(steps.telegramCode == nil)
        #expect(steps.telegram?.ready == true)
        #expect(steps.isOn(.telegram))
        #expect(f.setup.state.step == .alone)
    }

    @Test func aBotSavedBeforeIsTurnedOnAndPairedWithoutItsTokenAgain() async throws {
        let f = try Fixture(step: .access) { $0.createdInstance = "ledger" }
        let server = TelegramServer(pairedAfterPolls: 1, token: true)
        f.server.answer { server.answer($0) }
        await prepared(f)
        f.setup.steps.telegramPoll = .milliseconds(10)
        f.setup.steps.accessChecked = [.telegram]
        f.setup.steps.applyAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(posts(f) == ["/app/v1/setup/telegram/service", "/app/v1/setup/telegram/pair/ledger"])
        #expect(f.setup.state.step == .alone)
    }

    /// Fix round 2: an account paired already (by another bot) made the row complete without a running service, and the
    /// "done" it recorded then stood for a service that never came up.
    @Test func anAlreadyPairedAccountWithoutARunningServiceNeverCompletes() async throws {
        let f = try Fixture(step: .access) { $0.createdInstance = "ledger" }
        let server = TelegramServer(pairedAfterPolls: 1, token: true, paired: true, serviceAfterPolls: nil)
        f.server.answer { server.answer($0) }
        await prepared(f)
        let steps = f.setup.steps
        (steps.telegramPoll, steps.telegramServiceWait) = (.milliseconds(10), .milliseconds(120))
        #expect(!steps.isOn(.telegram))
        steps.accessChecked = [.telegram]
        steps.applyAccess(store: f.store, setup: f.setup)
        await until { steps.telegramStarting }
        #expect(steps.grantingRow == .telegram)               // "Starting the Telegram service…"
        await f.settle()
        #expect(posts(f) == ["/app/v1/setup/telegram/service"])
        #expect(steps.failedRow == .telegram)
        #expect(steps.granting == .failed("The Telegram service did not start: \(TelegramServer.problem). Try again."))
        #expect(!steps.telegramStarting)
        #expect(!steps.isOn(.telegram))
        #expect(!steps.accessDone.contains(.telegram))
        #expect(f.setup.state.step == .access)
    }

    @Test func theServiceStartingAfterAFewPollsCompletesTelegramAndAStoppedOneIsNotOn() async throws {
        let f = try Fixture(step: .access) { $0.createdInstance = "ledger" }
        let server = TelegramServer(pairedAfterPolls: 1, token: true, paired: true, serviceAfterPolls: 3)
        f.server.answer { server.answer($0) }
        await prepared(f)
        let steps = f.setup.steps
        steps.telegramPoll = .milliseconds(10)
        steps.accessChecked = [.telegram]
        steps.applyAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(posts(f) == ["/app/v1/setup/telegram/service"])  // paired already: no code
        #expect(steps.granting == .idle)
        #expect(steps.isOn(.telegram))
        #expect(f.setup.state.step == .alone)

        // Back on Access later, the service stopped meanwhile: the earlier "done" does not make the row on.
        server.stopService()
        f.setup.go(to: .access)
        steps.prepareAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(steps.accessDone.contains(.telegram))
        #expect(!steps.isOn(.telegram))
    }

    @Test func cancellingThePairingStopsTheWaitAndKeepsTheRowOff() async throws {
        let f = try Fixture(step: .access) { $0.createdInstance = "ledger" }
        let server = TelegramServer(pairedAfterPolls: 100_000)
        f.server.answer { server.answer($0) }
        await prepared(f)
        let steps = f.setup.steps
        steps.telegramPoll = .milliseconds(10)
        steps.accessChecked = [.telegram]
        steps.telegramToken = "123:bot-token"
        steps.applyAccess(store: f.store, setup: f.setup)
        await until { steps.telegramCode != nil }
        steps.cancelAccess()
        #expect(steps.telegramCode == nil)
        #expect(!steps.busy)
        let polled = server.polls
        try await Task.sleep(for: .milliseconds(100))
        #expect(server.polls == polled)                       // nobody asks any more
        #expect(!steps.accessDone.contains(.telegram))
        #expect(f.setup.state.step == .access)
    }

    @Test func anExpiredCodeStopsAtTelegramWithTryAgain() async throws {
        let f = try Fixture(step: .access) { $0.createdInstance = "ledger" }
        let server = TelegramServer(pairedAfterPolls: 100_000, minutes: 0)
        f.server.answer { server.answer($0) }
        await prepared(f)
        f.setup.steps.telegramPoll = .milliseconds(10)
        f.setup.steps.accessChecked = [.telegram]
        f.setup.steps.telegramToken = "123:bot-token"
        f.setup.steps.applyAccess(store: f.store, setup: f.setup)
        await f.settle()
        #expect(f.setup.steps.failedRow == .telegram)
        #expect(f.setup.steps.granting == .failed("The code expired before it reached @ledger_bot: Try again for a new one."))
        #expect(f.setup.state.step == .access)
    }

    // MARK: - Codex sign-in without a terminal

    nonisolated static let url = #"{"url": "https://auth.openai.com/codex/device"}"#
    nonisolated static let code = #"{"code": "WXYZ-12345"}"#

    @Test func codexLoginLinesParse() {
        #expect(CodexLogin.event(in: Self.url) == .url(URL(string: "https://auth.openai.com/codex/device")!))
        #expect(CodexLogin.event(in: Self.code) == .code("WXYZ-12345"))
        #expect(CodexLogin.event(in: #"{"ok": true}"#) == .ok)
        #expect(CodexLogin.event(in: #"{"ok": false, "error": "declined"}"#) == .failed("declined"))
        #expect(CodexLogin.event(in: #"{"url": "file:///etc/passwd"}"#) == nil)
        #expect(CodexLogin.event(in: "Open https://auth.openai.com/codex/device") == nil)
        #expect(CodexLogin.outcome(Data((Self.url + "\n" + Self.code + "\n" + #"{"ok": true}"# + "\n").utf8)) == .ok)
        #expect(CodexLogin.outcome(Data((Self.url + "\n").utf8)) == nil)
    }

    @Test func codexLoginShowsTheCodeOpensThePageOnceAndSignsInOnItsOk() async throws {
        let ok = #"{"ok": true}"#
        let runner = LoginRunner(lines: [Self.url, Self.url, Self.code, ok], status: 0)
        let f = try Fixture(step: .model, runner: runner)
        let installer = try runner.installer(home: f.home)
        f.setup.steps.signInToCodex(installer: installer)
        await until { f.setup.steps.signedIn || f.setup.steps.signingIn.failure != nil }
        #expect(f.setup.steps.signedIn)
        #expect(f.setup.steps.signingIn == .idle)
        #expect(f.opened.all == [URL(string: "https://auth.openai.com/codex/device")!])    // once, by the app
        #expect(runner.ran == [["codex", "login", "--json"]])
        #expect(f.setup.state.provider == nil)               // a sign-in saves no provider: Save does
    }

    @Test func aRefusedCodexLoginShowsCodexsOwnWords() async throws {
        let refused = #"{"ok": false, "error": "Error logging in with device code: device auth was declined"}"#
        let runner = LoginRunner(lines: [Self.url, Self.code, refused], status: 1)
        let f = try Fixture(step: .model, runner: runner)
        f.setup.steps.signInToCodex(installer: try runner.installer(home: f.home))
        await until { f.setup.steps.signingIn.failure != nil }
        #expect(f.setup.steps.signingIn == .failed("Error logging in with device code: device auth was declined"))
        #expect(!f.setup.steps.signedIn)
    }

    @Test func missingCodexShowsTheServersErrorWithoutADeviceCode() async throws {
        let message = "Codex is not installed on this Mac: install ChatGPT for Mac (Codex comes with it) or the Codex CLI "
            + "(`brew install codex` or `npm i -g @openai/codex`), or set SHAFT_CODEX_BIN."
        let runner = LoginRunner(lines: [#"{"ok": false, "error": "\#(message)"}"#], status: 1)
        let f = try Fixture(step: .model, runner: runner)
        f.setup.steps.signInToCodex(installer: try runner.installer(home: f.home))
        await until { f.setup.steps.signingIn.failure != nil }
        #expect(f.setup.steps.signingIn == .failed(message))
        #expect(f.setup.steps.loginCode == nil)
        #expect(f.opened.all.isEmpty)
    }

    @Test func cancellingTheSignInEndsTheCommand() async throws {
        let runner = LoginRunner(lines: [Self.url, Self.code], status: nil)
        let f = try Fixture(step: .model, runner: runner)
        f.setup.steps.signInToCodex(installer: try runner.installer(home: f.home))
        await until { f.setup.steps.loginCode != nil }
        #expect(f.setup.steps.loginCode == "WXYZ-12345")
        f.setup.steps.cancelSignIn()
        await until { runner.cancelled }
        #expect(runner.cancelled)                             // Process.terminate(): `shaft` stops Codex with it
        #expect(f.setup.steps.signingIn == .idle)
        #expect(f.setup.steps.loginCode == nil)
        #expect(!f.setup.steps.signedIn)
        #expect(!f.setup.steps.busy)
    }

    /// Fix of this task's own first draft: a line the cancelled sign-in's pipe still delivered reached the next sign-in
    /// (same wizard, so the generation alone let it through) and showed a dead code.
    @Test func aLateLineOfACancelledSignInNeverReachesTheNextOne() async throws {
        let runner = LoginRunner(lines: [Self.url, Self.code], status: nil, later: [],
                                 late: #"{"code": "DEAD-00000"}"#)
        let f = try Fixture(step: .model, runner: runner)
        let installer = try runner.installer(home: f.home)
        f.setup.steps.signInToCodex(installer: installer)
        await until { f.setup.steps.loginCode != nil }
        f.setup.steps.cancelSignIn()
        f.setup.steps.signInToCodex(installer: installer)     // at once: the late line hops in while this one runs
        await until { runner.ran.count == 2 }
        try await Task.sleep(for: .milliseconds(150))
        #expect(f.setup.steps.signingIn.running)
        #expect(f.setup.steps.loginCode == nil)
        f.setup.steps.cancelSignIn()
    }

    /// Review Focus 5: the app quits while the operator still has to enter the code. Reopened, the wizard is back on
    /// the Model step with no provider saved, and nothing of the abandoned sign-in carries over.
    @Test func quittingDuringLoginResumesAtTheStep() async throws {
        let runner = LoginRunner(lines: [Self.url, Self.code], status: nil)
        let f = try Fixture(step: .model, runner: runner)
        f.setup.steps.form.kind = .codex
        f.setup.steps.signInToCodex(installer: try runner.installer(home: f.home))
        await until { f.setup.steps.loginCode != nil }
        #expect(f.opened.all.count == 1)

        // Quitting closes the window first: the sign-in and its command end; nothing is written.
        f.setup.steps.stop()
        await until { runner.cancelled }
        #expect(runner.cancelled)
        #expect(f.setup.steps.signingIn == .idle)
        #expect((f.setup.steps.loginCode, f.setup.steps.loginURL) == (nil, nil))

        let home = f.home
        let reopened = MacSetup(states: SetupStateStore(defaults: f.defaults),
                                configure: { Installer.Configuration(home: home) }, runner: NoProcesses(),
                                steps: SetupSteps(openURL: { _ in }))
        #expect(reopened.stateError == nil)
        #expect(reopened.state.step == .model)
        #expect(reopened.state.provider == nil)
        #expect(reopened.state.model.isEmpty)
        #expect(reopened.wizardContinues)                      // on past Install, on its own pairing
        #expect(!reopened.steps.signedIn)
        #expect(reopened.steps.loginCode == nil)
        #expect(!SetupContinue.enabled(.model, SetupContinue.Context(state: reopened.state)))
    }
}

/// `shaft codex login --json` scripted: its lines, then its exit (`status`), or held until cancelled (nil). `uv python
/// find` answers the Python a `prepare` put under the test's own `~/SHAFT/.runtime/python`.
final class LoginRunner: ProcessRunning, @unchecked Sendable {
    private let lock = NSLock()
    private let lines: [String]
    private let later: [String]?
    private let status: Int32?
    private let late: String?
    private var python = ""
    private var _ran: [[String]] = []
    private var _cancelled = false

    /// `later`: the lines of a second sign-in (the first's by default). `late`: a line the cancelled command's pipe
    /// still delivers after its Cancel, as a reader thread may.
    init(lines: [String], status: Int32?, later: [String]? = nil, late: String? = nil) {
        self.lines = lines
        self.status = status
        self.later = later
        self.late = late
    }

    var ran: [[String]] { lock.withLock { _ran } }
    var cancelled: Bool { lock.withLock { _cancelled } }

    /// An Installer on `home` whose uv and Python exist (empty files: nothing runs them).
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
        let lines = lock.withLock { () -> [String] in
            _ran.append(args)
            return _ran.count > 1 ? (later ?? self.lines) : self.lines
        }
        for line in lines {
            onLine?(line)
            try? await Task.sleep(for: .milliseconds(20))      // a real sign-in prints over minutes, not at once
        }
        if let status {
            return ProcessResult(status: status, stdout: Data(lines.map { $0 + "\n" }.joined().utf8), stderr: Data())
        }
        while !Task.isCancelled { try? await Task.sleep(for: .milliseconds(5)) }
        lock.withLock { _cancelled = true }
        if let late { onLine?(late) }
        throw CancellationError()
    }
}

/// The Telegram routes of one instance, `ledger`, as the server would answer them: the token saved; the service asked
/// on, then running after `serviceAfterPolls` status reads (nil: never, with launchd's `problem`); a code issued, and
/// the pairing landing after `pairedAfterPolls` status reads once the code is out.
final class TelegramServer: @unchecked Sendable {
    static let problem = "the telegram service is spawn scheduled in launchd (last exit code 1), and it has not "
        + "recorded that it runs"
    private let lock = NSLock()
    private let pairedAfterPolls: Int
    private let serviceAfterPolls: Int?
    private let minutes: Int
    private var token: Bool
    private var service = false
    private var paired: Bool
    private var sinceService: Int?
    private var sincePair: Int?
    private var _polls = 0
    private var _serviceRequests = 0

    init(pairedAfterPolls: Int, token: Bool = false, paired: Bool = false, serviceAfterPolls: Int? = 0,
         minutes: Int = 10) {
        (self.pairedAfterPolls, self.token, self.paired, self.minutes) = (pairedAfterPolls, token, paired, minutes)
        self.serviceAfterPolls = serviceAfterPolls
    }

    /// Status reads since the code was issued, and `telegram on` requests.
    var polls: Int { lock.withLock { _polls } }
    var serviceRequests: Int { lock.withLock { _serviceRequests } }

    /// The service dies (or never ran): it is not running now.
    func stopService() { lock.withLock { (service, sinceService) = (false, nil) } }

    func answer(_ request: SetupStub.Request) -> SetupStub.Answer {
        lock.withLock {
            switch (request.method, request.path) {
            case ("GET", "/app/v1/setup/telegram/status/ledger"):
                if let n = sinceService {
                    sinceService = n + 1
                    if let after = serviceAfterPolls, n >= after { service = true }
                }
                if let n = sincePair {
                    sincePair = n + 1
                    _polls += 1
                    if n + 1 >= pairedAfterPolls { paired = true }
                }
                return .init(200, SetupAccessTests.telegram(token: token, paired: paired, service: service,
                                                            problem: token && !service ? Self.problem : nil))
            case ("POST", "/app/v1/setup/telegram"):
                token = true
                return .init(200, #"{"said": "saved"}"#)
            case ("POST", "/app/v1/setup/telegram/service"):
                _serviceRequests += 1
                sinceService = sinceService ?? 0       // `telegram on` answers before the service records itself
                return .init(200, #"{"said": "telegram installed"}"#)
            case ("POST", "/app/v1/setup/telegram/pair/ledger"):
                sincePair = 0
                return .init(200, SetupAccessTests.telegram(token: token, paired: false, service: service,
                                                            code: "CAFE0001", minutes: minutes))
            default:
                return SetupAccessTests.read(request) ?? SetupAccessTests.notFound
            }
        }
    }
}
