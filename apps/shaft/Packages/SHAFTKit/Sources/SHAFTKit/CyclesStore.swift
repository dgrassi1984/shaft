import Foundation
import Observation

/// The read-only plan, day pages and selected cycle (§15).
@MainActor
@Observable
public final class CyclesStore {
    public let instance: String
    public private(set) var plan: CyclePlan?
    public private(set) var days: [CycleDay] = []
    public private(set) var next: String?
    public private(set) var open: CycleDetail?
    public private(set) var loading = false
    public private(set) var error: APIError?
    public private(set) var filter = ""
    public private(set) var judgeError = ""

    @ObservationIgnored private let client: APIClient
    @ObservationIgnored private var planETag: String?
    @ObservationIgnored private var pageETag: String?

    public init(client: APIClient, instance: String) {
        self.client = client
        self.instance = instance
    }

    public func refresh() async {
        await run { [self] in
            let newPlan = try await client.plan(instance, etag: planETag)
            let page = try await client.cycles(instance, filter: filter, etag: pageETag)
            if case .value(let value, let etag) = newPlan {
                plan = value
                planETag = etag
            }
            if case .value(let value, let etag) = page {
                judgeError = value.judgeError
                days = value.days
                next = value.next
                pageETag = etag
            }
        }
    }

    public func loadEarlier() async {
        guard let next else { return }
        let requestedFilter = filter
        await run { [self] in
            if case .value(let value, _) = try await client.cycles(instance, before: next, filter: requestedFilter) {
                guard filter == requestedFilter else { return }
                judgeError = value.judgeError
                days.append(contentsOf: value.days)
                self.next = value.next
            }
        }
    }

    public func setFilter(_ value: String) async {
        loading = true
        filter = value
        days = []
        next = nil
        pageETag = nil
        await refresh()
    }

    public func load(_ ts: String) async {
        await run { [self] in open = try await client.cycle(instance, ts: ts) }
    }

    private func run(_ action: @MainActor () async throws -> Void) async {
        loading = true
        defer { loading = false }
        do {
            try await action()
            error = nil
        } catch is CancellationError {
            // Cancellation is not a failure to show on screen.
        } catch {
            self.error = APIError.from(error)
        }
    }
}
