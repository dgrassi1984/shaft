import Foundation

// The /app/v1 models, field for field as docs/app-api.md lists them. A field the contract says is always present is
// non-optional, so a server that drops one fails to decode (the fixture tests catch it); `T?` is a field that can be
// null. The one family of absent fields is a partial feed's († in §4.2): `Feed.content` is nil then.

// MARK: - Pairing (§2)

public struct PairRequest: Codable, Sendable, Equatable {
    public var link: String?
    public var token: String?
    public var platform: String

    public init(link: String? = nil, token: String? = nil, platform: String) {
        self.link = link
        self.token = token
        self.platform = platform
    }
}

public struct PairResponse: Codable, Sendable, Equatable {
    public var device: String
    public var secret: String
    public var name: String
    public var login: String
}

// MARK: - The chat list (§3)

public struct InstancesResponse: Codable, Sendable, Equatable {
    public var instances: [InstanceRow]
    public var badge: Int
    public var instancesRev: OrderRevision? = nil
}

public struct InstanceRow: Codable, Sendable, Equatable, Identifiable {
    public var name: String
    public var state: String
    public var since: String
    public var busyNote: String?
    public var model: String
    public var usdToday: Double
    public var line: String
    public var last: Date?
    public var replying: Bool
    public var unread: Bool
    public var nextCycle: String
    public var voiceMode: String
    /// Its open threads (§3, topic threads); nil from a server before threads, which has one chat per instance.
    public var threads: Int?
    public var threadsRev: OrderRevision? = nil
    public var unreadMain: Bool? = nil
    public var unreadThreads: Int? = nil

    public var id: String { name }
}

// MARK: - Threads (§3.1, `GET /app/v1/threads/<n>`)

/// An instance's open threads and its From feed. A server before threads answers the request `404`: the app then keeps
/// one chat per instance (`InstancesStore.ThreadsState.unsupported`).
public struct ThreadsResponse: Codable, Sendable, Equatable {
    public var threads: [ThreadRow]
    public var from: FromRow
    public var threadsRev: OrderRevision? = nil

    public init(threads: [ThreadRow], from: FromRow) {
        self.threads = threads
        self.from = from
    }

    /// The most recently active thread, used only to adopt historical untargeted data.
    public var newest: ThreadRow? {
        threads.max { ($0.last ?? $0.created) < ($1.last ?? $1.created) }
    }
}

/// One open thread: its own conversation on its own topic.
public struct ThreadRow: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var title: String
    public var created: Date
    /// When it last moved; nil when nothing was said in it yet.
    public var last: Date?
    /// Its last line, as the rail's (`"you: …"` for the operator's own).
    public var line: String
    /// `"idle"` or `"replying"`.
    public var state: String
    /// While its reply waits for the instance: what it waits on (`waiting: answering in «…»`); nil otherwise.
    public var waiting: String?
    public var unread: Bool
    public var failed: Bool? = nil

    public init(id: String, title: String, created: Date, last: Date?, line: String, state: String, waiting: String?,
                unread: Bool) {
        self.id = id
        self.title = title
        self.created = created
        self.last = last
        self.line = line
        self.state = state
        self.waiting = waiting
        self.unread = unread
    }

    public var replying: Bool { state == "replying" }

    private enum CodingKeys: String, CodingKey { case id, title, created, last, line, state, waiting, unread, failed }

    /// `last` and `waiting` are null-able; a server that leaves them out means null too.
    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        title = try container.decode(String.self, forKey: .title)
        created = try container.decode(Date.self, forKey: .created)
        last = try container.decodeIfPresent(Date.self, forKey: .last)
        line = try container.decode(String.self, forKey: .line)
        state = try container.decode(String.self, forKey: .state)
        waiting = try container.decodeIfPresent(String.self, forKey: .waiting)
        unread = try container.decode(Bool.self, forKey: .unread)
        failed = try container.decodeIfPresent(Bool.self, forKey: .failed)
    }
}

