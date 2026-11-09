import Foundation

// The voice engine's shared vocabulary (PLAN-voice §2, §3, §6; M2). Every voice file builds on these names:
// - SpeechText.swift        what is spoken: markdown pieces in, spoken sentences out (SpeechChunker: SpeechChunking)
// - VoiceTurn.swift         the turn state machine: VoiceInput in, VoiceAction out, no audio, no network
// - ChatStreamClient.swift  the reply stream (§6.1 of docs/app-api.md) and the voice routes (§17)
// - VoiceAudio*.swift       microphone + on-device transcriber, speech output, working sound, earcons
// - VoiceSession.swift      the conductor that wires them together
// Times are seconds on one monotonic clock (VoiceClock), never wall-clock dates: the state machine is driven by the
// times in its inputs, so tests run it without waiting.

/// The two languages a voice session speaks and hears. The voice screen's toggle picks one; it sets the
/// transcriber's locale, the speaking voice and the reply language.
public enum VoiceLanguage: String, Codable, Sendable, CaseIterable {
    case en, it

    /// The transcriber's locale (SpeechTranscriber).
    public var localeIdentifier: String { self == .en ? "en_US" : "it_IT" }
    /// The speech synthesizer's language (AVSpeechSynthesisVoice).
    public var bcp47: String { self == .en ? "en-US" : "it-IT" }
}

/// One monotonic clock for every voice time stamp.
public enum VoiceClock {
    public static func now() -> TimeInterval { ProcessInfo.processInfo.systemUptime }
}

/// The voice settings (PLAN-voice §2). Defaults are the plan's; M3 tunes them from a real session.
public struct VoiceTuning: Sendable, Equatable, Codable {
    /// Silence after the transcriber's last final result that ends your turn.
    public var endpointS: TimeInterval
    /// Words of yours, heard while the instance thinks or speaks, that stop its speech (barge-in).
    public var bargeWords: Int
    /// No answer text and no tool for this long: one status line is read. A tool still running after
    /// `fillerS × 3`: its status is read. At most one such line every `fillerS × 3`.
    public var fillerS: TimeInterval
    /// The working sound while a tool runs or the reply waits for a cycle to pause.
    public var workingSound: Bool
    /// Its volume, 0…1.
    public var workingVolume: Float
    /// After a `tool_end`, the working sound keeps playing this long, so back-to-back tools leave no gap.
    public var workingGraceS: TimeInterval

    public init(endpointS: TimeInterval = 0.8, bargeWords: Int = 2, fillerS: TimeInterval = 3,
                workingSound: Bool = true, workingVolume: Float = 0.25, workingGraceS: TimeInterval = 0.6) {
        self.endpointS = endpointS
        self.bargeWords = bargeWords
        self.fillerS = fillerS
        self.workingSound = workingSound
        self.workingVolume = workingVolume
        self.workingGraceS = workingGraceS
    }
}

/// One result from the on-device transcriber. A volatile result replaces the previous volatile one; a final result
/// is kept and the volatile text after it starts again empty (SpeechTranscriber's own semantics).
public struct TranscriptEvent: Sendable, Equatable {
    public var text: String
    public var isFinal: Bool
    public var at: TimeInterval

    public init(text: String, isFinal: Bool, at: TimeInterval) {
        self.text = text
        self.isFinal = isFinal
        self.at = at
    }
}

/// What the voice screen shows, each with its earcon: listening → you're talking → thinking → speaking → listening.
public enum VoiceState: String, Sendable, Equatable, CaseIterable {
    case idle            // not started
    case listening       // waiting for you
    case userTalking     // you are talking
    case thinking        // your turn was sent; nothing spoken yet
    case speaking        // the instance's reply is being spoken
    case ended           // End pressed: the chat stays open as it is
}

