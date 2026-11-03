import Foundation
import Testing
@testable import SHAFTKit

/// A counter the stub's answers share across requests.
final class Steps: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    private var slept: [Duration] = []

    func next() -> Int { lock.withLock { count += 1; return count } }
    func sleep(_ duration: Duration) { lock.withLock { slept.append(duration) } }
    var sleeps: [Duration] { lock.withLock { slept } }
}

struct FeedPollTests {


    @Test func theLongPollAsksAtOnceThenWaitsAndLaysPartialsOver() async throws {
        let steps = Steps()
        let server = StubServer { request in
            switch steps.next() {
            case 1: return .json(200, fixtureText(APIClientTests.idle))
            case 2: return .notModified
            case 3: return .json(200, fixtureText(APIClientTests.live))
            case 4: return .json(200, fixtureText(APIClientTests.livePartial))
            case 5: return .json(500, #"{"error": {"code": "internal", "message": "/app/v1/chat/thelos failed: OSError: disk"}}"#)
            default: return .json(404, #"{"error": {"code": "not_found", "message": "no instance thelos"}}"#)
            }
        }
        let poll = FeedPoll(client: server.client, instance: "thelos", random: { 0 },
                            sleep: { steps.sleep($0) })
        var events = [FeedEvent]()
        let error = await #expect(throws: APIError.self) {
            for try await event in poll { events.append(event) }
        }
        #expect(error?.code == "not_found")

        // What came out: idle, live, live with the partial laid over, then the 500 as a recoverable failure.
        #expect(events.count == 4)
        guard events.count == 4 else { return }
        guard case .feed(let idle) = events[0], case .feed(let live) = events[1], case .feed(let merged) = events[2],
              case .failed(let failure, let retryIn) = events[3] else {
            Issue.record("unexpected events: \(events)"); return
        }
        #expect(idle.version == "9f2c4e1ab07d3c55")
        #expect(live.version == "41d07be2c95a8f13")
        #expect(merged.partial == false)
        #expect(merged.live == "7b19e4c0d2a65f38")
        #expect(merged.content == live.content)
        #expect(merged.reply?.answer == "October's report is running; so far 214 accounts reconciled.")
        #expect(failure.code == "internal")
        #expect(retryIn == .milliseconds(500))
        #expect(steps.sleeps == [.milliseconds(500)])

        // What went out: the first asks at once with no revision, the rest long-poll with the revision held.
        let requests = server.requests
        #expect(requests.count == 6)
        #expect(requests[0].query("since") == nil)
        #expect(requests[0].query("wait") == "0")
        #expect(requests[1].query("since") == "9f2c4e1ab07d3c55")
        #expect(requests[1].query("live") == "")
        #expect(requests[1].query("wait") == "25")
        #expect(requests[2].query("since") == "9f2c4e1ab07d3c55")
        #expect(requests[3].query("since") == "41d07be2c95a8f13")
        #expect(requests[3].query("live") == "c08e57f2a1b9d364")
        // After a failure the loop asks again with what it held.
        #expect(requests[5].query("since") == "41d07be2c95a8f13")
        #expect(requests[5].query("live") == "7b19e4c0d2a65f38")
        for request in requests {
            #expect(request.path == "/app/v1/chat/thelos")
            #expect(request.query("watch") == "1")
            #expect(request.query("at") != nil)
            #expect(request.header("Authorization") == "Bearer 8c1f4e2a9b07d3e5.s3cret")
        }
    }

    @Test func allAndWatchGoOnEveryRequest() async throws {
        let server = StubServer { _ in .json(200, fixtureText(APIClientTests.idle)) }
        let poll = FeedPoll(client: server.client, instance: "thelos", cid: "web-3f9a1c0d7e2b4a65", all: true,
                            watching: { false })
        var iterator = poll.makeAsyncIterator()
        _ = try await iterator.next()
        let request = try #require(server.requests.first)
        #expect(request.path == "/app/v1/chat/thelos/web-3f9a1c0d7e2b4a65")
        #expect(request.query("all") == "1")
        #expect(request.query("watch") == "0")
    }

    @Test func failuresBackOffAndASuccessResetsThem() async throws {
        let steps = Steps()
        let server = StubServer { _ in
            let step = steps.next()
            if step <= 3 { throw StubServer.NetworkDown() }
            return .json(200, fixtureText(APIClientTests.idle))
        }
        let poll = FeedPoll(client: server.client, instance: "thelos", random: { 0.999_999 }, sleep: { steps.sleep($0) })
        var iterator = poll.makeAsyncIterator()
        for _ in 0..<3 {
            guard case .failed(let error, _) = try await iterator.next() else { Issue.record("not a failure"); return }
            #expect(error.isRetryable)
        }
        guard case .feed = try await iterator.next() else { Issue.record("no feed after the failures"); return }
        let sleeps = steps.sleeps.map { Int(($0 / .milliseconds(1)).rounded()) }
        #expect(sleeps == [1000, 2000, 4000])
    }

    @Test func cancellingTheConsumerEndsTheLoop() async throws {
        let server = StubServer { _ in throw StubServer.NetworkDown() }
        let poll = FeedPoll(client: server.client, instance: "thelos", backoff: Backoff(base: .seconds(60)))
        let task = Task {
            var count = 0
            for try await _ in poll { count += 1 }
            return count
        }
        // Let it fail once and start its 30–60 s backoff, then cancel: it must end now, not after the wait.
        while server.requests.isEmpty { try await Task.sleep(for: .milliseconds(10)) }
        try await Task.sleep(for: .milliseconds(50))
        task.cancel()
        let clock = ContinuousClock()
        let started = clock.now
        let count = try await task.value
        #expect(clock.now - started < .seconds(5))
        #expect(count == 1)
        #expect(server.requests.count == 1)
    }

    @Test func backoffDoublesUpToItsCapWithJitter() {
        let backoff = Backoff(base: .seconds(1), cap: .seconds(30))
        #expect(backoff.delay(failures: 1, unit: 0) == .milliseconds(500))
        #expect(backoff.delay(failures: 1, unit: 1) == .seconds(1))
        #expect(backoff.delay(failures: 3, unit: 0.5) == .seconds(3))
        #expect(backoff.delay(failures: 10, unit: 1) == .seconds(30))
        #expect(backoff.delay(failures: 1000, unit: 0) == .seconds(15))
    }
}

func fixtureText(_ data: Data) -> String { String(decoding: data, as: UTF8.self) }
