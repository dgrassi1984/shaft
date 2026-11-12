import Foundation
import SHAFTKit

// The setup wizard's rules after Install, kept free of views so the SHAFTMacTests bundle checks them: what the server
// would refuse (the instance name, a drive too long), what each step's Continue waits for, and how the Model step's
// form and the "How it works alone" choice become the requests of docs/app-api.md §18.

/// A rule the operator's input breaks, said as the step shows it.
struct FormProblem: Error, Equatable, LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// The instance name `shaft new` takes: `core.NAME_RE` (`^[a-z][a-z0-9-]{0,30}$`: a lowercase ASCII letter, then up to 30
/// lowercase ASCII letters, digits or `-`) and not `operator`, which names the operator.
enum InstanceName {
    static let maxLength = 31

    /// Nil when the server takes it, else why not.
    static func problem(_ name: String) -> String? {
        if name.isEmpty { return "Give it a name." }
        if name == "operator" { return "operator is taken: it is what SHAFT calls you." }
        let scalars = Array(name.unicodeScalars)
        let lower = { (s: Unicode.Scalar) in ("a"..."z").contains(s) }
        let digit = { (s: Unicode.Scalar) in ("0"..."9").contains(s) }
        guard let first = scalars.first, lower(first) else { return "Start with a lowercase letter (a to z)." }
        guard scalars.count <= maxLength else { return "At most \(maxLength) characters." }
        guard scalars.dropFirst().allSatisfy({ lower($0) || digit($0) || $0 == "-" }) else {
            return "Only lowercase letters, digits and -."
        }
        return nil
    }
}

/// One line the server takes as a field of `/setup/instance` (`operator`, `model`): not empty, no line break, at most
/// 500 characters (`app_api._text`).
enum OneLine {
    static func problem(_ text: String, _ what: String) -> String? {
        if text.trimmingCharacters(in: .whitespaces).isEmpty { return "\(what) is empty." }
        if text.contains(where: \.isNewline) { return "\(what) is one line." }
        if text.unicodeScalars.count > 500 { return "\(what) is at most 500 characters." }
        return nil
    }
}

/// The drive's text, which becomes the instance's `drive.md`: at most 20000 characters (`web_config.DRIVE_MAX`, as
/// the server counts them: code points).
enum DriveText {
    static let maxLength = 20000

    static func problem(_ text: String) -> String? {
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "The drive is empty: pick a card or write one." }
        let count = text.unicodeScalars.count
        return count > maxLength ? "The drive is \(count) characters; at most \(maxLength)." : nil
    }

    /// A drive card picked: its text replaces the editable one, which the operator then edits.
    static func choose(_ preset: DrivePreset, into state: inout SetupState) {
        state.drive = preset.id
        state.driveText = preset.text
    }

    /// The access a card suggests, in the words of the Access step.
    static func suggests(_ preset: DrivePreset) -> String {
        let words = ["folder": "a folder", "browser": "the browser", "web": "web search", "telegram": "Telegram",
                     "mail": "mail", "calendar": "calendar", "drive": "Drive", "chat": "Google Chat"]
        let named = preset.suggests.map { words[$0] ?? $0 }
        return named.isEmpty ? "Suggests no access" : "Suggests " + ListFormatter.localizedString(byJoining: named)
    }
}

/// The three ways to the models (the Model step's cards).
enum ProviderKind: String, CaseIterable, Identifiable, Sendable {
    case openrouter, codex, custom
    var id: String { rawValue }

    var title: String {
        switch self {
        case .openrouter: "OpenRouter"
        case .codex: "ChatGPT (Codex)"
        case .custom: "Custom"
        }
    }

    var subtitle: String {
        switch self {
        case .openrouter: "One key reaches many models: the instance, its judges and SHAFT's helpers."
        case .codex: "Your ChatGPT plan through the Codex in ChatGPT for Mac. No key: you sign in."
        case .custom: "Any OpenAI-compatible endpoint: its URL, a key and the models with their prices."
        }
    }

    /// The key it needs, set through `/setup/secret`; Codex signs in instead.
    var secretName: String? {
        switch self {
        case .openrouter: "OPENROUTER_API_KEY"
        case .codex: nil
        case .custom: "SHAFT_CUSTOM_API_KEY"
        }
    }
}

/// The Model step's form. The key is held only until it is saved (`/setup/secret`, before the provider): never in
/// `setup.state`, a log, or the request to `/setup/provider`.
struct ProviderForm: Equatable {
    var kind: ProviderKind = .openrouter
    var key = ""
    var baseURL = ""
    var model = ""
    var priceIn = ""
    var priceOut = ""
    var judgeModel = ""
    var judgePriceIn = ""
    var judgePriceOut = ""
    var readsImages = false

    /// The key to save first, nil when none was typed (one saved before stays as it is) or the provider has none.
    var secret: (name: String, value: String)? {
        let value = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let name = kind.secretName, !value.isEmpty else { return nil }
        return (name, value)
    }

