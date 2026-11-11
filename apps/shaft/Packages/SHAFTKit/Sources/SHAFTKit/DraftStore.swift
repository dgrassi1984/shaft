import Foundation

/// What the composer holds for one instance: the text, the ↩ Reply cards with their answers, and the files attached.
public struct Draft: Codable, Sendable, Equatable {
    public var text: String
    public var quotes: [Quote]
    public var attachments: [DraftAttachment]

    public init(text: String = "", quotes: [Quote] = [], attachments: [DraftAttachment] = []) {
        self.text = text
        self.quotes = quotes
        self.attachments = attachments
    }

    public var isEmpty: Bool { text.isEmpty && quotes.isEmpty && attachments.isEmpty }
}

/// A file attached to a draft: a copy the app owns, at `DraftStore.fileURL(of:for:)`.
public struct DraftAttachment: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var name: String
    public var mimeType: String
    public var bytes: Int
}

/// A draft per conversation that survives a quit (PLAN §1): `<drafts>/<instance>.json` for the current chat (where a
/// build before threads kept it), `<drafts>/<instance>@<cid>.json` for a thread or the From feed (`cid` `"from"`); its
/// files under `<drafts>/files/<instance>/<id>/<name>` or `<drafts>/files/<instance>@<cid>/<id>/<name>`. A draft typed
/// in one thread is never another's: each has its own file and its own folder of files, which only its own save prunes.
/// Attaching copies the file in, so the draft never depends on a file the picker lent for a moment.
public struct DraftStore: Sendable {
    public let directory: URL

    public init(directory: URL) {
        self.directory = directory
    }

    public static func standard() throws -> DraftStore {
        DraftStore(directory: try LocalFiles.drafts())
    }

    /// The draft for `instance`'s conversation `cid` (nil: the current chat); an empty one when there is none.
    public func load(_ instance: String, cid: String? = nil) throws -> Draft {
        let key = try Self.key(instance, cid)
        let url = draftURL(key)
        guard FileManager.default.fileExists(atPath: url.path) else { return Draft() }
        return try SHAFTJSON.decoder().decode(Draft.self, from: Data(contentsOf: url))
    }

    /// Keep `draft` for `instance`'s conversation `cid`; an empty draft removes the file. Files no longer in the draft
    /// are deleted.
    public func save(_ draft: Draft, for instance: String, cid: String? = nil) throws {
        let key = try Self.key(instance, cid)
        if draft.isEmpty {
            try? FileManager.default.removeItem(at: draftURL(key))
        } else {
            try LocalFiles.write(try SHAFTJSON.encoder().encode(draft), to: draftURL(key))
        }
        try prune(key, keeping: Set(draft.attachments.map(\.id)))
    }

    /// Copy `source` in as an attachment of the draft of `instance`'s conversation `cid`. The caller adds it to the
    /// draft and saves.
    public func attach(_ source: URL, name: String? = nil, for instance: String,
                       cid: String? = nil) throws -> DraftAttachment {
        let key = try Self.key(instance, cid)
        let id = UUID().uuidString.lowercased()
        let fileName = LocalFiles.safeName(name ?? source.lastPathComponent)
        let copied = try LocalFiles.copy(source, into: filesURL(key).appending(path: id), name: fileName)
        let bytes = (try? copied.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        return DraftAttachment(id: id, name: fileName, mimeType: MultipartForm.mimeType(for: fileName), bytes: bytes)
    }

    /// Attach bytes the app holds (a pasted image, a photo) as `name`.
    public func attach(data: Data, name: String, for instance: String, cid: String? = nil) throws -> DraftAttachment {
        let key = try Self.key(instance, cid)
        let id = UUID().uuidString.lowercased()
        let fileName = LocalFiles.safeName(name)
        try LocalFiles.write(data, to: filesURL(key).appending(path: id).appending(path: fileName))
        return DraftAttachment(id: id, name: fileName, mimeType: MultipartForm.mimeType(for: fileName), bytes: data.count)
    }

    public func fileURL(of attachment: DraftAttachment, for instance: String, cid: String? = nil) -> URL {
        let key = (try? Self.key(instance, cid)) ?? instance
        return filesURL(key).appending(path: attachment.id).appending(path: attachment.name)
    }

    /// The draft's files as a send takes them.
    public func outgoingFiles(_ draft: Draft, for instance: String, cid: String? = nil) -> [OutgoingFile] {
        draft.attachments.map {
            OutgoingFile(name: $0.name, mimeType: $0.mimeType, fileURL: fileURL(of: $0, for: instance, cid: cid))
        }
    }

    /// Drop the draft and its files (after it was handed to the outbox, which keeps its own copies).
    public func clear(_ instance: String, cid: String? = nil) throws {
        try save(Draft(), for: instance, cid: cid)
    }

    /// Move the historical draft into its thread. Keep both texts, quotes and files when the thread has a draft.
    @discardableResult
    public func adoptCurrent(_ instance: String, into cid: String) throws -> Bool {
        try moveDraft(instance, from: nil, into: cid)
    }

    /// Copy files before saving the merged draft, then remove the source. An open composer may hold newer text.
    @discardableResult
    func moveDraft(_ instance: String, from source: String?, into destination: String?, draft: Draft? = nil) throws -> Bool {
        let from = try Self.key(instance, source)
        let to = try Self.key(instance, destination)
        let original = try draft ?? load(instance, cid: source)
        guard !original.isEmpty else { return false }
        var merged = try load(instance, cid: destination)
        merged.text = [merged.text, original.text].filter { !$0.isEmpty }.joined(separator: "\n\n")
        merged.quotes += original.quotes
        let present = Set(merged.attachments.map(\.id))
        for attachment in original.attachments where !present.contains(attachment.id) {
            let sourceURL = filesURL(from).appending(path: attachment.id).appending(path: attachment.name)
            let targetURL = filesURL(to).appending(path: attachment.id).appending(path: attachment.name)
            try LocalFiles.write(Data(contentsOf: sourceURL), to: targetURL)
            merged.attachments.append(attachment)
        }
        try save(merged, for: instance, cid: destination)
        if FileManager.default.fileExists(atPath: draftURL(from).path) {
            try FileManager.default.removeItem(at: draftURL(from))
        }
        try prune(from, keeping: [])
        return true
    }

    /// The file name of a conversation's draft: the instance, and `@<cid>` for a thread or the From feed. `@` is in no
    /// instance name, so two keys never meet.
    static func key(_ instance: String, _ cid: String?) throws -> String {
        try LocalFiles.checkInstance(instance)
        guard let cid else { return instance }
        try LocalFiles.checkInstance(cid)
        return "\(instance)@\(cid)"
    }

    private func draftURL(_ key: String) -> URL {
        directory.appending(path: "\(key).json", directoryHint: .notDirectory)
    }

    private func filesURL(_ key: String) -> URL {
        directory.appending(path: "files", directoryHint: .isDirectory).appending(path: key, directoryHint: .isDirectory)
    }

    private func prune(_ key: String, keeping ids: Set<String>) throws {
        let folder = filesURL(key)
        guard let present = try? FileManager.default.contentsOfDirectory(atPath: folder.path) else { return }
        for id in present where !ids.contains(id) {
            try FileManager.default.removeItem(at: folder.appending(path: id))
        }
    }
}