/// The From *instance* feed's row: what it sent from its cycles.
public struct FromRow: Codable, Sendable, Equatable {
    public var line: String
    public var last: Date?
    public var unread: Bool
    public var state: String? = nil
    public var waiting: String? = nil
    public var failed: Bool? = nil

    public init(line: String, last: Date?, unread: Bool) {
        self.line = line
        self.last = last
        self.unread = unread
    }

    private enum CodingKeys: String, CodingKey { case line, last, unread, state, waiting, failed }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        line = try container.decode(String.self, forKey: .line)
        last = try container.decodeIfPresent(Date.self, forKey: .last)
        unread = try container.decode(Bool.self, forKey: .unread)
        state = try container.decodeIfPresent(String.self, forKey: .state)
        failed = try container.decodeIfPresent(Bool.self, forKey: .failed)
        waiting = try container.decodeIfPresent(String.self, forKey: .waiting)
    }
}

/// `POST /app/v1/chat/<n>/new` (§7.7): the thread made.
public struct NewThreadResponse: Codable, Sendable, Equatable {
    public struct Chat: Codable, Sendable, Equatable {
        public var id: String
        public var title: String
    }

    public var chat: Chat
    public var said: String
}

// MARK: - The feed (§4)

/// A feed response (§4.2). `content` holds the fields marked †: nil in a partial feed, where only `version`, `live`
/// and `reply` moved. `merging(_:)` lays a partial over the full feed the app holds.
public struct Feed: Codable, Sendable, Equatable {
    public var instance: String
    public var partial: Bool
    public var version: String
    public var live: String
    public var reply: Reply?
    public var content: FeedContent?

    public init(instance: String, partial: Bool, version: String, live: String, reply: Reply?, content: FeedContent?) {
        self.instance = instance
        self.partial = partial
        self.version = version
        self.live = live
        self.reply = reply
        self.content = content
    }

    private enum CodingKeys: String, CodingKey {
        case instance, partial, version, live, reply
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        instance = try container.decode(String.self, forKey: .instance)
        partial = try container.decode(Bool.self, forKey: .partial)
        version = try container.decode(String.self, forKey: .version)
        live = try container.decode(String.self, forKey: .live)
        reply = try container.decode(Reply?.self, forKey: .reply)
        content = partial ? nil : try FeedContent(from: decoder)
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(instance, forKey: .instance)
        try container.encode(partial, forKey: .partial)
        try container.encode(version, forKey: .version)
        try container.encode(live, forKey: .live)
        try container.encode(reply, forKey: .reply)
        try content?.encode(to: encoder)
    }

    /// This full feed with `newer` laid over it: a full `newer` replaces it, a partial one moves `version`, `live`
    /// and `reply` and keeps the rest (§4.2). The result is always full when `self` is.
    public func merging(_ newer: Feed) -> Feed {
        guard newer.partial else { return newer }
        var merged = self
        merged.version = newer.version
        merged.live = newer.live
        merged.reply = newer.reply
        return merged
    }
}

/// The fields of a full feed that a partial one leaves out († in §4.2).
public struct FeedContent: Codable, Sendable, Equatable {
    public var chat: ChatInfo?
    public var archived: Bool
    public var earlier: Int
    public var items: [FeedItem]
    public var queued: [QueuedMessage]
    public var pendingFeedback: [PendingFeedback]
    public var pendingEdits: [String]
    public var mids: [String]
    public var last: Date?
    public var canStop: Bool
    public var canEnd: Bool
    public var canClear: Bool

    private enum CodingKeys: String, CodingKey {
        case chat, archived, earlier, items, queued, pendingFeedback, pendingEdits, mids, last, canStop, canEnd, canClear
    }

