import Foundation

/// Everything `APIClient` throws. `message` is what to put on the screen; `server` carries the server's own words.
public enum APIError: Error, Sendable, Equatable, LocalizedError {
    /// The server answered with its JSON error (§1.5).
    case server(status: Int, error: ServerError)
    /// An error status without the JSON error body: something in front of the web service answered (a
    /// `tailscale serve` page, a proxy). `body` is the start of what came back.
    case http(status: Int, body: String)
    /// The request did not get an answer: no network, the server down, a timeout.
    case transport(code: Int, message: String)
    /// A 2xx answer the app could not read: the server and the app disagree on the contract.
    case decoding(path: String, message: String)
    /// No pairing is stored: the app shows its pairing screen.
    case notPaired
    /// A pairing link the app could not read.
    case badLink(String)

    public var status: Int? {
        switch self {
        case .server(let status, _), .http(let status, _): status
        default: nil
        }
    }

    /// The server's `code` (§1.5), nil when it did not send one.
    public var code: String? {
        if case .server(_, let error) = self { return error.code }
        return nil
    }

    /// The device is not (or no longer) paired: `401 unpaired`, or nothing stored.
    public var isUnpaired: Bool {
        self == .notPaired || code == "unpaired"
    }

    /// Worth asking again after a wait: the network, the server or something in front of it failing. A refusal
    /// (4xx) is not: asking again gets the same answer.
    public var isRetryable: Bool {
        switch self {
        case .transport: true
        case .server(let status, _), .http(let status, _): status >= 500 || status == 429
        case .decoding, .notPaired, .badLink: false
        }
    }

    public var message: String {
        switch self {
        case .server(_, let error): Self.spoken(error.message)
        case .http(let status, let body):
            if body.trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix("<") {
                "The server answered HTTP \(status) with a web page, not the app API. It may not have /app/v1 yet: update its SHAFT web service."
            } else {
                body.isEmpty ? "The server answered HTTP \(status)." : "The server answered HTTP \(status): \(body)"
            }
        case .transport(_, let message): message
        case .decoding(let path, let message): "Could not read the answer to \(path): \(message)"
        case .notPaired: "This device is not paired. Paste a pairing link from `shaft web pair`."
        case .badLink(let why): why
        }
    }

    public var errorDescription: String? { message }

    /// The server's own words as a sentence on screen: the CLI verbs behind a refusal start with "refused: ", which the
    /// screen around it already says.
    static func spoken(_ words: String) -> String {
        guard words.hasPrefix("refused: ") else { return words }
        let rest = words.dropFirst("refused: ".count)
        return rest.last?.isPunctuation == true ? String(rest) : rest + "."
    }

    /// Any error as an `APIError` (a cancelled or foreign one as `.transport`).
    public static func from(_ error: any Error) -> APIError {
        if let error = error as? APIError { return error }
        if let error = error as? URLError {
            return .transport(code: error.errorCode, message: error.localizedDescription)
        }
        let error = error as NSError
        return .transport(code: error.code, message: error.localizedDescription)
    }
}
