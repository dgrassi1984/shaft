import Foundation
import Observation

/// The app's state while paired: the client, the chat list, the outbox and the drafts. The UI holds one; `SHAFTSession`
/// makes it from the stored pairing.
@MainActor
@Observable
public final class InstancesStore {
    public private(set) var instances: [InstanceRow] = []
    public private(set) var paused: [PausedInstance] = []
    /// Instances with something this device has not seen (§12): the app badge.
    public private(set) var badge = 0
    /// The last failure to load the list; nil once it loads again.
    public private(set) var error: APIError?
    public private(set) var loaded = false
    public private(set) var pendingNotes: [String: [PendingNote]] = [:]
    public private(set) var notesError: [String: APIError] = [:]
    public private(set) var moving: Set<String> = []
    /// What `GET /app/v1/version` said of the server (§19), nil until asked. `.needsNewerServer` takes the session's place.
    public private(set) var compatibility: ServerCompatibility.Verdict?
    @ObservationIgnored private var compatibilityGeneration = 0
    @ObservationIgnored private var orderRevisions: [String: Int] = [:]
    @ObservationIgnored private var instancesRev: OrderRevision?
    @ObservationIgnored private var listGeneration = 0
    @ObservationIgnored private var migrated: Set<String> = []
    /// Created ids stay open until a successful list read checks them.
    @ObservationIgnored private var createdThreads: [String: Set<String>] = [:]
    /// Instances a New thread is being started for, until the server answers: the buttons show it and take no second tap.
    public private(set) var creatingThread: Set<String> = []

    @ObservationIgnored public let client: APIClient
    @ObservationIgnored public let outbox: Outbox
    @ObservationIgnored public let drafts: DraftStore
    /// Called when the server says this device is not paired (`401 unpaired`): the app shows its pairing screen.
    @ObservationIgnored public var onUnpaired: (@MainActor () -> Void)?
    /// Reapply the server's count to the icon even when a push changed the icon but not this store.
    @ObservationIgnored public var onBadge: (@MainActor (Int) -> Void)?
    @ObservationIgnored private var badgeKnown = false
    @ObservationIgnored private var etag: String?
    @ObservationIgnored private var seenGeneration = 0
    @ObservationIgnored private var settingsStores: [String: InstanceSettingsStore] = [:]
    public let services: ServicesStore
    @ObservationIgnored private var chats: [ChatKey: ChatStore] = [:]
    /// Each instance's open threads, as `GET /app/v1/threads/<n>` last answered (§3.1).
    public private(set) var threadStates: [String: ThreadsState] = [:]
    /// Why an instance's threads could not be read the last time (the list keeps what it had).
    public private(set) var threadsError: [String: APIError] = [:]
    /// The rail row each instance's threads were last read at: a row that moved since asks for them again.
    @ObservationIgnored private var threadsReadAt: [String: InstanceRow] = [:]
    /// The newest request per instance: an older answer arriving after a newer one is dropped.
    @ObservationIgnored private var threadsGeneration: [String: Int] = [:]
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored let sleep: @Sendable (Duration) async throws -> Void

    public init(client: APIClient, outbox: Outbox, drafts: DraftStore,
                sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.client = client
        self.outbox = outbox
        self.drafts = drafts
        self.sleep = sleep
        self.services = ServicesStore(client: client, sleep: sleep)
        self.services.attach(to: self)
        outbox.sender = client
    }

    /// Load the list once (`If-None-Match`: a `304` confirms the known badge too).
    public func refresh() async {
        let generation = seenGeneration
        let listGeneration = self.listGeneration
        do {
            switch try await client.instances(etag: etag) {
            case .notModified:
                if badgeKnown { onBadge?(badge) }
            case .value(let answer, let etag):
                guard generation == seenGeneration, listGeneration == self.listGeneration, !moving.contains("instances") else { return }
                if let rev = answer.instancesRev?.counter {
                    guard rev >= (orderRevisions["instances"] ?? 0) else { return }
                    orderRevisions["instances"] = rev
                }
                self.etag = etag
                if instancesRev != answer.instancesRev {
                    threadsReadAt.removeAll()
                    instancesRev = answer.instancesRev
                }
                instances = answer.instances
                paused = answer.paused ?? []
                reportBadge(answer.badge)
            }
            loaded = true
            error = nil
        } catch {
            guard let error = APIError.from(error) else { return }
            report(error)
        }
    }