    public init(from decoder: any Decoder) throws {
        // `chat` and `last` are null-able but never absent in a full feed: decode them as present.
        let container = try decoder.container(keyedBy: CodingKeys.self)
        chat = try container.decode(ChatInfo?.self, forKey: .chat)
        archived = try container.decode(Bool.self, forKey: .archived)
        earlier = try container.decode(Int.self, forKey: .earlier)
        items = try container.decode([FeedItem].self, forKey: .items)
        queued = try container.decode([QueuedMessage].self, forKey: .queued)
        pendingFeedback = try container.decode([PendingFeedback].self, forKey: .pendingFeedback)
        pendingEdits = try container.decode([String].self, forKey: .pendingEdits)
        mids = try container.decode([String].self, forKey: .mids)
        last = try container.decode(Date?.self, forKey: .last)
        canStop = try container.decode(Bool.self, forKey: .canStop)
        canEnd = try container.decode(Bool.self, forKey: .canEnd)
        canClear = try container.decode(Bool.self, forKey: .canClear)
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(chat, forKey: .chat)
        try container.encode(archived, forKey: .archived)
        try container.encode(earlier, forKey: .earlier)
        try container.encode(items, forKey: .items)
        try container.encode(queued, forKey: .queued)
        try container.encode(pendingFeedback, forKey: .pendingFeedback)
        try container.encode(pendingEdits, forKey: .pendingEdits)
        try container.encode(mids, forKey: .mids)
        try container.encode(last, forKey: .last)
        try container.encode(canStop, forKey: .canStop)
        try container.encode(canEnd, forKey: .canEnd)
        try container.encode(canClear, forKey: .canClear)
    }
}

public struct ChatInfo: Codable, Sendable, Equatable {
    public var id: String
    /// Its title (§4.2): explicit, else the first line of the operator's first message, else "New thread". Nil from a
    /// server before threads.
    public var title: String?
    /// `"idle"`, `"replying"` or `"ended"`.
    public var state: String
    public var created: Date
    public var ended: Date?
}

/// The reply running now (§4.6). `activity` and `answer` move `live`, `started` moves `version`.
public struct Reply: Codable, Sendable, Equatable {
    public var started: Date
    public var activity: [String]
    public var answer: String
}

public struct QueuedMessage: Codable, Sendable, Equatable, Identifiable {
    public var key: String
    public var mid: String
    public var text: String
    public var attachments: [QueuedAttachment]

    public var id: String { key }
}

/// A queued message's file: not stored yet, so no URL (§4.7).
public struct QueuedAttachment: Codable, Sendable, Equatable {
    public var name: String
    public var bytes: Int
}

public struct PendingFeedback: Codable, Sendable, Equatable, Identifiable {
    public var key: String
    public var id: String
    public var replyTs: Date
}

// MARK: Items (§4.4)

/// One item of the conversation, by its `type`. A type this build does not know decodes as `.unknown`, so a newer
/// server never breaks the whole feed; the fixture tests refuse it.
public enum FeedItem: Codable, Sendable, Equatable, Identifiable {
    case user(UserItem)
    case assistant(AssistantItem)
    case retried(RetriedItem)
    case sent(SentItem)
    case later(LaterItem)
    case ended(EndedItem)
    case unknown(key: String, type: String)

    public var key: String {
        switch self {
        case .user(let item): item.key
        case .assistant(let item): item.key
        case .retried(let item): item.key
        case .sent(let item): item.key
        case .later(let item): item.key
        case .ended(let item): item.key
        case .unknown(let key, _): key
        }
    }

    public var id: String { key }

    public var ts: Date? {
        switch self {
        case .user(let item): item.ts
        case .assistant(let item): item.ts
        case .retried(let item): item.ts
        case .sent(let item): item.ts
        case .later(let item): item.ts
        case .ended(let item): item.ts
        case .unknown: nil
        }
    }

