import Foundation
import Observation

@MainActor @Observable
public final class ArtifactsStore {
    public private(set) var list: ArtifactsList?
    public private(set) var open: ArtifactPage?
    public private(set) var error: APIError?
    /// Why what shows may be out of date: a refresh that failed, or the open artifact gone from the list. Never
    /// silent: the open page stays, with this above it.
    public private(set) var stale: String?

    private let client: APIClient
    private let instance: String
    @ObservationIgnored private var requestedSlug: String?

    public init(client: APIClient, instance: String) {
        self.client = client
        self.instance = instance
    }

    public func refresh() async {
        do {
            try await refreshList()
            if requestedSlug == nil { error = nil }
        } catch {
            if requestedSlug == nil, let apiError = APIError.from(error) { self.error = apiError }
        }
    }

    public func load(_ slug: String) async {
        if requestedSlug != slug {
            requestedSlug = slug
            open = nil
            error = nil
        }
        do {
            let page = try await client.artifactPage(instance, slug: slug)
            guard requestedSlug == slug, !Task.isCancelled else { return }
            open = page
            error = nil
        } catch {
            if requestedSlug == slug, !Task.isCancelled, let apiError = APIError.from(error) {
                self.error = apiError
            }
        }
    }

    public func close() {
        stale = nil
        requestedSlug = nil
        open = nil
        error = nil
    }

    public func follow() async {
        if list == nil {
            await refresh()
            return
        }
        do {
            try await refreshList()
        } catch {
            if !Task.isCancelled, let apiError = APIError.from(error) { stale = "Not refreshed: \(apiError.message)" }
            return
        }
        guard let page = open else {
            stale = nil
            return
        }
        guard let entry = list?.artifacts.first(where: { $0.slug == page.slug }) else {
            stale = "This artifact is not there any more: what shows is its last version."
            return
        }
        stale = nil
        if entry.version != page.version { await load(page.slug) }
    }

    private func refreshList() async throws {
        let updated = try await client.artifacts(instance)
        try Task.checkCancellation()
        if list != updated { list = updated }
    }
}