    /// `POST /setup/provider`'s body, or what to fix first. OpenRouter and Codex take no field (their models are the
    /// preset's); a custom endpoint needs its URL, its model and the model's prices (0 allowed), and a judge model only
    /// with its own prices.
    func request() -> Result<ProviderRequest, FormProblem> {
        guard kind == .custom else { return .success(ProviderRequest(provider: kind.rawValue)) }
        let url = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let parsed = URL(string: url), ["http", "https"].contains(parsed.scheme?.lowercased() ?? ""),
              parsed.host() != nil else {
            return .failure(FormProblem("The base URL is the endpoint's address, http:// or https://, e.g. https://host/v1."))
        }
        let model = model.trimmingCharacters(in: .whitespacesAndNewlines)
        if let problem = OneLine.problem(model, "The model") { return .failure(FormProblem(problem)) }
        let priceIn: Double, priceOut: Double
        switch (Self.price(self.priceIn, "Its price in"), Self.price(self.priceOut, "Its price out")) {
        case (.failure(let p), _), (_, .failure(let p)): return .failure(p)
        case (.success(let i), .success(let o)): (priceIn, priceOut) = (i, o)
        }
        var request = ProviderRequest(provider: "custom", model: model, baseUrl: url, priceIn: priceIn, priceOut: priceOut,
                                      vision: readsImages ? true : nil)
        let judge = judgeModel.trimmingCharacters(in: .whitespacesAndNewlines)
        if !judge.isEmpty {
            if let problem = OneLine.problem(judge, "The judge model") { return .failure(FormProblem(problem)) }
            switch (Self.price(judgePriceIn, "The judge's price in"), Self.price(judgePriceOut, "The judge's price out")) {
            case (.failure(let p), _), (_, .failure(let p)): return .failure(p)
            case (.success(let i), .success(let o)):
                request.judgeModel = judge
                request.judgePriceIn = i
                request.judgePriceOut = o
            }
        }
        return .success(request)
    }

    /// Dollars per million tokens: a number, 0 or more.
    static func price(_ text: String, _ what: String) -> Result<Double, FormProblem> {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard let value = Double(trimmed), value.isFinite, value >= 0 else {
            return .failure(FormProblem("\(what) is dollars per million tokens: a number, 0 or more."))
        }
        return .success(value)
    }
}

/// "How it works alone": the two-way choice, saved in `setup.state.autonomy` as `/setup/instance` names it, and sent
/// to `/setup/autonomy/<n>` as what the first answered reply does.
enum AloneChoice: String, CaseIterable, Identifiable {
    case talk, own
    var id: String { rawValue }

    init(autonomy: String) {
        self = autonomy == "talk" ? .talk : .own
    }

    /// `POST /setup/autonomy/<n>`'s `then`.
    var then: String { self == .own ? "resume" : "stay" }

    /// The choice's label; `minutes` is the instance's wake interval when known (2 h by default).
    func title(minutes: Int?) -> String {
        switch self {
        case .talk: return "Only when I talk to it"
        case .own:
            let m = minutes ?? 120
            return m > 0 && m % 60 == 0 ? "On its own, every \(m / 60) h" : "On its own, every \(m) min"
        }
    }
}

/// The daily budgets shown on "How it works alone".
enum Budget {
    /// The model's daily cap as typed: empty is no override (the supervisor's default, `SHAFT_CLOUD_DAILY_USD`,
    /// applies), else a positive amount of dollars, as `core.limit_value` takes it (rounded to the cent, above 0).
    static func cloudCap(_ text: String) -> Result<Double?, FormProblem> {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { return .success(nil) }
        guard let value = Double(trimmed), value.isFinite, value >= 0.01 else {
            return .failure(FormProblem("The model's daily budget is a positive amount of dollars (at least 0.01), or "
                                        + "empty for the default."))
        }
        return .success(value)
    }

    /// The cap as the field shows it: empty for no override; dollars to the cent, trailing zeros dropped, in the
    /// form `cloudCap` reads back whatever the Mac's locale (no conversion to Int, which traps past Int.max).
    static func text(_ cap: Double?) -> String {
        guard let cap else { return "" }
        guard cap.isFinite else { return String(cap) }
        var text = String(format: "%.2f", cap)
        while text.hasSuffix("0") { text.removeLast() }
        if text.hasSuffix(".") { text.removeLast() }
        return text
    }

    /// The budgets this version cannot change from the app: (what, the supervisor setting, its default).
    static let fixed: [(what: String, setting: String, dollars: Int)] = [
        ("Coding", "SHAFT_CODER_DAILY_USD", 5),
        ("Second opinions", "SHAFT_CONSULT_DAILY_USD", 3),
        ("Trials", "SHAFT_TRIAL_DAILY_USD", 3),
    ]
}

/// What `shaft codex login --json` prints, one JSON object a line: the sign-in page (`url`, opened in the browser), the
/// one-time code to enter there (`code`), then `{"ok": true}` or `{"ok": false, "error"}` with Codex's own words.
enum CodexLogin {
    enum Event: Equatable {
        case url(URL)
        case code(String)
        case ok
        case failed(String)
    }