    public func resume(_ name: String) async {
        do {
            _ = try await client.setPaused(name, paused: false)
            await refresh()
        } catch {
            guard let error = APIError.from(error) else { return }
            report(error)
        }
    }

    /// Ask the server whether it speaks the `/app/v1` this app needs (§19): once per session, and again after the server
    /// was updated. An older answer arriving after a newer request is dropped.
    public func checkCompatibility() async {
        compatibilityGeneration += 1
        let generation = compatibilityGeneration
        let result: Result<ServerVersion, APIError>
        do {
            result = .success(try await client.version())
        } catch {
            guard let error = APIError.from(error) else { return }
            if error.isUnpaired { report(error) }
            result = .failure(error)
        }
        guard generation == compatibilityGeneration else { return }
        compatibility = ServerCompatibility.verdict(result)
    }

    /// Poll the list while it is on screen (§3: "every few seconds"), and the threads of each instance whose row moved.
    public func start(every interval: Duration = .seconds(4)) {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self, sleep] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                await self.refreshMovedThreads()
                do { try await sleep(interval) } catch { return }
            }
        }
    }

    public func stop() {
        services.stop()
        pollTask?.cancel()
        pollTask = nil
    }

    public func row(_ name: String) -> InstanceRow? {
        instances.first { $0.name == name }
    }

    /// The store of `name`'s conversation `ref` (one per conversation, kept while the app runs); `.current` is the one
    /// chat of a server before threads. An earlier chat's is `archivedChat`.
    public func chat(_ name: String, _ ref: ChatRef = .current) -> ChatStore {
        let key = ChatKey(instance: name, ref: ref)
        if let store = chats[key] { return store }
        let store = ChatStore(instance: name, ref: ref, owner: self)
        if !ref.isEarlier { chats[key] = store }
        return store
    }

    /// One settings store per instance, shared by every screen in this session.
    public func settings(_ name: String) -> InstanceSettingsStore {
        if let store = settingsStores[name] { return store }
        let store = InstanceSettingsStore(instance: name, owner: self)
        settingsStores[name] = store
        return store
    }

    /// A store for an earlier chat (`cid`), not kept.
    public func archivedChat(_ name: String, cid: String) -> ChatStore {
        ChatStore(instance: name, ref: .earlier(cid), owner: self)
    }

    // MARK: Threads (§3.1)

    /// What the app knows of an instance's threads.
    public enum ThreadsState: Equatable, Sendable {
        /// Not read yet (or every read failed): the app shows the current chat meanwhile.
        case unknown
        /// The server answered `404` for `/threads`: a server before threads, one chat per instance, as before.
        case unsupported
        case loaded(ThreadsResponse)
    }

    public func threadsState(_ name: String) -> ThreadsState { threadStates[name] ?? .unknown }

    /// The instance's open threads and From feed, once read; nil before, and from a server before threads.
    public func threads(_ name: String) -> ThreadsResponse? {
        if case .loaded(let answer) = threadsState(name) { return answer }
        return nil
    }

    /// An untargeted navigation opens Main. Only a confirmed server before threads uses the legacy current chat.
    public func defaultRef(_ name: String) -> ChatRef {
        threadsState(name) == .unsupported ? .current : .from
    }

    /// A named thread that is no longer open retains its identity as a read-only earlier chat.
    public func resolve(_ name: String, _ ref: ChatRef?) -> ChatRef {
        guard let ref else { return defaultRef(name) }
        switch (ref, threadsState(name)) {
        case (.earlier(let cid), .loaded(let answer)):
            return answer.threads.contains { $0.id == cid } ? .thread(cid) : ref
        case (.earlier, _): return ref
        case (.current, .unsupported): return .current
        case (.current, _): return .from
        case (.thread, .unsupported), (.from, .unsupported): return .current
        case (.thread(let cid), .loaded(let answer)):
            let open = answer.threads.contains { $0.id == cid } || createdThreads[name]?.contains(cid) == true
            return open ? ref : .earlier(cid)
        case (.thread, .unknown), (.from, _): return ref
        }
    }

    /// Detect support and refresh the ordered list. Historical data adoption runs separately, once per instance.
    public func refreshThreads(_ name: String) async {
        guard !moving.contains(name) else { return }
        let generation = (threadsGeneration[name] ?? 0) + 1
        threadsGeneration[name] = generation
        let row = row(name)
        do {
            let answer = try await client.threads(name)
            guard threadsGeneration[name] == generation else { return }
            migrateHistoricalCurrent(name, answer: answer)
            createdThreads[name] = nil
            threadStates[name] = .loaded(answer)
            threadsError[name] = nil
            threadsReadAt[name] = row
        } catch {
            guard let apiError = APIError.from(error), threadsGeneration[name] == generation else { return }
            if apiError.status == 404, apiError.code == "not_found", row != nil, threads(name) == nil {
                restoreLegacyCurrent(name)
                threadStates[name] = .unsupported
                threadsError[name] = nil
                threadsReadAt[name] = row
            } else {
                threadsError[name] = apiError
                if apiError.isUnpaired { report(apiError) }
            }
        }
    }

    /// Read the threads of each instance whose rail row moved since its threads were read (a reply, a new line, a
    /// thread more or less), or that were never read; `all`: of every instance (pull to refresh). A server before
    /// threads is asked once per instance.
    public func refreshMovedThreads(all: Bool = false) async {
        // Each instance on its own, all at once: one after another, a phone paid two round trips per instance in turn.
        await withTaskGroup(of: Void.self) { group in
            for row in instances {
                group.addTask { await self.refreshMoved(row, all: all) }
            }
        }
    }

    private func refreshMoved(_ row: InstanceRow, all: Bool) async {
        await refreshNotes(row.name)
        if threadsState(row.name) == .unsupported { return }
        if !all, threadsReadAt[row.name] == row, threadsError[row.name] == nil { return }
        await refreshThreads(row.name)
    }

    /// Something moved in one of `name`'s conversations (a send, a reply, a thread ended or renamed): read its threads
    /// again, when it has them.
    func threadsMoved(_ name: String) {
        guard case .loaded = threadsState(name) else { return }
        Task { await refreshThreads(name) }
    }

    /// Start a thread (§7.7): it ends no other. Returns its identity even if the list refresh fails; nil when refused,
    /// or while one is already being started for `name` (a second tap). `opened` gets it as soon as the server made it,
    /// before the list is read again: the screen shows the thread after one round trip, not two (the read stays here,
    /// awaited, and `createdThreads` keeps the thread open until it lands).
    @discardableResult
    public func newThread(_ name: String, title: String? = nil,
                          opened: @MainActor (ChatRef) -> Void = { _ in }) async -> ChatRef? {
        guard !creatingThread.contains(name) else { return nil }
        TransitionClock.begin("new thread")
        creatingThread.insert(name)
        do {
            let answer = try await client.newThread(name, title: title)
            creatingThread.remove(name)
            createdThreads[name, default: []].insert(answer.chat.id)
            threadsGeneration[name, default: 0] += 1
            let ref = ChatRef.thread(answer.chat.id)
            opened(ref)
            await refreshThreads(name)
            return ref
        } catch {
            creatingThread.remove(name)
            TransitionClock.cancel()
            guard let apiError = APIError.from(error) else { return nil }
            report(apiError)
            return nil
        }
    }

    /// Historical untargeted data is adopted once. New writes always have an explicit destination.
    private func migrateHistoricalCurrent(_ name: String, answer: ThreadsResponse) {
        guard !migrated.contains(name) else { return }
        // Historical sends without cid targeted the newest thread. Pin before moving any draft files.
        let destination = answer.newest?.id ?? ChatRef.fromID
        outbox.adoptCurrent(name, into: destination)
        if adoptCurrent(name, into: destination) { migrated.insert(name) }
    }

    public func refreshNotes(_ name: String) async {
        do {
            pendingNotes[name] = try await client.notes(name).notes ?? []
            notesError[name] = nil
        } catch {
            guard let error = APIError.from(error) else { return }
            if error.status == 404 { pendingNotes[name] = []; notesError[name] = nil }
            else { notesError[name] = error; if error.isUnpaired { report(error) } }
        }
    }

    public func cancelNote(_ name: String, note: PendingNote) async {
        guard note.cancellable == true, note.mid != nil || note.file != nil else { return }
        do {
            _ = try await client.uncycle(name, mid: note.mid, file: note.file)
            await refreshNotes(name)
        } catch {
            if let error = APIError.from(error) { notesError[name] = error }
        }
    }

    /// One move per list at a time. In-flight polls cannot overwrite an optimistic order.
    public func moveThread(_ name: String, id: String, after: String?) async {
        guard case .loaded(var answer) = threadsState(name), id != ChatRef.fromID,
              let index = answer.threads.firstIndex(where: { $0.id == id }), !moving.contains(name) else { return }
        let previous = answer
        let item = answer.threads.remove(at: index)
        let target = after.flatMap { anchor in answer.threads.firstIndex { $0.id == anchor }.map { $0 + 1 } } ?? 0
        if id == after { return }
        answer.threads.insert(item, at: target)
        moving.insert(name)
        threadsGeneration[name, default: 0] += 1
        threadStates[name] = .loaded(answer)
        do {
            let response = try await client.moveThread(name, id: id, after: after)
            if let rev = response.rev?.counter, rev >= (orderRevisions[name] ?? 0), let ids = response.ids {
                orderRevisions[name] = rev
                let ordered = ids.compactMap { id in answer.threads.first { $0.id == id } }
                let listed = Set(ids)
                answer.threads = ordered + answer.threads.filter { !listed.contains($0.id) }
                threadStates[name] = .loaded(answer)
            }
            moving.remove(name)
            await refreshThreads(name)
        } catch {
            moving.remove(name)
            threadStates[name] = .loaded(previous)
            if let error = APIError.from(error) { report(error) }
            await refreshThreads(name)
        }
    }

    public func moveInstance(_ name: String, after: String?) async {
        guard let index = instances.firstIndex(where: { $0.name == name }), name != after,
              !moving.contains("instances") else { return }
        let previous = instances
        let item = instances.remove(at: index)
        let target = after.flatMap { anchor in instances.firstIndex { $0.name == anchor }.map { $0 + 1 } } ?? 0
        instances.insert(item, at: target)
        moving.insert("instances")
        listGeneration += 1
        do {
            let response = try await client.moveInstance(name, after: after)
            if let rev = response.rev?.counter, rev >= (orderRevisions["instances"] ?? 0), let ids = response.ids {
                orderRevisions["instances"] = rev
                instances = ids.compactMap { id in instances.first { $0.name == id } }
            }
            moving.remove("instances")
        } catch {
            moving.remove("instances")
            instances = previous
            if let error = APIError.from(error) { report(error) }
        }
        etag = nil
        await refresh()
    }

    /// The current chat's draft and not-sent messages move to the thread `cid`. The current chat's store lets go of
    /// the draft it held (its file is gone): the thread's store reads it from now on, and two stores never write one
    /// draft.
    @discardableResult
    private func adoptCurrent(_ name: String, into cid: String) -> Bool {
        let moved: Bool
        do {
            moved = try drafts.adoptCurrent(name, into: cid)
        } catch {
            report(.transport(code: (error as NSError).code,
                              message: "Could not move \(name)'s draft to its thread: \(error.localizedDescription)"))
            return false
        }
        if moved {
            if let store = chats[ChatKey(instance: name, ref: .current)] { store.draft = Draft() }
            let destination: ChatRef = cid == ChatRef.fromID ? .from : .thread(cid)
            if let store = chats[ChatKey(instance: name, ref: destination)] {
                store.draft = (try? drafts.load(name, cid: cid)) ?? Draft()
            }
        }
        return true
    }

    /// Main's data from before capability detection belongs to the legacy current chat.
    private func restoreLegacyCurrent(_ name: String) {
        outbox.restoreLegacyCurrent(name)
        do {
            let source = chats[ChatKey(instance: name, ref: .from)]
            let moved = try drafts.moveDraft(name, from: ChatRef.fromID, into: nil, draft: source?.draft)
            if moved {
                source?.draft = Draft()
                if let current = chats[ChatKey(instance: name, ref: .current)] {
                    current.draft = try drafts.load(name)
                }
            }
        } catch {
            report(.transport(code: (error as NSError).code,
                              message: "Could not move \(name)'s Main draft: \(error.localizedDescription)"))
        }
    }

    /// Mark `name`'s conversation `cid` (a thread's id, `"from"`, nil for the current chat) seen up to `version`, the
    /// feed version on screen (§7.5). False when the feed moved since (`409 stale_version`: send it again once the new
    /// version is shown) or the request failed.
    @discardableResult
    public func markSeen(_ name: String, version: String, cid: String? = nil) async -> Bool {
        do {
            let answer = try await client.seen(name, version: version, cid: cid)
            seenGeneration += 1
            reportBadge(answer.badge)
            var unread = answer.unread
            if let cid, case .loaded(var threads) = threadsState(name) {
                if cid == ChatRef.fromID {
                    threads.from.unread = false
                } else if let index = threads.threads.firstIndex(where: { $0.id == cid }) {
                    threads.threads[index].unread = false
                }
                threadStates[name] = .loaded(threads)
                // The row is unread while any other thread, or the From feed, is.
                unread = unread || threads.from.unread || threads.threads.contains(where: \.unread)
            }
            if let index = instances.firstIndex(where: { $0.name == name }) {
                instances[index].unread = unread
                if let threads = threads(name) {
                    instances[index].unreadMain = threads.from.unread
                    instances[index].unreadThreads = threads.threads.filter(\.unread).count
                }
            }
            return true
        } catch {
            guard let apiError = APIError.from(error) else { return false }
            if apiError.code != "stale_version" { report(apiError) }
            return false
        }
    }

    private func reportBadge(_ count: Int) {
        badge = count
        badgeKnown = true
        onBadge?(count)
    }

    func report(_ error: APIError) {
        self.error = error
        if error.isUnpaired { onUnpaired?() }
    }
}

