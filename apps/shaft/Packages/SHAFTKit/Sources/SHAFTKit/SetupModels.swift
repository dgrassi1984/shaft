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

/// `POST /setup/provider`: what the provider's preset covers and what stays off, and why.
public struct ProviderUseResponse: Codable, Sendable, Equatable {
    public var provider: String
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
