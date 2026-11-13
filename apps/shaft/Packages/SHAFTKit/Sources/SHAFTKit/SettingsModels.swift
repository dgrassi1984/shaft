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
/// also the checkout it runs from (`project`), never over the tailnet.
public struct ServerVersion: Codable, Sendable, Equatable {
    public var server: String
    public var api: Int
    public var project: String?

    public init(server: String, api: Int, project: String? = nil) {
        self.server = server
        self.api = api
        self.project = project
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
