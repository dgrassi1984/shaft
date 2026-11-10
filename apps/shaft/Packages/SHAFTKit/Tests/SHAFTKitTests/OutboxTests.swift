import Foundation
import Testing
@testable import SHAFTKit

/// A sender that records what it was asked to send and fails while `down` is set.
final class RecordingSender: MessageSending, @unchecked Sendable {
    private let lock = NSLock()
    private var sentLog: [(instance: String, message: OutgoingMessage, fileBytes: [Data])] = []
    private var isDown: Bool

    init(down: Bool = false) { isDown = down }

    var down: Bool {
        get { lock.withLock { isDown } }
        set { lock.withLock { isDown = newValue } }
    }

    var sent: [(instance: String, message: OutgoingMessage, fileBytes: [Data])] { lock.withLock { sentLog } }

    func send(_ instance: String, _ message: OutgoingMessage) async throws -> SendResponse {
        let bytes = message.files.map { (try? Data(contentsOf: $0.fileURL)) ?? Data() }
        let down = lock.withLock {
            sentLog.append((instance, message, bytes))
            return isDown
        }
        if down { throw APIError.transport(code: NSURLErrorNotConnectedToInternet, message: "The Internet connection appears to be offline.") }
        return SendResponse(said: "sent to \(instance)", chat: "web-1", file: nil)
    }
}

