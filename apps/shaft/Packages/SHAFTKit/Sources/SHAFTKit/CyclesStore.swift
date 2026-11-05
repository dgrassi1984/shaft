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
    public private(set) var detailLoading = false
    public private(set) var detailError: APIError?
    public private(set) var filter = ""
    public private(set) var judgeError = ""

    @ObservationIgnored private let client: APIClient
    @ObservationIgnored private var planETag: String?
    @ObservationIgnored private var pageETag: String?
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var detailGeneration = 0

    public init(client: APIClient, instance: String) {
        self.client = client
        self.instance = instance
    }

    public func refresh() async {
        let requestedGeneration = generation
        let requestedFilter = filter
        let requestedPageETag = pageETag
        await run(generation: requestedGeneration) { [self] in
            let newPlan = try await client.plan(instance, etag: planETag)
            guard generation == requestedGeneration else { return }
            let page = try await client.cycles(instance, filter: requestedFilter, etag: requestedPageETag)
            guard generation == requestedGeneration else { return }
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
        let requestedGeneration = generation
        let requestedFilter = filter
        await run(generation: requestedGeneration) { [self] in
            if case .value(let value, _) = try await client.cycles(instance, before: next, filter: requestedFilter) {
                guard generation == requestedGeneration else { return }
                judgeError = value.judgeError
                days.append(contentsOf: value.days)
                self.next = value.next
            }
        }
    }

    public func setFilter(_ value: String) async {
        generation += 1
        loading = true
        filter = value
        days = []
        next = nil
        pageETag = nil
        await refresh()
    }

    public func load(_ ts: String) async {
        detailGeneration += 1
        let requestedGeneration = detailGeneration
        detailLoading = true
        defer { if detailGeneration == requestedGeneration { detailLoading = false } }
        do {
            let detail = try await client.cycle(instance, ts: ts)
            guard detailGeneration == requestedGeneration else { return }
            open = detail
            detailError = nil
        } catch is CancellationError {
            // Cancellation is not a failure to show on screen.
        } catch {
            guard detailGeneration == requestedGeneration else { return }
            detailError = APIError.from(error)
        }
    }

    private func run(generation requestedGeneration: Int, _ action: @MainActor () async throws -> Void) async {
        loading = true
        defer { if generation == requestedGeneration { loading = false } }
        do {
            try await action()
            guard generation == requestedGeneration else { return }
            error = nil
        } catch is CancellationError {
            // Cancellation is not a failure to show on screen.
        } catch {
            guard generation == requestedGeneration else { return }
            self.error = APIError.from(error)
        }
    }
}
