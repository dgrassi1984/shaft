import Foundation
import SHAFTKit
import Testing

/// The wizard's steps after Install (macOS/Setup/SetupForms.swift, SetupSteps.swift): the rules the server applies,
/// what each Continue waits for, how the forms become the §18 requests, and the steps' work against a stub server.
/// Nothing here touches the real ~/SHAFT, the Keychain, launchd or a network.
struct SetupFormsTests {
    /// The same table as `core.NAME_RE` (`^[a-z][a-z0-9-]{0,30}$`) and `shaft new`'s refusal of "operator", checked
    /// against the Python rule when it was written (the report of the onboarding task 15).
    @Test func instanceNameValidationMatchesTheServer() {
        let accepted = ["a", "ada", "ledger", "a-1", "x-", "a--b", "k9", "a" + String(repeating: "b", count: 30),
                        "operators", "my-operator"]
        let refused = ["", "operator", "1a", "-a", "Ada", "aDa", "a_b", "a b", "a.b", " a", "a ", "é", "aé",
                       "a" + String(repeating: "b", count: 31), "ada\n", "../x", "a/b"]
        for name in accepted { #expect(InstanceName.problem(name) == nil, "\(name) is refused") }
        for name in refused { #expect(InstanceName.problem(name) != nil, "\(name) is accepted") }
        #expect(InstanceName.problem("operator")?.contains("operator") == true)
    }

    @Test func continueIsDisabledUntilRisksAcknowledged() {
        var context = SetupContinue.Context()
        #expect(!SetupContinue.enabled(.welcome, context))
        context.understood = true
        #expect(SetupContinue.enabled(.welcome, context))
        context.busy = true
        #expect(!SetupContinue.enabled(.welcome, context))
    }

    @Test func driveCardFillsTheEditableText() throws {
        let research = try JSONDecoder().decode(DrivePreset.self, from: Data(#"""
            {"id": "research", "title": "Research partner", "text": "You follow the topics.", "suggests": ["web", "drive"]}
            """#.utf8))
        var state = SetupState()
        state.driveText = "my own words"
        DriveText.choose(research, into: &state)
        #expect(state.drive == "research")
        #expect(state.driveText == "You follow the topics.")
        // The text stays the operator's to edit, and is checked as the server checks it.
        state.driveText += " And report weekly."
        #expect(state.drive == "research")
        #expect(DriveText.problem(state.driveText) == nil)
        #expect(DriveText.problem(String(repeating: "x", count: DriveText.maxLength)) == nil)
        #expect(DriveText.problem(String(repeating: "x", count: DriveText.maxLength + 1)) != nil)
        #expect(DriveText.problem(" \n") != nil)
        #expect(DriveText.suggests(research) == "Suggests web search and Drive")
    }

    @Test func providerFormMapsToTheRequest() throws {
        // OpenRouter and Codex: the provider alone; the key never goes in the request.
        var form = ProviderForm(kind: .openrouter, key: " sk-or-1 ")
        #expect(try form.request().get() == ProviderRequest(provider: "openrouter"))
        #expect(form.secret?.name == "OPENROUTER_API_KEY")
        #expect(form.secret?.value == "sk-or-1")
        form.kind = .codex
        #expect(try form.request().get() == ProviderRequest(provider: "codex"))
        #expect(form.secret == nil)

        // Custom: URL, model and prices (0 allowed); a judge only with its prices; vision only when ticked.
        form = ProviderForm(kind: .custom, key: "", baseURL: " https://host/v1 ", model: "big", priceIn: "0",
                            priceOut: "1.5")
        #expect(try form.request().get() == ProviderRequest(provider: "custom", model: "big", baseUrl: "https://host/v1",
                                                            priceIn: 0, priceOut: 1.5))
        #expect(form.secret == nil)                           // nothing typed: the saved key stays
        form.readsImages = true
        form.judgeModel = "small"
        #expect(throws: FormProblem.self) { try form.request().get() }     // a judge without its prices
        form.judgePriceIn = "0.25"
        form.judgePriceOut = "1"
        #expect(try form.request().get() == ProviderRequest(
            provider: "custom", model: "big", judgeModel: "small", baseUrl: "https://host/v1", priceIn: 0, priceOut: 1.5,
            judgePriceIn: 0.25, judgePriceOut: 1, vision: true))
        for broken in [ProviderForm(kind: .custom, baseURL: "host/v1", model: "m", priceIn: "1", priceOut: "1"),
                       ProviderForm(kind: .custom, baseURL: "ftp://host", model: "m", priceIn: "1", priceOut: "1"),
                       ProviderForm(kind: .custom, baseURL: "https://host/v1", model: " ", priceIn: "1", priceOut: "1"),
                       ProviderForm(kind: .custom, baseURL: "https://host/v1", model: "m", priceIn: "", priceOut: "1"),
                       ProviderForm(kind: .custom, baseURL: "https://host/v1", model: "m", priceIn: "-1", priceOut: "1"),
                       ProviderForm(kind: .custom, baseURL: "https://host/v1", model: "m", priceIn: "1", priceOut: "nan")] {
            #expect(throws: FormProblem.self, "\(broken)") { try broken.request().get() }
        }
        form.key = "sk-custom"
        #expect(form.secret?.name == "SHAFT_CUSTOM_API_KEY")
    }

    @Test func modelContinuesOnlyAfterATestThatPassedForWhatIsSavedNow() {
        let openrouter = ProviderRequest(provider: "openrouter")
        var context = SetupContinue.Context(saved: openrouter, checked: openrouter, checkPassed: true, current: openrouter)
        #expect(SetupContinue.enabled(.model, context))
        context.checkPassed = false                           // a model failed its check
        #expect(!SetupContinue.enabled(.model, context))
        context.checkPassed = true
        context.current = ProviderRequest(provider: "codex")  // another card picked since
        #expect(!SetupContinue.enabled(.model, context))
        context.current = openrouter
        context.checked = nil                                 // saved, not tested
        #expect(!SetupContinue.enabled(.model, context))
        context.checked = ProviderRequest(provider: "custom", model: "m")   // tested before the last save
        #expect(!SetupContinue.enabled(.model, context))
    }

    @Test func instanceContinuesWithValidFieldsOrOnceCreated() {
        var state = SetupState()
        state.instanceName = "ledger"
        state.operatorName = "Ada"
        state.model = "openrouter/x/y"
        state.driveText = "Keep the ledgers."
        var context = SetupContinue.Context(state: state)
        #expect(SetupContinue.enabled(.instance, context))
        let breaks: [(inout SetupState) -> Void] = [{ $0.instanceName = "Ledger" }, { $0.instanceName = "operator" },
                                                    { $0.operatorName = "" }, { $0.operatorName = "A\nB" },
                                                    { $0.model = "" }, { $0.driveText = "" }]
        for broken in breaks {
            var bad = state
            broken(&bad)
            #expect(!SetupContinue.enabled(.instance, SetupContinue.Context(state: bad)))
        }
        // Created: Continue only moves on, whatever the fields say now; the later steps need it.
        var created = SetupState()
        #expect(!SetupContinue.enabled(.access, SetupContinue.Context(state: created)))
        #expect(!SetupContinue.enabled(.hello, SetupContinue.Context(state: created)))
        created.createdInstance = "ledger"
        context = SetupContinue.Context(state: created)
        #expect(SetupContinue.enabled(.instance, context))
        #expect(SetupContinue.enabled(.access, context))
        #expect(SetupContinue.enabled(.hello, context))
        context.capText = "4.5"
        #expect(SetupContinue.enabled(.alone, context))
        context.capText = "four"
        #expect(!SetupContinue.enabled(.alone, context))
        context.capText = ""                                  // the default
        #expect(SetupContinue.enabled(.alone, context))
        context.busy = true
        #expect(!SetupContinue.enabled(.hello, context))
    }

    @Test func theAloneChoiceMapsToWhatTheFirstReplyDoes() throws {
        #expect(AloneChoice(autonomy: "own").then == "resume")
        #expect(AloneChoice(autonomy: "talk").then == "stay")
        #expect(AloneChoice(autonomy: SetupState().autonomy) == .own)    // the default: on its own
        #expect(AloneChoice.own.title(minutes: nil) == "On its own, every 2 h")
        #expect(AloneChoice.own.title(minutes: 90) == "On its own, every 90 min")
        #expect(AloneChoice.talk.title(minutes: 120) == "Only when I talk to it")
        #expect(try Budget.cloudCap("").get() == nil)
        #expect(try Budget.cloudCap(" 8 ").get() == 8)
        #expect(Budget.text(8) == "8")
        #expect(Budget.text(2.5) == "2.5")
        #expect(Budget.text(nil) == "")
    }

    @Test func aCodexLoginURLLineOpensOnlyAWebPage() {
        #expect(CodexLogin.url(in: #"{"url": "https://auth.openai.com/authorize?x=1"}"#)?.host() == "auth.openai.com")
        #expect(CodexLogin.url(in: #"{"step": "login", "ok": true}"#) == nil)
        #expect(CodexLogin.url(in: #"{"url": "file:///etc/passwd"}"#) == nil)
        #expect(CodexLogin.url(in: "Open https://auth.openai.com in your browser") == nil)
    }
}

/// SetupSteps against a stub of the server's §18 routes: what is sent, in which order, and what the wizard keeps.
@MainActor
struct SetupStepsTests {
    @MainActor
    final class Fixture {
        let root = FileManager.default.temporaryDirectory.appending(path: "setup-steps-\(UUID().uuidString)",
                                                                     directoryHint: .isDirectory)
        let suite = "setup-steps-\(UUID().uuidString)"
        let defaults: UserDefaults
        let server: SetupStub
        let setup: MacSetup
        let store: InstancesStore

        init(step: SetupWizardStep, edit: (inout SetupState) -> Void = { _ in }) throws {
            defaults = try #require(UserDefaults(suiteName: suite))
            let states = SetupStateStore(defaults: defaults)
            var state = SetupState()
            state.step = step
            state.operatorName = "Ada"
            edit(&state)
            try states.save(state)
            let home = root.appending(path: "home", directoryHint: .isDirectory)
            try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
            setup = MacSetup(states: states, configure: { Installer.Configuration(home: home) }, runner: NoProcesses())
            server = SetupStub()
            store = InstancesStore(client: APIClient(credential: server.credential, session: server.session),
                                   outbox: try Outbox(directory: root.appending(path: "outbox")),
                                   drafts: DraftStore(directory: root.appending(path: "drafts")))
        }

        deinit {
            try? FileManager.default.removeItem(at: root)
            UserDefaults(suiteName: suite)?.removePersistentDomain(forName: suite)
        }

        func settle() async {
            for _ in 0..<1000 where setup.steps.busy { try? await Task.sleep(for: .milliseconds(5)) }
        }
    }

    @Test func theKeyGoesFirstAndIsNeverKept() async throws {
        let f = try Fixture(step: .model)
        f.server.answer { request in
            switch request.path {
            case "/app/v1/setup/secret": .init(200, #"{"said": "OPENROUTER_API_KEY saved"}"#)
            case "/app/v1/setup/provider":
                .init(200, #"{"provider": "openrouter", "model": "openrouter/x/y", "changed": [], "covers": ["instances"], "off": [], "not_live": []}"#)
            default: .init(404, #"{"error": {"code": "not_found", "message": "no"}}"#)
            }
        }
        f.setup.steps.form = ProviderForm(kind: .openrouter, key: "sk-or-v1-never-kept")
        f.setup.steps.saveProvider(store: f.store, setup: f.setup)
        await f.settle()
        #expect(f.server.requests.map(\.path) == ["/app/v1/setup/secret", "/app/v1/setup/provider"])
        #expect(f.server.requests[0].json?["value"] as? String == "sk-or-v1-never-kept")
        #expect(f.server.requests[1].json?.keys.sorted() == ["provider"])
        #expect(f.setup.steps.form.key == "")
        #expect(f.setup.state.provider == "openrouter")
        #expect(f.setup.state.model == "openrouter/x/y")
        let kept = String(decoding: try #require(f.defaults.data(forKey: SetupStateStore.key)), as: UTF8.self)
        #expect(!kept.contains("sk-or-v1-never-kept"))
        // Saved, not tested: Continue waits for Test.
        #expect(f.setup.steps.saved == ProviderRequest(provider: "openrouter"))
        #expect(f.setup.steps.check == nil)
    }

    @Test func leavingYourInstanceCreatesItOnceThenOnlyMovesOn() async throws {
        let f = try Fixture(step: .instance) { state in
            state.instanceName = "ledger"
            state.model = "openrouter/x/y"
            state.driveText = "Keep the ledgers."
        }
        f.server.answer { request in
            request.path == "/app/v1/setup/instance"
                ? .init(200, #"{"instance": "ledger", "done": ["new", "config", "drive"], "paused": true}"#)
                : .init(404, #"{"error": {"code": "not_found", "message": "no"}}"#)
        }
        f.setup.steps.createInstance(store: f.store, setup: f.setup)
        await f.settle()
        let made = f.server.requests.filter { $0.path == "/app/v1/setup/instance" }
        #expect(made.count == 1)
        #expect(made.first?.json as? [String: String] == ["name": "ledger", "operator": "Ada", "model": "openrouter/x/y",
                                                         "drive": "Keep the ledgers.", "autonomy": "own"])
        #expect(f.setup.state.createdInstance == "ledger")
        #expect(f.setup.state.step == .access)
        f.setup.go(to: .instance)                              // Back, then Continue again
        f.setup.steps.createInstance(store: f.store, setup: f.setup)
        await f.settle()
        #expect(f.server.requests.filter { $0.path == "/app/v1/setup/instance" }.count == 1)
        #expect(f.setup.state.step == .access)
    }

    @Test func aRefusalAfterNewKeepsTheInstanceAndSaysWhatWasNotDone() async throws {
        let f = try Fixture(step: .instance) { state in
            state.instanceName = "ledger"
            state.model = "custom/nope"
            state.driveText = "Keep the ledgers."
        }
        f.server.answer { _ in
            .init(409, #"{"error": {"code": "refused", "message": "refused: LLM_MODEL: no such model", "done": ["new"]}}"#)
        }
        f.setup.steps.createInstance(store: f.store, setup: f.setup)
        await f.settle()
        #expect(f.setup.state.createdInstance == "ledger")     // never a second `new` that would say it exists
        #expect(f.setup.state.step == .instance)
        let said = try #require(f.setup.steps.creating.failure)
        #expect(said.contains("ledger was created"))
        #expect(said.contains("LLM_MODEL: no such model"))
        #expect(said.contains("its drive"))
    }

    /// Starting over (or the wizard ending) cancels the creation in flight: nothing it answers is recorded.
    @Test func theWizardStartingOverDuringCreationRecordsNothing() async throws {
        let f = try Fixture(step: .instance) { state in
            state.instanceName = "ledger"
            state.model = "openrouter/x/y"
            state.driveText = "Keep the ledgers."
        }
        let gate = Gate()
        f.server.answer { _ in
            .init(200, #"{"instance": "ledger", "done": ["new", "config", "drive"], "paused": true}"#, hold: { await gate.wait() })
        }
        f.setup.steps.createInstance(store: f.store, setup: f.setup)
        for _ in 0..<400 where f.server.requests.isEmpty { try await Task.sleep(for: .milliseconds(5)) }
        f.setup.steps.reset()
        gate.open()
        try await Task.sleep(for: .milliseconds(100))
        #expect(f.setup.state.createdInstance == nil)
        #expect(f.setup.state.step == .instance)
        #expect(!f.setup.steps.busy)
    }

    /// Closing the window stops the reads, not a creation already sent: the server makes the instance either way, and
    /// a wizard that forgot it would try `new` again and be refused ("already exists").
    @Test func closingTheWindowDuringCreationStillRecordsIt() async throws {
        let f = try Fixture(step: .instance) { state in
            state.instanceName = "ledger"
            state.model = "openrouter/x/y"
            state.driveText = "Keep the ledgers."
        }
        let gate = Gate()
        f.server.answer { _ in
            .init(200, #"{"instance": "ledger", "done": ["new", "config", "drive"], "paused": true}"#,
                  hold: { await gate.wait() })
        }
        f.setup.steps.createInstance(store: f.store, setup: f.setup)
        for _ in 0..<400 where f.server.requests.isEmpty { try await Task.sleep(for: .milliseconds(5)) }
        f.setup.steps.stop()
        gate.open()
        await f.settle()
        #expect(f.setup.state.createdInstance == "ledger")
        #expect(f.setup.state.step == .access)
    }

    @Test func howItWorksAloneSendsTheChoiceThenTheChangedBudget() async throws {
        let f = try Fixture(step: .alone) { state in
            state.createdInstance = "ledger"
            state.autonomy = "talk"
        }
        let settings = Self.settings
        f.server.answer { request in
            switch (request.method, request.path) {
            case ("GET", "/app/v1/settings/ledger"): .init(200, settings)
            case ("POST", "/app/v1/setup/autonomy/ledger"): .init(200, #"{"instance": "ledger", "then": "stay"}"#)
            case ("POST", "/app/v1/settings/ledger"): .init(200, #"{"settings": \#(settings), "applies": "now"}"#)
            default: .init(404, #"{"error": {"code": "not_found", "message": "no"}}"#)
            }
        }
        f.setup.steps.loadSettings(store: f.store, name: "ledger")
        await f.settle()
        let shown = try #require(f.setup.steps.settings)
        f.setup.steps.capText = "4.5"
        f.setup.steps.saveAlone(store: f.store, setup: f.setup)
        await f.settle()
        #expect(f.setup.steps.savingAlone == .idle)
        let posts = f.server.requests.filter { $0.method == "POST" }
        #expect(posts.map(\.path) == ["/app/v1/setup/autonomy/ledger", "/app/v1/settings/ledger"])
        #expect(posts[0].json as? [String: String] == ["then": "stay"])
        #expect(posts[1].json?["field"] as? String == "cloud_cap")
        #expect(posts[1].json?["value"] as? Double == 4.5)
        #expect(shown.cloudCap.value == nil)
        #expect(posts[1].json?["expect"] is NSNull)            // what was shown: no override
        #expect(f.setup.state.step == .hello)
    }

    @Test func anUnchangedBudgetIsNotWrittenAndTheDefaultChoiceResumes() async throws {
        let f = try Fixture(step: .alone) { $0.createdInstance = "ledger" }
        let settings = Self.settings
        f.server.answer { request in
            request.path == "/app/v1/settings/ledger" ? .init(200, settings)
                : .init(200, #"{"instance": "ledger", "then": "resume"}"#)
        }
        f.setup.steps.loadSettings(store: f.store, name: "ledger")
        await f.settle()
        f.setup.steps.saveAlone(store: f.store, setup: f.setup)
        await f.settle()
        let posts = f.server.requests.filter { $0.method == "POST" }
        #expect(posts.map(\.path) == ["/app/v1/setup/autonomy/ledger"])
        #expect(posts.first?.json as? [String: String] == ["then": "resume"])
    }

    @Test func aRefusedChoiceShowsTheServersWordsAndStays() async throws {
        let f = try Fixture(step: .alone) { $0.createdInstance = "ledger" }
        f.server.answer { _ in
            .init(409, #"{"error": {"code": "refused", "message": "ledger is not waiting for its first reply"}}"#)
        }
        f.setup.steps.saveAlone(store: f.store, setup: f.setup)
        await f.settle()
        #expect(f.setup.steps.savingAlone == .failed("ledger is not waiting for its first reply"))
        #expect(f.setup.state.step == .alone)
    }

    /// `GET /settings/ledger` (docs/app-api.md §15.1): no cloud-cap override.
    static let settings = #"""
    {"instance":"ledger","paused":true,
     "model":{"value":"openrouter/x/y","choices":[{"model":"openrouter/x/y","label":"openrouter/x/y (cloud)"}]},
     "effort":{"value":"medium","choices":["low","medium","high"]},
     "wake_interval":{"value":120,"floor":null,"effective":120},
     "cloud_cap":{"value":null,"spent_today":0.0},
     "channel":{"value":"auto","choices":["auto","web","telegram"]},
     "operator_name":{"value":"Ada"},
     "voice_mode":{"value":"off","choices":["on","off"]},
     "voice_effort":{"value":"","choices":["","low","medium","high"]}}
    """#
}

/// A one-shot gate a held answer waits on.
final class Gate: @unchecked Sendable {
    private let lock = NSLock()
    private var opened = false
    private var waiting: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            let now = lock.withLock { () -> Bool in
                if opened { return true }
                waiting.append(continuation)
                return false
            }
            if now { continuation.resume() }
        }
    }

    func open() {
        let resumed = lock.withLock { () -> [CheckedContinuation<Void, Never>] in
            opened = true
            defer { waiting = [] }
            return waiting
        }
        for continuation in resumed { continuation.resume() }
    }
}

/// No command runs in these tests.
private struct NoProcesses: ProcessRunning {
    struct Ran: Error {}
    func run(_ exe: URL, _ args: [String], env: [String: String], stdin: Data?,
             onLine: (@Sendable (String) -> Void)?) async throws -> ProcessResult {
        throw Ran()
    }
}

/// A server stand-in behind `URLProtocol`, its own host per test: the requests it saw and what it answers.
final class SetupStub: @unchecked Sendable {
    struct Request: Sendable {
        let method: String
        let path: String
        let body: Data
        var json: [String: Any]? { (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] }
    }

    struct Answer: Sendable {
        var status: Int
        var body: String
        var hold: (@Sendable () async -> Void)?
        init(_ status: Int, _ body: String, hold: (@Sendable () async -> Void)? = nil) {
            self.status = status
            self.body = body
            self.hold = hold
        }
    }

    private static let registry = NSLock()
    nonisolated(unsafe) private static var servers: [String: SetupStub] = [:]
    let host = "setup-\(UUID().uuidString.lowercased()).test"
    private let lock = NSLock()
    private var log: [Request] = []
    private var handler: @Sendable (Request) -> Answer = { _ in Answer(404, #"{"error": {"code": "not_found", "message": "no"}}"#) }

    init() { Self.registry.withLock { Self.servers[host] = self } }
    deinit { Self.registry.withLock { Self.servers[host] = nil } }

    var credential: ServerCredential {
        ServerCredential(baseURL: URL(string: "http://\(host):8090")!, device: "d-1", secret: "s", name: "mac-app",
                         login: "local@mac")
    }

    var session: URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [SetupStubProtocol.self]
        return URLSession(configuration: configuration)
    }

    var requests: [Request] { lock.withLock { log } }

    func answer(_ handler: @escaping @Sendable (Request) -> Answer) { lock.withLock { self.handler = handler } }

    fileprivate static func server(for host: String?) -> SetupStub? {
        guard let host else { return nil }
        return registry.withLock { servers[host] }
    }

    fileprivate func handle(_ request: Request) -> Answer {
        let handler = lock.withLock {
            log.append(request)
            return self.handler
        }
        return handler(request)
    }
}

final class SetupStubProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { SetupStub.server(for: request.url?.host) != nil }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url, let server = SetupStub.server(for: url.host) else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotFindHost))
            return
        }
        var body = request.httpBody ?? Data()
        if body.isEmpty, let stream = request.httpBodyStream {
            stream.open()
            var buffer = [UInt8](repeating: 0, count: 64 * 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                body.append(buffer, count: count)
            }
            stream.close()
        }
        let answer = server.handle(.init(method: request.httpMethod ?? "GET", path: url.path, body: body))
        nonisolated(unsafe) let stub = self
        Task {
            await answer.hold?()
            let http = HTTPURLResponse(url: url, statusCode: answer.status, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
            stub.client?.urlProtocol(stub, didReceive: http, cacheStoragePolicy: .notAllowed)
            stub.client?.urlProtocol(stub, didLoad: Data(answer.body.utf8))
            stub.client?.urlProtocolDidFinishLoading(stub)
        }
    }

    override func stopLoading() {}
}
