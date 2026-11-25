import Foundation

public extension APIClient {
    func createMeeting(_ request: MeetingCreate) async throws -> MeetingCreated {
        try await post("/app/v1/meetings", json: request)
    }

    func putMeetingChunk(_ id: String, index: Int, data: Data) async throws -> [Int] {
        let path = "/app/v1/meetings/\(id)/chunk/\(index)"
        var request = makeRequest("PUT", path)
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue(String(data.count), forHTTPHeaderField: "Content-Length")
        request.httpBody = data
        let (body, _) = try await send(request, path: path)
        let result: MeetingChunkReceipt = try Self.decode(body, path: path)
        return result.received
    }

    func meetingUploadStatus(_ id: String) async throws -> UploadStatus {
        try await get("/app/v1/meetings/\(id)/upload")
    }

    func finishMeeting(_ id: String, _ request: FinishRequest) async throws -> Meeting {
        try await post("/app/v1/meetings/\(id)/finish", json: request)
    }

    func meetings() async throws -> [Meeting] {
        let result: MeetingsResponse = try await get("/app/v1/meetings")
        return result.meetings
    }

    func meeting(_ id: String, page: Int = 1) async throws -> Meeting {
        try await get("/app/v1/meetings/\(id)", query: [URLQueryItem(name: "page", value: String(page))])
    }

    func reprocessMeeting(_ id: String) async throws -> Meeting {
        let path = "/app/v1/meetings/\(id)/reprocess"
        let (body, _) = try await send(makeRequest("POST", path), path: path)
        return try Self.decode(body, path: path)
    }

    func deleteMeeting(_ id: String) async throws -> MeetingDeleted {
        let path = "/app/v1/meetings/\(id)"
        let (body, _) = try await send(makeRequest("DELETE", path), path: path)
        return try Self.decode(body, path: path)
    }
}

public struct MeetingDeleted: Decodable, Sendable, Equatable {
    public let id: String
    public let deleted: Bool
}

private struct MeetingChunkReceipt: Decodable {
    let received: [Int]
}

private struct MeetingsResponse: Decodable {
    let meetings: [Meeting]
}
