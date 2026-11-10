import Foundation

/// The turn state machine of a voice session: what you say, the reply's stream and the speech
/// output's progress go in as `VoiceInput`; what the session must do comes out as `VoiceAction`s, in order. It plays
/// nothing, sends nothing and keeps no timer: every time it uses comes from its inputs (the conductor sends a `tick`
/// at least every 100 ms), so tests drive a whole conversation without waiting.
///
/// Choices the contract left open, made for a hands-free spoken conversation:
/// - **Nothing is spoken over you.** While you talk during a reply (after a barge-in), the reply's text is not
///   spoken. The step that was interrupted is not resumed either: its text is dropped until its `step_end`, since you
///   cut it off and the instance reads your interjection only at its next step. The next step, or a new reply, is
///   spoken again.
/// - **Backchannels and echo are not interjections.** Words heard while it thinks or speaks that never reach
///   `bargeWords` (an "okay", a word of its own voice leaking past echo cancellation) are dropped at the end of their
///   turn and never sent. The one exception is a stop word.
/// - **"Stop" acts on the transcriber's final result**, not after the end-of-turn silence: a final result that makes
///   the whole interrupting turn a stop word is already "said alone". `.stopReply` is sent only while a reply is
///   still expected; when only its last sentences are still being spoken, speech stops and nothing is posted.
/// - **A reply ends at `end`, or at a failure,** and the session returns to listening once the speech already
///   queued has been spoken. A failure (`sendFailed`, `streamClosed` with a message) leaves the reply it was
///   following: nothing more of that reply is spoken.
/// - **`streamClosed(message: nil)`** after the stream's own `end` changes nothing; without one (`409 not_replying`)
///   the reply is over, as at `end`. The close of a reply you stopped, coming after your next turn was sent, is
///   ignored: that turn's reply is still to come.
/// - **Latency is measured once per turn you send**: from the moment the turn ended (the input that sent it) to the
///   first spoken word of the instance's own text after it. One turn, one reply: exactly once per reply.
/// - **Fillers are for the silence before any text**: only while thinking and before the reply's first text (a
///   narration counts: it already said what it is doing, and the working sound plays during its tools). The same
///   line is never read twice in a row. A tool's name is read with spaces for underscores ("using web search").
///   A status with a `detail` is read as "<name> is <detail>"; a waiting phase without one ("waiting: x is pausing
///   its cycle for you (live)") as "<name> is pausing its cycle for you"; anything else as "<name> is thinking".
///   In Italian the server's three waiting phrases are translated, and any other status is "<name> sta pensando", so
///   the Italian voice never reads English.
/// - **Muting while you talk drops what you were saying**: nothing is sent.
public struct VoiceTurn: Sendable {
    /// What the screen shows.
    public private(set) var state: VoiceState = .idle
    /// The microphone is off: transcripts are ignored, the instance keeps speaking.
    public private(set) var muted = false

    private let tuning: VoiceTuning
    private let language: VoiceLanguage
    private let instanceName: String
    private let makeChunker: @Sendable () -> any SpeechChunking

    private var out: [VoiceAction] = []
    private var nextId = 1
    /// Utterances queued and not yet finished; the status lines among them.
    private var queued: Set<Int> = []
    private var statusIds: Set<Int> = []
    private var working = false
    private var ducked = false
    private var shown = ""

    private var heard = Heard()
    /// A volatile result that was taken as final; the transcriber's own final for the same words is not heard twice.
    private var promoted: String?

    private var reply = Reply()
    private var chunker: any SpeechChunking
    /// Replies already over: a late event of one is ignored.
    private var doneReplies: Set<String> = []
    /// A reply is expected or running: from your turn's send (or a new reply's first event) to its end or a failure.
    private var replyOpen = false
    /// An event of a followed reply came since the last send.
    private var seenSinceSend = false
    /// The reply awaited was stopped or failed before its first event: when it shows up, it is not spoken.
    private var dropUnseen = false

    private var sentAt: TimeInterval?
    /// The first utterance id that counts for this turn's latency; nil once measured (or nothing to measure).
    private var latencyFrom: Int?
    /// Text of the reply came since your turn was sent (or since a new reply began): no more fillers.
    private var textSinceSend = false
    /// When the silence the first filler measures began: the send, a new reply, the last tool's end.
    private var quietSince: TimeInterval = 0
    private var lastFillerAt: TimeInterval?
    private var lastFillerText: String?

