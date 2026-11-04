import Foundation
import Observation

/// The app's state while paired: the client, the chat list, the outbox and the drafts. The UI holds one; `SHAFTSession`
/// makes it from the stored pairing.
@MainActor
@Observable
public final class InstancesStore {
    public private(set) var instances: [InstanceRow] = []
    /// Instances with something this device has not seen (§12): the app badge.
    public private(set) var badge = 0
    /// The last failure to load the list; nil once it loads again.
    public private(set) var error: APIError?
    public private(set) var loaded = false

    @ObservationIgnored public let client: APIClient
    @ObservationIgnored public let outbox: Outbox
    @ObservationIgnored public let drafts: DraftStore
    /// Called when the server says this device is not paired (`401 unpaired`): the app shows its pairing screen.
    @ObservationIgnored public var onUnpaired: (@MainActor () -> Void)?
    @ObservationIgnored private var etag: String?
    @ObservationIgnored private var chats: [String: ChatStore] = [:]
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private let sleep: @Sendable (Duration) async throws -> Void

    public init(client: APIClient, outbox: Outbox, drafts: DraftStore,
                sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.client = client
        self.outbox = outbox
        self.drafts = drafts
        self.sleep = sleep
        outbox.sender = client
    }

    /// Load the list once (`If-None-Match`: a `304` changes nothing).
    public func refresh() async {
        do {
            switch try await client.instances(etag: etag) {
            case .notModified:
                break
            case .value(let answer, let etag):
                self.etag = etag
                instances = answer.instances
                badge = answer.badge
            }
            loaded = true
            error = nil
        } catch is CancellationError {
            return
        } catch {
            report(APIError.from(error))
        }
    }

    /// Poll the list while it is on screen (§3: "every few seconds").
    public func start(every interval: Duration = .seconds(4)) {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self, sleep] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                do { try await sleep(interval) } catch { return }
            }
        }
    }

    public func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    public func row(_ name: String) -> InstanceRow? {
        instances.first { $0.name == name }
    }

    /// The store of `name`'s current chat (one per instance, kept while the app runs).
    public func chat(_ name: String) -> ChatStore {
        if let store = chats[name] { return store }
        let store = ChatStore(instance: name, owner: self)
        chats[name] = store
        return store
    }

    /// A store for an earlier chat (`cid`), not kept.
    public func archivedChat(_ name: String, cid: String) -> ChatStore {
        ChatStore(instance: name, cid: cid, owner: self)
    }

    /// Mark `name` seen up to `version`, the feed version on screen (§7.5). False when the feed moved since
    /// (`409 stale_version`: send it again once the new version is shown) or the request failed.
    @discardableResult
    public func markSeen(_ name: String, version: String) async -> Bool {
        do {
            let answer = try await client.seen(name, version: version)
            badge = answer.badge
            if let index = instances.firstIndex(where: { $0.name == name }) {
                instances[index].unread = answer.unread
            }
            return true
        } catch {
            let apiError = APIError.from(error)
            if apiError.code != "stale_version" { report(apiError) }
            return false
        }
    }

    func report(_ error: APIError) {
        self.error = error
        if error.isUnpaired { onUnpaired?() }
    }
}

/// Lets the feed loop read, off the main actor, whether its chat is on screen (`watch`, §4).
final class WatchFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = true

    var isOn: Bool {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
    }
}

/// One chat: its feed, kept current by the long-poll; its draft; its not-sent messages; and its actions.
@MainActor
@Observable
public final class ChatStore {
    public enum Connection: Equatable, Sendable {
        case connecting
        case live
        /// The last request failed; the loop asks again at `retryAt`.
        case retrying(APIError, retryAt: Date)
        /// The loop stopped on a refusal (`unpaired`, `not_found`, …).
        case stopped(APIError)
        case idle
    }

