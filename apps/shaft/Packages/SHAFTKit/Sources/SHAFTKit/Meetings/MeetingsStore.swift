import Foundation
import Observation

@MainActor
@Observable
public final class MeetingsStore {
    public private(set) var meetings: [Meeting] = []
    public private(set) var detail: Meeting?
    public private(set) var segments: [MeetingTranscript.Segment] = []
    public private(set) var people: [SpeakerPerson] = []
    /// Pending names instances proposed for speakers, every meeting's.
    public private(set) var proposals: [MeetingSpeakerProposal] = []
    public private(set) var loading = false
    public private(set) var loadingDetail = false
    public private(set) var mutating = false
    public private(set) var listError: String?
    public private(set) var detailError: String?
    public private(set) var peopleError: String?
    public private(set) var actionError: String?

    @ObservationIgnored private let client: APIClient
    @ObservationIgnored private var listGeneration = 0
    @ObservationIgnored private var detailGeneration = 0
    @ObservationIgnored private var peopleGeneration = 0
    @ObservationIgnored private var proposalsGeneration = 0
    @ObservationIgnored private var requestedID: String?

    public init(client: APIClient) {
        self.client = client
    }

    public var needsPolling: Bool {
        meetings.contains { ["uploading", "queued", "processing"].contains($0.state) }
    }

    public func refresh() async {
        listGeneration += 1
        let generation = listGeneration
        loading = true
        defer { if listGeneration == generation { loading = false } }
        do {
            let result = try await client.meetings()
            guard listGeneration == generation, !Task.isCancelled else { return }
            meetings = result
            listError = nil
        } catch {
            guard listGeneration == generation, !Task.isCancelled, let error = APIError.from(error) else { return }
            listError = error.message
        }
    }

    /// Owned by the visible window. A refresh or a local mutation can restart polling when work appears.
    public func poll() async {
        await poll(every: .seconds(10))
    }

    func poll(every interval: Duration) async {
        while needsPolling && !Task.isCancelled {
            do {
                try await Task.sleep(for: interval)
            } catch is CancellationError {
                return
            } catch {
                listError = error.localizedDescription
                return
            }
            guard needsPolling else { return }
            await refresh()
        }
    }

    public func load(_ id: String) async {
        requestedID = id
        detailGeneration += 1
        let generation = detailGeneration
        if detail?.id != id {
            detail = nil
            segments = []
        }
        loadingDetail = true
        detailError = nil
        defer { if detailGeneration == generation { loadingDetail = false } }

        do {
            var result = try await client.meeting(id)
            var rows = result.transcript?.segments ?? []
            if let transcript = result.transcript {
                var page = transcript.page
                while rows.count < transcript.totalSegments {
                    try Task.checkCancellation()
                    guard detailGeneration == generation else { return }
                    page += 1
                    let next = try await client.meeting(id, page: page)
                    guard let nextTranscript = next.transcript else {
                        // The server no longer has a transcript to page through.
                        result = next
                        rows = []
                        break
                    }
                    guard nextTranscript.page == page,
                          nextTranscript.totalSegments == transcript.totalSegments,
                          nextTranscript.speakers == transcript.speakers,
                          !nextTranscript.segments.isEmpty else {
                        throw APIError.decoding(path: "/app/v1/meetings/\(id)",
                                                message: "The transcript changed while it was loading. Refresh to try again.")
                    }
                    rows.append(contentsOf: nextTranscript.segments)
                }
            }
            guard detailGeneration == generation, !Task.isCancelled else { return }
            detail = result
            segments = rows
        } catch {
            guard detailGeneration == generation, !Task.isCancelled, let error = APIError.from(error) else { return }
            detailError = error.message
        }
    }

    public func loadPeople() async {
        peopleGeneration += 1
        let generation = peopleGeneration
        do {
            let result = try await client.speakers()
            guard peopleGeneration == generation, !Task.isCancelled else { return }
            people = result
            peopleError = nil
        } catch {
            guard peopleGeneration == generation, !Task.isCancelled, let error = APIError.from(error) else { return }
            peopleError = error.message
        }
    }

    /// A failed load keeps the last list and says why in `peopleError` (both feed the speakers section).
    public func loadProposals() async {
        proposalsGeneration += 1
        let generation = proposalsGeneration
        do {
            let result = try await client.meetingProposals()
            guard proposalsGeneration == generation, !Task.isCancelled else { return }
            proposals = result
            peopleError = nil
        } catch {
            guard proposalsGeneration == generation, !Task.isCancelled, let error = APIError.from(error) else { return }
            peopleError = error.message
        }
    }

    /// Confirm (enrols the voice under the proposed name) or reject an instance's proposal.
    public func decide(_ proposal: MeetingSpeakerProposal, approve: Bool) async {
        await mutate {
            defer { proposals.removeAll { $0.id == proposal.id } }
            try await client.decideMeetingProposal(proposal.id, approve: approve)
            if approve, requestedID == proposal.meeting { await load(proposal.meeting) }
            if approve { await loadPeople() }
        }
        await loadProposals()
    }

    /// Rename a known person everywhere: every meeting that names them shows the new name.
    public func renamePerson(_ person: String, name: String, meeting id: String) async {
        await mutate {
            _ = try await client.renameSpeaker(person, name: name)
            if requestedID == id { await load(id) }
            await loadPeople()
        }
    }

    /// Forget a known person's voice: no meeting is matched to them again. `anonymise` also turns their name in past
    /// meetings into "Speaker X".
    public func forgetPerson(_ person: String, anonymise: Bool, meeting id: String) async {
        await mutate {
            _ = try await client.forgetSpeaker(person, anonymise: anonymise)
            if requestedID == id { await load(id) }
            await loadPeople()
        }
    }

    public func name(_ id: String, label: String, name: String) async {
        await mutate {
            _ = try await client.nameMeetingSpeaker(id, label: label, name: name)
            if requestedID == id { await load(id) }
            await loadPeople()
        }
    }

    public func merge(_ id: String, label: String, into target: String) async {
        await mutate {
            _ = try await client.mergeMeetingSpeakers(id, label: label, into: target)
            if requestedID == id { await load(id) }
        }
    }

    public func file(_ id: String, account: String?, title: String?) async {
        await mutate {
            let updated = try await client.fileMeeting(id, account: account, title: title)
            if let index = meetings.firstIndex(where: { $0.id == id }) {
                invalidateListRefresh()
                meetings[index] = updated
            }
            if requestedID == id { await load(id) }
        }
    }

    public func reprocess(_ id: String) async {
        await mutate {
            let updated = try await client.reprocessMeeting(id)
            if let index = meetings.firstIndex(where: { $0.id == id }) {
                invalidateListRefresh()
                meetings[index] = updated
            }
            if requestedID == id { await load(id) }
        }
    }

    public func delete(_ id: String, forgetVoices: Bool) async {
        await mutate {
            _ = try await client.deleteMeeting(id, forgetVoices: forgetVoices)
            invalidateListRefresh()
            meetings.removeAll { $0.id == id }
            if requestedID == id {
                detailGeneration += 1
                requestedID = nil
                detail = nil
                segments = []
                loadingDetail = false
                detailError = nil
            }
        }
    }

    public func clip(_ id: String, label: String) async throws -> Data {
        try await client.meetingClip(id, speaker: label)
    }

    private func invalidateListRefresh() {
        listGeneration += 1
        loading = false
    }

    private func mutate(_ action: () async throws -> Void) async {
        guard !mutating else { return }
        mutating = true
        actionError = nil
        defer { mutating = false }
        do {
            try await action()
        } catch {
            actionError = APIError.from(error)?.message
        }
    }
}
