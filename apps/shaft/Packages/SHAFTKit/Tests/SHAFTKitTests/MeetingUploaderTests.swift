import CryptoKit
import Foundation
import Synchronization
import Testing
@testable import SHAFTKit

@MainActor
struct MeetingUploaderTests {
    nonisolated private static let id = "m-20261125-1402-00af"
    nonisolated private static let base = "/app/v1/meetings"
    nonisolated private static let detail = "\(base)/\(id)"
    nonisolated private static let digest = "72399361da6a7754fec986dca5b7cbaf1c810a28ded4abaf56b2106d06cb78b0"
    nonisolated private static let processing = #"{"diarisation":"example","asr":"example","versions":{"mlx-whisper":"0.4.4"}}"#
    nonisolated private static let metadata = """
        {
          "id": "\(id)", "title": "Planning", "created": "2026-11-25T14:02:31+01:00",
          "started": "2026-11-25T13:21:18+01:00", "ended": null, "mode": "online",
          "device": "example", "ext": "m4a", "channels": ["microphone", "system"], "account": null,
          "state": "queued", "error": null, "size": 10, "sha256": "\(digest)", "chunk_size": 4,
          "chunks_received": [0,1,2], "processed_with": null
        }
        """

    private struct Recording {
        let directory: URL
        let audio: URL

        init(_ data: Data = Data("abcdefghij".utf8)) throws {
            directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
            audio = directory.appending(path: "recording.m4a")
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try data.write(to: audio)
        }

        func cleanUp() {
            try? FileManager.default.removeItem(at: directory)
        }
    }

    private var create: MeetingCreate {
        MeetingCreate(title: "Planning", mode: "online", started: Date(timeIntervalSince1970: 0),
                      device: "example", ext: "m4a")
    }