    /// - Parameters:
    ///   - instanceName: as spoken in status lines ("Tundra is thinking").
    ///   - chunker: makes a fresh sentence chunker; one is used per step of the reply.
    public init(tuning: VoiceTuning, language: VoiceLanguage, instanceName: String,
                chunker: @escaping @Sendable () -> any SpeechChunking) {
        self.tuning = tuning
        self.language = language
        self.instanceName = instanceName
        self.makeChunker = chunker
        self.chunker = chunker()
    }

    /// One input, in order; the actions it causes, in order. After `end`, nothing.
    public mutating func handle(_ input: VoiceInput) -> [VoiceAction] {
        guard state != .ended else { return [] }
        out = []
        switch input {
        case .start:
            if state == .idle { setState(.listening) }
        case .end:
            out.append(.stopSpeaking)
            queued.removeAll()
            statusIds.removeAll()
            if working {
                working = false
                out.append(.workingSound(false))
            }
            setState(.ended)
        default:
            guard state != .idle else { return [] }
            step(input)
        }
        return out
    }

    // MARK: - Inputs

    private mutating func step(_ input: VoiceInput) {
        switch input {
        case .start, .end:
            break
        case .transcript(let event):
            hear(event)
        case .tick(let at):
            endTurnIfDue(at: at)
            syncWorking(at: at)
            fillIfQuiet(at: at)
        case .sendAnswered(let said, _):
            if !said.isEmpty { out.append(.showStatus(said)) }
        case .sendFailed(let message, let at):
            fail(message, at: at)
        case .stream(let event, let at):
            stream(event, at: at)
        case .streamClosed(let message, let at):
            if let message {
                fail(message, at: at)
            } else if replyOpen {
                // The close of a reply you stopped, after your next turn was sent: that turn's reply is to come.
                if reply.dropped && !seenSinceSend { return }
                finishReply(at: at, failure: nil, flush: true)
            }
        case .speechStarted(let id, let at):
            guard queued.contains(id), !statusIds.contains(id) else { return }
            if state == .thinking { setState(.speaking) }
            if let from = latencyFrom, id >= from, let sentAt {
                latencyFrom = nil
                out.append(.latency(at - sentAt))
            }
        case .speechFinished(let id, _):
            queued.remove(id)
            statusIds.remove(id)
            settle()
        case .mute(let on, _):
            guard on != muted else { return }
            muted = on
            if on { abandonTurn() }
        }
    }

    // MARK: - Your turn

    /// What has been heard of the turn you are saying now.
    private struct Heard {
        var finals: [String] = []
        var volatile = ""
        var volatileChangedAt: TimeInterval = 0
        var lastSpeechAt: TimeInterval = 0
        /// Started while the instance thought or spoke.
        var interrupting = false
        /// Shown, with the state `userTalking`: a turn from listening, or a barge-in.
        var visible = false

        var isEmpty: Bool { finals.isEmpty && volatile.isEmpty }
        var live: String { (volatile.isEmpty ? finals : finals + [volatile]).joined(separator: " ") }
        var finalText: String { finals.joined(separator: " ") }
    }

    private mutating func hear(_ event: TranscriptEvent) {
        guard !muted, state != .ended else { return }
        var text = event.text.trimmingCharacters(in: .whitespacesAndNewlines)
        if let promoted, heard.isEmpty {
            let words = Self.normalized(text)
            if words.isEmpty || words == promoted {
                if event.isFinal { self.promoted = nil }
                return
            }
            self.promoted = nil
            // The transcriber's late final for a turn already sent, with words the volatile lacked: only those words
            // are new. Sent whole, the turn would go twice (M2 end-to-end: "…by the" then "…by the door?").
            if words.hasPrefix(promoted + " ") {
                text = Self.dropping(promoted.split(separator: " ").count, wordsOf: text)
            }
        }
        let wasEmpty = heard.isEmpty
        if event.isFinal {
            heard.volatile = ""
            if !text.isEmpty {
                heard.finals.append(text)
                heard.lastSpeechAt = event.at
            }
        } else if text != heard.volatile {
            heard.volatile = text
            if !text.isEmpty {
                heard.volatileChangedAt = event.at
                heard.lastSpeechAt = event.at
            }
        }
        if wasEmpty && !heard.isEmpty && !heard.visible {
            heard.interrupting = state == .thinking || state == .speaking
        }
        // Checked before barge-in, so "Stop it." (two words) stops at once instead of first showing you talking.
        if event.isFinal && heard.interrupting && Self.isStopWord(heard.finalText) {
            stop(at: event.at)
            return
        }
        if !heard.visible && !heard.isEmpty {
            if state == .listening {
                becomeVisible()
            } else if (state == .thinking || state == .speaking)
                        && Self.wordCount(heard.live) >= tuning.bargeWords {
                bargeIn()
            }
        }
        if heard.visible { show(heard.live) }
    }

