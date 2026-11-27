import Foundation
import Synchronization
import Testing
@testable import SHAFTKit

@MainActor
struct MeetingsStoreTests {
    nonisolated private static let id = "m-20261125-1402-00af"
    nonisolated private static let base = "/app/v1/meetings"
    nonisolated private static let detail = "\(base)/\(id)"
    nonisolated private static let gallery = "/app/v1/speakers"
    nonisolated private static let processing = #"{"diarisation":"community-1","asr":"whisper","versions":{"mlx-whisper":"0.4.3"}}"#
    nonisolated private static let segment = #"{"start":1.25,"end":8.5,"channel":2,"speaker":"S2","lang":"en","text":"Hello","words":[{"w":"Hello","start":1.25,"end":1.75}]}"#
    nonisolated private static let person = #"{"id":"p-12345678","name":"Ada","created":"2026-11-25T14:02:31+01:00","count":2}"#

    nonisolated private static func metadata(_ state: String, transcript: String? = nil) -> String {
        let transcriptField = transcript.map { ", \"transcript\": \($0)" } ?? ""
        return """
        {
          "id": "\(id)", "title": "Planning", "created": "2026-11-25T14:02:31+01:00",
          "started": "2026-11-25T13:21:18+01:00", "ended": "2026-11-25T13:31:18+01:00",
          "mode": "online", "device": "example", "account": null, "state": "\(state)",
          "error": null, "size": 10, "sha256": null, "ext": "m4a", "channels": null,
          "chunk_size": 8388608, "chunks_received": [],
          "processed_with": \(state == "ready" ? processing : "null")\(transcriptField)
        }
        """
    }

    nonisolated private static func speaker(named: Bool) -> String {
        named ? #"{"channel":2,"seconds":7.25,"clips":[[1.25,8.5]],"person":"p-12345678","name":"Ada"}"#
              : #"{"channel":2,"seconds":7.25,"clips":[[1.25,8.5]]}"#
    }

