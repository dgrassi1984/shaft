import Foundation
import Testing
@testable import SHAFTKit

/// Bug: a cancelled request reached the screen as "The operation couldn't be completed. (Swift.CancellationError
/// error 1.)". Work given up on purpose is never a failure to show.
@MainActor
struct CancellationTests {
    @Test func aCancellationIsNoAPIError() {
        #expect(APIError.from(CancellationError()) == nil)
        #expect(APIError.from(URLError(.cancelled)) == nil)
        #expect(APIError.from(URLError(.timedOut)) == .transport(code: URLError.Code.timedOut.rawValue,
                                                                 message: URLError(.timedOut).localizedDescription))
        #expect(APIError.from(APIError.notPaired) == .notPaired)
    }

    /// A server that holds every answer until the test ends, and says when the first request arrived.
    private func heldServer() -> (StubServer, AsyncStream<Void>, () -> Void) {
        let began = AsyncStream<Void>.makeStream()
        let release = AsyncStream<Void>.makeStream()
        var held = StubServer.Response.json(200, #"{"said": "done"}"#)
        held.beforeReply = {
            began.continuation.yield(())
            for await _ in release.stream { break }
        }
        let reply = held
        return (StubServer { _ in reply }, began.stream,
                { began.continuation.finish(); release.continuation.finish() })
    }

    @Test func aChatActionCancelledInFlightShowsNothing() async throws {
        let folder = try temporaryFolder("action-cancel")
        defer { try? FileManager.default.removeItem(at: folder) }
        let (server, began, finish) = heldServer()
        defer { finish() }
        let store = InstancesStore(client: server.client,
                                   outbox: try Outbox(directory: folder.appending(path: "outbox")),
                                   drafts: DraftStore(directory: folder.appending(path: "drafts")))
        let chat = store.chat("wisp")
        let task = Task { await chat.unqueue("m-1") }
        for await _ in began { break }
        task.cancel()
        await task.value

        #expect(chat.actionError == nil, "shown in the chat: \(String(describing: chat.actionError?.message))")
        #expect(store.error == nil, "shown on the list: \(String(describing: store.error?.message))")
    }

    @Test func aDocumentSaveCancelledInFlightIsNoRefusal() async throws {
        let (server, began, finish) = heldServer()
        defer { finish() }
        let docs = DocumentsStore(client: server.client, instance: "tundra")
        let task = Task { await docs.save(path: "notes.md", text: "# Notes\n", sha: "none") }
        for await _ in began { break }
        task.cancel()

        #expect(await task.value == .cancelled)
        #expect(docs.error == nil, "shown on the documents: \(String(describing: docs.error?.message))")
    }
}
