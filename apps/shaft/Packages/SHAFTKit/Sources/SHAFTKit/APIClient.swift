import Foundation

/// What a paired device holds: where the server is and its bearer (§1.2, §2). Kept in the Keychain (`PairingStore`).
public struct ServerCredential: Codable, Sendable, Equatable {
    /// Everything before `/pair` in the pairing link: `https://<mac>.<tailnet>.ts.net:8444`.
    public var baseURL: URL
    public var device: String
    public var secret: String
    /// The name given to `shaft web pair --name`.
    public var name: String
    /// The tailnet login it was paired by.
    public var login: String

    public init(baseURL: URL, device: String, secret: String, name: String, login: String) {
        self.baseURL = baseURL
        self.device = device
        self.secret = secret
        self.name = name
        self.login = login
    }

    var bearer: String { "Bearer \(device).\(secret)" }
}

/// A conditional or long-polled `GET`: the value, or `304` (nothing new).
public enum Conditional<Value: Sendable>: Sendable {
    case value(Value, etag: String?)
    case notModified
}

/// A file the server sent (§8, §10.3).
public struct DownloadedFile: Sendable, Equatable {
    public var data: Data
    /// From `Content-Disposition` (`filename*=UTF-8''…` or `filename="…"`), nil when it names none.
    public var filename: String?
    public var mimeType: String?
}

/// A file to send with a message: a local copy the app owns (the draft's or the outbox's), read at send time.
public struct OutgoingFile: Codable, Sendable, Equatable {
    public var name: String
    public var mimeType: String
    public var fileURL: URL

    public init(name: String, mimeType: String, fileURL: URL) {
        self.name = name
        self.mimeType = mimeType
        self.fileURL = fileURL
    }
}

/// A message as `POST /app/v1/chat/<n>/send` takes it (§6).
public struct OutgoingMessage: Codable, Sendable, Equatable {
    public var mid: String
    public var text: String
    public var mode: SendMode
    public var quotes: [Quote]
    public var retryOf: Int?
    public var inReplyTo: String? { quotes.last(where: { $0.inReplyTo != nil })?.inReplyTo }
    public var files: [OutgoingFile]
    /// Spoken in voice mode and transcribed on the device: sent as `voice=1` (§6). False for a typed message.
    public var voice: Bool
    /// The conversation it was written in (`ChatRef.draftKey`): a thread's id, sent as `cid` in every mode; `"from"`
    /// for the From conversation; nil for the current chat (a server before threads, or
    /// a send that lets the server pick the most recently active thread).
    public var cid: String?

    public init(mid: String = OutgoingMessage.newMid(), text: String, mode: SendMode = .now, quotes: [Quote] = [],
                retryOf: Int? = nil, files: [OutgoingFile] = [], voice: Bool = false, cid: String? = nil) {
        self.mid = mid
        self.text = text
        self.mode = mode
        self.quotes = quotes
        self.retryOf = retryOf
        self.files = files
        self.voice = voice
        self.cid = cid
    }

    private enum CodingKeys: String, CodingKey {
        case mid, text, mode, quotes, retryOf, files, voice, cid
    }

    /// The conversation id in every send mode, including `"from"`.
    public var sentCid: String? {
        guard let cid, !cid.isEmpty else { return nil }
        return cid
    }

    /// A message stored before `voice` existed has no such key: it was typed. One stored before threads has no `cid`:
    /// it was for the current chat.
    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        mid = try container.decode(String.self, forKey: .mid)
        text = try container.decode(String.self, forKey: .text)
        mode = try container.decode(SendMode.self, forKey: .mode)
        quotes = try container.decode([Quote].self, forKey: .quotes)
        retryOf = try container.decodeIfPresent(Int.self, forKey: .retryOf)
        files = try container.decode([OutgoingFile].self, forKey: .files)
        voice = try container.decodeIfPresent(Bool.self, forKey: .voice) ?? false
        cid = try container.decodeIfPresent(String.self, forKey: .cid)
    }

    /// A new message id: a lowercase UUID (§1.3).
    public static func newMid() -> String { UUID().uuidString.lowercased() }
}

