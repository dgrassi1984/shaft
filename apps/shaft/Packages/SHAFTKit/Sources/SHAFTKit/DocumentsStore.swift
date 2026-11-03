import Foundation
import Observation

/// An instance's documents and their comment threads (§10): the documents sheet reads this. Every write reads the open
/// document again afterwards (§10.6), so the threads and the sha on screen are the server's.
@MainActor
@Observable
public final class DocumentsStore {
    /// A save the server refused because the document changed since it was read (`409 stale`, §10.4): what it has now
    /// (`sha`, nil when creating over an existing one) and what it has that the app's text has not (`diff`).
    public struct Conflict: Sendable, Equatable {
        public var path: String
        public var text: String
        public var sha: String?
        public var diff: String?
        public var message: String
    }

    public enum SaveOutcome: Sendable, Equatable {
        case saved(DocSaveResponse)
        case stale(Conflict)
        case refused(APIError)
    }

    public let instance: String
    /// The chat the sheet is over (`in_chat` marks are about it); nil for the current one.
    public let cid: String?
    public private(set) var list: DocsList?
    public private(set) var open: DocumentDetail?
    public private(set) var loading = false
    /// The last answer (`said`) of a write, for the status line.
    public private(set) var said: String?
    /// The last failure: a refusal's message, the network's.
    public private(set) var error: APIError?

    @ObservationIgnored private let client: APIClient

    public init(client: APIClient, instance: String, cid: String? = nil) {
        self.client = client
        self.instance = instance
        self.cid = cid
    }

    // MARK: Reading

    public func refresh() async {
        await run { [self] in list = try await client.docs(instance, cid: cid) }
    }

    /// Open a document (§10.2). A document over the size limit answers `409 too_big`: `error` says so and the sheet
    /// offers its download.
    public func load(_ path: String) async {
        await run { [self] in open = try await client.doc(instance, path: path, cid: cid) }
    }

    public func close() {
        open = nil
    }

    /// The document's bytes whatever its size (§10.3), for Share or a document too big to show.
    public func download(_ path: String) async -> DownloadedFile? {
        do {
            error = nil
            return try await client.rawDoc(instance, path: path)
        } catch {
            self.error = APIError.from(error)
            return nil
        }
    }

    // MARK: Writing (§10.4, §10.5)

    /// Save `text` over the version read (`sha`), or create a document with `sha` `"none"`. A stale save comes back
    /// as `.stale`: save again with its `sha` to replace the newer version knowingly, or take that version.
    public func save(path: String, text: String, sha: String, dir: String? = nil) async -> SaveOutcome {
        do {
            let answer = try await client.saveDoc(instance, path: path, text: text, sha: sha, dir: dir)
            said = answer.said
            error = nil
            await reload(answer.path)
            await refreshQuietly()
            return .saved(answer)
        } catch {
            let apiError = APIError.from(error)
            if case .server(_, let body) = apiError, body.code == "stale" {
                return .stale(Conflict(path: path, text: text, sha: body.sha, diff: body.diff, message: body.message))
            }
            self.error = apiError
            return .refused(apiError)
        }
    }

    /// Delete a document as read (`sha`). True when it is gone; a stale sha or a refusal leaves `error` set.
    @discardableResult
    public func delete(path: String, sha: String) async -> Bool {
        let ok = await run { [self] in said = try await client.deleteDoc(instance, path: path, sha: sha).said }
        if ok {
            if open?.path == path { open = nil }
            await refreshQuietly()
        }
        return ok
    }

    // MARK: Comments (§10.6)

    /// How a post goes: kept unsent (`""`), now (a chat message) or for the next cycle.
    public enum Delivery: String, Sendable, CaseIterable {
        case keep = ""
        case now
        case cycle
    }

    /// A new thread on the open document (`anchor` nil: on the whole document).
    @discardableResult
    public func comment(anchor: CommentAnchor?, text: String, send: Delivery,
                        pid: String = APIClient.newPid()) async -> Bool {
        guard let doc = open else { return false }
        return await write { [self] in
            try await client.comment(instance, pid: pid, doc: doc.path, anchor: anchor, sha: doc.sha, text: text,
                                     send: send.rawValue).said
        }
    }

    @discardableResult
    public func reply(thread: String, text: String, send: Delivery, pid: String = APIClient.newPid()) async -> Bool {
        await write { [self] in
            try await client.reply(instance, pid: pid, thread: thread, text: text, send: send.rawValue).said
        }
    }

    @discardableResult
    public func edit(pid: String, text: String) async -> Bool {
        await write { [self] in try await client.editComment(instance, pid: pid, text: text).said }
    }

    @discardableResult
    public func deletePost(pid: String) async -> Bool {
        await write { [self] in try await client.deleteComment(instance, pid: pid).said }
    }

    /// Send a stored post: `.now` or `.cycle`.
    @discardableResult
    public func sendPost(pid: String, when: Delivery) async -> Bool {
        await write { [self] in try await client.sendComment(instance, pid: pid, when: when.rawValue).said }
    }

    @discardableResult
    public func setState(thread: String, resolved: Bool) async -> Bool {
        await write { [self] in
            try await client.setThreadState(instance, thread: thread, state: resolved ? "resolved" : "open").said
        }
    }

    // MARK: Plumbing

    private func write(_ action: @escaping @MainActor () async throws -> String) async -> Bool {
        let ok = await run { [self] in said = try await action() }
        if let path = open?.path { await reload(path) }
        await refreshQuietly()
        return ok
    }

    private func reload(_ path: String) async {
        if let detail = try? await client.doc(instance, path: path, cid: cid) { open = detail }
    }

