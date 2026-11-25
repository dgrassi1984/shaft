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
        let meetingId: String
        let size: Int
        let sha256: String
        let chunkSize: Int
        var finishedAt: Date? = nil
    }

    private enum LocalError: LocalizedError {
        case audioChanged
        case invalidChunkSize

        var errorDescription: String? {
            switch self {
            case .audioChanged: "The audio file changed since the upload started."
            case .invalidChunkSize: "The server reported an invalid or different meeting chunk size."
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
        var received: [Int]
        if let saved = try Self.loadRecord(at: recordURL) {
            guard saved.size == totalBytes, saved.sha256 == sizeAndDigest.sha256 else {
                throw LocalError.audioChanged
            }
            if saved.finishedAt != nil {
                bytesSent = totalBytes
                state = .done(saved.meetingId)
                return
            }
            record = saved
            received = try await receivedChunks(for: saved)
        } else {
            var request = create
            if let sidecarJSON {
                request.sidecar = try JSONDecoder().decode([String: MeetingJSONValue].self, from: sidecarJSON)
            }
            let created = try await retry { try await client.createMeeting(request) }
            guard created.chunkSize > 0 else { throw LocalError.invalidChunkSize }
            record = UploadRecord(meetingId: created.id, size: totalBytes, sha256: sizeAndDigest.sha256,
                                  chunkSize: created.chunkSize)
            try Self.save(record, at: recordURL)
            received = created.received
        }

        let chunkCount = record.size / record.chunkSize + (record.size % record.chunkSize == 0 ? 0 : 1)
        let finish = FinishRequest(size: record.size, sha256: record.sha256, chunks: chunkCount)
        let id = record.meetingId
        var recoveryDelay = 1
        while true {
            try await sendChunks(id, chunkSize: record.chunkSize, count: chunkCount, received: Set(received))
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
                received = try await receivedChunks(for: record)
                if mismatch {
                    let current = try await Self.fingerprint(audioURL)
                    guard current == sizeAndDigest else { throw LocalError.audioChanged }
                    // Indices alone cannot identify a corrupt chunk.
                    received = []
                }
            }
        }
    }

    private func receivedChunks(for record: UploadRecord) async throws -> [Int] {
        let status = try await retry { try await client.meetingUploadStatus(record.meetingId) }
        guard status.chunkSize > 0, status.chunkSize == record.chunkSize else {
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

    /// Remove finished audio files and their upload records from this directory. The default cutoff is seven days ago.
    public static func pruneUploaded(in directory: URL,
                                     olderThan cutoff: Date = Date().addingTimeInterval(-7 * 24 * 60 * 60)) throws {
        let files = FileManager.default
        for url in try files.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            where url.pathExtension == recordExtension {
            guard let record = try loadRecord(at: url),
                  let finishedAt = record.finishedAt, finishedAt < cutoff else { continue }
            // Only the file the record describes: another recording at the same path is not sent yet, keep it
            // (review 2026-11-25).
            let audio = url.deletingPathExtension()
            if let size = (try? files.attributesOfItem(atPath: audio.path)[.size]) as? Int, size != record.size {
                continue
            }
            do {
                try files.removeItem(at: url.deletingPathExtension())
            } catch let error as CocoaError where error.code == .fileNoSuchFile {
                // A previous prune may already have removed the audio.
            }
            try files.removeItem(at: url)
        }
    }
}
