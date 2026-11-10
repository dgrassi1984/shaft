import Foundation
import Observation

/// One voice conversation with an instance: the conductor that runs `VoiceTurn` and carries out
/// its actions with the audio (transcriber, speech, sounds) and the server (send with `voice=1`, the reply stream).
///
/// Every input — a transcriber result, a speech start or finish, a stream event, the 100 ms tick, a send's answer —
/// goes through one inbox and is handled in arrival order on the main actor, so the state machine never sees two
/// inputs at once or out of order, whatever thread the audio callbacks come on.
///
/// The voice screen (M3) reads the observable properties and calls `start`, `setMuted` and `end`.
///
/// A spoken turn is sent straight to the server, never through the Outbox: a sentence said aloud and delivered
/// minutes later, after the conversation moved on, would be wrong, so a failed send is shown and you say it again.
@MainActor @Observable
public final class VoiceSession {
    /// What the screen shows.
    public private(set) var state: VoiceState = .idle
    /// Your live transcript ("" between turns).
    public private(set) var transcript = ""
    /// The latest line for the screen: a send's status, a failure, a voice substitution.
    public private(set) var statusLine = ""
    /// The voice standing in for the chosen one, said for as long as it stands in (plan §4); nil when the chosen
    /// voice speaks.
    public private(set) var voiceSubstitution: String?
    /// The sentence being spoken, and whether it is a status line rather than the instance's own words.
    public private(set) var speaking: (text: String, isStatus: Bool)?
    /// From the end of your last turn to the reply's first spoken word.
    public private(set) var latency: TimeInterval?
    public private(set) var muted = false
    public let language: VoiceLanguage
    public let instance: String
    /// The thread the session was started from (§3.5): its turns go there and its replies are followed there. Nil for
    /// the current chat (a server before threads).
    public let cid: String?

    private let client: APIClient
    private let transcriber: any TranscriptSource
    private let speech: any SpeechOutput
    private let sounds: any VoiceSounds
    private let graph: VoiceAudioGraph?
    private var turn: VoiceTurn
    private var texts: [Int: (String, Bool)] = [:]

    private let inbox: AsyncStream<VoiceInput>
    private let post: AsyncStream<VoiceInput>.Continuation
    private var tasks: [Task<Void, Never>] = []
    /// The reply stream being followed, and what of it the app holds.
    private var follow: Task<Void, Never>?
    private var held: (reply: String?, after: Int) = (nil, 0)
    /// A turn was sent while the stream was followed: an interjection. The instance may answer it with a reply of its
    /// own after this one ends (docs/app-api.md §6.1), which is followed in turn.
    private var interjected = false
    /// The chat the last send went into (for Stop).
    private var chat: String?
    private var ended = false

    public init(client: APIClient, instance: String, cid: String? = nil, instanceName: String, language: VoiceLanguage,
                tuning: VoiceTuning = VoiceTuning(), transcriber: any TranscriptSource, speech: any SpeechOutput,
                sounds: any VoiceSounds, graph: VoiceAudioGraph? = nil) {
        self.client = client
        self.instance = instance
        self.cid = cid
        self.language = language
        self.transcriber = transcriber
        self.speech = speech
        self.sounds = sounds
        self.graph = graph
        turn = VoiceTurn(tuning: tuning, language: language, instanceName: instanceName,
                         chunker: { SpeechChunker(language: language) })
        sounds.setWorkingVolume(tuning.workingVolume)
        (inbox, post) = AsyncStream.makeStream(of: VoiceInput.self)
    }

