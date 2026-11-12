import Foundation

// The setup routes (docs/app-api.md §18): what the Mac app's setup wizard reads back. Decoded from the server's own
// fixtures (supervisor/tests/app_api_fixtures/setup-*.json, access.json, google-*.json).

/// `POST /setup/instance`: the steps done (`new`, `config`, `drive`). A refusal carries the steps already done in
/// `ServerError.done`.
public struct SetupInstanceResponse: Codable, Sendable, Equatable {
    public var instance: String
    public var done: [String]
    public var paused: Bool
}

/// `GET /setup/drives`: four starting drives; `suggests` names the access rows to pre-tick (`DrivePreset.grants`).
public struct DrivePresets: Codable, Sendable, Equatable {
    public var drives: [DrivePreset]
}

public struct DrivePreset: Codable, Sendable, Equatable, Identifiable {
    /// The access rows a drive may suggest, as the server names them.
    public static let grants = ["folder", "browser", "web", "telegram", "mail", "calendar", "drive", "chat"]

    public var id: String
    public var title: String
    public var text: String
    public var suggests: [String]
}

/// `POST /setup/provider`: the provider's model fields. Which ones a provider takes is the verb's to decide (`custom`
/// needs `model`, `baseUrl` and the prices); a nil field is not sent. The key itself is never here: it goes first, alone,
/// through `POST /setup/secret`.
public struct ProviderRequest: Codable, Sendable, Equatable {
    public var provider: String
    public var model: String?
    public var judgeModel: String?
    public var baseUrl: String?
    public var priceIn: Double?
    public var priceOut: Double?
    public var judgePriceIn: Double?
    public var judgePriceOut: Double?
    public var vision: Bool?

    public init(provider: String, model: String? = nil, judgeModel: String? = nil, baseUrl: String? = nil,
                priceIn: Double? = nil, priceOut: Double? = nil, judgePriceIn: Double? = nil,
                judgePriceOut: Double? = nil, vision: Bool? = nil) {
        self.provider = provider
        self.model = model
        self.judgeModel = judgeModel
        self.baseUrl = baseUrl
        self.priceIn = priceIn
        self.priceOut = priceOut
        self.judgePriceIn = judgePriceIn
        self.judgePriceOut = judgePriceOut
        self.vision = vision
    }
}

/// `POST /setup/provider`: what the provider's preset covers and what stays off, and why.
public struct ProviderUseResponse: Codable, Sendable, Equatable {
    public var provider: String
    /// The instance model it suggests: a new instance's `LLM_MODEL`.
    public var model: String
    public var changed: [String]
    public var covers: [String]
    public var off: [Off]
    public var notLive: [String]

    public struct Off: Codable, Sendable, Equatable {
        public var function: String
        public var why: String
    }
}

/// `POST /setup/provider/check`: one live call per configured model of the provider.
public struct ProviderCheckResponse: Codable, Sendable, Equatable {
    public var provider: String
    public var ok: Bool
    public var models: [Row]

    public struct Row: Codable, Sendable, Equatable {
        public var ok: Bool
        public var model: String?
        public var error: String?
    }
}

/// `POST /setup/autonomy/<n>`: what the operator's first answered reply does, `resume` or `stay`.
public struct SetupAutonomyResponse: Codable, Sendable, Equatable {
    public var instance: String
    public var then: String
}

/// `GET|POST /access/<n>`: what the operator has granted an instance. Keys are said set or not, never their values.
public struct AccessResponse: Codable, Sendable, Equatable {
    public var instance: String
    public var folder: String?
    public var browser: Browser
    public var webSearch: Bool
    public var telegram: Bool
    public var mail: [MailGrant]
    public var calendar: [CalendarGrant]
    public var drive: [DriveGrant]
    public var chat: [ChatGrant]
    public var googleAccounts: [GoogleAccount]

    public struct Browser: Codable, Sendable, Equatable {
        /// The shared Chromium's Playwright version, nil when it is not installed.
        public var installed: String?
        /// Whether the instance's `run` tool is on: it browses from inside it.
        public var run: Bool
    }

    public struct MailGrant: Codable, Sendable, Equatable {
        public var account: String
        public var label: String
        public var draft: Bool
    }

    public struct CalendarGrant: Codable, Sendable, Equatable {
        public var account: String
        public var calendar: String
        public var name: String
        public var write: Bool
    }

    public struct DriveGrant: Codable, Sendable, Equatable {
        public var account: String
        /// `own` (its own files) or `all` (also those shared with it).
        public var reach: String
    }

    public struct ChatGrant: Codable, Sendable, Equatable {
        public var account: String
    }

    public struct GoogleAccount: Codable, Sendable, Equatable {
        public var alias: String
        public var address: String
        public var scopes: [String]
    }
}

/// `POST /access/<n>`: one grant (`folder`, `browser`, `mail`, `calendar`, `drive`, `chat`) or its revocation. Only the
/// fields that grant takes are set; a nil one is not sent (the server refuses a field another grant takes).
public struct AccessGrant: Codable, Sendable, Equatable {
    /// `browser install` (900 s on the server) and the request around it.
    public static let browserTimeout: TimeInterval = 960

    public var grant: String
    public var action: String
    public var alias: String?
    public var what: String?
    public var all: Bool?
    public var writable: Bool?

    public init(grant: String, action: String = "grant", alias: String? = nil, what: String? = nil, all: Bool? = nil,
                writable: Bool? = nil) {
        self.grant = grant
        self.action = action
        self.alias = alias
        self.what = what
        self.all = all
        self.writable = writable
    }

    /// `share <n> <folder>`: the folder it may read.
    public static func folder(_ path: String) -> AccessGrant { AccessGrant(grant: "folder", what: path) }
    /// `browser install` when none is installed, then `tools <n> on run` when `run` is off.
    public static let browser = AccessGrant(grant: "browser")
}

/// `GET /setup/telegram/status/<n>` (and `POST /setup/telegram/pair/<n>`, which adds `code` and `minutes`): where the
/// instance's Telegram stands. `token`: it has a bot token; `bot`: the bot's username; `paired`: an account is paired
/// with SHAFT's bots (one for all), `account` naming it; `service`: the Telegram service runs; `pairing`: a code is out.
public struct TelegramStatus: Codable, Sendable, Equatable {
    public var instance: String
    public var token: Bool
    public var bot: String?
    public var paired: Bool
    public var account: String?
    public var service: Bool
    public var pairing: Bool
    /// The pairing answer only: the code to send the bot as `/pair <code>`, and how many minutes it lasts. Nil when an
    /// account is paired already.
    public var code: String?
    public var minutes: Int?

    public init(instance: String, token: Bool, bot: String? = nil, paired: Bool, account: String? = nil, service: Bool,
                pairing: Bool = false, code: String? = nil, minutes: Int? = nil) {
        (self.instance, self.token, self.bot, self.paired, self.account) = (instance, token, bot, paired, account)
        (self.service, self.pairing, self.code, self.minutes) = (service, pairing, code, minutes)
    }

    /// The operator can talk with the instance there: a bot, a paired account, the service running.
    public var ready: Bool { token && paired && service }
}

/// `GET /google/<alias>/labels`.
public struct GoogleLabels: Codable, Sendable, Equatable {
    public var alias: String
    public var labels: [String]
}

/// `GET /google/<alias>/calendars`, primary first.
public struct GoogleCalendars: Codable, Sendable, Equatable {
    public var alias: String
    public var calendars: [Calendar]

    public struct Calendar: Codable, Sendable, Equatable, Identifiable {
        public var id: String
        public var title: String
        public var primary: Bool
        public var access: String
    }
}
