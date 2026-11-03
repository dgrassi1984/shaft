import Foundation
import Observation

/// What sends a message: `APIClient` in the app, a stub in tests.
public protocol MessageSending: Sendable {
    func send(_ instance: String, _ message: OutgoingMessage) async throws -> SendResponse
}

extension APIClient: MessageSending {}

/// A message on its way, or one that did not get there (PLAN §2.3, "A send that fails").
public struct OutboxEntry: Codable, Sendable, Equatable, Identifiable {
    public enum State: String, Codable, Sendable {
        case sending
        /// Shown in the chat as "not sent", with Retry.
        case notSent
    }

    public var instance: String
    public var mid: String
    public var text: String
    public var mode: SendMode
    public var quotes: [Quote]
    public var retryOf: Int?
    /// The files, copied into the outbox's own storage (`Outbox.fileURL(of:in:)`).
    public var files: [DraftAttachment]
    public var created: Date
    public var state: State
    /// Why it was not sent: the server's message or the network's.
    public var reason: String?

    public var id: String { mid }
}

/// The send queue with `mid` (PLAN §2.3):
///
/// - A message goes into the outbox, its files copied into the app's storage, before it is sent; it leaves when the
///   server took it (or when a feed lists its `mid`, §4.2 `mids`).
/// - A send that fails stays as `.notSent` with its reason. Retry sends it again with the same `mid`, so a send the
///   server did take is not posted twice (§1.3).
/// - Editing the text makes a new `mid`: it is another message.
/// - Nothing is sent again on its own: a send cut short by the app quitting comes back as `.notSent`.
@MainActor
@Observable
public final class Outbox {
    public private(set) var entries: [OutboxEntry] = []
    public let directory: URL
    @ObservationIgnored public var sender: (any MessageSending)?

    public init(directory: URL, sender: (any MessageSending)? = nil) throws {
        self.directory = directory
        self.sender = sender
        let url = directory.appending(path: "outbox.json")
        if FileManager.default.fileExists(atPath: url.path) {
            entries = try SHAFTJSON.decoder().decode([OutboxEntry].self, from: Data(contentsOf: url))
            // A send the app quit during: not replayed, shown as not sent.
            for index in entries.indices where entries[index].state == .sending {
                entries[index].state = .notSent
                entries[index].reason = "The app closed before the send finished."
            }
            try persist()
        }
    }

    public static func standard(sender: (any MessageSending)? = nil) throws -> Outbox {
        try Outbox(directory: try LocalFiles.outbox(), sender: sender)
    }

    public func entries(for instance: String) -> [OutboxEntry] {
        entries.filter { $0.instance == instance }
    }

    public func fileURL(of file: DraftAttachment, in entry: OutboxEntry) -> URL {
        filesURL(entry.mid).appending(path: file.id).appending(path: file.name)
    }

    /// Put a message in the outbox and send it. `files` are copied in first (from the draft, usually), so the draft
    /// can be cleared at once. Returns the server's answer, or the error that left it `.notSent`.
    @discardableResult
    public func send(_ message: OutgoingMessage, to instance: String) async -> Result<SendResponse, APIError> {
        do {
            try enqueue(message, to: instance)
        } catch {
            return .failure(APIError.from(error))
        }
        return await attempt(message.mid)
    }