/// The `/app/v1` client (docs/app-api.md). Every method throws `APIError` and nothing else, bar `CancellationError`
/// when its task is cancelled.
public final class APIClient: Sendable {
    public let credential: ServerCredential
    let session: URLSession

    public init(credential: ServerCredential, session: URLSession = .shared) {
        self.credential = credential
        self.session = session
    }

    // MARK: Pairing (§2)

    /// Pair this device: post the pasted link to `<base>/app/v1/pair` and return what to keep. No bearer.
    public static func pair(link: PairingLink, platform: String = Platform.current,
                            session: URLSession = .shared) async throws -> ServerCredential {
        let url = link.baseURL.appending(path: "app/v1/pair")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try encode(PairRequest(link: link.link, platform: platform), path: "/app/v1/pair")
        let (data, _) = try await perform(request, session: session, path: "/app/v1/pair")
        let paired: PairResponse = try decode(data, path: "/app/v1/pair")
        return ServerCredential(baseURL: link.baseURL, device: paired.device, secret: paired.secret,
                                name: paired.name, login: paired.login)
    }

    public func unpair() async throws -> SaidResponse {
        try await post("/app/v1/unpair", json: EmptyBody())
    }

    // MARK: The chat list (§3)

    public func instances(etag: String? = nil) async throws -> Conditional<InstancesResponse> {
        try await conditionalGet("/app/v1/instances", etag: etag)
    }

    // MARK: The feed (§4)

    /// One feed request. With `since`/`live` from the feed the app holds and `wait` > 0 it is the long-poll: `304`
    /// when nothing moved in `wait` seconds. `FeedPoll` loops on it.
    public func feed(_ instance: String, cid: String? = nil, since: String? = nil, live: String? = nil,
                     wait: Int = 0, all: Bool = false, watch: Bool = true,
                     at: Date = Date()) async throws -> Conditional<Feed> {
        try await feed(instance, ref: cid.map(ChatRef.earlier) ?? .current, since: since, live: live, wait: wait,
                       all: all, watch: watch, at: at)
    }

    /// The feed of `ref`: the current chat, a thread or an earlier chat by its id (both `/chat/<n>/<cid>`), or the From
    /// feed (`/from/<n>`, §4's shape).
    public func feed(_ instance: String, ref: ChatRef, since: String? = nil, live: String? = nil,
                     wait: Int = 0, all: Bool = false, watch: Bool = true,
                     at: Date = Date()) async throws -> Conditional<Feed> {
        var query = [URLQueryItem]()
        if let since, let live {
            query.append(URLQueryItem(name: "since", value: since))
            query.append(URLQueryItem(name: "live", value: live))
        }
        query.append(URLQueryItem(name: "wait", value: String(wait)))
        if all { query.append(URLQueryItem(name: "all", value: "1")) }
        query.append(URLQueryItem(name: "at", value: String(Int64(at.timeIntervalSince1970 * 1000))))
        query.append(URLQueryItem(name: "watch", value: watch ? "1" : "0"))
        let path = Self.feedPath(instance, ref)
        var request = makeRequest("GET", path, query: query)
        // The server holds the request up to `wait` s (§4): the timeout must outlast it.
        request.timeoutInterval = TimeInterval(wait + 30)
        let (data, response) = try await send(request, path: path)
        if response.statusCode == 304 { return .notModified }
        return .value(try Self.decode(data, path: path), etag: nil)
    }

    static func feedPath(_ instance: String, _ ref: ChatRef) -> String {
        switch ref {
        case .current: "/app/v1/chat/\(instance)"
        case .thread(let cid), .earlier(let cid): "/app/v1/chat/\(instance)/\(cid)"
        case .from: "/app/v1/from/\(instance)"
        }
    }

    public func chats(_ instance: String) async throws -> ChatsResponse {
        try await get("/app/v1/chats/\(instance)")
    }