    private enum CodingKeys: String, CodingKey { case key, type }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let type = try container.decode(String.self, forKey: .type)
        switch type {
        case "user": self = .user(try UserItem(from: decoder))
        case "assistant": self = .assistant(try AssistantItem(from: decoder))
        case "retried": self = .retried(try RetriedItem(from: decoder))
        case "sent": self = .sent(try SentItem(from: decoder))
        case "later": self = .later(try LaterItem(from: decoder))
        case "ended": self = .ended(try EndedItem(from: decoder))
        default: self = .unknown(key: try container.decode(String.self, forKey: .key), type: type)
        }
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .user(let item):
            try container.encode("user", forKey: .type)
            try item.encode(to: encoder)
        case .assistant(let item):
            try container.encode("assistant", forKey: .type)
            try item.encode(to: encoder)
        case .retried(let item):
            try container.encode("retried", forKey: .type)
            try item.encode(to: encoder)
        case .sent(let item):
            try container.encode("sent", forKey: .type)
            try item.encode(to: encoder)
        case .later(let item):
            try container.encode("later", forKey: .type)
            try item.encode(to: encoder)
        case .ended(let item):
            try container.encode("ended", forKey: .type)
            try item.encode(to: encoder)
        case .unknown(let key, let type):
            try container.encode(type, forKey: .type)
            try container.encode(key, forKey: .key)
        }
    }
}

/// A message from the operator (key `u<k>`). `text` is not Markdown (§4.4): `> ` lines are a quote, ``` fences code.
public struct UserItem: Codable, Sendable, Equatable {
    public var key: String
    public var k: Int
    public var ts: Date
    public var text: String
    public var mid: String?
    public var interjected: Bool
    public var via: String?
    public var attachments: [Attachment]
    public var edits: [String]
    public var deleted: [String]
    public var comments: [CommentRef]
    public var feedback: [String]
}

/// The instance's answer (key `a<k>`). `content` is GFM Markdown.
public struct AssistantItem: Codable, Sendable, Equatable {
    public var key: String
    public var k: Int
    public var ts: Date
    public var content: String
    public var activity: [String]
    public var incomplete: Bool
    public var failure: Failure?
    public var docs: [DocChange]
    public var answered: [CommentRef]
    public var opened: [CommentRef]
    public var rating: Rating?
    public var last: Bool
}

/// A try that failed and was sent again by Retry (key `a<k>`).
public struct RetriedItem: Codable, Sendable, Equatable {
    public var key: String
    public var k: Int
    public var ts: Date
}

/// A message or question the instance sent the operator (key `s<file>`).
public struct SentItem: Codable, Sendable, Equatable {
    public var key: String
    public var file: String?
    public var ts: Date
    /// `"message"` or `"question"`.
    public var kind: String
    /// Written later, in a cycle, for work queued in this thread: part of the thread ("⏾ from its cycle").
    public var fromCycle: Bool
    /// In the From feed: the ended thread it answers, shown as a link to that earlier chat.
    public var re: ThreadLink?
    public var read: Bool
    public var text: String
    public var attachments: [Attachment]
    public var rating: Rating?

    private enum CodingKeys: String, CodingKey {
        case key, file, ts, kind, fromCycle, re, read, text, attachments, rating
    }

    /// A server before thread replies sends neither `from_cycle` nor `re`: not from a cycle, answering no thread.
    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        key = try container.decode(String.self, forKey: .key)
        file = try container.decodeIfPresent(String.self, forKey: .file)
        ts = try container.decode(Date.self, forKey: .ts)
        kind = try container.decode(String.self, forKey: .kind)
        fromCycle = try container.decodeIfPresent(Bool.self, forKey: .fromCycle) ?? false
        re = try container.decodeIfPresent(ThreadLink.self, forKey: .re)
        read = try container.decode(Bool.self, forKey: .read)
        text = try container.decode(String.self, forKey: .text)
        attachments = try container.decode([Attachment].self, forKey: .attachments)
        rating = try container.decodeIfPresent(Rating.self, forKey: .rating)
    }
}

/// A thread a message names (`re`): its id and its title.
public struct ThreadLink: Codable, Sendable, Equatable {
    public var cid: String
    public var title: String
}

