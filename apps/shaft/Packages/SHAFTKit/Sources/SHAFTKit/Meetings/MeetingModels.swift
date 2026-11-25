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

    public func chatReference(timeZone: TimeZone = .current) -> String? {
        guard account != nil else { return nil }
        let date = DateFormatter()
        date.locale = Locale(identifier: "en_US_POSIX")
        date.timeZone = timeZone
        date.dateFormat = "d MMM yyyy HH:mm"
        let name = String(reflecting: title.isEmpty ? "Untitled meeting" : title)
        return "Meeting \(name) (\(id)), \(date.string(from: started)) — read it with meeting_read(\"\(id)\")"
    }
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
    public let recordingGaps: [Gap]
    public let speakers: [String: Speaker]
    public let processedWith: Processing?
    public let page: Int
    public let totalSegments: Int

    private enum CodingKeys: String, CodingKey {
        case meeting, segments, gaps, speakers, processedWith, page, totalSegments
    }

    public init(from decoder: any Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        meeting = try values.decode(String.self, forKey: .meeting)
        segments = try values.decode([Segment].self, forKey: .segments)
        gaps = try values.decodeIfPresent(MeetingJSONValue.self, forKey: .gaps)
        recordingGaps = try values.decodeIfPresent([Gap].self, forKey: .gaps) ?? []
        speakers = try values.decode([String: Speaker].self, forKey: .speakers)
        processedWith = try values.decodeIfPresent(Processing.self, forKey: .processedWith)
        page = try values.decode(Int.self, forKey: .page)
        totalSegments = try values.decode(Int.self, forKey: .totalSegments)
    }

    public struct Gap: Decodable, Sendable, Equatable {
        public let start: Double
        public let end: Double
        public let reason: String

        public init(start: Double, end: Double, reason: String) {
            self.start = start
            self.end = end
            self.reason = reason
        }
    }

    public struct Segment: Decodable, Sendable, Equatable {
        public let start: Double
        public let end: Double
        public let channel: Int
        public let speaker: String
        public let lang: String?
        public let text: String
        public let words: [Word]?

        public init(start: Double, end: Double, channel: Int, speaker: String, lang: String? = nil, text: String, words: [Word]? = nil) {
            self.start = start
            self.end = end
            self.channel = channel
            self.speaker = speaker
            self.lang = lang
            self.text = text
            self.words = words
        }
    }

    public struct Word: Decodable, Sendable, Equatable {
        public let w: String
        public let start: Double
        public let end: Double

        public init(w: String, start: Double, end: Double) {
            self.w = w
            self.start = start
            self.end = end
        }
    }

    public struct Speaker: Decodable, Sendable, Equatable {
        public let channel: Int
        public let seconds: Double
        public let person: String?
        public let name: String?
        public let clips: [[Double]]?

        public init(channel: Int, seconds: Double, person: String? = nil, name: String? = nil, clips: [[Double]]? = nil) {
            self.channel = channel
            self.seconds = seconds
            self.person = person
            self.name = name
            self.clips = clips
        }
    }

    public struct Processing: Decodable, Sendable, Equatable {
        public let diarisation: String?
        public let asr: String?
        public let versions: [String: String]?
    }
}

public struct SpeakerPerson: Decodable, Sendable, Equatable, Identifiable {
    public let id: String
    public let name: String
    public let created: Date
    /// Only the gallery list includes the voiceprint count.
    public let count: Int?
}

public struct MeetingSpeakerUpdate: Decodable, Sendable, Equatable {
    public let label: String
    public let speaker: MeetingTranscript.Speaker

    private enum CodingKeys: String, CodingKey { case label }

    public init(from decoder: any Decoder) throws {
        label = try decoder.container(keyedBy: CodingKeys.self).decode(String.self, forKey: .label)
        speaker = try MeetingTranscript.Speaker(from: decoder)
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