    // MARK: Threads (§3.1)

    /// The instance's open threads and its From feed. `404 not_found` from a server before threads.
    public func threads(_ instance: String) async throws -> ThreadsResponse {
        try await get("/app/v1/threads/\(instance)")
    }

    public func moveThread(_ instance: String, id: String, after: String?) async throws -> OrderMoveResponse {
        try await post("/app/v1/threads/\(instance)/move", json: ThreadMove(id: id, after: after))
    }

    public func moveInstance(_ name: String, after: String?) async throws -> OrderMoveResponse {
        try await post("/app/v1/instances/move", json: InstanceMove(name: name, after: after))
    }

    public func notes(_ instance: String) async throws -> PendingNotesResponse {
        try await get("/app/v1/notes/\(instance)")
    }

    /// A new open thread (§7.7); it ends no other. A nil or blank title takes the first message's line once sent.
    public func newThread(_ instance: String, title: String? = nil) async throws -> NewThreadResponse {
        try await post("/app/v1/chat/\(instance)/new", json: title.map { ["title": $0] } ?? [:])
    }

    /// Rename an open thread (§7.8).
    public func rename(_ instance: String, cid: String, title: String) async throws -> SaidResponse {
        try await post("/app/v1/chat/\(instance)/rename", json: ["cid": cid, "title": title])
    }

    // MARK: Sending and actions (§6, §7)

    /// Answer a structured question. Refusals retain their status, code, message and existing answer.
    public func answer(_ instance: String, _ body: ChoiceAnswerRequest) async throws -> ChoiceAnswerResponse {
        try await post("/app/v1/chat/\(instance)/answer", json: body)
    }