    nonisolated private static func answer(_ request: StubServer.Request, chunkSize: Int = 4,
                                           received: [Int] = []) -> StubServer.Response {
        switch (request.method, request.path) {
        case ("POST", base):
            return .json(201, #"{"id":"\#(id)","chunk_size":\#(chunkSize),"received":[]}"#)
        case ("GET", "\(detail)/upload"):
            return .json(200, #"{"chunk_size":\#(chunkSize),"received":\#(received)}"#)
        case ("POST", "\(detail)/finish"):
            return .json(200, metadata)
        case ("PUT", let path) where path.hasPrefix("\(detail)/chunk/"):
            return .json(200, #"{"received":[0]}"#)
        default:
            return .json(400, #"{"error":{"code":"bad_request","message":"Unexpected request."}}"#)
        }
    }

    private func chunks(_ server: StubServer) -> [StubServer.Request] {
        server.requests.filter { $0.method == "PUT" }
    }

    @Test func sendsThreeChunksInOrderAndFinishesWithTheirHash() async throws {
        let recording = try Recording()
        defer { recording.cleanUp() }
        let server = StubServer { Self.answer($0) }
        let uploader = MeetingUploader(client: server.client, audioURL: recording.audio)
        let sidecar = Data(#"{"audioSource":{"channel_name":"mic","enabled":true,"nullable":null}}"#.utf8)
        await uploader.upload(create, sidecarJSON: sidecar)

        #expect(uploader.state == .done(Self.id))
        #expect(uploader.bytesSent == 10)
        #expect(uploader.totalBytes == 10)
        #expect(server.requests.map(\.path) == [Self.base, "\(Self.detail)/chunk/0", "\(Self.detail)/chunk/1",
                                               "\(Self.detail)/chunk/2", "\(Self.detail)/finish"])
        let uploaded = chunks(server)
        #expect(uploaded.map { String(decoding: $0.body, as: UTF8.self) } == ["abcd", "efgh", "ij"])
        #expect(uploaded.map { $0.header("Content-Length") } == ["4", "4", "2"])
        #expect(uploaded.allSatisfy { $0.header("Content-Type") == "application/octet-stream" })
        let createRequest = try #require(server.requests.first)
        let sidecarSent = try #require(createRequest.json?["sidecar"] as? [String: Any])
        let source = try #require(sidecarSent["audioSource"] as? [String: Any])
        #expect(source["channel_name"] as? String == "mic")
        #expect(source["enabled"] as? Bool == true)
        #expect(source["nullable"] is NSNull)
        let finish = try #require(server.requests.last?.json)
        let sent = uploaded.reduce(into: Data()) { $0.append($1.body) }
        #expect(SHA256.hash(data: sent).map { String(format: "%02x", $0) }.joined() == Self.digest)
        #expect(finish["sha256"] as? String == Self.digest)
        #expect(finish["size"] as? Int == 10)
        #expect(finish["chunks"] as? Int == 3)
    }

    @Test func aLostCreateResponseReusesTheSavedClientID() async throws {
        let recording = try Recording()
        defer { recording.cleanUp() }
        let server = StubServer { _ in
            .json(400, #"{"error":{"code":"bad_request","message":"Create response unavailable."}}"#)
        }
        let first = MeetingUploader(client: server.client, audioURL: recording.audio)
        await first.upload(create)
        #expect(first.state == .failed("Create response unavailable."))
        let saved = try Data(contentsOf: recording.audio.appendingPathExtension("shaft-upload"))
        let record = try #require(JSONSerialization.jsonObject(with: saved) as? [String: Any])
        let id = try #require(record["clientId"] as? String)
        #expect(UUID(uuidString: id) != nil)
        #expect(record["meetingId"] == nil)

        server.setAnswer { Self.answer($0) }
        let resumed = MeetingUploader(client: server.client, audioURL: recording.audio)
        await resumed.upload(create)
        #expect(resumed.state == .done(Self.id))
        let creates = server.requests.filter { $0.method == "POST" && $0.path == Self.base }
        #expect(creates.compactMap { $0.json?["client_id"] as? String } == [id, id])
    }

    @Test func newUploaderResumesWithOnlyTheMissingChunk() async throws {
        let recording = try Recording()
        defer { recording.cleanUp() }
        let server = StubServer { request in
            if request.path == "\(Self.detail)/chunk/2" {
                return .json(400, #"{"error":{"code":"bad_request","message":"Upload interrupted."}}"#)
            }
            return Self.answer(request)
        }
        let uploader = MeetingUploader(client: server.client, audioURL: recording.audio)
        await uploader.upload(create)
        #expect(uploader.state == .failed("Upload interrupted."))

        let count = server.requests.count
        server.setAnswer { Self.answer($0, received: [0, 1]) }
        let resumed = MeetingUploader(client: server.client, audioURL: recording.audio)
        await resumed.upload(create)

        #expect(resumed.state == .done(Self.id))
        #expect(resumed.bytesSent == 10)
        #expect(server.requests.dropFirst(count).map(\.path) == ["\(Self.detail)/upload", "\(Self.detail)/chunk/2",
                                                                 "\(Self.detail)/finish"])
    }

    @Test func retries503WithTheSameChunk() async throws {
        let recording = try Recording()
        defer { recording.cleanUp() }
        let attempts = Mutex(0)
        let server = StubServer { request in
            if request.path == "\(Self.detail)/chunk/0", attempts.withLock({ value in
                value += 1
                return value == 1
            }) {
                return .json(503, #"{"error":{"code":"unavailable","message":"Try again."}}"#)
            }
            return Self.answer(request)
        }
        let uploader = MeetingUploader(client: server.client, audioURL: recording.audio)
        await uploader.upload(create)

        #expect(uploader.state == .done(Self.id))
        #expect(chunks(server).map { String(decoding: $0.body, as: UTF8.self) } == ["abcd", "abcd", "efgh", "ij"])
    }

    @Test func stopsOn400WithTheServerMessage() async throws {
        let recording = try Recording()
        defer { recording.cleanUp() }
        let server = StubServer { _ in
            .json(400, #"{"error":{"code":"bad_request","message":"mode must be online or inperson."}}"#)
        }
        let uploader = MeetingUploader(client: server.client, audioURL: recording.audio)
        await uploader.upload(create)

        #expect(uploader.state == .failed("mode must be online or inperson."))
        #expect(server.requests.map(\.path) == [Self.base])
    }

    @Test(arguments: ["The audio SHA-256 does not match.", "Audio size is 11 bytes. Expected 10."])
    func checksStatusThenReplacesChunksAfterFinishMismatch(_ message: String) async throws {
        let recording = try Recording()
        defer { recording.cleanUp() }
        let attempts = Mutex(0)
        let server = StubServer { request in
            if request.path == "\(Self.detail)/finish", attempts.withLock({ value in
                value += 1
                return value == 1
            }) {
                return .json(409, #"{"error":{"code":"refused","message":"\#(message)"}}"#)
            }
            return Self.answer(request, received: [0, 1, 2])
        }
        let uploader = MeetingUploader(client: server.client, audioURL: recording.audio)
        await uploader.upload(create)

        #expect(uploader.state == .done(Self.id))
        #expect(server.requests.dropFirst(4).map(\.path) == ["\(Self.detail)/finish", "\(Self.detail)/upload",
                    "\(Self.detail)/chunk/0", "\(Self.detail)/chunk/1", "\(Self.detail)/chunk/2", "\(Self.detail)/finish"])
        #expect(chunks(server).map { String(decoding: $0.body, as: UTF8.self) } == ["abcd", "efgh", "ij", "abcd", "efgh", "ij"])
    }

    @Test func sendsLargeRecordingInServerSizedRequests() async throws {
        let chunkSize = 1024 * 1024
        let recording = try Recording(Data(repeating: 7, count: 5 * chunkSize))
        defer { recording.cleanUp() }
        let server = StubServer { Self.answer($0, chunkSize: chunkSize) }
        let uploader = MeetingUploader(client: server.client, audioURL: recording.audio)
        await uploader.upload(create)

        #expect(uploader.state == .done(Self.id))
        #expect(chunks(server).map(\.body.count) == Array(repeating: chunkSize, count: 5))
    }

    @Test func retainsFinishedFileUntilPruningCutoff() async throws {
        let recording = try Recording()
        defer { recording.cleanUp() }
        let server = StubServer { Self.answer($0) }
        let uploader = MeetingUploader(client: server.client, audioURL: recording.audio)
        await uploader.upload(create)
        #expect(uploader.state == .done(Self.id))
        let files = FileManager.default
        try MeetingUploader.pruneUploaded(in: recording.directory)
        #expect(files.fileExists(atPath: recording.audio.path))
        #expect(try files.contentsOfDirectory(atPath: recording.directory.path).count == 2)

        let count = server.requests.count
        await MeetingUploader(client: server.client, audioURL: recording.audio).upload(create)
        #expect(server.requests.count == count)
        try MeetingUploader.pruneUploaded(in: recording.directory, olderThan: .distantFuture)
        #expect(try files.contentsOfDirectory(atPath: recording.directory.path).isEmpty)
    }

    /// Review 2026-11-25: pruning checked only the record's date, so another file at a finished upload's path (not
    /// sent yet) would have been deleted.
    @Test func pruningKeepsAFileTheRecordDoesNotDescribe() async throws {
        let recording = try Recording()
        defer { recording.cleanUp() }
        let server = StubServer { Self.answer($0) }
        let uploader = MeetingUploader(client: server.client, audioURL: recording.audio)
        await uploader.upload(create)
        #expect(uploader.state == .done(Self.id))
        try Data("abcdefghik".utf8).write(to: recording.audio)
        try MeetingUploader.pruneUploaded(in: recording.directory, olderThan: .distantFuture)
        #expect(try Data(contentsOf: recording.audio) == Data("abcdefghik".utf8))
    }

    @Test func meetingRoutesDecodeMetadataAndTranscriptAsReturnedByServer() async throws {
        let transcript = """
            "transcript": {
                "meeting": "\(Self.id)", "segments": [
                    {"start":0.5,"end":2.0,"channel":1,"speaker":"M1","lang":"en","text":"Hello",
                     "words":[{"w":"Hello","start":0.5,"end":2.0}]}
                ], "gaps":[], "speakers":{"M1":{"channel":1,"seconds":1.5}},
                "processed_with":\(Self.processing),
                "page":2,"total_segments":201
            }
            """
        let ready = Self.metadata.replacingOccurrences(of: #""state": "queued""#, with: #""state": "ready""#)
            .replacingOccurrences(of: #""processed_with": null"#, with: "\"processed_with\": \(Self.processing)")
            .dropLast() + ",\(transcript)}"
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("GET", Self.base):
                return .json(200, #"{"meetings":[\#(Self.metadata)]}"#)
            case ("GET", Self.detail) where request.query("page") == "2":
                return .json(200, String(ready))
            case ("POST", "\(Self.detail)/reprocess"):
                return .json(200, Self.metadata)
            case ("DELETE", Self.detail):
                return .json(200, #"{"id":"\#(Self.id)","deleted":true}"#)
            default:
                return Self.answer(request)
            }
        }
        let client = server.client
        let list = try await client.meetings()
        let row = try #require(list.first)
        #expect(row.id == Self.id)
        #expect(row.title == "Planning")
        #expect(row.created == SHAFTJSON.parseStamp("2026-11-25T13:02:31Z"))
        #expect(row.ended == nil)
        #expect(row.chunkSize == 4)
        #expect(row.chunksReceived == [0, 1, 2])
        #expect(row.channels == .array([.string("microphone"), .string("system")]))
        #expect(row.workerCrashes == nil)
        #expect(row.transcript == nil)

        let meeting = try await client.meeting(Self.id, page: 2)
        let page = try #require(meeting.transcript)
        #expect(page.page == 2)
        #expect(page.totalSegments == 201)
        #expect(page.speakers["M1"]?.seconds == 1.5)
        #expect(page.segments.first?.words?.first?.w == "Hello")
        #expect(page.processedWith?.versions?["mlx-whisper"] == "0.4.4")
        #expect(try await client.reprocessMeeting(Self.id).state == "queued")
        #expect(try await client.deleteMeeting(Self.id).deleted)
    }
}
