import Foundation

/// The iPhone's half of a turn the Apple Watch asked for (WatchProtocol.swift): send what was dictated on the watch as
/// a voice message, follow the reply to its end, and hand back what the watch shows and speaks. No WatchConnectivity
/// here: the iOS app's `WatchRelay` carries the requests and answers; this is the part the tests drive.
public enum WatchTurn {
    /// How long the instance's voice may take to look up before the reply goes out with the system voice.
    static let voiceLookupTimeout: Duration = .seconds(2)

    /// Send `text` to `instance` as a voice message (`voice=1`, so the reply is written to be heard) in its current
    /// chat. Returns the server's confirmation; throws its refusal or the transport error.
    public static func send(_ text: String, mid: String, to instance: String,
                            client: APIClient) async throws -> SendResponse {
        try await client.send(instance, OutgoingMessage(mid: mid, text: text, mode: .now, voice: true))
    }

    /// The reply started by a send answered with `chat`: followed to its end. The send starts the reply before it
    /// answers (§6.1), so a stream with nothing to follow (`409`) means the reply already ended: then it is the last
    /// message the instance wrote in that chat. Nil only when the chat holds no reply at all.
    public static func reply(to mid: String, in instance: String, chat: String?, client: APIClient,
                             fallback: VoiceLanguage) async throws -> WatchReplyPayload? {
        if let (text, failure) = try await follow(instance, cid: chat, client: client) {
            return await payload(text, failure: failure, instance: instance, mid: mid, client: client,
                                 fallback: fallback)
        }
        guard case .value(let feed, _) = try await client.feed(instance, cid: chat, watch: false),
              let text = lastWritten(in: feed) else { return nil }
        return await payload(text, failure: nil, instance: instance, mid: mid, client: client, fallback: fallback)
    }

    /// The reply still running in `instance`'s current chat, followed to its end; else the last message the instance
    /// wrote there. Nil when it has written nothing yet.
    public static func latest(_ instance: String, client: APIClient,
                              fallback: VoiceLanguage) async throws -> WatchReplyPayload? {
        if let (text, failure) = try await follow(instance, cid: nil, client: client), !text.isEmpty || failure != nil {
            return await payload(text, failure: failure, instance: instance, mid: nil, client: client,
                                 fallback: fallback)
        }
        guard case .value(let feed, _) = try await client.feed(instance, watch: false),
              let text = lastWritten(in: feed) else { return nil }
        return await payload(text, failure: nil, instance: instance, mid: nil, client: client, fallback: fallback)
    }

    /// The last message the instance wrote in this feed: its newest reply with words in it.
    static func lastWritten(in feed: Feed) -> String? {
        for item in (feed.content?.items ?? []).reversed() {
            if case .assistant(let reply) = item, !reply.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return reply.content
            }
        }
        return nil
    }

    /// The reply stream to its end: the `answer` text (`""` when it had none) and the `end` event's failure. Nil when
    /// the stream carried no event (`409 not_replying`).
    private static func follow(_ instance: String, cid: String?,
                               client: APIClient) async throws -> (String, String?)? {
        var answer: String?
        var failure: String?
        var events = 0
        // watch: false: the iPhone follows this reply for the watch; its push must still come (the notification the
        // watch mirrors is the fallback when the relay can't deliver).
        for try await event in client.followReply(instance, cid: cid, watch: false) {
            events += 1
            switch event.type {
            case .answer: answer = event.text ?? ""
            case .end: failure = event.failure
            default: break
            }
        }
        guard events > 0 else { return nil }
        return (answer ?? "", failure)
    }

    /// The reply as the watch takes it: the language read from its words (else the instance's language in the app),
    /// the instance's voice for that language when it has one, and the sentences to speak.
    static func payload(_ text: String, failure: String?, instance: String, mid: String?, client: APIClient,
                        fallback: VoiceLanguage) async -> WatchReplyPayload {
        let language = ReadAloudLanguage.detect(text) ?? fallback
        var chunker = SpeechChunker(language: language)
        let spoken = chunker.feed(text) + chunker.finish()
        let voice = await voiceIdentifier(for: instance, language: language, client: client)
        return WatchReplyPayload(instance: instance, mid: mid, text: text, spoken: spoken,
                                 language: language.bcp47, voice: voice, failure: failure)
    }

    /// The instance's voice for `language` (§17), or nil when it has none, the lookup fails or takes too long: the
    /// watch then speaks with its system voice for the language.
    static func voiceIdentifier(for instance: String, language: VoiceLanguage, client: APIClient) async -> String? {
        await withTaskGroup(of: String?.self) { group in
            group.addTask {
                let voices = try? await client.voices(instance)
                return (voices?.voices[language.rawValue] ?? nil)?.identifier
            }
            group.addTask {
                try? await Task.sleep(for: voiceLookupTimeout)
                return nil
            }
            let first = await group.next() ?? nil
            group.cancelAll()
            return first
        }
    }

    /// An error in words for the watch's small screen.
    public static func describe(_ error: any Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}