    /// Send a message (multipart, §6). The same `mid` again answers as the first send did, never sending twice.
    public func send(_ instance: String, _ message: OutgoingMessage) async throws -> SendResponse {
        let path = "/app/v1/chat/\(instance)/send"
        var form = MultipartForm()
        form.addField("text", message.text)
        form.addField("mid", message.mid)
        form.addField("mode", message.mode.rawValue)
        if !message.quotes.isEmpty {
            let quotes = try Self.encode(message.quotes, path: path)
            form.addField("quotes", String(decoding: quotes, as: UTF8.self))
        }
        if let target = message.inReplyTo { form.addField("in_reply_to", target) }
        if let retryOf = message.retryOf { form.addField("retry_of", String(retryOf)) }
        if message.voice { form.addField("voice", "1") }
        if let cid = message.sentCid { form.addField("cid", cid) }
        let bodyURL = FileManager.default.temporaryDirectory.appending(path: "shaft-upload-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: bodyURL) }
        do {
            try form.writeBody(files: message.files, to: bodyURL)
        } catch {
            guard let error = APIError.from(error) else { throw CancellationError() }
            throw error
        }
        var request = makeRequest("POST", path)
        request.setValue(form.contentType, forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 300
        let (data, _) = try await send(request, path: path, upload: bodyURL)
        return try Self.decode(data, path: path)
    }

    /// Cancel a queued message; `cid` the thread it is queued in (without it, the server's most recently active one).
    public func unqueue(_ instance: String, mid: String, cid: String? = nil) async throws -> SaidResponse {
        var body = ["mid": mid]
        if let cid { body["cid"] = cid }
        return try await post("/app/v1/chat/\(instance)/unqueue", json: body)
    }

    /// Cancel a next-cycle note by its message id, or its file on older notes.
    public func uncycle(_ instance: String, mid: String? = nil, file: String? = nil) async throws -> SaidResponse {
        var body: [String: String] = [:]
        if let mid { body["mid"] = mid }
        else if let file { body["file"] = file }
        return try await post("/app/v1/chat/\(instance)/uncycle", json: body)
    }

    public func stop(_ instance: String, cid: String) async throws -> SaidResponse {
        try await post("/app/v1/chat/\(instance)/stop", json: ["cid": cid])
    }

    public func end(_ instance: String, cid: String) async throws -> SaidResponse {
        try await post("/app/v1/chat/\(instance)/end", json: ["cid": cid])
    }

    public func clear(_ instance: String, cid: String) async throws -> SaidResponse {
        try await post("/app/v1/chat/\(instance)/clear", json: ["cid": cid])
    }

    /// Mark the chat seen up to `version` (§7.5). `409 stale_version` means the feed moved since it was drawn. `cid`:
    /// the thread shown, or `"from"`; nil for the shown chat (a server before threads).
    public func seen(_ instance: String, version: String, cid: String? = nil) async throws -> SeenResponse {
        var body = ["version": version]
        if let cid { body["cid"] = cid }
        return try await post("/app/v1/chat/\(instance)/seen", json: body)
    }

    /// The chat left the screen (§7.6). `cid` as `seen`'s.
    public func away(_ instance: String, at: Date = Date(), cid: String? = nil) async throws -> OkResponse {
        try await post("/app/v1/chat/\(instance)/away", json: AwayBody(at: Int64(at.timeIntervalSince1970 * 1000), cid: cid))
    }

    private struct AwayBody: Encodable {
        var at: Int64
        /// Left out when nil: a server before threads reads the body as `{"at"}`.
        var cid: String?
    }

    // MARK: Files (§8)

    /// A file by its server-relative URL, as an attachment's `url` gives it (`/app/v1/file/…`).
    public func download(_ relativeURL: String) async throws -> DownloadedFile {
        guard let url = URL(string: relativeURL, relativeTo: credential.baseURL)?.absoluteURL else {
            throw APIError.decoding(path: relativeURL, message: "not a URL")
        }
        guard LocalFiles.serverOrigin(url) == LocalFiles.serverOrigin(credential.baseURL) else {
            throw APIError.decoding(path: relativeURL, message: "Attachment URL is outside the paired server origin.")
        }
        var request = URLRequest(url: url)
        request.setValue(credential.bearer, forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 300
        let (data, response) = try await send(request, path: url.path)
        return DownloadedFile(data: data, filename: Self.filename(response), mimeType: response.mimeType)
    }

    // MARK: Feedback (§9)

    public func feedback(_ instance: String, _ body: FeedbackRequest) async throws -> FeedbackResponse {
        try await post("/app/v1/feedback/\(instance)", json: body)
    }

    // MARK: Documents and comments (§10)

    public func docs(_ instance: String, cid: String? = nil) async throws -> DocsList {
        try await get("/app/v1/docs/\(instance)", query: cid.map { [URLQueryItem(name: "cid", value: $0)] } ?? [])
    }

    public func doc(_ instance: String, path: String, cid: String? = nil) async throws -> DocumentDetail {
        var query = [URLQueryItem(name: "path", value: path)]
        if let cid { query.append(URLQueryItem(name: "cid", value: cid)) }
        return try await get("/app/v1/docs/\(instance)/doc", query: query)
    }

    public func rawDoc(_ instance: String, path: String) async throws -> DownloadedFile {
        let route = "/app/v1/docs/\(instance)/raw"
        let request = makeRequest("GET", route, query: [URLQueryItem(name: "path", value: path)])
        let (data, response) = try await send(request, path: route)
        return DownloadedFile(data: data, filename: Self.filename(response), mimeType: response.mimeType)
    }

    /// Save (or, with `sha` `"none"`, create) a document. `409 stale` carries the current `sha` and `diff`.
    public func saveDoc(_ instance: String, path: String, text: String, sha: String,
                        dir: String? = nil) async throws -> DocSaveResponse {
        var body = ["path": path, "text": text, "sha": sha]
        if let dir { body["dir"] = dir }
        return try await post("/app/v1/docs/\(instance)/save", json: body)
    }

    public func deleteDoc(_ instance: String, path: String, sha: String) async throws -> SaidResponse {
        try await post("/app/v1/docs/\(instance)/delete", json: ["path": path, "sha": sha])
    }

    /// A new thread on `doc` (`anchor` nil: the whole document). `send`: `""` keeps it unsent, `"now"`, `"cycle"`.
    public func comment(_ instance: String, pid: String = APIClient.newPid(), doc: String, anchor: CommentAnchor?,
                        sha: String, text: String, send: String) async throws -> CommentResponse {
        try await post("/app/v1/docs/\(instance)/comment",
                       json: NewThreadBody(pid: pid, doc: doc, anchor: anchor, sha: sha, text: text, send: send))
    }

    public func reply(_ instance: String, pid: String = APIClient.newPid(), thread: String, text: String,
                      send: String) async throws -> CommentResponse {
        try await post("/app/v1/docs/\(instance)/comment",
                       json: ["pid": pid, "thread": thread, "text": text, "send": send])
    }

    public func editComment(_ instance: String, pid: String, text: String) async throws -> CommentResponse {
        try await post("/app/v1/docs/\(instance)/comment/edit", json: ["pid": pid, "text": text])
    }

    public func deleteComment(_ instance: String, pid: String) async throws -> CommentResponse {
        try await post("/app/v1/docs/\(instance)/comment/delete", json: ["pid": pid])
    }

    /// Send a stored post: `when` `"now"` (a chat message) or `"cycle"` (its inbox).
    public func sendComment(_ instance: String, pid: String, when: String) async throws -> CommentResponse {
        try await post("/app/v1/docs/\(instance)/comment/send", json: ["pid": pid, "when": when])
    }

    /// `state` `"resolved"` or `"open"`.
    public func setThreadState(_ instance: String, thread: String, state: String) async throws -> CommentResponse {
        try await post("/app/v1/docs/\(instance)/comment/state", json: ["thread": thread, "state": state])
    }

    /// A comment post's id: `p-` + 16 lowercase hex (§1.3).
    public static func newPid() -> String {
        "p-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(16)
    }

    // MARK: Push (§11)

    public func registerPush(_ body: PushRegisterRequest) async throws -> PushRegisterResponse {
        try await post("/app/v1/push/register", json: body)
    }

    public func unregisterPush() async throws -> RemovedResponse {
        let path = "/app/v1/push/register"
        let (data, _) = try await send(makeRequest("DELETE", path), path: path)
        return try Self.decode(data, path: path)
    }

    public func pushStatus() async throws -> PushStatus {
        try await get("/app/v1/push/status")
    }

    /// What the next notification shows (§11.4). It takes the events off the server's queue.
    public func pushLatest() async throws -> PushLatest {
        try await pushLatest(instance: nil)
    }

    func pushLatest(instance: String?) async throws -> PushLatest {
        try await get("/app/v1/push/latest", query: instance.map { [URLQueryItem(name: "instance", value: $0)] } ?? [])
    }

    // MARK: Cycles (§16)

    public func plan(_ instance: String, etag: String? = nil) async throws -> Conditional<CyclePlan> {
        try await conditionalGet("/app/v1/plan/\(instance)", etag: etag)
    }

    public func cycles(_ instance: String, before: String? = nil, filter: String = "",
                       etag: String? = nil) async throws -> Conditional<CycleDaysResponse> {
        var query = [URLQueryItem(name: "filter", value: filter)]
        if let before { query.append(URLQueryItem(name: "before", value: before)) }
        return try await conditionalGet("/app/v1/cycles/\(instance)", etag: etag, query: query)
    }

    public func cycle(_ instance: String, ts: String) async throws -> CycleDetail {
        try await get("/app/v1/cycles/\(instance)/\(ts)")
    }

    // MARK: Settings and services (§15)

    public func settings(_ instance: String) async throws -> InstanceSettings {
        try await get("/app/v1/settings/\(instance)")
    }

    public func saveSetting(_ instance: String, field: SettingField, value: SettingValue,
                            expect: SettingValue) async throws -> SettingSave {
        try await post("/app/v1/settings/\(instance)",
                       json: SettingWrite(field: field, value: value, expect: expect))
    }

    public func setPaused(_ instance: String, paused: Bool) async throws -> InstanceSettings {
        let answer: SettingsResponse = try await post(
            "/app/v1/settings/\(instance)/\(paused ? "pause" : "resume")", json: EmptyBody(), timeout: 70)
        return answer.settings
    }

    public func services() async throws -> ServicesResponse {
        try await get("/app/v1/services")
    }

    /// The server's release and the `/app/v1` version it speaks (§19): what `ServerCompatibility` decides on. A server
    /// older than the route answers `404 not_found`.
    public func version() async throws -> ServerVersion {
        try await get("/app/v1/version")
    }

    /// The API restart watcher must not let an individual poll outlast its confirmation deadline.
    func services(timeout: TimeInterval) async throws -> ServicesResponse {
        let path = "/app/v1/services"
        var request = makeRequest("GET", path)
        request.timeoutInterval = timeout
        let (data, _) = try await send(request, path: path)
        return try Self.decode(data, path: path)
    }

    public func restart(service: String) async throws -> ServicesResponse {
        try await post("/app/v1/services/\(service)/restart", json: EmptyBody(), timeout: 100)
    }

    public func cancelSchedulerRestart() async throws -> ServicesResponse {
        try await post("/app/v1/services/scheduler/cancel", json: EmptyBody())
    }

    private struct SettingWrite: Encodable {
        var field: SettingField
        var value: SettingValue
        var expect: SettingValue
    }

    private struct SettingsResponse: Decodable {
        var settings: InstanceSettings
    }

    // MARK: Setup (§18)

    /// The four starting drives.
    public func setupDrives() async throws -> DrivePresets {
        try await get("/app/v1/setup/drives")
    }

    /// One provider or tool key, into the server's `.env` on the verb's stdin. The answer never carries it.
    public func setupSecret(name: String, value: String) async throws -> SaidResponse {
        try await post("/app/v1/setup/secret", json: ["name": name, "value": value])
    }

    /// `provider use`: held up to the verb's own limit (`actions.SLOW_S`, 90 s) and a margin.
    public func setupProvider(_ request: ProviderRequest) async throws -> ProviderUseResponse {
        try await post("/app/v1/setup/provider", json: request, timeout: 120)
    }

    /// `provider check`: one live call per configured model, up to `2 × actions.SLOW_S`.
    public func checkProvider(_ provider: String) async throws -> ProviderCheckResponse {
        try await post("/app/v1/setup/provider/check", json: ["provider": provider], timeout: 200)
    }

    /// `new`, `config set` and `drive set` in a row: `new` alone may take `actions.SLOW_S`.
    public func setupInstance(name: String, operator: String, model: String, drive: String,
                              autonomy: String) async throws -> SetupInstanceResponse {
        try await post("/app/v1/setup/instance",
                       json: ["name": name, "operator": `operator`, "model": model, "drive": drive, "autonomy": autonomy],
                       timeout: 240)
    }

    /// `telegram token <name>`, the bot's token on the verb's stdin (Telegram is asked who the bot is first).
    public func setupTelegram(name: String, token: String) async throws -> SaidResponse {
        try await post("/app/v1/setup/telegram", json: ["name": name, "token": token])
    }

    /// Where the instance's Telegram stands: its bot, the paired account, the service.
    public func telegramStatus(_ instance: String) async throws -> TelegramStatus {
        try await get("/app/v1/setup/telegram/status/\(instance)")
    }

    /// `telegram pair --json`: the code the operator sends to the bot; none when an account is paired already.
    public func telegramPair(_ instance: String) async throws -> TelegramStatus {
        try await post("/app/v1/setup/telegram/pair/\(instance)", json: EmptyBody())
    }

    /// `telegram on`: the Telegram service installed, or restarted to pick up a new bot (the verb's `actions.SLOW_S`).
    public func telegramService() async throws -> SaidResponse {
        try await post("/app/v1/setup/telegram/service", json: EmptyBody(), timeout: 120)
    }

    /// What the operator has granted the instance (§18.6).
    public func access(_ instance: String) async throws -> AccessResponse {
        try await get("/app/v1/access/\(instance)")
    }

    /// One grant or revocation through its verb, answered with the fresh access. The browser's may first install the
    /// shared Chromium, held up to the server's 15 minutes (`app_api.BROWSER_INSTALL_S`): this waits that and a margin.
    public func grantAccess(_ instance: String, _ grant: AccessGrant) async throws -> AccessResponse {
        try await post("/app/v1/access/\(instance)", json: grant,
                       timeout: grant.grant == "browser" ? AccessGrant.browserTimeout : 120)
    }

    /// The Gmail labels of the operator's Google account `alias`, read live from Google (§18.7): what a mail grant
    /// may name.
    public func googleLabels(_ alias: String) async throws -> GoogleLabels {
        try await get("/app/v1/google/\(alias)/labels")
    }

    /// The calendars of the operator's Google account `alias`, primary first (§18.7): what a calendar grant may name.
    public func googleCalendars(_ alias: String) async throws -> GoogleCalendars {
        try await get("/app/v1/google/\(alias)/calendars")
    }

    /// `first-reply`: `resume` (it works on its own after the first answered reply) or `stay`.
    public func setupAutonomy(_ instance: String, then: String) async throws -> SetupAutonomyResponse {
        try await post("/app/v1/setup/autonomy/\(instance)", json: ["then": then])
    }

    /// The hidden opening: the instance writes its welcome in Main, whose id the answer carries.
    public func setupHello(_ instance: String) async throws -> NewThreadResponse {
        try await post("/app/v1/setup/hello/\(instance)", json: EmptyBody())
    }

    // MARK: Plumbing

    private struct EmptyBody: Encodable {}

    private struct NewThreadBody: Encodable {
        var pid: String
        var doc: String
        var anchor: CommentAnchor?
        var sha: String
        var text: String
        var send: String

        func encode(to encoder: any Encoder) throws {
            // `anchor` goes as null for the whole document, never absent.
            enum Keys: String, CodingKey { case pid, doc, anchor, sha, text, send }
            var container = encoder.container(keyedBy: Keys.self)
            try container.encode(pid, forKey: .pid)
            try container.encode(doc, forKey: .doc)
            try container.encode(anchor, forKey: .anchor)
            try container.encode(sha, forKey: .sha)
            try container.encode(text, forKey: .text)
            try container.encode(send, forKey: .send)
        }
    }

    func makeRequest(_ method: String, _ path: String, query: [URLQueryItem] = []) -> URLRequest {
        var components = URLComponents(url: credential.baseURL.appending(path: path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            components.queryItems = query
            // URLComponents leaves `+` as it is, which the server's query parser reads as a space.
            components.percentEncodedQuery = components.percentEncodedQuery?
                .replacingOccurrences(of: "+", with: "%2B")
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.setValue(credential.bearer, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        let (data, _) = try await send(makeRequest("GET", path, query: query), path: path)
        return try Self.decode(data, path: path)
    }

    private func conditionalGet<T: Decodable & Sendable>(
        _ path: String, etag: String?, query: [URLQueryItem] = []
    ) async throws -> Conditional<T> {
        var request = makeRequest("GET", path, query: query)
        if let etag { request.setValue(etag, forHTTPHeaderField: "If-None-Match") }
        let (data, response) = try await send(request, path: path)
        if response.statusCode == 304 { return .notModified }
        return .value(try Self.decode(data, path: path), etag: response.value(forHTTPHeaderField: "ETag"))
    }

    private func post<T: Decodable, Body: Encodable>(_ path: String, json body: Body,
                                               timeout: TimeInterval? = nil) async throws -> T {
        var request = makeRequest("POST", path)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try Self.encode(body, path: path)
        if let timeout { request.timeoutInterval = timeout }
        let (data, _) = try await send(request, path: path)
        return try Self.decode(data, path: path)
    }

    private func send(_ request: URLRequest, path: String, upload: URL? = nil) async throws -> (Data, HTTPURLResponse) {
        try await Self.perform(request, session: session, path: path, upload: upload)
    }

    /// Run a request; an error status becomes `APIError` with the server's message (§1.5). `304` passes through.
    static func perform(_ request: URLRequest, session: URLSession, path: String,
                        upload: URL? = nil) async throws -> (Data, HTTPURLResponse) {
        let data: Data
        let response: URLResponse
        let delegate = OriginRedirectDelegate(origin: request.url!)
        do {
            if let upload {
                (data, response) = try await session.upload(for: request, fromFile: upload, delegate: delegate)
            } else {
                (data, response) = try await session.data(for: request, delegate: delegate)
            }
        } catch {
            guard let apiError = APIError.from(error), !Task.isCancelled else { throw CancellationError() }
            throw apiError
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIError.decoding(path: path, message: "not an HTTP response")
        }
        if (200..<300).contains(http.statusCode) || http.statusCode == 304 { return (data, http) }
        if let body = try? SHAFTJSON.decoder().decode(ErrorBody.self, from: data) {
            throw APIError.server(status: http.statusCode, error: body.error)
        }
        let text = String(decoding: data.prefix(300), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        throw APIError.http(status: http.statusCode, body: text)
    }

    static func decode<T: Decodable>(_ data: Data, path: String) throws -> T {
        do {
            return try SHAFTJSON.decoder().decode(T.self, from: data)
        } catch {
            throw APIError.decoding(path: path, message: String(describing: error))
        }
    }

    static func encode<T: Encodable>(_ value: T, path: String) throws -> Data {
        do {
            return try SHAFTJSON.encoder().encode(value)
        } catch {
            throw APIError.decoding(path: path, message: "could not encode the request: \(error)")
        }
    }

    /// The file name from `Content-Disposition`: `filename*=UTF-8''…` first, then `filename="…"`.
    static func filename(_ response: HTTPURLResponse) -> String? {
        guard let disposition = response.value(forHTTPHeaderField: "Content-Disposition") else { return nil }
        for part in disposition.split(separator: ";").map({ $0.trimmingCharacters(in: .whitespaces) }) {
            if part.lowercased().hasPrefix("filename*="), let range = part.range(of: "''") {
                return String(part[range.upperBound...]).removingPercentEncoding
            }
        }
        for part in disposition.split(separator: ";").map({ $0.trimmingCharacters(in: .whitespaces) }) {
            if part.lowercased().hasPrefix("filename=") {
                return String(part.dropFirst("filename=".count)).trimmingCharacters(in: CharacterSet(charactersIn: "\""))
            }
        }
        return nil
    }
}

/// Which platform this build is, as `pair` and `push/register` name it.
public enum Platform {
    #if os(macOS)
    public static let current = "macos"
    #else
    public static let current = "ios"
    #endif
}

/// Refuse redirects to another origin before URLSession can send credentials there.
final class OriginRedirectDelegate: NSObject, URLSessionTaskDelegate, Sendable {
    let origin: URL

    init(origin: URL) { self.origin = origin }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        guard let url = request.url, LocalFiles.serverOrigin(url) == LocalFiles.serverOrigin(origin) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }
}

// A nil anchor must be JSON null, not an omitted key.
private struct ThreadMove: Encodable {
    let id: String
    let after: String?
    enum CodingKeys: String, CodingKey { case id, after }
    func encode(to encoder: any Encoder) throws {
        var box = encoder.container(keyedBy: CodingKeys.self)
        try box.encode(id, forKey: .id)
        try box.encode(after, forKey: .after)
    }
}

private struct InstanceMove: Encodable {
    let name: String
    let after: String?
    enum CodingKeys: String, CodingKey { case name, after }
    func encode(to encoder: any Encoder) throws {
        var box = encoder.container(keyedBy: CodingKeys.self)
        try box.encode(name, forKey: .name)
        try box.encode(after, forKey: .after)
    }
}