    public let instance: String
    /// An earlier chat; nil for the current one.
    public let cid: String?
    /// The feed on screen: always full (a partial answer is laid over it).
    public private(set) var feed: Feed?
    public private(set) var connection: Connection = .idle
    /// Every turn instead of the last 80 (§4.1): set by `showEarlier()`.
    public private(set) var showingAll = false
    /// What the composer holds; kept on disk as it changes.
    public var draft: Draft {
        didSet { saveDraft() }
    }
    /// The last action's answer, for the status line (`said`).
    public private(set) var said: String?
    /// The last action's failure (a refusal's message, or the network's).
    public private(set) var actionError: APIError?

    @ObservationIgnored private weak var owner: InstancesStore?
    @ObservationIgnored private let watchFlag = WatchFlag()
    @ObservationIgnored private var pollTask: Task<Void, Never>?

    init(instance: String, cid: String? = nil, owner: InstancesStore) {
        self.instance = instance
        self.cid = cid
        self.owner = owner
        draft = (try? owner.drafts.load(instance)) ?? Draft()
    }

    var client: APIClient? { owner?.client }

    /// Messages of this chat that are on their way or did not get there, oldest first.
    public var unsent: [OutboxEntry] {
        owner?.outbox.entries(for: instance) ?? []
    }

    public var content: FeedContent? { feed?.content }

    /// Whether the chat is on screen now (`watch`, §4): false while a sheet covers it. Read at each request; use
    /// `setWatching` to have the change reach the server at once.
    public var watching: Bool {
        get { watchFlag.isOn }
        set { watchFlag.isOn = newValue }
    }

    /// A sheet covers the chat (false) or left it (true). A held long-poll keeps marking the device as watching until
    /// it answers, so the loop starts again now with the new `watch` (§4): the server stops skipping this device's
    /// pushes for the chat at once, as the web's covering sheet does.
    public func setWatching(_ on: Bool) {
        guard watchFlag.isOn != on else { return }
        watchFlag.isOn = on
        if pollTask != nil {
            stop()
            start()
        }
    }

    /// The long-poll is running (the chat is on screen and the app in front).
    public var isPolling: Bool { pollTask != nil }

    // MARK: The feed loop

    /// Start the long-poll (§4). Call when the chat comes on screen; `stop()` when it leaves.
    public func start() {
        guard pollTask == nil, let client else { return }
        connection = .connecting
        let flag = watchFlag
        let poll = FeedPoll(client: client, instance: instance, cid: cid, all: showingAll,
                            watching: { flag.isOn })
        pollTask = Task { [weak self] in
            do {
                for try await event in poll {
                    guard let self else { return }
                    self.apply(event)
                }
            } catch {
                self?.halt(APIError.from(error))
            }
        }
    }

    public func stop() {
        pollTask?.cancel()
        pollTask = nil
        if case .stopped = connection { return }
        connection = .idle
    }

    /// "Show the N earlier messages": every turn from now on (§4.1).
    public func showEarlier() {
        guard !showingAll else { return }
        showingAll = true
        if pollTask != nil {
            stop()
            start()
        }
    }

    func apply(_ event: FeedEvent) {
        switch event {
        case .feed(let feed):
            self.feed = feed
            connection = .live
            if let mids = feed.content?.mids, cid == nil {
                owner?.outbox.reconcile(instance: instance, mids: mids)
            }
        case .failed(let error, let delay):
            connection = .retrying(error, retryAt: Date().addingTimeInterval(delay.seconds))
        }
    }

    func halt(_ error: APIError) {
        pollTask = nil
        connection = .stopped(error)
        owner?.report(error)
    }

    // MARK: Sending

    /// Send what the composer holds (text, ↩ Reply cards, files) in `mode`. The outbox takes it first, so the draft
    /// clears at once and a failed send stays in the chat as not sent.
    @discardableResult
    public func send(mode: SendMode = .now) async -> Result<SendResponse, APIError> {
        guard let owner else { return .failure(.notPaired) }
        let message = OutgoingMessage(text: draft.text, mode: mode, quotes: draft.quotes,
                                      files: owner.drafts.outgoingFiles(draft, for: instance))
        do {
            try owner.outbox.enqueue(message, to: instance)
        } catch {
            return finish(.failure(error))
        }
        draft = Draft()
        let result = finish(await owner.outbox.deliver(message.mid), inRow: true)
        // Queued, its own row says the same words at once, with Cancel.
        if mode == .queue, case .success = result { said = nil }
        return result
    }