/// Everything that happens to a voice session, in order, each with its time on VoiceClock.
public enum VoiceInput: Sendable, Equatable {
    /// Voice mode opened: start listening.
    case start(at: TimeInterval)
    /// A transcriber result (ignored while muted).
    case transcript(TranscriptEvent)
    /// The clock moved on. The conductor sends one at least every 100 ms.
    case tick(at: TimeInterval)
    /// The send of your turn answered (§6); `said` is its status line.
    case sendAnswered(said: String, at: TimeInterval)
    /// The send failed; `message` is for the screen.
    case sendFailed(message: String, at: TimeInterval)
    /// An event of the reply stream (docs/app-api.md §6.1).
    case stream(ChatStreamEvent, at: TimeInterval)
    /// The stream closed or could not open; `message` is for the screen (nil: closed after `end`, or `409
    /// not_replying`).
    case streamClosed(message: String?, at: TimeInterval)
    /// The speech output began the utterance `id` (a `.speak` action's id).
    case speechStarted(id: Int, at: TimeInterval)
    /// The speech output finished the utterance `id`, or dropped it on `.stopSpeaking`.
    case speechFinished(id: Int, at: TimeInterval)
    /// Mute on: the microphone is off and the instance keeps speaking. Off: listening again.
    case mute(Bool, at: TimeInterval)
    /// End pressed.
    case end(at: TimeInterval)
}

/// What the session must do, in order. The state machine decides; the conductor carries it out.
public enum VoiceAction: Sendable, Equatable {
    /// The state changed (show it, play its earcon).
    case state(VoiceState)
    /// Send your turn: `POST /app/v1/chat/<n>/send`, `voice=1`, `mode=now` (a new reply, or an interjection), then
    /// follow the reply stream.
    case send(text: String)
    /// `POST /app/v1/chat/<n>/stop`: "stop" / "basta" said alone while it replied.
    case stopReply
    /// Queue `text` to be spoken, after what is already queued. `isStatus`: a status line (a filler), not the
    /// instance's own words — shown and spoken as status.
    case speak(id: Int, text: String, isStatus: Bool)
    /// Drop everything queued and stop the current utterance now (barge-in, stop, end).
    case stopSpeaking
    /// The working sound on or off.
    case workingSound(Bool)
    /// Lower the working sound while you talk (true), back to its volume after (false).
    case duckWorkingSound(Bool)
    /// Your live transcript, for the screen ("" clears it).
    case showTranscript(String)
    /// A line for the screen: a failure, a substitution, the send's status.
    case showStatus(String)
    /// From the end of your turn to the first spoken word of the reply, measured; shown on the voice screen.
    case latency(TimeInterval)
}

/// Markdown pieces of a reply in (as they stream), sentences ready to speak out. SpeechText.swift has the real one;
/// VoiceTurn takes any, so its tests need no markdown.
public protocol SpeechChunking: Sendable {
    /// A piece of the reply's markdown, as streamed. Returns the sentences now complete, rendered for speech
    /// (markdown marks dropped, tables and code announced), in order. Empty while a sentence is still open.
    mutating func feed(_ markdown: String) -> [String]
    /// The reply's text ended: whatever is still open, rendered.
    mutating func finish() -> [String]
}

/// The microphone and transcriber, or a recorded file standing in for them (Debug test hook).
public protocol TranscriptSource: AnyObject, Sendable {
    /// Start hearing in `language`. Each result goes to `onResult`, on any thread.
    func start(language: VoiceLanguage, onResult: @escaping @Sendable (TranscriptEvent) -> Void) async throws
    /// Stop hearing (mute or end). Safe to call when not started.
    func stop() async
}

/// Speech out in the instance's voice. Utterances play in the order queued.
public protocol SpeechOutput: AnyObject, Sendable {
    /// Speak with this voice (`AVSpeechSynthesisVoice.identifier`). Returns nil when it is installed, else the
    /// line to show while the system voice for `language` stands in ("Ava (Premium) is not on this Mac: …").
    func useVoice(identifier: String?, name: String?, language: VoiceLanguage) -> String?
    /// Queue one utterance. `onStart` and `onFinish` come once each, on any thread; `onFinish` also when it is
    /// dropped by `stopAll`.
    func speak(id: Int, text: String, onStart: @escaping @Sendable (Int) -> Void,
               onFinish: @escaping @Sendable (Int) -> Void)
    /// Drop the queue and stop the current utterance now.
    func stopAll()
}

/// The soft looping "working" sound and the state earcons.
public protocol VoiceSounds: AnyObject, Sendable {
    func working(_ on: Bool)
    func duckWorking(_ ducked: Bool)
    func setWorkingVolume(_ volume: Float)
    func earcon(_ state: VoiceState)
}