    /// Open the session: the audio, the instance's voice for this language, then listening. Throws when the audio
    /// cannot start (no permission, the transcriber's language model not installed): the screen shows why.
    public func start() async throws {
        guard state == .idle, !ended else { return }
        try graph?.start()
        if let voices = try? await client.voices(instance), let voice = voices.voices[language.rawValue] ?? nil {
            voiceSubstitution = speech.useVoice(identifier: voice.identifier, name: voice.name, language: language)
        } else {
            voiceSubstitution = speech.useVoice(identifier: nil, name: nil, language: language)
        }
        let post = post
        try await transcriber.start(language: language) { post.yield(.transcript($0)) }
        tasks.append(Task { [weak self] in await self?.run() })
        tasks.append(Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(100))
                post.yield(.tick(at: VoiceClock.now()))
            }
        })
        post.yield(.start(at: VoiceClock.now()))
    }

    /// Mute: the microphone is off and the instance keeps speaking. Unmute: listening again.
    public func setMuted(_ on: Bool) async {
        guard !ended, on != muted else { return }
        muted = on
        post.yield(.mute(on, at: VoiceClock.now()))
        if on {
            await transcriber.stop()
        } else {
            let post = post
            do {
                try await transcriber.start(language: language) { post.yield(.transcript($0)) }
            } catch {
                statusLine = error.localizedDescription
            }
        }
    }

    /// End: speech and sounds stop, the microphone closes. The chat stays open as it is; a reply still running goes
    /// on and lands in the chat.
    public func end() async {
        guard !ended else { return }
        apply(turn.handle(.end(at: VoiceClock.now())))
        ended = true
        follow?.cancel()
        tasks.forEach { $0.cancel() }
        post.finish()
        await transcriber.stop()
        speech.stopAll()
        sounds.working(false)
        sounds.waiting(false)
        graph?.stop()
    }

    // MARK: The loop

    private func run() async {
        for await input in inbox {
            if ended { break }
            if case .speechStarted(let id, _) = input, let t = texts.removeValue(forKey: id) {
                speaking = (t.0, t.1)
            }
            apply(turn.handle(input))
        }
    }

    private func apply(_ actions: [VoiceAction]) {
        for action in actions {
            switch action {
            case .state(let s):
                state = s
                if s == .listening || s == .ended { speaking = nil }
                sounds.earcon(s)
            case .send(let text):
                send(text)
            case .stopReply:
                stopReply()
            case .speak(let id, let text, let isStatus):
                texts[id] = (text, isStatus)
                let post = post
                speech.speak(id: id, text: text,
                             onStart: { post.yield(.speechStarted(id: $0, at: VoiceClock.now())) },
                             onFinish: { post.yield(.speechFinished(id: $0, at: VoiceClock.now())) })
            case .stopSpeaking:
                speech.stopAll()
            case .workingSound(let on):
                sounds.working(on)
            case .waitingSound(let on):
                sounds.waiting(on)
            case .duckWorkingSound(let on):
                sounds.duckWorking(on)
            case .showTranscript(let text):
                transcript = text
            case .showStatus(let line):
                statusLine = line
            case .latency(let seconds):
                latency = seconds
            }
        }
    }

    // MARK: The server

    private func send(_ text: String) {
        let message = OutgoingMessage(text: text, mode: .now, voice: true, cid: cid)
        let following = follow != nil
        if following { interjected = true }
        let post = post
        Task { [client, instance] in                  // on the main actor, as this method
            do {
                let answer = try await client.send(instance, message)
                if let chat = answer.chat { self.chat = chat }
                post.yield(.sendAnswered(said: answer.said, at: VoiceClock.now()))
                // The send starts the reply before it answers (§6.1): follow it now, unless the reply already
                // followed takes this turn as an interjection.
                if !self.ended, self.follow == nil { self.followReply(fresh: true) }
            } catch {
                post.yield(.sendFailed(message: Self.describe(error), at: VoiceClock.now()))
            }
        }
    }

    private func stopReply() {
        // Its late events would be ignored anyway (VoiceTurn); stop reading them.
        follow?.cancel()
        follow = nil
        interjected = false
        guard let chat = chat ?? cid else { return }
        Task { [client, instance] in _ = try? await client.stop(instance, cid: chat) }
    }

    /// Follow the reply stream to its end. `fresh`: the reply just started by a send (no `after`). Else the reply
    /// after the one held (an interjection answered late): asked with the reply and `after` held, a few times while
    /// it may still be starting.
    private func followReply(fresh: Bool) {
        let post = post
        let start = fresh ? (reply: String?.none, after: 0) : held
        follow = Task { [client, instance, cid, weak self] in     // on the main actor; awaits the network
            var sawEnd = false
            var events = 0
            let tries = fresh ? 1 : 5
            for attempt in 0..<tries {
                if attempt > 0 { try? await Task.sleep(for: .milliseconds(300)) }
                do {
                    for try await event in client.followReply(instance, after: start.after, reply: start.reply,
                                                              cid: cid) {
                        events += 1
                        self?.held = (event.reply, event.seq)
                        post.yield(.stream(event, at: VoiceClock.now()))
                        if event.type == .end { sawEnd = true }
                    }
                } catch {
                    if Task.isCancelled || error is CancellationError { return }
                    post.yield(.streamClosed(message: Self.describe(error), at: VoiceClock.now()))
                    self?.follow = nil
                    return
                }
                if Task.isCancelled { return }
                if events > 0 { break }          // 409 with nothing: the next reply may not have begun yet
            }
            post.yield(.streamClosed(message: nil, at: VoiceClock.now()))
            guard let self else { return }
            self.follow = nil
            if sawEnd, self.interjected, !self.ended {
                self.interjected = false
                self.followReply(fresh: false)
            }
        }
    }

    private static func describe(_ error: any Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}
