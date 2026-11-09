import Foundation

/// One JSON data line of the reply stream (§6.1). Payload fields depend on `type` and may be null.
public struct ChatStreamEvent: Codable, Sendable, Equatable {
    public var seq: Int
    public var type: Kind
    public var reply: String
    public var text: String?
    public var answer: Bool?
    public var phase: String?
    public var detail: String?
    public var name: String?
    public var ok: Bool?
    public var failure: String?

    public enum Kind: String, Codable, Sendable, CaseIterable {
        case draft, status, answer, end
        case stepEnd = "step_end"
        case toolStart = "tool_start"
        case toolEnd = "tool_end"
    }

    private enum CodingKeys: String, CodingKey {
        case seq, type, reply, text, answer, phase, detail, name, ok, failure
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(seq, forKey: .seq)
        try container.encode(type, forKey: .type)
        try container.encode(reply, forKey: .reply)
        // Encode the payload for this type, including null values supplied by the server.
        switch type {
        case .draft, .answer:
            try container.encode(text, forKey: .text)
        case .stepEnd:
            try container.encode(answer, forKey: .answer)
        case .status:
            try container.encode(phase, forKey: .phase)
            try container.encode(detail, forKey: .detail)
        case .toolStart:
            try container.encode(name, forKey: .name)
        case .toolEnd:
            try container.encode(name, forKey: .name)
            try container.encode(ok, forKey: .ok)
        case .end:
            try container.encode(failure, forKey: .failure)
        }
    }
}

/// The instance's chosen voices, proposals and device inventories (§17).
public struct VoicesResponse: Codable, Sendable, Equatable {
    public var voices: [String: Voice?]
    public var proposals: [Proposal]
    public var available: [String: [AvailableVoice]]

    public struct Voice: Codable, Sendable, Equatable {
        public var identifier: String
        public var name: String
        public var by: String
        public var ts: Date
    }

    public struct Proposal: Codable, Sendable, Equatable {
        public var id: String
        public var language: String
        public var identifier: String
        public var name: String
        public var why: String
        public var ts: Date
        public var state: String
    }

    public struct AvailableVoice: Codable, Sendable, Equatable {
        public var identifier: String
        public var name: String
        public var language: String
        public var quality: String
    }
}
