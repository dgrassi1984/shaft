import CryptoKit
import Foundation
import Observation

/// Uploads a finalized recording. Keep the audio file unchanged until upload finishes.
@MainActor @Observable
public final class MeetingUploader {
    public enum State: Sendable, Equatable {
        case idle
        case creating
        case uploading(Double)
        case finishing
        case done(String)
        case failed(String)
    }

    public private(set) var state: State = .idle
    public private(set) var bytesSent = 0
    public private(set) var totalBytes = 0

    private let client: APIClient
    private let audioURL: URL
    private static var activeRecordings: Set<URL> = []

    private struct UploadRecord: Codable {
        var clientId: UUID?
        var meetingId: String?
        let size: Int
        let sha256: String
        var chunkSize: Int?
        var finishedAt: Date? = nil
    }

    private enum LocalError: LocalizedError {
        case audioChanged
        case invalidChunkSize
        case invalidRecord

        var errorDescription: String? {
            switch self {
            case .audioChanged: "The audio file changed since the upload started."
            case .invalidChunkSize: "The server reported an invalid or different meeting chunk size."
            case .invalidRecord: "The saved meeting upload record is incomplete."
            }
        }
    }

    public init(client: APIClient, audioURL: URL) {
        self.client = client
        self.audioURL = audioURL.standardizedFileURL
    }

    /// Cancel the task to pause. Calling upload again consults the server for the saved chunk indices.
    /// Optional raw sidecar JSON overrides the sidecar in the create request.
    public func upload(_ create: MeetingCreate, sidecarJSON: Data? = nil) async {
        guard Self.activeRecordings.insert(audioURL).inserted else { return }
        defer { Self.activeRecordings.remove(audioURL) }
        bytesSent = 0
        totalBytes = 0
        state = .creating

        do {
            try await runUpload(create, sidecarJSON: sidecarJSON)
        } catch {
            if Task.isCancelled || APIError.isCancellation(error) {
                state = .idle
            } else {
                state = .failed(error.localizedDescription)
            }
        }
    }

    private func runUpload(_ create: MeetingCreate, sidecarJSON: Data?) async throws {
        let recordURL = audioURL.appendingPathExtension(Self.recordExtension)
        let sizeAndDigest = try await Self.fingerprint(audioURL)
        totalBytes = sizeAndDigest.size

        var record: UploadRecord
        if let saved = try Self.loadRecord(at: recordURL) {
            guard saved.size == totalBytes, saved.sha256 == sizeAndDigest.sha256 else {
                throw LocalError.audioChanged
            }
            record = saved
        } else {
            record = UploadRecord(clientId: UUID(), size: totalBytes, sha256: sizeAndDigest.sha256)
            try Self.save(record, at: recordURL)
        }

        if let id = record.meetingId, record.finishedAt != nil {
            bytesSent = totalBytes
            state = .done(id)
            return
        }

        let received: [Int]
        if record.meetingId == nil {
            guard let clientId = record.clientId else { throw LocalError.invalidRecord }
            var request = create
            request.clientId = clientId
            if let sidecarJSON {
                request.sidecar = try JSONDecoder().decode([String: MeetingJSONValue].self, from: sidecarJSON)
            }
            let created = try await retry { try await client.createMeeting(request) }
            guard created.chunkSize > 0 else { throw LocalError.invalidChunkSize }
            record.meetingId = created.id
            record.chunkSize = created.chunkSize
            try Self.save(record, at: recordURL)
            received = created.received
        } else {
            guard let id = record.meetingId, let chunkSize = record.chunkSize else { throw LocalError.invalidRecord }
            received = try await receivedChunks(id, chunkSize: chunkSize)
        }

        guard let id = record.meetingId, let chunkSize = record.chunkSize, chunkSize > 0 else {
            throw LocalError.invalidRecord
        }
        let chunkCount = record.size / chunkSize + (record.size % chunkSize == 0 ? 0 : 1)
        let finish = FinishRequest(size: record.size, sha256: record.sha256, chunks: chunkCount)
        var pendingReceipt = received
        var recoveryDelay = 1
        while true {
            try await sendChunks(id, chunkSize: chunkSize, count: chunkCount, received: Set(pendingReceipt))
            state = .finishing
            do {
                _ = try await retry { try await client.finishMeeting(id, finish) }
                record.finishedAt = Date()
                try Self.save(record, at: recordURL)
                state = .done(id)
                return
            } catch let error as APIError where error.status == 409 {
                let mismatch = error.message == "The audio SHA-256 does not match."
                    || error.message.hasPrefix("Audio size is ")
                guard mismatch || (error.message.hasPrefix("Chunk ") && error.message.hasSuffix(" is missing.")) else {
                    throw error
                }
                try await Self.backoff(&recoveryDelay)
                pendingReceipt = try await receivedChunks(id, chunkSize: chunkSize)
                if mismatch {
                    let current = try await Self.fingerprint(audioURL)
                    guard current == sizeAndDigest else { throw LocalError.audioChanged }
                    // Indices alone cannot identify a corrupt chunk.
                    pendingReceipt = []
                }
            }
        }
    }