/// A message the operator left for the next cycle (key `b<file>`).
public struct LaterItem: Codable, Sendable, Equatable {
    public var key: String
    public var file: String
    public var ts: Date
    public var text: String
    public var mid: String?
    public var cancellable: Bool?
    public var cancelled: Date?
    public var read: Bool
    public var attachments: [Attachment]
    public var comments: [CommentRef]
}

/// The chat ended (key `ended`).
public struct EndedItem: Codable, Sendable, Equatable {
    public var key: String
    public var ts: Date
}

/// A file with a message (§4.4). `url` is relative to the base URL: `APIClient.download(_:)` takes it as it is.
public struct Attachment: Codable, Sendable, Equatable {
    public var name: String
    public var bytes: Int
    public var image: Bool
    public var text: Bool
    public var url: String
}

/// A comment thread on a document: `{id, doc}`.
public struct CommentRef: Codable, Sendable, Equatable {
    public var id: String
    public var doc: String
}

/// A document an answer created (`created` true) or changed.
public struct DocChange: Codable, Sendable, Equatable {
    public var path: String
    public var created: Bool
}

/// Why a reply failed and how to recover (§4.4).
public struct Failure: Codable, Sendable, Equatable {
    /// `"stopped"`, `"connection"`, `"service"`, `"model"` or `"unknown"`.
    public var kind: String
    public var why: String
    public var action: String?
    public var detail: String
    public var retry: FailureRetry?
    public var links: [FailureLink]
}

public struct FailureRetry: Codable, Sendable, Equatable {
    public var text: String
    /// Send with `retry_of` (§6) so the failed try folds into one line; nil: a new message.
    public var retryOf: Int?
    /// Ask this first; nil: one tap.
    public var confirm: String?
}

/// A web UI path to check before retrying (`/i/<n>/activity`, `/services`), opened in the browser at the base URL.
public struct FailureLink: Codable, Sendable, Equatable {
    public var label: String
    public var url: String
}

/// The operator's thumb on an answer or a sent message (§4.5).
public struct Rating: Codable, Sendable, Equatable {
    public var id: String
    /// `"up"` or `"down"`.
    public var thumb: String
    public var reason: String
    public var sent: RatingSent?
    public var pending: Bool
}

public struct RatingSent: Codable, Sendable, Equatable {
    public var ts: Date
    /// `"chat"` or `"inbox"`.
    public var via: String
    public var message: String?
}

// MARK: - Earlier chats (§5)

public struct ChatsResponse: Codable, Sendable, Equatable {
    public var chats: [ChatSummary]
}

public struct ChatSummary: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var created: Date
    public var ended: Date
    public var excerpt: String
    public var messages: Int
}

// MARK: - Sending and actions (§6, §7)

/// `mode` of a send (§6).
public enum SendMode: String, Codable, Sendable, CaseIterable {
    /// A new reply, or an interjection while one runs ("Send now").
    case now
    /// After the running reply ("Queue after reply"); sent now when idle.
    case queue
    /// To the inbox for the next cycle ("Next cycle").
    case background
}

/// A ↩ Reply card: the quoted text, its own answer, and the document it came from (`""` for the chat).
public struct Quote: Codable, Sendable, Equatable, Hashable {
    public var inReplyTo: String?
    public var quote: String
    public var answer: String
    public var doc: String

    public init(quote: String, answer: String = "", doc: String = "") {
        self.quote = quote
        self.answer = answer
        self.doc = doc
    }
}

public struct SendResponse: Codable, Sendable, Equatable {
    public var said: String
    public var chat: String?
    public var file: String?
}

/// `{"said": str}`: most actions' answer.
public struct SaidResponse: Codable, Sendable, Equatable {
    public var said: String
}

public struct SeenResponse: Codable, Sendable, Equatable {
    public var unread: Bool
    public var badge: Int
}

public struct OkResponse: Codable, Sendable, Equatable {
    public var ok: Bool
}

// MARK: - Feedback (§9)