    static func event(in line: String) -> Event? {
        guard let object = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any] else { return nil }
        if let text = object["url"] as? String {
            guard let url = URL(string: text), ["https", "http"].contains(url.scheme?.lowercased() ?? "") else { return nil }
            return .url(url)
        }
        if let code = object["code"] as? String, !code.isEmpty { return .code(code) }
        if let ok = object["ok"] as? Bool {
            return ok ? .ok : .failed((object["error"] as? String) ?? "Codex sign-in failed, without a reason.")
        }
        return nil
    }

    /// A web page to open; nothing else (a `file:` URL, prose) is ever opened.
    static func url(in line: String) -> URL? {
        if case .url(let url) = event(in: line) { url } else { nil }
    }

    /// How the sign-in ended, from everything it printed: its last `ok` line, nil when it printed none.
    static func outcome(_ stdout: Data) -> Event? {
        String(decoding: stdout, as: UTF8.self).split(whereSeparator: \.isNewline).compactMap { line -> Event? in
            switch event(in: String(line)) {
            case .ok?: .ok
            case .failed(let why)?: .failed(why)
            default: nil
            }
        }.last
    }
}

/// The Access step's rows: the access a drive may suggest (`DrivePreset.grants`, the server's names), grouped as the step
/// shows them. Continue applies the checked rows of `applied`, in that order; the Google rows are their own step.
enum AccessRow: String, CaseIterable, Identifiable, Sendable {
    case folder, browser, mail, calendar, drive, chat, web, telegram
    var id: String { rawValue }

    /// The rows granted from this step, in the order Continue applies them.
    static let applied: [AccessRow] = [.folder, .browser, .web, .telegram]
    static let google: [AccessRow] = [.mail, .calendar, .drive, .chat]

    var title: String {
        switch self {
        case .folder: "A folder you choose"
        case .browser: "A real browser"
        case .mail: "Mail labels"
        case .calendar: "Calendars"
        case .drive: "Drive files"
        case .chat: "Google Chat"
        case .web: "Web search"
        case .telegram: "Telegram"
        }
    }

    /// The rows the chosen drive suggests: pre-checked when the step first opens.
    static func suggested(by preset: DrivePreset?) -> Set<AccessRow> {
        Set((preset?.suggests ?? []).compactMap(AccessRow.init(rawValue:)))
    }

    /// What the server says is granted already (§18.6): such a row shows as on, and Continue does not send it again.
    /// Telegram is on only when it works: its bot saved, an account paired and the service running (`telegram`).
    static func granted(in access: AccessResponse?, telegram: TelegramStatus? = nil) -> Set<AccessRow> {
        var on: Set<AccessRow> = telegram?.ready == true ? [.telegram] : []
        guard let access else { return on }
        if access.folder != nil { on.insert(.folder) }
        if access.browser.installed != nil, access.browser.run { on.insert(.browser) }
        if access.webSearch { on.insert(.web) }
        if !access.mail.isEmpty { on.insert(.mail) }
        if !access.calendar.isEmpty { on.insert(.calendar) }
        if !access.drive.isEmpty { on.insert(.drive) }
        if !access.chat.isEmpty { on.insert(.chat) }
        return on
    }
}

/// What each step's Continue waits for. The footer asks this alone, so the rules are the ones the tests check.
enum SetupContinue {
    struct Context {
        var understood = false
        var installComplete = false
        /// A command or request of the step on screen is running.
        var busy = false
        var state = SetupState()
        /// The Model step: the provider request saved, the one the last check answered (with whether every model
        /// answered), and the form's request now.
        var saved: ProviderRequest?
        var checked: ProviderRequest?
        var checkPassed = false
        var current: ProviderRequest?
        /// "How it works alone": the model's daily cap as typed.
        var capText = ""
        /// Access: what is granted was read for the created instance, and its suggestions ticked.
        var accessReady = false
    }

    static func enabled(_ step: SetupWizardStep, _ c: Context) -> Bool {
        if c.busy { return false }
        switch step {
        case .welcome: return SetupGate.welcomeCanContinue(understood: c.understood)
        case .install: return c.installComplete
        case .model:
            // Only after a check that passed for what is saved now, and the form still says the same.
            guard c.checkPassed, let saved = c.saved else { return false }
            return saved == c.checked && saved == c.current
        case .instance:
            if c.state.createdInstance != nil { return true }   // created already: Continue only moves on
            return InstanceName.problem(c.state.instanceName) == nil
                && OneLine.problem(c.state.operatorName, "Your name") == nil
                && OneLine.problem(c.state.model, "The model") == nil
                && DriveText.problem(c.state.driveText) == nil
        case .access:
            // A failed read never lets Continue skip the drive's suggestions unseen: it shows Try again (and Skip all).
            return c.state.createdInstance != nil && c.accessReady
        case .alone:
            guard c.state.createdInstance != nil, case .success = Budget.cloudCap(c.capText) else { return false }
            return true
        case .hello: return c.state.createdInstance != nil
        }
    }
}