    private func receivedChunks(_ id: String, chunkSize: Int) async throws -> [Int] {
        let status = try await retry { try await client.meetingUploadStatus(id) }
        guard status.chunkSize > 0, status.chunkSize == chunkSize else {
            throw LocalError.invalidChunkSize
        }
        return status.received
    }

    private func sendChunks(_ id: String, chunkSize: Int, count: Int, received: Set<Int>) async throws {
        bytesSent = received.lazy.filter { $0 >= 0 && $0 < count }.reduce(0) { bytes, index in
            bytes + min(chunkSize, totalBytes - index * chunkSize)
        }
        updateProgress()
        let file = try FileHandle(forReadingFrom: audioURL)
        defer { try? file.close() }
        for index in 0..<count where !received.contains(index) {
            try Task.checkCancellation()
            let offset = index * chunkSize
            let length = min(chunkSize, totalBytes - offset)
            try file.seek(toOffset: UInt64(offset))
            guard let data = try file.read(upToCount: length), data.count == length else {
                throw LocalError.audioChanged
            }
            _ = try await retry { try await client.putMeetingChunk(id, index: index, data: data) }
            bytesSent += data.count
            updateProgress()
        }
    }

    private func updateProgress() {
        state = .uploading(totalBytes == 0 ? 1 : Double(bytesSent) / Double(totalBytes))
    }

    private func retry<T: Sendable>(_ request: @MainActor () async throws -> T) async throws -> T {
        var delay = 1
        while true {
            try Task.checkCancellation()
            do {
                return try await request()
            } catch let error as APIError where error.isRetryable || error.status == 408 {
                try await Self.backoff(&delay)
            }
        }
    }

    private static func backoff(_ delay: inout Int) async throws {
        try await Task.sleep(for: .seconds(delay))
        delay = min(delay * 2, 60)
    }

    nonisolated private static func fingerprint(_ url: URL) async throws -> (size: Int, sha256: String) {
        try digest(of: url)
    }

    nonisolated private static func digest(of url: URL) throws -> (size: Int, sha256: String) {
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        var hash = SHA256()
        var size = 0
        while let data = try file.read(upToCount: 1024 * 1024), !data.isEmpty {
            try Task.checkCancellation()
            size += data.count
            hash.update(data: data)
        }
        let digest = hash.finalize().map { String(format: "%02x", $0) }.joined()
        return (size, digest)
    }

    private static let recordExtension = "shaft-upload"

    private static func loadRecord(at url: URL) throws -> UploadRecord? {
        do {
            return try JSONDecoder().decode(UploadRecord.self, from: Data(contentsOf: url))
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
            return nil
        }
    }

    private static func save(_ record: UploadRecord, at url: URL) throws {
        try JSONEncoder().encode(record).write(to: url, options: .atomic)
    }

    /// A recording whose sidecar lists problems is the one worth keeping on the device (review 2026-11-27): never
    /// pruned, only deleted by the operator.
    nonisolated static func hasProblems(_ audio: URL) -> Bool {
        guard let data = try? Data(contentsOf: audio.deletingPathExtension().appendingPathExtension("json")),
              let fields = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let problems = fields["problems"] as? [Any] else { return false }
        return !problems.isEmpty
    }

    /// Remove finished audio files and their upload records from this directory. The default cutoff is seven days ago.
    public static func pruneUploaded(in directory: URL,
                                     olderThan cutoff: Date = Date().addingTimeInterval(-7 * 24 * 60 * 60)) throws {
        let files = FileManager.default
        for url in try files.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            where url.pathExtension == recordExtension {
            guard let record = try loadRecord(at: url),
                  let finishedAt = record.finishedAt, finishedAt < cutoff else { continue }
            let audio = url.deletingPathExtension().standardizedFileURL
            guard !activeRecordings.contains(audio), !hasProblems(audio) else { continue }
            do {
                let current = try digest(of: audio)
                guard current.size == record.size, current.sha256 == record.sha256 else { continue }
                try files.removeItem(at: audio)
            } catch let error as CocoaError where error.code == .fileNoSuchFile || error.code == .fileReadNoSuchFile {
                // A previous prune may already have removed the audio.
            }
            try files.removeItem(at: url)
        }
    }
}