func temporaryFolder(_ label: String) throws -> URL {
    let folder = FileManager.default.temporaryDirectory.appending(path: "shaft-\(label)-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    return folder
}

@MainActor
struct OutboxTests {
    @Test func aFailedSendStaysNotSentWithItsFilesAndMid() async throws {
        let folder = try temporaryFolder("outbox")
        defer { try? FileManager.default.removeItem(at: folder) }
        let picked = folder.appending(path: "picked.txt")
        try Data("totals".utf8).write(to: picked)

        let sender = RecordingSender(down: true)
        let outbox = try Outbox(directory: folder.appending(path: "outbox"), sender: sender)
        let message = OutgoingMessage(text: "And send me the totals.", mode: .queue,
                                      files: [OutgoingFile(name: "totals.txt", mimeType: "text/plain", fileURL: picked)])
        let result = await outbox.send(message, to: "tundra")
        guard case .failure(let error) = result else { Issue.record("the send did not fail"); return }
        #expect(error.isRetryable)

        let entry = try #require(outbox.entries(for: "tundra").first)
        #expect(entry.mid == message.mid)
        #expect(entry.state == .notSent)
        #expect(entry.reason == "The Internet connection appears to be offline.")
        // The file is the outbox's own copy: the picked file can go and Retry still sends it.
        try FileManager.default.removeItem(at: picked)
        let kept = outbox.fileURL(of: try #require(entry.files.first), in: entry)
        #expect(kept.path.hasPrefix(folder.appending(path: "outbox").path))
        #expect(try Data(contentsOf: kept) == Data("totals".utf8))
    }

    @Test func retryKeepsTheMidAndSendsTheKeptFiles() async throws {
        let folder = try temporaryFolder("outbox")
        defer { try? FileManager.default.removeItem(at: folder) }
        let picked = folder.appending(path: "a.png")
        try Data([1, 2, 3]).write(to: picked)
        let sender = RecordingSender(down: true)
        let outbox = try Outbox(directory: folder.appending(path: "outbox"), sender: sender)
        let message = OutgoingMessage(text: "look", files: [OutgoingFile(name: "a.png", mimeType: "image/png", fileURL: picked)])
        await outbox.send(message, to: "tundra")

        sender.down = false
        let result = try #require(await outbox.retry(message.mid))
        guard case .success = result else { Issue.record("the retry failed"); return }
        #expect(sender.sent.map(\.message.mid) == [message.mid, message.mid])
        #expect(sender.sent.last?.fileBytes == [Data([1, 2, 3])])
        #expect(outbox.entries.isEmpty)
        // Its files went with it.
        let files = folder.appending(path: "outbox/files/\(message.mid)")
        #expect(!FileManager.default.fileExists(atPath: files.path))
    }

    @Test func editingMakesANewMid() async throws {
        let folder = try temporaryFolder("outbox")
        defer { try? FileManager.default.removeItem(at: folder) }
        let sender = RecordingSender(down: true)
        let outbox = try Outbox(directory: folder, sender: sender)
        let message = OutgoingMessage(text: "Rerun the report", retryOf: 3)
        await outbox.send(message, to: "tundra")

        let newMid = try #require(try outbox.edit(message.mid, text: "Rerun the report for October."))
        #expect(newMid != message.mid)
        let entry = try #require(outbox.entries.first)
        #expect(entry.mid == newMid)
        #expect(entry.text == "Rerun the report for October.")
        #expect(entry.retryOf == nil)
        #expect(entry.state == .notSent)
        #expect(outbox.entries.count == 1)

        sender.down = false
        await outbox.retry(newMid)
        #expect(sender.sent.map(\.message.mid) == [message.mid, newMid])
    }

    @Test func aSendCutShortByQuittingComesBackNotSentAndIsNotReplayed() async throws {
        let folder = try temporaryFolder("outbox")
        defer { try? FileManager.default.removeItem(at: folder) }
        let message = OutgoingMessage(text: "sent as the app quit")
        do {
            // Enqueued (written to disk) but never delivered: the app quit before the request ended.
            let outbox = try Outbox(directory: folder, sender: nil)
            try outbox.enqueue(message, to: "tundra")
            #expect(outbox.entries.first?.state == .sending)
        }
        let sender = RecordingSender()
        let reopened = try Outbox(directory: folder, sender: sender)
        let entry = try #require(reopened.entries.first)
        #expect(entry.mid == message.mid)
        #expect(entry.state == .notSent)
        #expect(entry.reason == "The app closed before the send finished.")
        // Opening the outbox sent nothing: only Retry does.
        try await Task.sleep(for: .milliseconds(50))
        #expect(sender.sent.isEmpty)
    }

    @Test func aMidTheFeedHoldsHasArrived() async throws {
        let folder = try temporaryFolder("outbox")
        defer { try? FileManager.default.removeItem(at: folder) }
        let outbox = try Outbox(directory: folder, sender: RecordingSender(down: true))
        let arrived = OutgoingMessage(text: "the server took this one; its answer was lost")
        let other = OutgoingMessage(text: "this one never got there")
        await outbox.send(arrived, to: "tundra")
        await outbox.send(other, to: "tundra")
        await outbox.send(arrived, to: "wisp")

        outbox.reconcile(instance: "tundra", mids: [arrived.mid, "unrelated"])
        #expect(outbox.entries(for: "tundra").map(\.mid) == [other.mid])
        #expect(outbox.entries(for: "wisp").map(\.mid) == [arrived.mid])
        #expect(try Outbox(directory: folder).entries.count == 2)
    }

    @Test func discardDropsANotSentMessage() async throws {
        let folder = try temporaryFolder("outbox")
        defer { try? FileManager.default.removeItem(at: folder) }
        let outbox = try Outbox(directory: folder, sender: RecordingSender(down: true))
        let message = OutgoingMessage(text: "never mind")
        await outbox.send(message, to: "tundra")
        outbox.discard(message.mid)
        #expect(outbox.entries.isEmpty)
        #expect(try Outbox(directory: folder).entries.isEmpty)
    }
}

struct DraftStoreTests {
    @Test func aDraftSurvivesAQuit() throws {
        let folder = try temporaryFolder("drafts")
        defer { try? FileManager.default.removeItem(at: folder) }
        let picked = folder.appending(path: "q3.csv")
        try Data("a,b\n1,2\n".utf8).write(to: picked)

        let store = DraftStore(directory: folder.appending(path: "drafts"))
        let attachment = try store.attach(picked, for: "tundra")
        let pasted = try store.attach(data: Data([0xFF, 0xD8]), name: "photo.jpg", for: "tundra")
        let draft = Draft(text: "Both, please.",
                          quotes: [Quote(quote: "Per account or overall?", answer: "Per account", doc: "ledger.md"),
                                   Quote(quote: "Should the nightly report keep the per-account lines?")],
                          attachments: [attachment, pasted])
        try store.save(draft, for: "tundra")
        try FileManager.default.removeItem(at: picked)

        // A new store over the same folder: the app after a quit.
        let reopened = DraftStore(directory: folder.appending(path: "drafts"))
        let loaded = try reopened.load("tundra")
        #expect(loaded == draft)
        #expect(attachment.mimeType == "text/csv")
        #expect(pasted.mimeType == "image/jpeg")
        #expect(try Data(contentsOf: reopened.fileURL(of: loaded.attachments[0], for: "tundra")) == Data("a,b\n1,2\n".utf8))
        #expect(reopened.outgoingFiles(loaded, for: "tundra").map(\.name) == ["q3.csv", "photo.jpg"])
        #expect(try reopened.load("wisp") == Draft())
    }

    @Test func removingAnAttachmentDeletesItsFileAndClearingDeletesAll() throws {
        let folder = try temporaryFolder("drafts")
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = DraftStore(directory: folder)
        let first = try store.attach(data: Data("1".utf8), name: "one.txt", for: "tundra")
        let second = try store.attach(data: Data("2".utf8), name: "two.txt", for: "tundra")
        try store.save(Draft(text: "x", attachments: [first, second]), for: "tundra")

        try store.save(Draft(text: "x", attachments: [second]), for: "tundra")
        #expect(!FileManager.default.fileExists(atPath: store.fileURL(of: first, for: "tundra").path))
        #expect(FileManager.default.fileExists(atPath: store.fileURL(of: second, for: "tundra").path))

        try store.clear("tundra")
        #expect(try store.load("tundra").isEmpty)
        #expect(!FileManager.default.fileExists(atPath: store.fileURL(of: second, for: "tundra").path))
        #expect(!FileManager.default.fileExists(atPath: folder.appending(path: "tundra.json").path))
    }

    @Test func aNameThatIsNotAnInstanceIsRefused() throws {
        let folder = try temporaryFolder("drafts")
        defer { try? FileManager.default.removeItem(at: folder) }
        #expect(throws: (any Error).self) { try DraftStore(directory: folder).save(Draft(text: "x"), for: "../escape") }
    }
}