    /// Retry a failed reply (§4.4 `failure.retry`): its text, with `retry_of` when the server gave one.
    @discardableResult
    public func retryReply(_ retry: FailureRetry) async -> Result<SendResponse, APIError> {
        guard let owner else { return .failure(.notPaired) }
        let message = OutgoingMessage(text: retry.text, mode: .now, retryOf: retry.retryOf)
        return finish(await owner.outbox.send(message, to: instance), inRow: true)
    }

    /// Send a not-sent message again with its `mid`.
    @discardableResult
    public func retry(_ mid: String) async -> Result<SendResponse, APIError>? {
        guard let result = await owner?.outbox.retry(mid) else { return nil }
        return finish(result, inRow: true)
    }

    /// Put a not-sent message back in the composer to change it: it leaves the outbox, and sending it again makes a
    /// new message (a new `mid`, PLAN §2.3). Its files come back as the draft's.
    public func editUnsent(_ mid: String) {
        guard let owner, let entry = owner.outbox.entries.first(where: { $0.mid == mid && $0.state == .notSent })
        else { return }
        var restored = draft
        restored.text = [restored.text, entry.text].filter { !$0.isEmpty }.joined(separator: "\n\n")
        restored.quotes += entry.quotes
        for file in entry.files {
            if let copy = try? owner.drafts.attach(owner.outbox.fileURL(of: file, in: entry), name: file.name,
                                                   for: instance) {
                restored.attachments.append(copy)
            }
        }
        draft = restored
        owner.outbox.discard(mid)
    }

    /// Drop a not-sent message.
    public func discardUnsent(_ mid: String) {
        owner?.outbox.discard(mid)
    }

    // MARK: The composer's draft

    /// Copy a file into the draft (Files, a drop). Its copy is the app's, so the picker's loan can end.
    @discardableResult
    public func attach(fileAt url: URL, name: String? = nil) -> Bool {
        guard let owner else { return false }
        do {
            let file = try owner.drafts.attach(url, name: name, for: instance)
            draft.attachments.append(file)
            return true
        } catch {
            actionError = .transport(code: (error as NSError).code,
                                     message: "Could not attach \(name ?? url.lastPathComponent): \(error.localizedDescription)")
            return false
        }
    }

    /// Attach bytes (a photo, a pasted image) as `name`.
    @discardableResult
    public func attach(data: Data, name: String) -> Bool {
        guard let owner else { return false }
        do {
            draft.attachments.append(try owner.drafts.attach(data: data, name: name, for: instance))
            return true
        } catch {
            actionError = .transport(code: (error as NSError).code,
                                     message: "Could not attach \(name): \(error.localizedDescription)")
            return false
        }
    }

    public func removeAttachment(_ id: String) {
        draft.attachments.removeAll { $0.id == id }
    }

    /// Where a draft attachment's copy is, for a preview.
    public func fileURL(of attachment: DraftAttachment) -> URL? {
        owner?.drafts.fileURL(of: attachment, for: instance)
    }

    /// A ↩ Reply card: `quote` with an empty answer (`doc`: the document it came from). Returns its index.
    @discardableResult
    public func addQuote(_ quote: String, doc: String = "") -> Int {
        draft.quotes.append(Quote(quote: quote, doc: doc))
        return draft.quotes.count - 1
    }

    public func removeQuote(at index: Int) {
        guard draft.quotes.indices.contains(index) else { return }
        draft.quotes.remove(at: index)
    }

    // MARK: Feedback (§9)

    /// Thumbs on an answer (`k` in this chat) or a message the instance sent (`file`). Returns the rating's id.
    @discardableResult
    public func rate(k: Int? = nil, file: String? = nil, thumb: String, reason: String? = nil) async -> String? {
        let body: FeedbackRequest
        if let file {
            body = .rateMessage(file: file, thumb: thumb, reason: reason)
        } else if let k, let cid = cid ?? feed?.content?.chat?.id {
            body = .rateChat(cid: cid, k: k, thumb: thumb, reason: reason)
        } else {
            return nil
        }
        return await feedback(body)
    }