    /// Put a message in the outbox, its files copied into the outbox's storage, without sending it yet: `deliver`
    /// sends it. The composer enqueues, clears its draft, then delivers.
    public func enqueue(_ message: OutgoingMessage, to instance: String) throws(APIError) {
        var entry = OutboxEntry(instance: instance, mid: message.mid, text: message.text, mode: message.mode,
                                quotes: message.quotes, retryOf: message.retryOf, files: [], created: Date(),
                                state: .sending, reason: nil)
        do {
            try LocalFiles.checkInstance(instance)
            for file in message.files {
                let id = UUID().uuidString.lowercased()
                let copied = try LocalFiles.copy(file.fileURL, into: filesURL(entry.mid).appending(path: id),
                                                 name: file.name)
                let bytes = (try? copied.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
                entry.files.append(DraftAttachment(id: id, name: copied.lastPathComponent, mimeType: file.mimeType,
                                                   bytes: bytes))
            }
        } catch {
            try? FileManager.default.removeItem(at: filesURL(entry.mid))
            throw .transport(code: (error as NSError).code,
                             message: "Could not keep the message's files: \(error.localizedDescription)")
        }
        entries.removeAll { $0.mid == entry.mid }
        entries.append(entry)
        savePersisting()
    }

    /// Send an entry `enqueue` put in the outbox.
    @discardableResult
    public func deliver(_ mid: String) async -> Result<SendResponse, APIError> {
        await attempt(mid)
    }

    /// Send a `.notSent` entry again, with the same `mid`.
    @discardableResult
    public func retry(_ mid: String) async -> Result<SendResponse, APIError>? {
        guard let index = entries.firstIndex(where: { $0.mid == mid }), entries[index].state == .notSent else {
            return nil
        }
        entries[index].state = .sending
        entries[index].reason = nil
        savePersisting()
        return await attempt(mid)
    }

    /// Change a not-sent message's text (and quotes): it becomes a new message with a new `mid`, still not sent.
    /// Returns the new `mid`.
    @discardableResult
    public func edit(_ mid: String, text: String, quotes: [Quote]? = nil) throws -> String? {
        guard let index = entries.firstIndex(where: { $0.mid == mid }), entries[index].state == .notSent else {
            return nil
        }
        var entry = entries[index]
        let newMid = OutgoingMessage.newMid()
        if FileManager.default.fileExists(atPath: filesURL(mid).path) {
            try FileManager.default.moveItem(at: filesURL(mid), to: filesURL(newMid))
        }
        entry.mid = newMid
        entry.text = text
        if let quotes { entry.quotes = quotes }
        // An edited retry is a new message: the failed try stays as it is (§4.4 `retry_of`).
        entry.retryOf = nil
        entry.reason = "Edited: not sent yet."
        entries[index] = entry
        savePersisting()
        return newMid
    }

    /// Drop a not-sent message and its files.
    public func discard(_ mid: String) {
        guard let index = entries.firstIndex(where: { $0.mid == mid }), entries[index].state == .notSent else { return }
        entries.remove(at: index)
        try? FileManager.default.removeItem(at: filesURL(mid))
        savePersisting()
    }

    /// Drop the entries of `instance` whose `mid` the server's feed holds: they arrived, whatever the app saw of the
    /// send (§4.2 `mids`).
    public func reconcile(instance: String, mids: [String]) {
        let arrived = Set(mids)
        let gone = entries.filter { $0.instance == instance && arrived.contains($0.mid) }
        guard !gone.isEmpty else { return }
        entries.removeAll { $0.instance == instance && arrived.contains($0.mid) }
        for entry in gone { try? FileManager.default.removeItem(at: filesURL(entry.mid)) }
        savePersisting()
    }

    /// The last error persisting the outbox hit, nil when the last write worked.
    public private(set) var persistError: String?

    private func attempt(_ mid: String) async -> Result<SendResponse, APIError> {
        guard let entry = entries.first(where: { $0.mid == mid }) else {
            return .failure(.decoding(path: "outbox", message: "the message left the outbox"))
        }
        guard let sender else {
            markNotSent(mid, reason: APIError.notPaired.message)
            return .failure(.notPaired)
        }
        let message = OutgoingMessage(
            mid: entry.mid, text: entry.text, mode: entry.mode, quotes: entry.quotes, retryOf: entry.retryOf,
            files: entry.files.map { OutgoingFile(name: $0.name, mimeType: $0.mimeType, fileURL: fileURL(of: $0, in: entry)) })
        do {
            let answer = try await sender.send(entry.instance, message)
            entries.removeAll { $0.mid == mid }
            try? FileManager.default.removeItem(at: filesURL(mid))
            savePersisting()
            return .success(answer)
        } catch {
            let apiError = error is CancellationError
                ? APIError.transport(code: NSURLErrorCancelled, message: "The send was cancelled.")
                : APIError.from(error)
            markNotSent(mid, reason: apiError.message)
            return .failure(apiError)
        }
    }

    private func markNotSent(_ mid: String, reason: String) {
        guard let index = entries.firstIndex(where: { $0.mid == mid }) else { return }
        entries[index].state = .notSent
        entries[index].reason = reason
        savePersisting()
    }

    private func filesURL(_ mid: String) -> URL {
        directory.appending(path: "files", directoryHint: .isDirectory).appending(path: mid, directoryHint: .isDirectory)
    }

    private func savePersisting() {
        do {
            try persist()
            persistError = nil
        } catch {
            persistError = "Could not save the outbox: \(error.localizedDescription)"
        }
    }

    private func persist() throws {
        try LocalFiles.write(try SHAFTJSON.encoder().encode(entries), to: directory.appending(path: "outbox.json"))
    }
}