public struct FeedbackRequest: Codable, Sendable, Equatable {
    /// `rate`, `clear`, `send` or `unpend`.
    public var op: String
    public var kind: String?
    public var cid: String?
    public var k: Int?
    public var file: String?
    public var thumb: String?
    public var reason: String?
    public var id: String?

    public init(op: String, kind: String? = nil, cid: String? = nil, k: Int? = nil, file: String? = nil,
                thumb: String? = nil, reason: String? = nil, id: String? = nil) {
        self.op = op
        self.kind = kind
        self.cid = cid
        self.k = k
        self.file = file
        self.thumb = thumb
        self.reason = reason
        self.id = id
    }

    public static func rateChat(cid: String, k: Int, thumb: String, reason: String? = nil) -> FeedbackRequest {
        FeedbackRequest(op: "rate", kind: "chat", cid: cid, k: k, thumb: thumb, reason: reason)
    }

    public static func rateMessage(file: String, thumb: String, reason: String? = nil) -> FeedbackRequest {
        FeedbackRequest(op: "rate", kind: "message", file: file, thumb: thumb, reason: reason)
    }
}

public struct FeedbackResponse: Codable, Sendable, Equatable {
    public var said: String
    public var id: String?
}

// MARK: - Documents and comments (§10)

public struct DocsList: Codable, Sendable, Equatable {
    public var dir: String
    public var maxBytes: Int
    public var docs: [DocEntry]
}

public struct DocEntry: Codable, Sendable, Equatable, Identifiable {
    public var path: String
    public var bytes: Int
    public var mtime: Date
    /// SHA-256 hex, `"too-big"` when `tooBig`.
    public var sha: String
    public var tooBig: Bool
    public var openComments: Int
    public var inChat: Bool

    public var id: String { path }
}

public struct DocumentDetail: Codable, Sendable, Equatable {
    public var path: String
    public var text: String
    public var sha: String
    public var inChat: Bool
    public var threads: [CommentThread]
    public var overwritten: Overwritten?
}

/// Lines the operator wrote that a later write of the instance dropped (§10.2).
public struct Overwritten: Codable, Sendable, Equatable {
    public var lines: Int
    public var ts: Date
    public var diff: String
    public var text: String
}

public struct CommentThread: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var doc: String
    public var by: String
    /// `"open"` or `"resolved"`.
    public var state: String
    public var created: Date
    public var anchor: CommentAnchor?
    public var docChanged: Bool
    public var posts: [CommentPost]
    public var unsectioned: [String]
}

/// Where a comment sits, in the document's rendered text (UTF-16 offsets, §10.2).
public struct CommentAnchor: Codable, Sendable, Equatable {
    public var exact: String
    public var prefix: String
    public var suffix: String
    public var start: Int

    public init(exact: String, prefix: String, suffix: String, start: Int) {
        self.exact = exact
        self.prefix = prefix
        self.suffix = suffix
        self.start = start
    }
}

public struct CommentPost: Codable, Sendable, Equatable, Identifiable {
    public var pid: String
    public var by: String
    public var text: String
    public var ts: Date
    public var unsent: Bool
    public var sent: String?
    public var turn: String?
    public var seen: String?

    public var id: String { pid }
}

public struct DocSaveResponse: Codable, Sendable, Equatable {
    public var said: String
    public var path: String
    public var sha: String
}

public struct CommentResponse: Codable, Sendable, Equatable {
    public var said: String
    public var thread: String
}

// MARK: - Push (§11)

public struct PushRegisterRequest: Codable, Sendable, Equatable {
    public var token: String
    public var env: String
    public var topic: String
    public var platform: String
}

public struct PushRegisterResponse: Codable, Sendable, Equatable {
    public var push: PushState
}

public struct PushState: Codable, Sendable, Equatable {
    public var on: Bool
    public var env: String?
    public var reason: String
}

public struct PushStatus: Codable, Sendable, Equatable {
    public var on: Bool
    public var env: String?
    public var reason: String
    public var problem: String?

