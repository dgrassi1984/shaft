import Foundation

public struct InstanceSettings: Codable, Sendable, Equatable {
    public var instance: String
    public var paused: Bool
    public var model: Model
    public var effort: Effort
    public var wakeInterval: WakeInterval
    public var cloudCap: CloudCap
    public var channel: Channel
    public var operatorName: OperatorName
    public var voiceMode: VoiceMode
    public var voiceEffort: VoiceEffort
    /// WATCH_MAIL / WATCH_CALENDAR / WATCH_GCHAT / WATCH_MEETINGS (off, inbox or wake); nil from a server without them.
    public var watchMail: Watch?
    public var watchCalendar: Watch?
    public var watchGchat: Watch?
    public var watchMeetings: Watch?

    public struct Watch: Codable, Sendable, Equatable {
        public var value: String
        public var choices: [String]
    }

    /// The watch a field names, for the four watch fields.
    public func watch(_ field: SettingField) -> Watch? {
        switch field {
        case .watchMail: watchMail
        case .watchCalendar: watchCalendar
        case .watchGchat: watchGchat
        case .watchMeetings: watchMeetings
        default: nil
        }
    }

    public struct Model: Codable, Sendable, Equatable {
        public var value: String
        public var choices: [Choice]

        public struct Choice: Codable, Sendable, Equatable {
            public var model: String
            public var label: String
        }
    }

    public struct Effort: Codable, Sendable, Equatable {
        public var value: String?
        public var choices: [String]?
    }

    public struct WakeInterval: Codable, Sendable, Equatable {
        public var value: Int
        public var floor: Int?
        public var effective: Int
    }

    public struct CloudCap: Codable, Sendable, Equatable {
        public var value: Double?
        public var spentToday: Double
    }

    public struct Channel: Codable, Sendable, Equatable {
        public var value: String
        public var choices: [String]
    }

    public struct VoiceMode: Codable, Sendable, Equatable {
        public var value: String
        public var choices: [String]
    }

    public struct VoiceEffort: Codable, Sendable, Equatable {
        public var value: String
        public var choices: [String]?
    }

    public struct OperatorName: Codable, Sendable, Equatable {
        public var value: String
    }

    public func value(for field: SettingField) -> SettingValue {
        switch field {
        case .model: .string(model.value)
        case .effort: effort.value.map(SettingValue.string) ?? .null
        case .wakeInterval: .int(wakeInterval.value)
        case .cloudCap: cloudCap.value.map(SettingValue.double) ?? .null
        case .channel: .string(channel.value)
        case .operatorName: .string(operatorName.value)
        case .voiceMode: .string(voiceMode.value)
        case .voiceEffort: .string(voiceEffort.value)
        case .watchMail, .watchCalendar, .watchGchat, .watchMeetings: .string(watch(field)?.value ?? "off")
        }
    }
}

public enum SettingField: String, Codable, Sendable, CaseIterable {
    case model, effort, channel
    case wakeInterval = "wake_interval"
    case cloudCap = "cloud_cap"
    case operatorName = "operator_name"
    case voiceMode = "voice_mode"
    case voiceEffort = "voice_effort"
    case watchMail = "watch_mail"
    case watchCalendar = "watch_calendar"
    case watchGchat = "watch_gchat"
    case watchMeetings = "watch_meetings"

    /// The watches, in the order the settings show them.
    public static let watches: [SettingField] = [.watchMeetings, .watchMail, .watchCalendar, .watchGchat]
}

/// A scalar JSON value. `.null` explicitly unsets effort or the cloud-cap override.
public enum SettingValue: Codable, Sendable, Equatable {
    case string(String)
    case int(Int)
    case double(Double)
    case null

    public init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode(Int.self) { self = .int(value) }
        else { self = .double(try container.decode(Double.self)) }
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }
}

public struct SettingSave: Codable, Sendable, Equatable {
    public var settings: InstanceSettings
    public var applies: String
    public var notes: [String]?
}

/// `GET /app/v1/version` (§19): the release the web service runs (git describe) and its `/app/v1` version; on loopback
/// also the checkout it runs from (`project`), never over the tailnet; with the loopback path on (SHAFT_WEB_LOCAL), its
/// port and its process's id (`localPort`, `localId`) to every caller.
public struct ServerVersion: Codable, Sendable, Equatable {
    public var server: String
    public var api: Int
    public var project: String?
    public var localPort: Int?          // `local_port` (SHAFTJSON converts snake case)
    /// `local_id`: what `GET <loopback>/app/v1/local` answers when it is this same server.
    public var localId: String?

    public init(server: String, api: Int, project: String? = nil, localPort: Int? = nil, localId: String? = nil) {
        self.server = server
        self.api = api
        self.project = project
        self.localPort = localPort
        self.localId = localId
    }
}

public struct ServicesResponse: Codable, Sendable, Equatable {
    public var services: [ServiceStatus]
    public var version: String?
}

public struct ServiceStatus: Codable, Sendable, Equatable {
    public var name: String
    public var running: Bool
    public var pid: Int?
    public var started: Date?
    public var commit: String?
    public var version: String?
    public var busy: String
    public var drain: ServiceDrain?
}

public struct ServiceDrain: Codable, Sendable, Equatable {
    public var state: String
    public var waiting: [String]
    public var deadline: Date?

    public var pending: Bool { state == "draining" }
}

public enum RestartOutcome: Sendable, Equatable {
    case restarted
    case waiting
    case failed(String)
    case unconfirmed
}
