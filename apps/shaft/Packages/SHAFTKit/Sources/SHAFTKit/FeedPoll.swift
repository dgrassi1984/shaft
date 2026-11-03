import Foundation

/// Exponential backoff with jitter for a failing long-poll: the n-th failure in a row waits between half and all of
/// `min(cap, base · 2^(n-1))`, so devices that lost the server together do not come back in step.
public struct Backoff: Sendable {
    public var base: Duration
    public var cap: Duration

    public init(base: Duration = .seconds(1), cap: Duration = .seconds(30)) {
        self.base = base
        self.cap = cap
    }

    /// The wait after `failures` failures in a row (1 for the first); `unit` in [0, 1) is the jitter's draw.
    public func delay(failures: Int, unit: Double) -> Duration {
        let exponent = min(max(failures - 1, 0), 30)
        let full = min(cap, base * (1 << exponent))
        return full * (0.5 + 0.5 * min(max(unit, 0), 1))
    }
}

/// What a feed loop yields: the feed (always full: a partial answer is laid over the one held), or a failure it will
/// recover from by itself after `retryIn`.
public enum FeedEvent: Sendable, Equatable {
    case feed(Feed)
    case failed(APIError, retryIn: Duration)
}

/// The feed of one chat as an `AsyncSequence` (§4): the first request answers at once, then each one long-polls with
/// the `since`/`live` it holds. A `304` asks again at once. A network or server failure yields `.failed` and asks
/// again after a backoff with jitter; a refusal (`401 unpaired`, `404 not_found`, `400`) ends it by throwing.
/// Cancelling the consuming task ends it cleanly, a held request included.
public struct FeedPoll: AsyncSequence, Sendable {
    public typealias Element = FeedEvent

    let client: APIClient
    let instance: String
    let cid: String?
    let all: Bool
    let wait: Int
    let watching: @Sendable () -> Bool
    let backoff: Backoff
    let random: @Sendable () -> Double
    let sleep: @Sendable (Duration) async throws -> Void

    /// - Parameters:
    ///   - cid: an earlier chat; nil for the current one.
    ///   - all: every turn instead of the last 80 (§4.1).
    ///   - wait: how long the server holds each request, 0–25 s.
    ///   - watching: whether the chat is on screen now (`watch`, §4); read at each request.
    public init(client: APIClient, instance: String, cid: String? = nil, all: Bool = false, wait: Int = 25,
                watching: @escaping @Sendable () -> Bool = { true }, backoff: Backoff = Backoff(),
                random: @escaping @Sendable () -> Double = { Double.random(in: 0..<1) },
                sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.client = client
        self.instance = instance
        self.cid = cid
        self.all = all
        self.wait = wait
        self.watching = watching
        self.backoff = backoff
        self.random = random
        self.sleep = sleep
    }

    public func makeAsyncIterator() -> Iterator {
        Iterator(poll: self)
    }

    public struct Iterator: AsyncIteratorProtocol {
        let poll: FeedPoll
        var current: Feed?
        var failures = 0
        var pendingDelay: Duration?

        public mutating func next() async throws -> FeedEvent? {
            if let delay = pendingDelay {
                pendingDelay = nil
                do { try await poll.sleep(delay) } catch { return nil }
            }
            while !Task.isCancelled {
                let answer: Conditional<Feed>
                do {
                    answer = try await poll.client.feed(
                        poll.instance, cid: poll.cid, since: current?.version, live: current?.live,
                        wait: current == nil ? 0 : poll.wait, all: poll.all, watch: poll.watching())
                } catch is CancellationError {
                    return nil
                } catch {
                    let apiError = APIError.from(error)
                    guard apiError.isRetryable else { throw apiError }
                    failures += 1
                    let delay = poll.backoff.delay(failures: failures, unit: poll.random())
                    pendingDelay = delay
                    return .failed(apiError, retryIn: delay)
                }
                failures = 0
                guard case .value(let feed, _) = answer else { continue }
                if feed.partial {
                    guard let held = current else {
                        // A partial answer to a request that sent no revision: the server broke §4.2.
                        throw APIError.decoding(path: "/app/v1/chat/\(poll.instance)",
                                                message: "a partial feed with no full one to lay it over")
                    }
                    current = held.merging(feed)
                } else {
                    current = feed
                }
                return .feed(current!)
            }
            return nil
        }
    }
}