    @Test func fileMeetingSendsAliasAndTitleOrExplicitNullAccount() async throws {
        let server = StubServer { request in
            guard request.method == "POST", request.path == Self.detail,
                  let fields = request.json as? [String: Any] else { return Self.unexpected }
            let metadata = Self.metadata("ready")
            if fields as? [String: String] == ["account": "work", "title": "Weekly sync"] {
                return .json(200, metadata
                    .replacingOccurrences(of: #""account": null"#, with: #""account": "work""#)
                    .replacingOccurrences(of: #""title": "Planning""#, with: #""title": "Weekly sync""#))
            }
            if fields.count == 1, fields["account"] is NSNull { return .json(200, metadata) }
            return Self.unexpected
        }
        let filed = try await server.client.fileMeeting(Self.id, account: "work", title: "Weekly sync")
        #expect(filed.account == "work")
        #expect(filed.title == "Weekly sync")
        let utc = try #require(TimeZone(secondsFromGMT: 0))
        #expect(filed.chatReference(timeZone: utc) ==
                "Meeting \"Weekly sync\" (\(Self.id)), 25 Nov 2026 12:21 — read it with meeting_read(\"\(Self.id)\")")
        let unfiled = try await server.client.fileMeeting(Self.id, account: nil, title: nil)
        #expect(unfiled.account == nil)
        #expect(unfiled.chatReference(timeZone: utc) == nil)
    }

    @Test func meetingAccessDecodesOptionalGrantsAndSendsGrantChanges() async throws {
        let without = """
        {"instance":"example","browser":{"installed":null,"run":true},"web_search":false,"telegram":false,
         "mail":[],"calendar":[],"drive":[],"chat":[],"google_accounts":[{"alias":"work","address":"","scopes":[]}]}
        """
        let with = without.dropLast() + #","meetings":[{"account":"work"}]}"#
        let server = StubServer { request in
            switch (request.method, request.path, request.json as? [String: String]) {
            case ("GET", "/app/v1/instances", _):
                return .json(200, #"{"instances":[],"paused":[{"name":"example","since":null}],"badge":0}"#)
            case ("GET", "/app/v1/access/example", _):
                return .json(200, without)
            case ("POST", "/app/v1/access/example", let fields):
                if fields == ["grant": "meetings", "action": "grant", "alias": "work"] {
                    return .json(200, String(with))
                }
                if fields == ["grant": "meetings", "action": "revoke", "alias": "work"] {
                    return .json(200, without)
                }
                return Self.unexpected
            default:
                return Self.unexpected
            }
        }
        let client = server.client
        let before = try await client.access("example")
        #expect(before.meetings == nil)
        let aliases = try await client.meetingAccountAliases()
        #expect(aliases == ["work"])
        let granted = try await client.grantAccess("example", .meetings(alias: "work"))
        #expect(granted.meetings?.map(\.account) == ["work"])
        let revoked = try await client.grantAccess("example", .meetings(alias: "work", action: "revoke"))
        #expect(revoked.meetings == nil)
    }

    nonisolated private static func transcript(
        named: Bool = false, page: Int = 1, segments: [String] = [segment], total: Int = 1
    ) -> String {
        """
        {
          "meeting": "\(id)", "segments": [\(segments.joined(separator: ","))],
          "gaps": [{"start":9,"end":10.5,"reason":"audio stalled"}],
          "speakers": {"S2":\(speaker(named: named)), "M1":{"channel":1,"seconds":3}},
          "processed_with": \(processing), "page": \(page), "total_segments": \(total)
        }
        """
    }

    nonisolated private static let unexpected = StubServer.Response.json(
        400, #"{"error":{"code":"bad_request","message":"Unexpected request."}}"#
    )

    @Test func pollsThroughPendingStatesAndStopsWhenReady() async {
        let states = Mutex(["uploading", "queued", "processing", "ready", "failed"])
        let server = StubServer { request in
            guard request.method == "GET", request.path == Self.base else { return Self.unexpected }
            let state = states.withLock { $0.isEmpty ? "failed" : $0.removeFirst() }
            return .json(200, "{\"meetings\":[\(Self.metadata(state))]}")
        }
        let store = MeetingsStore(client: server.client)

        await store.refresh()
        #expect(store.meetings.map(\.state) == ["uploading"])
        await store.poll(every: .zero)

        #expect(store.meetings.map(\.state) == ["ready"])
        #expect(!store.needsPolling)
        #expect(server.requests.count == 4)
    }

    @Test func namesLabelAndReloadsTranscriptAndGallery() async throws {
        let named = Mutex(false)
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("GET", Self.detail):
                return .json(200, Self.metadata("ready", transcript: Self.transcript(named: named.withLock { $0 })))
            case ("POST", "\(Self.detail)/speakers"):
                guard request.json as? [String: String] == ["label": "S2", "name": "Ada"] else { return Self.unexpected }
                named.withLock { $0 = true }
                return .json(200, #"{"label":"S2","channel":2,"seconds":7.25,"clips":[[1.25,8.5]],"person":"p-12345678","name":"Ada"}"#)
            case ("GET", Self.gallery):
                return .json(200, "{\"people\":[\(Self.person)]}")
            default:
                return Self.unexpected
            }
        }
        let store = MeetingsStore(client: server.client)
        await store.load(Self.id)
        let before = try #require(store.detail?.transcript?.speakers["S2"])
        #expect(before.name == nil)
        #expect(before.seconds == 7.25)

        await store.name(Self.id, label: "S2", name: "Ada")

        #expect(store.detail?.transcript?.speakers["S2"]?.name == "Ada")
        #expect(store.people.map { (p: SpeakerPerson) in p.name } == ["Ada"])
        #expect(store.people.first?.count == 2)
        #expect(server.requests.map { (r: StubServer.Request) in r.path } == [Self.detail, "\(Self.detail)/speakers", Self.detail, Self.gallery])
        let post = try #require(server.requests.first { $0.method == "POST" })
        #expect(post.json as? [String: String] == ["label": "S2", "name": "Ada"])
    }

    @Test func deleteSendsTheFlagOnlyWhenChecked() async throws {
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("GET", Self.base):
                return .json(200, "{\"meetings\":[\(Self.metadata("failed"))]}")
            case ("DELETE", Self.detail):
                return .json(200, #"{"id":"\#(Self.id)","deleted":true}"#)
            default:
                return Self.unexpected
            }
        }
        let client = server.client
        let store = MeetingsStore(client: client)
        await store.refresh()
        #expect(store.meetings.map { (m: Meeting) in m.id } == [Self.id])

        await store.delete(Self.id, forgetVoices: true)
        #expect(store.meetings.isEmpty)
        let receipt = try await client.deleteMeeting(Self.id)
        #expect(receipt.deleted)

        let deletes = server.requests.filter { $0.method == "DELETE" }
        #expect(deletes.map { $0.query("forget_voices") } == ["1", nil])
    }

    @Test func decodesServerTranscriptAndLoadsTheLastPage() async throws {
        let server = StubServer { request in
            guard request.path == Self.detail, let page = request.query("page") else { return Self.unexpected }
            switch page {
            case "1":
                let first = Array(repeating: Self.segment, count: 200)
                return .json(200, Self.metadata("ready", transcript: Self.transcript(named: true, segments: first, total: 201)))
            case "2":
                let last = #"{"start":11,"end":12,"channel":1,"speaker":"M1","lang":"it","text":"Ciao","words":[]}"#
                return .json(200, Self.metadata("ready", transcript: Self.transcript(named: true, page: 2, segments: [last], total: 201)))
            default:
                return Self.unexpected
            }
        }
        let store = MeetingsStore(client: server.client)
        await store.load(Self.id)

        let transcript = try #require(store.detail?.transcript)
        #expect(transcript.meeting == Self.id)
        #expect(transcript.page == 1)
        #expect(transcript.totalSegments == 201)
        #expect(transcript.processedWith?.versions == ["mlx-whisper": "0.4.3"])
        #expect(transcript.recordingGaps == [MeetingTranscript.Gap(start: 9, end: 10.5, reason: "audio stalled")])
        #expect(transcript.gaps == MeetingJSONValue.array([MeetingJSONValue.object(["start": MeetingJSONValue.integer(9), "end": MeetingJSONValue.number(10.5),
                                                  "reason": MeetingJSONValue.string("audio stalled")])]))
        #expect(transcript.speakers["S2"] == MeetingTranscript.Speaker(channel: 2, seconds: 7.25, person: "p-12345678",
                                                   name: "Ada", clips: [[1.25, 8.5]]))
        #expect(transcript.speakers["M1"] == MeetingTranscript.Speaker(channel: 1, seconds: 3, person: nil, name: nil, clips: nil))
        #expect(store.segments.count == 201)
        #expect(store.segments.first == MeetingTranscript.Segment(start: 1.25, end: 8.5, channel: 2, speaker: "S2", lang: "en",
                                              text: "Hello", words: [MeetingTranscript.Word(w: "Hello", start: 1.25, end: 1.75)]))
        #expect(store.segments.last == MeetingTranscript.Segment(start: 11, end: 12, channel: 1, speaker: "M1", lang: "it",
                                             text: "Ciao", words: []))
        #expect(server.requests.map { $0.query("page") } == ["1", "2"])
    }

    @Test func mergesLabelsAndDownloadsTheClipAsBytes() async throws {
        let audio = Data([0x52, 0x49, 0x46, 0x46])
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "\(Self.detail)/speakers"):
                guard request.json as? [String: String] == ["label": "S2", "merge_into": "M1"] else { return Self.unexpected }
                return .json(200, #"{"label":"M1","channel":1,"seconds":10.25,"clips":[[12,15]]}"#)
            case ("GET", "\(Self.detail)/clip"):
                guard request.query("speaker") == "M1", request.query("n") == "0" else { return Self.unexpected }
                return .data(audio)
            default:
                return Self.unexpected
            }
        }
        let client = server.client
        let updated = try await client.mergeMeetingSpeakers(Self.id, label: "S2", into: "M1")
        #expect(updated.label == "M1")
        #expect(updated.speaker.seconds == 10.25)
        let clip = try await client.meetingClip(Self.id, speaker: updated.label)
        #expect(clip == audio)
    }

    /// Instances' speaker proposals were confirmed only from the CLI (review 2026-11-27): the app lists them and sends
    /// the operator's decision.
    @Test func listsAndConfirmsAnInstanceProposal() async throws {
        let decided = Mutex(false)
        let proposal = #"{"id":"0123456789abcdef0123456789abcdef","instance":"wisp","meeting":"\#(Self.id)","speaker":"S2","name":"Ada","why":"The invite lists her.","created":"2026-11-25T14:02:31+01:00","state":"pending","revision":"r"}"#
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("GET", "\(Self.base)/proposals"):
                return .json(200, decided.withLock { $0 } ? #"{"proposals":[]}"# : "{\"proposals\":[\(proposal)]}")
            case ("POST", "\(Self.base)/proposals"):
                guard let body = request.json as? [String: Any], body["id"] as? String == "0123456789abcdef0123456789abcdef",
                      body["approve"] as? Bool == true else { return Self.unexpected }
                decided.withLock { $0 = true }
                return .json(200, proposal.replacingOccurrences(of: "pending", with: "confirmed"))
            case ("GET", Self.detail):
                return .json(200, Self.metadata("ready", transcript: Self.transcript(named: decided.withLock { $0 })))
            case ("GET", Self.gallery):
                return .json(200, "{\"people\":[\(Self.person)]}")
            default:
                return Self.unexpected
            }
        }
        let store = MeetingsStore(client: server.client)
        await store.load(Self.id)
        #expect(store.detail?.transcript?.speakers["S2"]?.name == nil)
        await store.loadProposals()
        let pending = try #require(store.proposals.first)
        #expect((pending.instance, pending.speaker, pending.name) == ("wisp", "S2", "Ada"))
        await store.decide(pending, approve: true)
        #expect(store.actionError == nil)
        #expect(store.proposals.isEmpty)
        #expect(store.detail?.transcript?.speakers["S2"]?.name == "Ada")
    }

    @Test func renamesAndForgetsAGalleryPerson() async throws {
        let server = StubServer { request in
            let path = "\(Self.gallery)/p-12345678"
            switch (request.method, request.path) {
            case ("POST", "\(path)/rename"):
                guard request.json as? [String: String] == ["name": "Ada Lovelace"] else { return Self.unexpected }
                return .json(200, #"{"id":"p-12345678","name":"Ada Lovelace","created":"2026-11-25T14:02:31+01:00"}"#)
            case ("DELETE", path):
                guard request.query("anonymise") == "1" else { return Self.unexpected }
                return .json(200, #"{"id":"p-12345678","deleted":true}"#)
            default:
                return Self.unexpected
            }
        }
        let client = server.client
        let person = try await client.renameSpeaker("p-12345678", name: "Ada Lovelace")
        #expect(person.name == "Ada Lovelace")
        #expect(person.count == nil)
        let receipt = try await client.forgetSpeaker(person.id, anonymise: true)
        #expect(receipt.id == "p-12345678")
        #expect(receipt.deleted)
    }
}