    /// Remove a rating.
    public func clearRating(_ id: String) async {
        await feedback(FeedbackRequest(op: "clear", id: id))
    }

    /// Deliver a rating (`reason`: an updated one makes a replacement first). Returns the current rating's id.
    @discardableResult
    public func sendRating(_ id: String, reason: String? = nil) async -> String? {
        await feedback(FeedbackRequest(op: "send", reason: reason, id: id))
    }

    /// Keep a rating but take it off the next message.
    public func unpend(_ id: String) async {
        await feedback(FeedbackRequest(op: "unpend", id: id))
    }

    @discardableResult
    private func feedback(_ body: FeedbackRequest) async -> String? {
        guard let client else { return nil }
        do {
            let answer = try await client.feedback(instance, body)
            said = answer.said
            actionError = nil
            return answer.id
        } catch {
            actionError = APIError.from(error)
            owner?.report(APIError.from(error))
            return nil
        }
    }

    /// Clear the last action's answer and failure once shown.
    public func dismissStatus() {
        said = nil
        actionError = nil
    }

    // MARK: Actions (§7)

    public func unqueue(_ mid: String) async {
        let name = instance
        await act { try await $0.unqueue(name, mid: mid) }
    }

    public func stopReply() async {
        guard let cid = feed?.content?.chat?.id else { return }
        let name = instance
        await act { try await $0.stop(name, cid: cid) }
    }

    public func end() async {
        guard let cid = feed?.content?.chat?.id else { return }
        let name = instance
        await act { try await $0.end(name, cid: cid) }
    }

    /// New chat (§7.4).
    public func clear() async {
        guard let cid = feed?.content?.chat?.id else { return }
        let name = instance
        await act { try await $0.clear(name, cid: cid) }
    }

    /// The feed's end is on screen and the app is in front: mark the version shown seen (§7.5).
    @discardableResult
    public func markSeen() async -> Bool {
        guard cid == nil, let version = feed?.version, let owner else { return false }
        return await owner.markSeen(instance, version: version)
    }

    /// The chat left the screen (§7.6): pushes for it reach this device again at once.
    public func away() async {
        guard cid == nil, let client else { return }
        _ = try? await client.away(instance)
    }

    private func act(_ action: @escaping @Sendable (APIClient) async throws -> SaidResponse) async {
        guard let client else { return }
        do {
            said = try await action(client).said
            actionError = nil
            reconnectIfWaiting()
        } catch {
            actionError = APIError.from(error)
            owner?.report(APIError.from(error))
        }
    }

    /// The server answered an action while the feed loop waits out a backoff: ask for the feed now rather than
    /// show "offline" over a chat that is reachable.
    public func reconnectIfWaiting() {
        guard pollTask != nil, case .retrying = connection else { return }
        stop()
        start()
    }

    /// `inRow`: the message stayed in the chat as not sent, and its row says why: a status line would say it again.
    private func finish(_ result: Result<SendResponse, APIError>, inRow: Bool = false) -> Result<SendResponse, APIError> {
        switch result {
        case .success(let answer):
            said = answer.said
            actionError = nil
            reconnectIfWaiting()
        case .failure(let error):
            actionError = inRow ? nil : error
            if error.isUnpaired { owner?.report(error) }
        }
        return result
    }

    private func saveDraft() {
        guard cid == nil, let owner else { return }
        do {
            try owner.drafts.save(draft, for: instance)
        } catch {
            actionError = .transport(code: (error as NSError).code,
                                     message: "Could not keep the draft: \(error.localizedDescription)")
        }
    }
}

extension Duration {
    var seconds: TimeInterval {
        let (seconds, attoseconds) = components
        return TimeInterval(seconds) + TimeInterval(attoseconds) / 1e18
    }
}