    private mutating func becomeVisible() {
        heard.visible = true
        setState(.userTalking)
        if working && !ducked {
            ducked = true
            out.append(.duckWorkingSound(true))
        }
    }

    /// You talk over the instance: its speech stops at once, and the step it was saying is not resumed.
    private mutating func bargeIn() {
        out.append(.stopSpeaking)
        queued.removeAll()
        statusIds.removeAll()
        latencyFrom = nil
        if reply.stepHasText { reply.dropStep = true }
        chunker = makeChunker()
        becomeVisible()
    }

    /// The end-of-turn rule: `endpointS` of silence after the last final result; a volatile result unchanged for
    /// `endpointS × 3` counts as final.
    private mutating func endTurnIfDue(at: TimeInterval) {
        guard !heard.isEmpty || heard.visible else { return }
        if heard.isEmpty {
            // Shown, then taken back by the transcriber, and nothing more said.
            if at - heard.lastSpeechAt >= tuning.endpointS * 3 { abandonTurn() }
            return
        }
        if !heard.volatile.isEmpty {
            guard at - heard.volatileChangedAt >= tuning.endpointS * 3 else { return }
            promoted = Self.normalized(heard.volatile)
            heard.finals.append(heard.volatile)
            heard.volatile = ""
            if heard.interrupting && Self.isStopWord(heard.finalText) {
                stop(at: at)
                return
            }
        } else {
            guard at - heard.lastSpeechAt >= tuning.endpointS else { return }
        }
        let text = heard.finalText.trimmingCharacters(in: .whitespacesAndNewlines)
        let visible = heard.visible
        heard = Heard()
        guard visible, !text.isEmpty else { return }       // a backchannel or echo while it replied: not sent
        out.append(.send(text: text))
        show("")
        unduck()
        setState(.thinking)
        replyOpen = true
        seenSinceSend = false
        dropUnseen = false
        sentAt = at
        latencyFrom = nextId
        textSinceSend = false
        quietSince = at
        lastFillerAt = nil
        lastFillerText = nil
    }

    /// What you were saying is dropped (mute, or the transcriber took it all back).
    private mutating func abandonTurn() {
        let visible = heard.visible
        heard = Heard()
        guard visible else { return }
        show("")
        unduck()
        setState(replyOpen ? .thinking : .listening)
    }

    /// "Stop" said alone while it replied.
    private mutating func stop(at: TimeInterval) {
        out.append(.stopSpeaking)
        queued.removeAll()
        statusIds.removeAll()
        if replyOpen {
            out.append(.stopReply)
            if reply.running { reply.dropped = true }
            if !seenSinceSend { dropUnseen = true }
        }
        replyOpen = false
        latencyFrom = nil
        heard = Heard()
        syncWorking(at: at)
        unduck()
        show("")
        setState(.listening)
    }

    // MARK: - The reply

    /// The reply being followed, by its stream's `reply` id.
    private struct Reply {
        var id: String?
        var drafts = false
        var stepHasText = false
        /// The step in progress was cut off by you: its text is not spoken.
        var dropStep = false
        /// Stopped by you, or left after a failure: none of it is spoken.
        var dropped = false
        var ended = false
        var tools = 0
        var tool: String?
        var toolStartedAt: TimeInterval = 0
        var waiting = false
        var graceUntil: TimeInterval?
        var phase = ""
        var detail = ""
        var hasStatus = false

        var running: Bool { id != nil && !ended && !dropped }
    }