/// A conversation's place in the store's cache.
struct ChatKey: Hashable {
    let instance: String
    let ref: ChatRef
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
    /// The conversation: the current chat, an open thread, the From feed, or an earlier chat (read-only).
    public let ref: ChatRef
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

    init(instance: String, ref: ChatRef = .current, owner: InstancesStore) {
        self.instance = instance
        self.ref = ref
        self.owner = owner
        draft = ref.isEarlier ? Draft() : (try? owner.drafts.load(instance, cid: ref.draftKey)) ?? Draft()
    }

    var client: APIClient? { owner?.client }

    /// An earlier chat: read-only, with no draft, no unsent messages, no seen marks.
    public var isEarlier: Bool { ref.isEarlier }

    /// The chat id actions name (§7): `"from"`, the thread's own, or the current chat's from the feed.
    public var chatID: String? { ref.seenID ?? feed?.content?.chat?.id }

    /// Messages of this conversation that are on their way or did not get there, oldest first. Only its own: another
    /// thread's never show here.
    public var unsent: [OutboxEntry] {
        guard !isEarlier else { return [] }
        return owner?.outbox.entries(for: instance, cid: ref.draftKey) ?? []
    }

    /// Structured answers stay on their cards while sending, and retain their body for Retry.
    public let questionAnswers = QuestionAnswers()

