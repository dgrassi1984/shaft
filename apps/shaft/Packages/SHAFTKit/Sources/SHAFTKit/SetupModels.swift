import Foundation

/// `GET /setup/secrets`: the names of the settable keys already stored, in the server's order.
public struct SetupSecretNames: Codable, Sendable, Equatable {
    public let names: [String]
}

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
    public var meetings: [MeetingGrant]?
    /// Whether the operator's Claude sessions may leave notes, nil on older servers.
    public var claudeNotes: Bool?
    /// The operator's Shortcuts it may run on the server Mac, nil on older servers.
    public var shortcuts: [ShortcutGrant]?
    /// Every shortcut on the server Mac (empty when they could not be listed), nil on older servers.
    public var shortcutsAvailable: [Shortcut]?
    /// Why the server Mac's shortcuts could not be listed, nil when they were.
    public var shortcutsProblem: String?
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

    public struct MeetingGrant: Codable, Sendable, Equatable {
        public var account: String
    }

    public struct ShortcutGrant: Codable, Sendable, Equatable {
        /// The shortcut's identifier: a grant survives a rename.
        public var id: String
        public var name: String
        /// False: no longer on the Mac; nil: the Mac's shortcuts could not be listed.
        public var present: Bool?
    }

    public struct Shortcut: Codable, Sendable, Equatable {
        public var id: String
        public var name: String
    }

    public struct GoogleAccount: Codable, Sendable, Equatable {
        public var alias: String
        public var address: String
        public var scopes: [String]
    }
}

/// `POST /access/<n>`: one grant or its revocation. Only the
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
    /// `mail <n> grant|revoke <alias> <label>`: the instance reads that label (never drafts: those are their own
    /// grant); a revoke takes the label back, its draft grant with it.
    public static func mail(alias: String, label: String, action: String = "grant") -> AccessGrant {
        AccessGrant(grant: "mail", action: action, alias: alias, what: label)
    }
    /// `calendar <n> grant <alias> <id>`, and with `writable` `grant-write` on the same calendar; with `action`
    /// `revoke`, `calendar <n> revoke` (read and write both).
    public static func calendar(alias: String, id: String, writable: Bool, action: String = "grant") -> AccessGrant {
        AccessGrant(grant: "calendar", action: action, alias: alias, what: id, writable: writable ? true : nil)
    }
    /// `calendar <n> revoke-write <alias> <id>`: it keeps reading the calendar, no longer changes its events.
    public static func calendarWriteRevoked(alias: String, id: String) -> AccessGrant {
        AccessGrant(grant: "calendar", action: "revoke", alias: alias, what: id, writable: true)
    }
    /// `gdrive <n> grant <alias> [--all]`: its own files, or with `all` also the files shared with the account (a grant
    /// replaces the reach in force); with `action` `revoke`, `gdrive <n> revoke <alias>`.
    public static func drive(alias: String, all: Bool, action: String = "grant") -> AccessGrant {
        AccessGrant(grant: "drive", action: action, alias: alias, all: all && action == "grant" ? true : nil)
    }
    /// `gchat <n> grant|revoke <alias>`.
    public static func chat(alias: String, action: String = "grant") -> AccessGrant {
        AccessGrant(grant: "chat", action: action, alias: alias)
    }

    /// `shortcuts <n> grant|revoke <id>`: the instance may run that one shortcut on the server Mac.
    public static func shortcut(id: String, action: String = "grant") -> AccessGrant {
        AccessGrant(grant: "shortcut", action: action, what: id)
    }

    /// `meetings grant|revoke <n> <alias>`.
    public static func meetings(alias: String, action: String = "grant") -> AccessGrant {
        AccessGrant(grant: "meetings", action: action, alias: alias)
    }
}

/// `GET /setup/telegram/status/<n>` (and `POST /setup/telegram/pair/<n>`, which adds `code` and `minutes`): where the
/// instance's Telegram stands. `token`: it has a bot token; `bot`: the bot's username; `paired`: an account is paired
/// with SHAFT's bots (one for all), `account` naming it; `service`: the Telegram service runs (it has recorded itself);
/// `problem`: with a bot and no running service, why (launchd's words); `pairing`: a code is out.
public struct TelegramStatus: Codable, Sendable, Equatable {
    public var instance: String
    public var token: Bool
    public var bot: String?
    public var paired: Bool
    public var account: String?
    public var service: Bool
    public var problem: String?
    public var pairing: Bool
    /// The pairing answer only: the code to send the bot as `/pair <code>`, and how many minutes it lasts. Nil when an
    /// account is paired already.
    public var code: String?
    public var minutes: Int?

    public init(instance: String, token: Bool, bot: String? = nil, paired: Bool, account: String? = nil, service: Bool,
                problem: String? = nil, pairing: Bool = false, code: String? = nil, minutes: Int? = nil) {
        (self.instance, self.token, self.bot, self.paired, self.account) = (instance, token, bot, paired, account)
        (self.service, self.problem, self.pairing, self.code, self.minutes) = (service, problem, pairing, code, minutes)
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