    private func refreshQuietly() async {
        if let list = try? await client.docs(instance, cid: cid) { self.list = list }
    }

    @discardableResult
    private func run(_ body: @MainActor () async throws -> Void) async -> Bool {
        loading = true
        defer { loading = false }
        do {
            try await body()
            error = nil
            return true
        } catch is CancellationError {
            return false
        } catch {
            self.error = APIError.from(error)
            return false
        }
    }
}

/// The documents list as the sheet shows it (the web's `docs_panel`): one folder level at a time, folders first, each
/// with what it holds added up, in the chosen order.
public enum DocsTree {
    public enum Sort: String, Sendable, CaseIterable {
        case name, size, modified

        /// The direction a head's first click gives: A→Z, largest, newest.
        public var firstAscending: Bool { self == .name }
    }

    public struct Folder: Sendable, Equatable, Identifiable {
        /// Relative to `notes/docs/`, no trailing slash.
        public var path: String
        public var name: String
        public var count: Int
        public var bytes: Int
        public var latest: Date?
        public var inChat: Bool
        public var openComments: Int

        public var id: String { path }
    }

    public struct Level: Sendable, Equatable {
        public var folders: [Folder]
        public var docs: [DocEntry]
    }

    /// The entries directly in `folder` (`""`: the top), folders first. `ascending`: A→Z, smallest first, oldest first;
    /// the web's default is newest first, and a head's first click gives A→Z, largest or newest (`Sort.firstAscending`).
    public static func level(_ docs: [DocEntry], in folder: String, sort: Sort = .modified,
                             ascending: Bool = false) -> Level {
        let prefix = folder.isEmpty ? "" : folder + "/"
        var folders: [String: Folder] = [:]
        var here: [DocEntry] = []
        for doc in docs where doc.path.hasPrefix(prefix) {
            let rest = doc.path.dropFirst(prefix.count)
            if let slash = rest.firstIndex(of: "/") {
                let name = String(rest[..<slash])
                var entry = folders[name] ?? Folder(path: prefix + name, name: name, count: 0, bytes: 0, latest: nil,
                                                    inChat: false, openComments: 0)
                entry.count += 1
                entry.bytes += doc.bytes
                entry.latest = max(entry.latest ?? doc.mtime, doc.mtime)
                entry.inChat = entry.inChat || doc.inChat
                entry.openComments += doc.openComments
                folders[name] = entry
            } else {
                here.append(doc)
            }
        }
        func ordered<T>(_ items: [T], name: (T) -> String, size: (T) -> Int, date: (T) -> Date?) -> [T] {
            items.sorted { a, b in
                switch sort {
                case .name:
                    let order = name(a).localizedStandardCompare(name(b))
                    return ascending ? order == .orderedAscending : order == .orderedDescending
                case .size:
                    return ascending ? size(a) < size(b) : size(a) > size(b)
                case .modified:
                    let (x, y) = (date(a) ?? .distantPast, date(b) ?? .distantPast)
                    return ascending ? x < y : x > y
                }
            }
        }
        return Level(folders: ordered(Array(folders.values), name: \.name, size: \.bytes, date: \.latest),
                     docs: ordered(here, name: { ($0.path as NSString).lastPathComponent }, size: \.bytes,
                                   date: { $0.mtime }))
    }

    /// A size as the web shows it: `812 B`, `1.5 KB`, `2.3 MB`.
    public static func size(_ bytes: Int) -> String {
        if bytes < 1024 { return "\(bytes) B" }
        if bytes < 1024 * 1024 { return String(format: "%.1f KB", Double(bytes) / 1024) }
        return String(format: "%.1f MB", Double(bytes) / 1024 / 1024)
    }
}

/// A comment's anchor in a document's rendered text (§10.2): the selected text, up to 32 characters either side, and
/// its offset in UTF-16 code units.
public enum Anchoring {
    public static func anchor(in rendered: String, range: NSRange) -> CommentAnchor? {
        let text = rendered as NSString
        guard range.length > 0, range.location >= 0, NSMaxRange(range) <= text.length else { return nil }
        let exact = text.substring(with: range)
        guard !exact.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        let before = max(0, range.location - 32)
        let after = min(text.length, NSMaxRange(range) + 32)
        return CommentAnchor(exact: String(exact.prefix(2000)),
                             prefix: text.substring(with: NSRange(location: before, length: range.location - before)),
                             suffix: text.substring(with: NSRange(location: NSMaxRange(range),
                                                                  length: after - NSMaxRange(range))),
                             start: range.location)
    }

    /// Where an anchor sits in `rendered` now: its `exact` text, preferring the occurrence with its prefix and suffix,
    /// then the one nearest `start`. Nil when the text is no longer in the document.
    public static func locate(_ anchor: CommentAnchor, in rendered: String) -> NSRange? {
        let text = rendered as NSString
        var found: [NSRange] = []
        var from = 0
        while from < text.length {
            let range = text.range(of: anchor.exact, options: [], range: NSRange(location: from, length: text.length - from))
            if range.location == NSNotFound { break }
            found.append(range)
            from = range.location + max(range.length, 1)
        }
        guard !found.isEmpty else { return nil }
        func fits(_ r: NSRange) -> Bool {
            let pre = text.substring(to: r.location)
            let post = text.substring(from: NSMaxRange(r))
            return pre.hasSuffix(anchor.prefix) && post.hasPrefix(anchor.suffix)
        }
        if let exact = found.first(where: fits) { return exact }
        return found.min { abs($0.location - anchor.start) < abs($1.location - anchor.start) }
    }
}