    public func answerQuestion(file: String, picked: [Int], other: String?) async {
        guard let client, let chatID else { return }
        await questionAnswers.send(file: file, cid: chatID, picked: picked, other: other,
                                   instance: instance, client: client)
    }

    public func retryQuestion(file: String) async {
        guard let client else { return }
        await questionAnswers.retry(file: file, instance: instance, client: client)
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
        let poll = FeedPoll(client: client, instance: instance, ref: ref, all: showingAll,
                            watching: { flag.isOn })
        pollTask = Task { [weak self] in
            do {
                for try await event in poll {
                    guard let self else { return }
                    self.apply(event)
                }
            } catch {
                guard let error = APIError.from(error) else { return }
                self?.halt(error)
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
            let moved = self.feed != nil && self.feed?.version != feed.version
            self.feed = feed
            connection = .live
            if let mids = feed.content?.mids, !isEarlier {
                owner?.outbox.reconcile(instance: instance, mids: mids)
            }
            // A turn, a title from the first message, a reply starting or ending: the thread list says so too.
            if moved, !isEarlier { owner?.threadsMoved(instance) }
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
    /// clears at once and a failed send stays in the chat as not sent. Successful sends leave no toast because
    /// the message's row in the timeline already shows it was sent.
    @discardableResult
    public func send(mode: SendMode = .now) async -> Result<SendResponse, APIError> {
        guard let owner, !isEarlier, canWrite else { return .failure(.notPaired) }
        let message = OutgoingMessage(text: draft.text, mode: mode, quotes: draft.quotes,
                                      files: owner.drafts.outgoingFiles(draft, for: instance, cid: ref.draftKey),
                                      cid: ref.draftKey)
        do {
            try owner.outbox.enqueue(message, to: instance)
        } catch {
            return finish(.failure(error))
        }
        draft = Draft()
        let result = finish(await owner.outbox.deliver(message.mid), inRow: true)
        if case .success = result { owner.threadsMoved(instance) }
        return result
    }

    /// Retry a failed reply (§4.4 `failure.retry`): its text, with `retry_of` when the server gave one.
    @discardableResult
    public func retryReply(_ retry: FailureRetry) async -> Result<SendResponse, APIError> {
        guard let owner, canWrite else { return .failure(.notPaired) }
        let message = OutgoingMessage(text: retry.text, mode: .now, retryOf: retry.retryOf, cid: ref.draftKey)
        return finish(await owner.outbox.send(message, to: instance), inRow: true)
    }

    /// Send a not-sent message again with its `mid`.
    @discardableResult
    public func retry(_ mid: String) async -> Result<SendResponse, APIError>? {
        guard canWrite else { return nil }
        guard let result = await owner?.outbox.retry(mid) else { return nil }
        return finish(result, inRow: true)
    }

    /// Put a not-sent message back in the composer to change it: it leaves the outbox, and sending it again makes a
    /// new message (a new `mid`, PLAN §2.3). Its files come back as the draft's.
    public func editUnsent(_ mid: String) {
        guard canWrite, let owner, let entry = owner.outbox.entries.first(where: { $0.mid == mid && $0.state == .notSent })
        else { return }
        var restored = draft
        restored.text = [restored.text, entry.text].filter { !$0.isEmpty }.joined(separator: "\n\n")
        restored.quotes += entry.quotes
        for file in entry.files {
            if let copy = try? owner.drafts.attach(owner.outbox.fileURL(of: file, in: entry), name: file.name,
                                                   for: instance, cid: ref.draftKey) {
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
        guard let owner, canWrite else { return false }
        do {
            let file = try owner.drafts.attach(url, name: name, for: instance, cid: ref.draftKey)
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
        guard let owner, canWrite else { return false }
        do {
            draft.attachments.append(try owner.drafts.attach(data: data, name: name, for: instance, cid: ref.draftKey))
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
        owner?.drafts.fileURL(of: attachment, for: instance, cid: ref.draftKey)
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
        } else if let k, let cid = ref.cid ?? feed?.content?.chat?.id {
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
            guard let error = APIError.from(error) else { return nil }
            actionError = error
            owner?.report(error)
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
        let cid = ref.seenID
        await act { try await $0.unqueue(name, mid: mid, cid: cid) }
    }

    public func uncycle(_ item: LaterItem) async {
        let name = instance
        await act { try await $0.uncycle(name, mid: item.mid, file: item.file) }
    }

    public func stopReply() async {
        guard let cid = chatID else { return }
        let name = instance
        await act { try await $0.stop(name, cid: cid) }
    }

    /// End this conversation (§7.3): on a server with threads, this thread only.
    public func end() async {
        guard let cid = chatID else { return }
        let name = instance
        await act { try await $0.end(name, cid: cid) }
        owner?.threadsMoved(instance)
    }

    /// New chat (§7.4): a server before threads ends the open chat and starts another. With threads it is
    /// `InstancesStore.newThread`, which ends nothing.
    public func clear() async {
        guard let cid = chatID else { return }
        let name = instance
        await act { try await $0.clear(name, cid: cid) }
    }

    /// Rename this thread (§7.8). False when refused (a blank title, an ended thread): `actionError` says why.
    @discardableResult
    public func rename(_ title: String) async -> Bool {
        guard case .thread(let cid) = ref, let owner else { return false }
        let name = instance
        guard await act({ try await $0.rename(name, cid: cid, title: title) }) else { return false }
        await owner.refreshThreads(instance)
        return true
    }

    /// The thread's title: the list's (current after a rename), else the feed's, else nil (a server before threads).
    public var title: String? {
        if case .thread(let cid) = ref, let row = owner?.threads(instance)?.threads.first(where: { $0.id == cid }) {
            return row.title
        }
        return feed?.content?.chat?.title
    }

    /// The feed's end is on screen in a window that shows the chat: mark the version shown seen (§7.5).
    @discardableResult
    public func markSeen() async -> Bool {
        guard !isEarlier, let version = feed?.version, let owner else { return false }
        return await owner.markSeen(instance, version: version, cid: ref.seenID)
    }

    /// Retry seen with the current feed version, backing off until it succeeds or the caller cancels.
    public func markSeenUntilDone() async -> Bool {
        var delay = 2
        while !Task.isCancelled {
            guard !isEarlier, feed != nil, let sleep = owner?.sleep else { return false }
            if await markSeen() { return !Task.isCancelled }
            guard !Task.isCancelled, feed != nil, owner != nil else { return false }
            do { try await sleep(.seconds(delay)) } catch { return false }
            delay = min(delay * 2, 30)
        }
        return false
    }

    /// The chat left the screen (§7.6): pushes for it reach this device again at once.
    public func away() async {
        guard !isEarlier, let client else { return }
        _ = try? await client.away(instance, cid: ref.seenID)
    }

    @discardableResult
    private func act(_ action: @escaping @Sendable (APIClient) async throws -> SaidResponse) async -> Bool {
        guard let client else { return false }
        do {
            said = try await action(client).said
            actionError = nil
            reconnectIfWaiting()
            return true
        } catch {
            guard let error = APIError.from(error) else { return false }
            actionError = error
            owner?.report(error)
            return false
        }
    }

    /// The server answered an action while the feed loop waits out a backoff: ask for the feed now rather than
    /// show "offline" over a chat that is reachable.
    public func reconnectIfWaiting() {
        guard pollTask != nil, case .retrying = connection else { return }
        stop()
        start()
    }

    /// Successful sends and retries clear the toast because the message's row in the timeline already shows it was sent.
    /// `inRow`: the message stayed in the chat as not sent, and its row says why: a status line would say it again.
    private func finish(_ result: Result<SendResponse, APIError>, inRow: Bool = false) -> Result<SendResponse, APIError> {
        switch result {
        case .success:
            said = nil
            actionError = nil
            reconnectIfWaiting()
        case .failure(let error):
            actionError = inRow ? nil : error
            if error.isUnpaired { owner?.report(error) }
        }
        return result
    }

    private var canWrite: Bool {
        switch ref {
        case .earlier: false
        case .current: owner?.threadsState(instance) == .unsupported
        case .thread, .from: owner?.threadsState(instance) != .unsupported
        }
    }

    private func saveDraft() {
        guard !isEarlier, canWrite, let owner else { return }
        do {
            try owner.drafts.save(draft, for: instance, cid: ref.draftKey)
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
