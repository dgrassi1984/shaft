import Foundation

public struct Meeting: Decodable, Sendable, Equatable, Identifiable {
    public let id: String
    public let title: String
    public let created: Date
    public let started: Date
    public let ended: Date?
    public let mode: String
    public let device: String
    public let account: String?
    public let state: String
    public let error: String?
    public let size: Int?
    public let sha256: String?
    public let ext: String
    public let channels: MeetingJSONValue?
    public let chunkSize: Int
    public let chunksReceived: [Int]
    public let processedWith: MeetingTranscript.Processing?
    public let workerCrashes: Int?
    public let transcript: MeetingTranscript?
}

public struct MeetingCreate: Encodable, Sendable, Equatable {
    public var title: String?
    public var mode: String
    public var started: Date
    public var ended: Date?
    public var device: String
    public var ext: String
    public var sidecar: [String: MeetingJSONValue]?
    public var account: String?

    public init(title: String? = nil, mode: String, started: Date, ended: Date? = nil,
                device: String, ext: String, sidecar: [String: MeetingJSONValue]? = nil, account: String? = nil) {
        self.title = title
        self.mode = mode
        self.started = started
        self.ended = ended
        self.device = device
        self.ext = ext
        self.sidecar = sidecar
        self.account = account
    }
}

public struct MeetingCreated: Decodable, Sendable, Equatable {
    public let id: String
    public let chunkSize: Int
    public let received: [Int]
}

public struct UploadStatus: Decodable, Sendable, Equatable {
    public let chunkSize: Int
    public let received: [Int]
}

public struct FinishRequest: Encodable, Sendable, Equatable {
    public let size: Int
    public let sha256: String
    public let chunks: Int

    public init(size: Int, sha256: String, chunks: Int) {
        self.size = size
        self.sha256 = sha256
        self.chunks = chunks
    }
}

public struct MeetingTranscript: Decodable, Sendable, Equatable {
    public let meeting: String
    public let segments: [Segment]
    public let gaps: MeetingJSONValue?
    public let speakers: [String: Speaker]
    public let processedWith: Processing?
    public let page: Int
    public let totalSegments: Int

    public struct Segment: Decodable, Sendable, Equatable {
        public let start: Double
        public let end: Double
        public let channel: Int
        public let speaker: String
        public let lang: String?
        public let text: String
        public let words: [Word]?
    }

    public struct Word: Decodable, Sendable, Equatable {
        public let w: String
        public let start: Double
        public let end: Double
    }

    public struct Speaker: Decodable, Sendable, Equatable {
        public let channel: Int
        public let seconds: Double
    }

    public struct Processing: Decodable, Sendable, Equatable {
        public let diarisation: String?
        public let asr: String?
        public let versions: [String: String]?
    }
}

/// Recorder metadata can contain arbitrary JSON, including keys that must keep their original spelling.
public enum MeetingJSONValue: Codable, Sendable, Equatable {
    case null
    case bool(Bool)
    case integer(Int64)
    case number(Double)
    case string(String)
    case array([MeetingJSONValue])
    case object([String: MeetingJSONValue])

    public init(from decoder: any Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let bool = try? value.decode(Bool.self) { self = .bool(bool) }
        else if let integer = try? value.decode(Int64.self) { self = .integer(integer) }
        else if let number = try? value.decode(Double.self) { self = .number(number) }
        else if let string = try? value.decode(String.self) { self = .string(string) }
        else if let array = try? value.decode([Self].self) { self = .array(array) }
        else { self = .object(try value.decode([String: Self].self)) }
    }

    public func encode(to encoder: any Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .null: try value.encodeNil()
        case .bool(let bool): try value.encode(bool)
        case .integer(let integer): try value.encode(integer)
        case .number(let number): try value.encode(number)
        case .string(let string): try value.encode(string)
        case .array(let array): try value.encode(array)
        case .object(let object): try value.encode(object)
        }
    }
}
