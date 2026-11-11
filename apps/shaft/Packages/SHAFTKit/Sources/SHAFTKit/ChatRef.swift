import Foundation

/// Which conversation of an instance a chat store follows: its feed, its draft, its unsent messages and its actions.
///
/// A server with topic threads keeps several open chats per instance, plus a From *instance* feed of what it sent from
/// its cycles; a server before threads keeps one, the current chat. Drafts and queued messages are keyed by the ref, so
/// they never cross threads.
public enum ChatRef: Hashable, Sendable {
    /// The shown chat (`GET /app/v1/chat/<n>`): a server before threads has no other; on one with threads it is the most
    /// recently active open thread.
    case current
    /// An open thread (`GET /app/v1/chat/<n>/<cid>`).
    case thread(String)
    /// The From *instance* feed (`GET /app/v1/from/<n>`): its own conversation and messages from cycles.
    case from
    /// An ended chat, read-only (§5).
    case earlier(String)

    /// The `cid` that names the From feed in `seen`, `away` and push events.
    public static let fromID = "from"

    /// The ref a server's `cid` names (a push event's, a link's): `"from"` the From feed, a chat id that thread; nil
    /// (none named) leaves the choice to the app.
    public init?(cid: String?) {
        guard let cid, !cid.isEmpty else { return nil }
        self = cid == Self.fromID ? .from : .thread(cid)
    }

    /// The chat id it names: a thread's or an earlier chat's; nil for the current chat and the From feed.
    public var cid: String? {
        switch self {
        case .thread(let cid), .earlier(let cid): cid
        case .current, .from: nil
        }
    }

    /// What `seen` and `away` send as `cid` (§7.5, §7.6): the thread's id or `"from"`; nil for the current chat, as a
    /// build before threads sent them.
    public var seenID: String? {
        switch self {
        case .thread(let cid): cid
        case .from: Self.fromID
        case .current, .earlier: nil
        }
    }

    /// The key its draft and its unsent messages are kept under: the thread's id, `"from"`, or nil for the current
    /// chat (where a build before threads kept them). An earlier chat has none: it is read-only.
    public var draftKey: String? { seenID }

    public var isEarlier: Bool {
        if case .earlier = self { return true }
        return false
    }

    public var isFrom: Bool { self == .from }
}
