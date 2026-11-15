import Foundation

// What the Apple Watch app and the iPhone app say to each other over WatchConnectivity. The watch has no network of
// its own (watchOS runs no VPN, so it can't reach the server on the tailnet) and holds no credential: it asks the
// iPhone, which talks to the server with its own pairing. This file is pure Foundation so the watch target compiles it
// as is (project.yml) while SHAFTKit's tests cover it. Every value is a property-list type, as WCSession requires.

/// A request from the watch, sent with `WCSession.sendMessage` and answered through its reply handler.
public enum WatchRequest: Equatable, Sendable {
    /// The instances to talk to.
    case instances
    /// Send `text` (dictated on the watch) to `instance` as a voice message. `mid` makes a retry answer as the first
    /// send did, never sending twice. The reply comes later as a `WatchReplyPayload`.
    case send(instance: String, text: String, mid: String)
    /// The reply still running in `instance`'s chat, else the last message it wrote there.
    case latest(instance: String)

    public var dictionary: [String: Any] {
        switch self {
        case .instances: ["op": "instances"]
        case .send(let instance, let text, let mid): ["op": "send", "instance": instance, "text": text, "mid": mid]
        case .latest(let instance): ["op": "latest", "instance": instance]
        }
    }

    public init?(_ dictionary: [String: Any]) {
        switch dictionary["op"] as? String {
        case "instances":
            self = .instances
        case "send":
            guard let instance = dictionary["instance"] as? String, let text = dictionary["text"] as? String,
                  let mid = dictionary["mid"] as? String, !instance.isEmpty, !mid.isEmpty else { return nil }
            self = .send(instance: instance, text: text, mid: mid)
        case "latest":
            guard let instance = dictionary["instance"] as? String, !instance.isEmpty else { return nil }
            self = .latest(instance: instance)
        default:
            return nil
        }
    }
}

/// The iPhone's immediate answer to a `WatchRequest`.
public struct WatchAnswer: Equatable, Sendable {
    /// For `.instances`: their names, in the app's order.
    public var instances: [String]
    /// For `.send`: the server's one-line confirmation.
    public var said: String?
    /// For `.latest`: the reply, when there is one.
    public var reply: WatchReplyPayload?
    /// Why the request failed (not paired, the server refused, no connection), in words for the watch's screen.
    public var error: String?

    public init(instances: [String] = [], said: String? = nil, reply: WatchReplyPayload? = nil, error: String? = nil) {
        self.instances = instances
        self.said = said
        self.reply = reply
        self.error = error
    }

    public static func failed(_ error: String) -> WatchAnswer { WatchAnswer(error: error) }

    public var dictionary: [String: Any] {
        var out: [String: Any] = ["instances": instances]
        if let said { out["said"] = said }
        if let reply { out["reply"] = reply.dictionary }
        if let error { out["error"] = error }
        return out
    }

    public init(_ dictionary: [String: Any]) {
        instances = dictionary["instances"] as? [String] ?? []
        said = dictionary["said"] as? String
        reply = (dictionary["reply"] as? [String: Any]).flatMap(WatchReplyPayload.init)
        error = dictionary["error"] as? String
    }
}

/// An instance's reply, as the watch shows and speaks it. Pushed by the iPhone after a `.send` (with the send's
/// `mid`), or returned by `.latest`.
public struct WatchReplyPayload: Equatable, Sendable {
    public var instance: String
    /// The `mid` of the watch's send this answers; nil for `.latest`.
    public var mid: String?
    /// The reply as the chat keeps it (Markdown), for the screen.
    public var text: String
    /// What to speak, sentence by sentence: the markup already turned into speech (`SpeechChunker`).
    public var spoken: [String]
    /// The language of the reply (BCP 47, e.g. `en-US`) and the instance's voice for it, when it has one.
    public var language: String
    public var voice: String?
    /// The reply failed or was cut short: why, in words.
    public var failure: String?

    public init(instance: String, mid: String?, text: String, spoken: [String], language: String, voice: String?,
                failure: String? = nil) {
        self.instance = instance
        self.mid = mid
        self.text = text
        self.spoken = spoken
        self.language = language
        self.voice = voice
        self.failure = failure
    }

    /// `text` as the watch draws it: its Markdown's emphasis, links and code rendered, its block markup reduced to plain
    /// lines (a heading's `#`, a list's `-` as •, code fences and table rules dropped). A watch has no room for blocks.
    public var screenText: AttributedString {
        let lines = text.components(separatedBy: "\n").compactMap { line -> String? in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") { return nil }
            if trimmed.hasPrefix("|"), trimmed.allSatisfy({ "|-: ".contains($0) }) { return nil }
            if let heading = trimmed.firstRange(of: /^#{1,6}\s+/) { return String(trimmed[heading.upperBound...]) }
            if let bullet = trimmed.firstRange(of: /^[-*+]\s+/) { return "• " + trimmed[bullet.upperBound...] }
            if trimmed.hasPrefix(">") { return String(trimmed.dropFirst()).trimmingCharacters(in: .whitespaces) }
            return line
        }
        let plain = lines.joined(separator: "\n")
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: plain, options: options)) ?? AttributedString(plain)
    }

    /// Marks a pushed payload (`WCSession.sendMessage` / `transferUserInfo`) apart from other user info.
    public static let kind = "shaft.watch.reply"

    public var dictionary: [String: Any] {
        var out: [String: Any] = ["kind": Self.kind, "instance": instance, "text": text, "spoken": spoken,
                                  "language": language]
        if let mid { out["mid"] = mid }
        if let voice { out["voice"] = voice }
        if let failure { out["failure"] = failure }
        return out
    }

    public init?(_ dictionary: [String: Any]) {
        guard dictionary["kind"] as? String == Self.kind, let instance = dictionary["instance"] as? String,
              let text = dictionary["text"] as? String, let language = dictionary["language"] as? String
        else { return nil }
        self.instance = instance
        mid = dictionary["mid"] as? String
        self.text = text
        spoken = dictionary["spoken"] as? [String] ?? []
        self.language = language
        voice = dictionary["voice"] as? String
        failure = dictionary["failure"] as? String
    }
}
