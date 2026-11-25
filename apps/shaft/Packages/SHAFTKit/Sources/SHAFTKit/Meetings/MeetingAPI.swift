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
        try await deleteMeeting(id, forgetVoices: false)
    }

    func deleteMeeting(_ id: String, forgetVoices: Bool) async throws -> MeetingDeleted {
        let path = "/app/v1/meetings/\(id)"
        let query = forgetVoices ? [URLQueryItem(name: "forget_voices", value: "1")] : []
        let (body, _) = try await send(makeRequest("DELETE", path, query: query), path: path)
        return try Self.decode(body, path: path)
    }

    func speakers() async throws -> [SpeakerPerson] {
        let result: SpeakersResponse = try await get("/app/v1/speakers")
        return result.people
    }

    func renameSpeaker(_ id: String, name: String) async throws -> SpeakerPerson {
        try await post("/app/v1/speakers/\(id)/rename", json: ["name": name])
    }

    func forgetSpeaker(_ id: String, anonymise: Bool = false) async throws -> SpeakerDeleted {
        let path = "/app/v1/speakers/\(id)"
        let query = anonymise ? [URLQueryItem(name: "anonymise", value: "1")] : []
        let (body, _) = try await send(makeRequest("DELETE", path, query: query), path: path)
        return try Self.decode(body, path: path)
    }

    func nameMeetingSpeaker(_ id: String, label: String, name: String) async throws -> MeetingSpeakerUpdate {
        try await post("/app/v1/meetings/\(id)/speakers", json: ["label": label, "name": name])
    }

    func mergeMeetingSpeakers(_ id: String, label: String, into target: String) async throws -> MeetingSpeakerUpdate {
        try await post("/app/v1/meetings/\(id)/speakers", json: ["label": label, "merge_into": target])
    }

    func meetingClip(_ id: String, speaker: String, n: Int = 0) async throws -> Data {
        let path = "/app/v1/meetings/\(id)/clip"
        let query = [URLQueryItem(name: "speaker", value: speaker), URLQueryItem(name: "n", value: String(n))]
        var request = makeRequest("GET", path, query: query)
        request.setValue("audio/wav", forHTTPHeaderField: "Accept")
        let (data, _) = try await send(request, path: path)
        return data
    }
}

public struct MeetingDeleted: Decodable, Sendable, Equatable {
    public let id: String
    public let deleted: Bool
}

public struct SpeakerDeleted: Decodable, Sendable, Equatable {
    public let id: String
    public let deleted: Bool
}

private struct SpeakersResponse: Decodable {
    let people: [SpeakerPerson]
}

private struct MeetingChunkReceipt: Decodable {
    let received: [Int]
}

private struct MeetingsResponse: Decodable {
    let meetings: [Meeting]
}