    /// What Settings says about push beside its Push row, each thing once and nothing the row says already: with the
    /// server out of reach the status and the registration fail in the same words.
    public static func notes(_ status: PushStatus?, loadProblem: String?, registered: String?) -> [String] {
        guard let status else {
            return [loadProblem, registered].compactMap { $0?.isEmpty == false ? $0 : nil }.uniqued()
        }
        guard !status.on else { return [] }
        return [status.reason, registered].compactMap { $0?.isEmpty == false ? $0 : nil }.uniqued()
    }
}

private extension Array where Element == String {
    func uniqued() -> [String] {
        var seen = Set<String>()
        return filter { seen.insert($0).inserted }
    }
}

public struct RemovedResponse: Codable, Sendable, Equatable {
    public var removed: Bool
}

public struct PushLatest: Codable, Sendable, Equatable {
    public var events: [PushEvent]
    public var badge: Int
}

public struct PushEvent: Codable, Sendable, Equatable {
    public var title: String
    public var body: String
    public var url: String
    public var tag: String
    public var instance: String?
    /// The thread it is about (a chat id), `"from"` for the From feed, nil for the instance's current chat or from a
    /// server before threads.
    public var cid: String?
}

// MARK: - Errors (§1.5)

public struct ErrorBody: Codable, Sendable, Equatable {
    public var error: ServerError
}

/// The server's error: `code` for the app, `message` for the screen. `sha`/`diff` come with `stale`, `version` with
/// `stale_version`.
public struct ServerError: Codable, Sendable, Equatable {
    public var code: String
    public var message: String
    public var sha: String?
    public var diff: String?
    public var version: String?
    public var settings: InstanceSettings? = nil

    public init(code: String, message: String, sha: String? = nil, diff: String? = nil, version: String? = nil) {
        self.code = code
        self.message = message
        self.sha = sha
        self.diff = diff
        self.version = version
    }
}

// MARK: - Cycles (§16)

public struct CyclePlan: Codable, Sendable, Equatable {
    public var now: Now
    public var check: Check
    public var nextStep: NextStep?
    public var unfinished: [String]
    public var commitments: [Commitment]
    public var commitmentsError: String
    public var queue: Queue
    public var goals: String
    public var goalsError: String
    public var improvement: Improvement?
    public var improvementError: String

    public struct Now: Codable, Sendable, Equatable {
        public var state: String?
        public var since: String?
        public var goal: String?
    }

    public struct Check: Codable, Sendable, Equatable {
        public var next: String
        public var skipped: String
        public var paused: Bool
    }

    public struct NextStep: Codable, Sendable, Equatable {
        public var at: Date?
        public var action: String
        public var reviseIf: String?
    }

    public struct Commitment: Codable, Sendable, Equatable {
        public var id: String
        public var promise: String
        public var due: String?
        public var status: String
    }

    public struct Queue: Codable, Sendable, Equatable {
        public var state: String
        public var open: Int
        public var overdue: Int
        public var error: String
        public var rows: [Row]

        public struct Row: Codable, Sendable, Equatable {
            public var id: Int
            public var task: String
            public var dueAt: Date?
            public var dueAtIsDate: Bool
            public var dueAtText: String?
            public var overdue: Bool

            private enum CodingKeys: String, CodingKey { case id, task, dueAt, dueAtText, overdue }

            public init(from decoder: any Decoder) throws {
                let container = try decoder.container(keyedBy: CodingKeys.self)
                id = try container.decode(Int.self, forKey: .id)
                task = try container.decode(String.self, forKey: .task)
                dueAtText = try container.decodeIfPresent(String.self, forKey: .dueAtText)
                overdue = try container.decode(Bool.self, forKey: .overdue)
                if let text = try container.decodeIfPresent(String.self, forKey: .dueAt) {
                    dueAtIsDate = text.range(of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression) != nil
                    // A plain due date has no time zone. Represent it at midnight UTC, never in the device's zone.
                    let stamp = dueAtIsDate ? text + "T00:00:00Z" : text
                    guard let date = SHAFTJSON.parseStamp(stamp) else {
                        throw DecodingError.dataCorruptedError(
                            forKey: .dueAt, in: container, debugDescription: "Invalid queue due date: \(text)")
                    }
                    dueAt = date
                } else {
                    dueAt = nil
                    dueAtIsDate = false
                }
            }
        }
    }

