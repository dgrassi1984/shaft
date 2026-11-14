import Foundation
import Observation

public struct ChoiceOption: Codable, Sendable, Equatable {
    public var label: String
    public var detail: String?
}

public struct Choices: Codable, Sendable, Equatable {
    public var options: [ChoiceOption]
    public var multiple: Bool
}

public struct ChoiceAnswer: Codable, Sendable, Equatable {
    public var picked: [Int]
    public var other: String?
    public var ts: Date

    public func summary(in choices: Choices) -> String {
        var parts = picked.compactMap { choices.options.indices.contains($0) ? choices.options[$0].label : nil }
        if let other, !other.isEmpty { parts.append("Other: \(other)") }
        return parts.joined(separator: "; ")
    }
}

public struct ChoiceAnswerRequest: Codable, Sendable, Equatable {
    public let cid: String
    public let file: String
    public let picked: [Int]
    public let other: String?
    public let mid: String

    public init(cid: String, file: String, picked: [Int], other: String?, mid: String = OutgoingMessage.newMid()) {
        self.cid = cid
        self.file = file
        self.picked = picked.sorted()
        self.other = other
        self.mid = mid
    }
}

public struct ChoiceAnswerResponse: Codable, Sendable, Equatable {
    public var said: String
    public var answer: ChoiceAnswer
}

/// A frozen submission per question. Retry reuses the entire request, including its idempotency id.
@MainActor
@Observable
public final class QuestionAnswers {
    public struct Pending: Sendable, Equatable {
        public let request: ChoiceAnswerRequest
        public var sending: Bool
        public var error: APIError?
        public var answer: ChoiceAnswer?
    }

    public private(set) var pending: [String: Pending] = [:]

    public init() {}

    nonisolated public static func valid(picked: [Int], other: String, choices: Choices) -> Bool {
        (!picked.isEmpty || !other.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            && (choices.multiple || picked.count <= 1)
            && picked.allSatisfy { choices.options.indices.contains($0) }
            && other.count <= 2000
    }

    public func send(file: String, cid: String, picked: [Int], other: String?,
                     instance: String, client: APIClient) async {
        guard pending[file] == nil else { return }
        let body = ChoiceAnswerRequest(cid: cid, file: file, picked: picked, other: other)
        pending[file] = Pending(request: body, sending: true)
        await attempt(file: file, instance: instance, client: client)
    }

    public func retry(file: String, instance: String, client: APIClient) async {
        guard let entry = pending[file], !entry.sending, entry.answer == nil else { return }
        pending[file]?.sending = true
        pending[file]?.error = nil
        await attempt(file: file, instance: instance, client: client)
    }

    private func attempt(file: String, instance: String, client: APIClient) async {
        guard let entry = pending[file] else { return }
        do {
            let response = try await client.answer(instance, entry.request)
            pending[file]?.answer = response.answer
        } catch {
            // An ambiguous cancellation can have reached the server, so preserve the request for manual Retry too.
            let failure = APIError.from(error) ?? .transport(code: NSURLErrorCancelled, message: "The answer send was interrupted.")
            pending[file]?.error = failure
            if case .server(409, let body) = failure, body.code == "answered" {
                pending[file]?.answer = body.answer
            }
        }
        pending[file]?.sending = false
    }
}

public extension FeedRows {
    /// Only the feed being shown counts; timestamps choose the oldest even if items arrive out of order.
    static func waitingQuestions(_ feed: Feed) -> [SentItem] {
        (feed.content?.items ?? []).compactMap { item in
            guard case .sent(let question) = item, question.kind == "question", question.open else { return nil }
            return question
        }.sorted { $0.ts < $1.ts }
    }
}
