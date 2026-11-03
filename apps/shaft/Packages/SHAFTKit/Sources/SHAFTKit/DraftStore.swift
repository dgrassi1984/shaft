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

/// A draft per instance that survives a quit (PLAN §1): `<drafts>/<instance>.json`, its files under
/// `<drafts>/files/<instance>/<id>/<name>`. Attaching copies the file in, so the draft never depends on a file the
/// picker lent for a moment.
public struct DraftStore: Sendable {
    public let directory: URL

    public init(directory: URL) {
        self.directory = directory
    }

    public static func standard() throws -> DraftStore {
        DraftStore(directory: try LocalFiles.drafts())
    }

    /// The draft for `instance`; an empty one when there is none.
    public func load(_ instance: String) throws -> Draft {
        try LocalFiles.checkInstance(instance)
        let url = draftURL(instance)
        guard FileManager.default.fileExists(atPath: url.path) else { return Draft() }
        return try SHAFTJSON.decoder().decode(Draft.self, from: Data(contentsOf: url))
    }

    /// Keep `draft` for `instance`; an empty draft removes the file. Files no longer in the draft are deleted.
    public func save(_ draft: Draft, for instance: String) throws {
        try LocalFiles.checkInstance(instance)
        if draft.isEmpty {
            try? FileManager.default.removeItem(at: draftURL(instance))
        } else {
            try LocalFiles.write(try SHAFTJSON.encoder().encode(draft), to: draftURL(instance))
        }
        try prune(instance, keeping: Set(draft.attachments.map(\.id)))
    }

    /// Copy `source` in as an attachment of `instance`'s draft. The caller adds it to the draft and saves.
    public func attach(_ source: URL, name: String? = nil, for instance: String) throws -> DraftAttachment {
        try LocalFiles.checkInstance(instance)
        let id = UUID().uuidString.lowercased()
        let fileName = LocalFiles.safeName(name ?? source.lastPathComponent)
        let copied = try LocalFiles.copy(source, into: filesURL(instance).appending(path: id), name: fileName)
        let bytes = (try? copied.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        return DraftAttachment(id: id, name: fileName, mimeType: MultipartForm.mimeType(for: fileName), bytes: bytes)
    }

    /// Attach bytes the app holds (a pasted image, a photo) as `name`.
    public func attach(data: Data, name: String, for instance: String) throws -> DraftAttachment {
        try LocalFiles.checkInstance(instance)
        let id = UUID().uuidString.lowercased()
        let fileName = LocalFiles.safeName(name)
        try LocalFiles.write(data, to: filesURL(instance).appending(path: id).appending(path: fileName))
        return DraftAttachment(id: id, name: fileName, mimeType: MultipartForm.mimeType(for: fileName), bytes: data.count)
    }

    public func fileURL(of attachment: DraftAttachment, for instance: String) -> URL {
        filesURL(instance).appending(path: attachment.id).appending(path: attachment.name)
    }

    /// The draft's files as a send takes them.
    public func outgoingFiles(_ draft: Draft, for instance: String) -> [OutgoingFile] {
        draft.attachments.map {
            OutgoingFile(name: $0.name, mimeType: $0.mimeType, fileURL: fileURL(of: $0, for: instance))
        }
    }

    /// Drop the draft and its files (after it was handed to the outbox, which keeps its own copies).
    public func clear(_ instance: String) throws {
        try save(Draft(), for: instance)
    }

    private func draftURL(_ instance: String) -> URL {
        directory.appending(path: "\(instance).json", directoryHint: .notDirectory)
    }

    private func filesURL(_ instance: String) -> URL {
        directory.appending(path: "files", directoryHint: .isDirectory).appending(path: instance, directoryHint: .isDirectory)
    }

    private func prune(_ instance: String, keeping ids: Set<String>) throws {
        let folder = filesURL(instance)
        guard let present = try? FileManager.default.contentsOfDirectory(atPath: folder.path) else { return }
        for id in present where !ids.contains(id) {
            try FileManager.default.removeItem(at: folder.appending(path: id))
        }
    }
}
