import Foundation

// The reply as it streams (docs/app-api.md §6.1) and the voice routes (§17), on the package's one client: the same
// request building, bearer, error mapping and JSON coders as every other route.

extension APIClient {
    // MARK: The reply stream (§6.1)

    /// No byte for this long (an event or the server's 2 s keepalive) and the stream counts as dropped. URLSession's
    /// request timeout is an idle timeout, reset by every byte, so a long reply that keeps sending never times out.
    static let streamIdleTimeout: TimeInterval = 20

    /// The reply running in the current chat, as Server-Sent Events: the events after `after` replayed, then live.
    /// `reply` is the reply the app holds; when the server's is newer it sends all of it from `seq` 1.
    ///
    /// - Finishes after the `end` event (nothing after it is read).
    /// - `409 not_replying` (nothing running, or the app has all of it): finishes at once with no events, no error.
    /// - Any other error status: the server's `APIError`, as the other routes throw it. A `data:` line that is not a
    ///   `ChatStreamEvent`: `APIError.decoding`.
    /// - The connection closing before `end`: `APIError.transport` (`followReply` reconnects on it).
    ///
    /// One reply per stream: the next reply is a new stream (its `seq` starts at 1 again).
    ///
    /// `cid`: the thread whose reply to follow, sent as `?cid=`; nil for the current chat (a server before threads
    /// ignores the parameter and streams its one chat).
    public func replyStream(_ instance: String, after: Int = 0, reply: String? = nil,
                            cid: String? = nil) -> AsyncThrowingStream<ChatStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    try await self.pourReplyStream(instance, after: after, reply: reply, cid: cid) {
                        continuation.yield($0)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// The running reply followed to its end: `replyStream`, reconnecting after a dropped connection with the
    /// `reply` and `after` it holds, so no `seq` of a reply is yielded twice. Up to 3 reconnects 0.5 s apart in a
    /// row; a connection that brought new events starts the count again. Ends after `end` or `409 not_replying`.
    /// A refusal (4xx) or an unreadable event is thrown at once: asking again gets the same answer.
    ///
    /// An interjection continues the reply already running: pass the `reply` and `after` the app holds.
    public func followReply(_ instance: String, after: Int = 0, reply: String? = nil,
                            cid: String? = nil) -> AsyncThrowingStream<ChatStreamEvent, Error> {
        followReply(instance, after: after, reply: reply, cid: cid, reconnects: 3, delay: .milliseconds(500))
    }

    /// `followReply` with its retry policy open, for tests.
    func followReply(_ instance: String, after: Int, reply: String?, cid: String? = nil, reconnects: Int,
                     delay: Duration) -> AsyncThrowingStream<ChatStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                var held = (reply: reply, after: after)
                var failures = 0
                while true {
                    var progressed = false
                    do {
                        for try await event in self.replyStream(instance, after: held.after, reply: held.reply,
                                                                cid: cid) {
                            if event.reply != held.reply {
                                held = (event.reply, 0)       // a newer reply than the one held: all of it is new
                            }
                            guard event.seq > held.after else { continue }    // replayed: already yielded
                            held.after = event.seq
                            progressed = true
                            continuation.yield(event)
                        }
                        // replyStream finishes normally only after `end` or on `409 not_replying`.
                        continuation.finish()
                        return
                    } catch {
                        guard let apiError = APIError.from(error), !Task.isCancelled else {
                            continuation.finish(throwing: CancellationError())
                            return
                        }
                        failures = progressed ? 1 : failures + 1
                        guard apiError.isRetryable, failures <= reconnects else {
                            continuation.finish(throwing: apiError)
                            return
                        }
                    }
                    do {
                        try await Task.sleep(for: delay)
                    } catch {
                        continuation.finish(throwing: CancellationError())
                        return
                    }
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// Open the stream and hand each event to `yield` as its blank line arrives. Returns after `end` or on `409
    /// not_replying`; throws `APIError` (or `CancellationError`) otherwise.
    private func pourReplyStream(_ instance: String, after: Int, reply: String?, cid: String?,
                                 yield: (ChatStreamEvent) -> Void) async throws {
        let path = "/app/v1/chat/\(instance)/stream"
        var query = [URLQueryItem(name: "after", value: String(after))]
        if let reply { query.append(URLQueryItem(name: "reply", value: reply)) }
        if let cid { query.append(URLQueryItem(name: "cid", value: cid)) }
        var request = makeRequest("GET", path, query: query)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.timeoutInterval = Self.streamIdleTimeout
        do {
            let (bytes, response) = try await session.bytes(for: request,
                                                            delegate: OriginRedirectDelegate(origin: request.url!))
            guard let http = response as? HTTPURLResponse else {
                throw APIError.decoding(path: path, message: "not an HTTP response")
            }
            guard (200..<300).contains(http.statusCode) else {
                var body = Data()
                for try await byte in bytes {
                    body.append(byte)
                    if body.count >= 64 * 1024 { break }
                }
                let error = Self.statusError(http.statusCode, body: body)
                if error.status == 409, error.code == "not_replying" { return }
                throw error
            }
            var parser = ServerSentEvents()
            for try await byte in bytes {
                guard let message = parser.consume(byte), message.event == "message" else { continue }
                let data = Data(message.data.utf8)
                // An event type this app does not know (a newer server) is skipped, not fatal: the reply goes on.
                if let kind = try? SHAFTJSON.decoder().decode(EventKind.self, from: data),
                   ChatStreamEvent.Kind(rawValue: kind.type) == nil {
                    continue
                }
                let event: ChatStreamEvent = try Self.decode(data, path: path)
                yield(event)
                if event.type == .end { return }
            }
            throw APIError.transport(code: URLError.networkConnectionLost.rawValue,
                                     message: "The reply stream closed before the reply ended.")
        } catch let error as APIError {
            throw error
        } catch {
            guard let apiError = APIError.from(error), !Task.isCancelled else { throw CancellationError() }
            throw apiError
        }
    }

    /// Just an event's `type`, read before the whole event.
    private struct EventKind: Decodable { var type: String }

    /// An error status as `perform` maps it (§1.5): the server's JSON error, else the status and the body's start.
    static func statusError(_ status: Int, body: Data) -> APIError {
        if let decoded = try? SHAFTJSON.decoder().decode(ErrorBody.self, from: body) {
            return .server(status: status, error: decoded.error)
        }
        let text = String(decoding: body.prefix(300), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return .http(status: status, body: text)
    }

    // MARK: Voices (§17)

    /// The instance's voices, its proposals and what each platform reported.
    public func voices(_ instance: String) async throws -> VoicesResponse {
        try await voiceCall("GET", "/app/v1/voices/\(instance)", body: nil as VoiceChoice?)
    }

    /// Pick the voice `identifier` for `language` (`"en"`, `"it"`): the operator's choice, over any proposal.
    public func setVoice(_ instance: String, language: String, identifier: String) async throws -> VoicesResponse {
        try await voiceCall("POST", "/app/v1/voices/\(instance)",
                            body: VoiceChoice(language: language, identifier: identifier, clear: nil))
    }

    /// No voice for `language`: the system voice speaks.
    public func clearVoice(_ instance: String, language: String) async throws -> VoicesResponse {
        try await voiceCall("POST", "/app/v1/voices/\(instance)",
                            body: VoiceChoice(language: language, identifier: nil, clear: true))
    }

    /// Accept or reject the instance's proposal `id`; the instance is told either way, with `reason` on a rejection.
    public func decideVoiceProposal(_ instance: String, id: String, accept: Bool,
                                    reason: String? = nil) async throws -> VoicesResponse {
        try await voiceCall("POST", "/app/v1/voices/\(instance)/proposals/\(id)",
                            body: VoiceDecision(accept: accept, reason: reason))
    }

    /// This device's installed voices, replacing what `platform` (`"ios"`, `"macos"`) reported before. Returns what
    /// every platform has reported (`available`).
    public func reportVoices(platform: String = Platform.current,
                             voices: [VoicesResponse.AvailableVoice]) async throws -> [String: [VoicesResponse.AvailableVoice]] {
        let answer: VoicesReported = try await voiceCall("POST", "/app/v1/voices/report",
                                                         body: VoicesReport(platform: platform, voices: voices))
        return answer.available
    }

    private func voiceCall<T: Decodable, Body: Encodable>(_ method: String, _ path: String,
                                                          body: Body?) async throws -> T {
        var request = makeRequest(method, path)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try Self.encode(body, path: path)
        }
        let (data, _) = try await Self.perform(request, session: session, path: path)
        return try Self.decode(data, path: path)
    }

    /// `{language, identifier}` or `{language, clear: true}`: the server refuses any other key set (§17).
    private struct VoiceChoice: Encodable {
        var language: String
        var identifier: String?
        var clear: Bool?

        func encode(to encoder: any Encoder) throws {
            enum Keys: String, CodingKey { case language, identifier, clear }
            var container = encoder.container(keyedBy: Keys.self)
            try container.encode(language, forKey: .language)
            try container.encodeIfPresent(identifier, forKey: .identifier)
            try container.encodeIfPresent(clear, forKey: .clear)
        }
    }

    /// `{accept, reason}`; `reason` left out when there is none (the server wants a string or nothing).
    private struct VoiceDecision: Encodable {
        var accept: Bool
        var reason: String?

        func encode(to encoder: any Encoder) throws {
            enum Keys: String, CodingKey { case accept, reason }
            var container = encoder.container(keyedBy: Keys.self)
            try container.encode(accept, forKey: .accept)
            try container.encodeIfPresent(reason, forKey: .reason)
        }
    }

    private struct VoicesReport: Encodable {
        var platform: String
        var voices: [VoicesResponse.AvailableVoice]
    }

    private struct VoicesReported: Decodable {
        var available: [String: [VoicesResponse.AvailableVoice]]
    }
}

/// A Server-Sent Events parser fed one byte at a time, as `URLSession.bytes` hands them over (the WHATWG
/// event-stream rules): lines end in LF, CRLF or CR (a CRLF split between two reads is one end); `data:` lines join
/// with `\n`; `id:` is kept until the next one; lines starting with `:` are comments; a blank line dispatches the
/// event, and one with no data dispatches nothing. Bytes are kept until their line ends, so a UTF-8 character split
/// between reads decodes whole. An event the stream closes before its blank line is never dispatched.
struct ServerSentEvents: Sendable {
    struct Message: Sendable, Equatable {
        /// The last `id:` seen, on this event or an earlier one.
        var id: String?
        /// `event:`, `"message"` when the event names none.
        var event: String
        var data: String
    }

    private var line: [UInt8] = []
    private var afterCR = false
    private var started = false
    private var data: String?
    private var event: String?
    private var lastID: String?

    /// One byte of the stream; the event it completes, if any.
    mutating func consume(_ byte: UInt8) -> Message? {
        if byte == 0x0A, afterCR {                    // the LF of a CRLF: that line already ended at the CR
            afterCR = false
            return nil
        }
        afterCR = byte == 0x0D
        guard byte == 0x0A || byte == 0x0D else {
            line.append(byte)
            return nil
        }
        var text = String(decoding: line, as: UTF8.self)
        line.removeAll(keepingCapacity: true)
        if !started {
            started = true
            if text.hasPrefix("\u{FEFF}") { text.removeFirst() }      // a byte order mark before the first line
        }
        return process(text)
    }

    /// Several bytes (a read); the events they complete, in order.
    mutating func consume(_ bytes: some Sequence<UInt8>) -> [Message] {
        bytes.compactMap { consume($0) }
    }

    private mutating func process(_ text: String) -> Message? {
        if text.isEmpty {
            defer { data = nil; event = nil }
            guard let data else { return nil }
            return Message(id: lastID, event: event.flatMap { $0.isEmpty ? nil : $0 } ?? "message", data: data)
        }
        if text.hasPrefix(":") { return nil }
        let field: Substring
        var value: Substring
        if let colon = text.firstIndex(of: ":") {
            field = text[..<colon]
            value = text[text.index(after: colon)...]
            if value.hasPrefix(" ") { value = value.dropFirst() }
        } else {
            field = Substring(text)
            value = ""
        }
        switch field {
        case "data": data = data.map { $0 + "\n" + value } ?? String(value)
        case "id": if !value.contains("\u{0}") { lastID = String(value) }
        case "event": event = String(value)
        default: break                                // `retry` and unknown fields: nothing the app uses
        }
        return nil
    }
}