    public struct Improvement: Codable, Sendable, Equatable {
        public var at: Date?
        public var text: String
    }
}

public struct CycleDaysResponse: Codable, Sendable, Equatable {
    public var days: [CycleDay]
    public var next: String?
    public var judgeError: String
}

public struct CycleDay: Codable, Sendable, Equatable {
    public var date: String
    public var skippedLines: Int
    public var counts: Counts
    public var rows: [CycleRow]

    public struct Counts: Codable, Sendable, Equatable {
        public var cycles: Int
        public var yes: Int
        public var partial: Int
        public var no: Int
        public var none: Int
        public var judged: Int
        public var forYou: Int
    }
}

public struct CycleRow: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var at: Date?
    public var kind: String
    public var goal: String?
    public var claim: String?
    public var verdict: String?
    public var delivered: Bool
    public var origin: String?
    public var endedBy: String?
}

public struct CycleDetail: Codable, Sendable, Equatable {
    public var id: String
    public var at: Date?
    public var kind: String
    public var content: String?
    public var planned: Planned?
    public var did: Did
    public var result: Result?
    public var left: Left?

    public struct Planned: Codable, Sendable, Equatable {
        public var goal: String?
        public var successCriterion: String?
        public var origin: String?
        public var originRef: String?
        public var confidence: Double?
        public var predictions: [String]
        public var attention: [String]
    }

    public struct Did: Codable, Sendable, Equatable {
        public var whatHappened: String?
        public var durationS: Int?
        public var usd: Double?
        public var toolCalls: Int
        public var endedBy: String?
        public var wrappedUp: String?
        public var deliveredVia: String?
    }

    public struct Result: Codable, Sendable, Equatable {
        public var claim: String?
        public var surprises: [String]
        public var doubts: [String]
        public var judgeError: String
        public var judge: Judgment?
        public var second: Judgment?
    }

    public struct Judgment: Codable, Sendable, Equatable {
        public var verdict: String?
        public var claimSupported: Bool?
        public var firstError: String?
        public var reasoning: String?
        public var model: String?
        public var error: String
    }

    public struct Left: Codable, Sendable, Equatable {
        public var unfinished: [String]
        public var nextAction: NextAction?
        public var nextGoal: String?
    }

    public struct NextAction: Codable, Sendable, Equatable {
        public var action: String?
        public var reviseIf: String?
    }
}

/// Content revisions are opaque; saved order counters can also arrive as strings.
public enum OrderRevision: Codable, Sendable, Equatable {
    case text(String)
    case number(Int)

    public init(from decoder: any Decoder) throws {
        let value = try decoder.singleValueContainer()
        if let number = try? value.decode(Int.self) { self = .number(number) }
        else { self = .text(try value.decode(String.self)) }
    }

    public func encode(to encoder: any Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .text(let text): try value.encode(text)
        case .number(let number): try value.encode(number)
        }
    }

    public var counter: Int? {
        switch self {
        case .number(let number): number
        case .text(let text): Int(text)
        }
    }
}

public struct OrderMoveResponse: Codable, Sendable, Equatable {
    public var rev: OrderRevision?
    public var ids: [String]?
}

public struct PendingNotesResponse: Codable, Sendable, Equatable {
    public var notes: [PendingNote]?
}

public struct PendingNote: Codable, Sendable, Equatable, Identifiable {
    public var file: String?
    public var mid: String?
    public var text: String?
    public var ts: Date?
    public var cid: String?
    public var title: String?
    public var cancellable: Bool?
    public var id: String { file ?? mid ?? "" }
}