    private mutating func stream(_ event: ChatStreamEvent, at: TimeInterval) {
        if event.reply != reply.id {
            guard !doneReplies.contains(event.reply) else { return }
            newReply(event.reply, at: at)
        }
        guard reply.running else { return }
        seenSinceSend = true
        switch event.type {
        case .draft:
            let text = event.text ?? ""
            guard !text.isEmpty else { return }
            reply.drafts = true
            textSinceSend = true
            reply.stepHasText = true
            quietAll()
            syncWorking(at: at)
            if state == .userTalking {
                reply.dropStep = true
                return
            }
            guard !reply.dropStep else { return }
            speak(chunker.feed(text), status: false)
        case .stepEnd:
            reply.waiting = false
            if state == .userTalking || reply.dropStep {
                reply.dropStep = false
            } else {
                speak(chunker.finish(), status: false)
            }
            chunker = makeChunker()
            reply.stepHasText = false
            syncWorking(at: at)
        case .status:
            reply.phase = event.phase ?? ""
            reply.detail = event.detail ?? ""
            reply.hasStatus = true
            let said = (reply.phase + " " + reply.detail).lowercased()
            reply.waiting = said.contains("waiting") || said.contains("pausing")
            syncWorking(at: at)
        case .toolStart:
            reply.tools += 1
            reply.tool = event.name
            reply.toolStartedAt = at
            reply.waiting = false
            reply.graceUntil = nil
            syncWorking(at: at)
        case .toolEnd:
            reply.tools = max(0, reply.tools - 1)
            if reply.tools == 0 {
                reply.tool = nil
                reply.graceUntil = at + tuning.workingGraceS
                quietSince = max(quietSince, at)
            }
            syncWorking(at: at)
        case .answer:
            quietAll()
            syncWorking(at: at)
            // A stream with no drafts (a typed-only reply) is still spoken, from the answer the chat keeps.
            guard !reply.drafts, state != .userTalking else { return }
            var fresh = makeChunker()
            let sentences = fresh.feed(event.text ?? "") + fresh.finish()
            if sentences.contains(where: { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) {
                textSinceSend = true
            }
            speak(sentences, status: false)
        case .end:
            finishReply(at: at, failure: event.failure, flush: event.failure == nil)
        }
    }

    /// A new `reply` id: the per-reply state starts again.
    private mutating func newReply(_ id: String, at: TimeInterval) {
        if let old = reply.id { doneReplies.insert(old) }
        reply = Reply(id: id)
        chunker = makeChunker()
        if dropUnseen {
            dropUnseen = false
            reply.dropped = true
            syncWorking(at: at)
            return
        }
        // The silence of a reply you are waiting for runs from your turn's end, not from its first event; only a
        // reply nobody was waiting for starts it now.
        if !replyOpen { quietSince = at }
        replyOpen = true
        textSinceSend = false
        lastFillerText = nil
        if state == .listening { setState(.thinking) }
        syncWorking(at: at)
    }

    /// Answer text started: no tool, no wait, no grace keeps the working sound on.
    private mutating func quietAll() {
        reply.waiting = false
        reply.graceUntil = nil
        reply.tools = 0
        reply.tool = nil
    }

    private mutating func finishReply(at: TimeInterval, failure: String?, flush: Bool) {
        if flush && reply.running && !reply.dropStep && state != .userTalking {
            speak(chunker.finish(), status: false)
        }
        chunker = makeChunker()
        if let id = reply.id {
            reply.ended = true
            doneReplies.insert(id)
        }
        replyOpen = false
        syncWorking(at: at)
        if let failure, failure != "stopped by you" { out.append(.showStatus(failure)) }
        settle()
    }

    /// The send or the stream failed: say so, and leave the reply it was following.
    private mutating func fail(_ message: String, at: TimeInterval) {
        out.append(.showStatus(message))
        if replyOpen && !seenSinceSend { dropUnseen = true }
        if reply.running { reply.dropped = true }
        replyOpen = false
        latencyFrom = nil
        syncWorking(at: at)
        settle()
    }

    /// The reply is over and nothing is left to say: listening.
    private mutating func settle() {
        if queued.isEmpty && !replyOpen && (state == .thinking || state == .speaking) { setState(.listening) }
    }

    // MARK: - Working sound and fillers

    private mutating func syncWorking(at: TimeInterval) {
        let grace = reply.graceUntil.map { at < $0 } ?? false
        let on = tuning.workingSound && replyOpen && reply.running && (reply.tools > 0 || reply.waiting || grace)
        guard on != working else { return }
        working = on
        out.append(.workingSound(on))
        if on && state == .userTalking && !ducked {
            ducked = true
            out.append(.duckWorkingSound(true))
        }
    }

    private mutating func unduck() {
        if ducked {
            ducked = false
            out.append(.duckWorkingSound(false))
        }
    }

    /// One status line in a long silence before the reply's first text.
    private mutating func fillIfQuiet(at: TimeInterval) {
        guard state == .thinking, replyOpen, !textSinceSend, queued.isEmpty else { return }
        if let lastFillerAt, at - lastFillerAt < tuning.fillerS * 3 { return }
        let line: String
        if reply.running, reply.tools > 0 {
            guard at - reply.toolStartedAt >= tuning.fillerS * 3 else { return }
            line = toolLine(reply.tool)
        } else {
            guard at - quietSince >= tuning.fillerS else { return }
            line = statusLine()
        }
        guard line != lastFillerText else { return }
        lastFillerAt = at
        lastFillerText = line
        speak([line], status: true)
    }

    private func toolLine(_ tool: String?) -> String {
        let name = (tool ?? "").replacingOccurrences(of: "_", with: " ").trimmingCharacters(in: .whitespaces)
        switch language {
        case .en: return "\(instanceName) is using \(name.isEmpty ? "a tool" : name)"
        case .it: return "\(instanceName) sta usando \(name.isEmpty ? "uno strumento" : name)"
        }
    }

    private func statusLine() -> String {
        let thinking = language == .en ? "\(instanceName) is thinking" : "\(instanceName) sta pensando"
        guard reply.running, reply.hasStatus else { return thinking }      // never a past reply's status
        let detail = reply.detail.trimmingCharacters(in: .whitespaces)
        var what = detail
        if what.isEmpty, reply.phase.lowercased().hasPrefix("waiting:"),
           let range = reply.phase.range(of: " is ") {
            what = String(reply.phase[range.upperBound...])
            if let open = what.range(of: " (", options: .backwards), what.hasSuffix(")") {
                what = String(what[..<open.lowerBound])
            }
            what = what.trimmingCharacters(in: .whitespaces)
        }
        guard !what.isEmpty else { return thinking }
        switch language {
        case .en:
            return "\(instanceName) is \(what)"
        case .it:
            switch what.lowercased() {
            case "pausing its cycle for you": return "\(instanceName) sta mettendo in pausa il suo ciclo per te"
            case "finishing a run": return "\(instanceName) sta finendo un'esecuzione"
            case "in another conversation": return "\(instanceName) è in un'altra conversazione"
            default: return thinking
            }
        }
    }

    // MARK: - Small helpers

    private mutating func speak(_ sentences: [String], status: Bool) {
        for sentence in sentences {
            let text = sentence.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let id = nextId
            nextId += 1
            queued.insert(id)
            if status { statusIds.insert(id) }
            out.append(.speak(id: id, text: text, isStatus: status))
        }
    }

    private mutating func setState(_ new: VoiceState) {
        guard new != state else { return }
        state = new
        out.append(.state(new))
    }

    private mutating func show(_ text: String) {
        guard text != shown else { return }
        shown = text
        out.append(.showTranscript(text))
    }

    /// Lower case, letters and digits only, words separated by one space.
    static func normalized(_ text: String) -> String {
        let mapped = String(text.lowercased().map { $0.isLetter || $0.isNumber ? $0 : " " })
        return mapped.split(separator: " ").joined(separator: " ")
    }

    /// `text` without its first `count` words as `normalized` counts them ("e.g." is two), keeping the rest as said.
    static func dropping(_ count: Int, wordsOf text: String) -> String {
        var left = count
        var tokens = text.split(whereSeparator: { $0.isWhitespace })[...]
        while left > 0, let token = tokens.first {
            left -= wordCount(String(token))
            tokens = tokens.dropFirst()
        }
        return tokens.joined(separator: " ")
    }

    static func wordCount(_ text: String) -> Int {
        normalized(text).split(separator: " ").count
    }

    /// "stop", "stop it", "basta", "fermati", said alone, in either language.
    static let stopWords: Set<String> = ["stop", "stop it", "basta", "fermati"]

    static func isStopWord(_ text: String) -> Bool {
        stopWords.contains(normalized(text))
    }
}
