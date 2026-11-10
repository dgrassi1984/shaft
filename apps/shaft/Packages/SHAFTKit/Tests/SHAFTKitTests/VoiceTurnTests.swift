import Foundation
import Testing
@testable import SHAFTKit

/// A tiny sentence chunker for these tests, so VoiceTurn is tested without the real markdown one: a sentence ends at
/// ".", "?" or "!"; `finish` gives what is still open.
struct TestChunker: SpeechChunking {
    private var open = ""

    mutating func feed(_ markdown: String) -> [String] {
        var sentences: [String] = []
        for character in markdown {
            open.append(character)
            if ".?!".contains(character) {
                let sentence = open.trimmingCharacters(in: .whitespaces)
                if !sentence.isEmpty { sentences.append(sentence) }
                open = ""
            }
        }
        return sentences
    }

    mutating func finish() -> [String] {
        defer { open = "" }
        let rest = open.trimmingCharacters(in: .whitespaces)
        return rest.isEmpty ? [] : [rest]
    }
}

private let r1 = "3f0c9e21a4b7d658"
private let r2 = "7a1e5c9b2d4f6083"
private let waitingPhase = "waiting: tundra is pausing its cycle for you (live-20261109)"

// MARK: - Scripted inputs (invented text)

private func vol(_ text: String, _ at: TimeInterval) -> VoiceInput {
    .transcript(TranscriptEvent(text: text, isFinal: false, at: at))
}
private func fin(_ text: String, _ at: TimeInterval) -> VoiceInput {
    .transcript(TranscriptEvent(text: text, isFinal: true, at: at))
}
private func tick(_ at: TimeInterval) -> VoiceInput { .tick(at: at) }
private func started(_ id: Int, _ at: TimeInterval) -> VoiceInput { .speechStarted(id: id, at: at) }
private func finished(_ id: Int, _ at: TimeInterval) -> VoiceInput { .speechFinished(id: id, at: at) }

private func event(_ type: ChatStreamEvent.Kind, _ reply: String, text: String? = nil, answer: Bool? = nil,
                   phase: String? = nil, detail: String? = nil, name: String? = nil, ok: Bool? = nil,
                   failure: String? = nil) -> ChatStreamEvent {
    ChatStreamEvent(seq: 0, type: type, reply: reply, text: text, answer: answer, phase: phase, detail: detail,
                    name: name, ok: ok, failure: failure)
}
private func draft(_ text: String, _ at: TimeInterval, _ reply: String = r1) -> VoiceInput {
    .stream(event(.draft, reply, text: text), at: at)
}
private func stepEnd(answer: Bool, _ at: TimeInterval, _ reply: String = r1) -> VoiceInput {
    .stream(event(.stepEnd, reply, answer: answer), at: at)
}
private func status(_ phase: String?, _ detail: String?, _ at: TimeInterval, _ reply: String = r1) -> VoiceInput {
    .stream(event(.status, reply, phase: phase, detail: detail), at: at)
}
private func toolStart(_ name: String, _ at: TimeInterval, _ reply: String = r1) -> VoiceInput {
    .stream(event(.toolStart, reply, name: name), at: at)
}
private func toolEnd(_ name: String, _ at: TimeInterval, _ reply: String = r1) -> VoiceInput {
    .stream(event(.toolEnd, reply, name: name, ok: true), at: at)
}
private func answer(_ text: String, _ at: TimeInterval, _ reply: String = r1) -> VoiceInput {
    .stream(event(.answer, reply, text: text), at: at)
}
private func end(_ failure: String?, _ at: TimeInterval, _ reply: String = r1) -> VoiceInput {
    .stream(event(.end, reply, failure: failure), at: at)
}

private func makeTurn(_ language: VoiceLanguage = .en, tuning: VoiceTuning = VoiceTuning()) -> VoiceTurn {
    VoiceTurn(tuning: tuning, language: language, instanceName: "Tundra", chunker: { TestChunker() })
}

/// Listening, then "What is the weather?" heard and sent at 2.5 (default tuning: endpoint 0.8 s, barge-in 2 words,
/// filler 3 s, working sound on with 0.6 s of grace).
private func thinkingTurn(_ language: VoiceLanguage = .en, tuning: VoiceTuning = VoiceTuning()) -> VoiceTurn {
    var turn = makeTurn(language, tuning: tuning)
    let actions = turn.run(.start(at: 0), vol("what is the", 1.0), fin("What is the weather?", 1.5), tick(2.5))
    precondition(actions == [.state(.listening), .state(.userTalking), .showTranscript("what is the"),
                             .showTranscript("What is the weather?"), .send(text: "What is the weather?"),
                             .showTranscript(""), .state(.thinking)], "\(actions)")
    return turn
}

extension VoiceTurn {
    mutating func run(_ inputs: VoiceInput...) -> [VoiceAction] {
        var actions: [VoiceAction] = []
        for input in inputs { actions += handle(input) }
        return actions
    }
}

/// The working sound alternates on, off, on…: never two ons (or two offs) in a row.
private func workingAlternates(_ actions: [VoiceAction]) -> Bool {
    var on = false
    for action in actions {
        if case .workingSound(let value) = action {
            if value == on { return false }
            on = value
        }
    }
    return true
}

struct VoiceTurnTests {
    // MARK: Start, your turn, end of turn

    @Test func startListensOnceAndNothingHappensBeforeIt() {
        var turn = makeTurn()
        #expect(turn.state == .idle)
        #expect(turn.run(vol("hello there", 0.5), fin("Hello there.", 0.6), tick(5), draft("Hi.", 5.1),
                         started(1, 5.2), .sendFailed(message: "no", at: 5.3)) == [])
        #expect(turn.state == .idle)
        #expect(turn.handle(.start(at: 6)) == [.state(.listening)])
        #expect(turn.handle(.start(at: 7)) == [])
        #expect(turn.state == .listening)
    }

    @Test func aTranscriptWhileListeningShowsYouTalkingAndTheLiveTextIsFinalsSoFarPlusTheVolatile() {
        var turn = makeTurn()
        _ = turn.handle(.start(at: 0))
        #expect(turn.handle(vol("what", 1.0)) == [.state(.userTalking), .showTranscript("what")])
        #expect(turn.handle(vol("what is the", 1.2)) == [.showTranscript("what is the")])
        #expect(turn.handle(vol("what is the", 1.3)) == [])
        #expect(turn.handle(fin("What is the weather?", 1.5)) == [.showTranscript("What is the weather?")])
        #expect(turn.handle(vol("in Milan", 1.8)) == [.showTranscript("What is the weather? in Milan")])
        #expect(turn.state == .userTalking)
    }

    @Test func theTurnEndsAfterEndpointOfSilenceAfterTheLastFinalAndSendsAllItsFinalText() {
        var turn = makeTurn()
        _ = turn.run(.start(at: 0), vol("what is", 1.0), fin("What is the weather?", 1.5), fin("In Milan.", 2.0))
        #expect(turn.handle(tick(2.7)) == [])
        #expect(turn.handle(tick(2.9)) == [.send(text: "What is the weather? In Milan."), .showTranscript(""),
                                           .state(.thinking)])
        #expect(turn.handle(tick(3.5)) == [])
        #expect(turn.state == .thinking)
    }

    @Test func aNewerResultHoldsTheEndOfTurn() {
        var turn = makeTurn()
        _ = turn.run(.start(at: 0), fin("What is the weather?", 1.5))
        #expect(turn.handle(vol("and", 2.0)) == [.showTranscript("What is the weather? and")])
        #expect(turn.handle(tick(2.4)) == [])
        #expect(turn.handle(tick(3.0)) == [])
        #expect(turn.handle(fin("and tomorrow?", 3.1)) == [.showTranscript("What is the weather? and tomorrow?")])
        #expect(turn.handle(tick(3.8)) == [])
        #expect(turn.handle(tick(4.0)) == [.send(text: "What is the weather? and tomorrow?"), .showTranscript(""),
                                           .state(.thinking)])
    }

    @Test func aVolatileResultUnchangedForThreeEndpointsCountsAsFinalAndItsLateFinalIsNotHeardTwice() {
        var turn = makeTurn()
        _ = turn.handle(.start(at: 0))
        #expect(turn.handle(vol("turn on the lights", 1.0)) == [.state(.userTalking),
                                                                 .showTranscript("turn on the lights")])
        #expect(turn.handle(vol("turn on the lights", 2.0)) == [])     // unchanged: the wait does not restart
        #expect(turn.handle(tick(3.3)) == [])
        #expect(turn.handle(tick(3.5)) == [.send(text: "turn on the lights"), .showTranscript(""),
                                           .state(.thinking)])
        // The transcriber's own final for the same words comes late: not a second turn, not a barge-in.
        #expect(turn.handle(fin("Turn on the lights.", 3.6)) == [])
        #expect(turn.handle(tick(5.0)) == [])
        #expect(turn.state == .thinking)
    }

    /// Bug (M2 end-to-end): a stale volatile was sent after endpointS × 3, then the transcriber's late final with the
    /// missing words was taken as new speech and sent whole — the same sentence twice.
    @Test func aLateFinalThatExtendsATurnAlreadySentCountsOnlyItsNewWords() {
        var turn = makeTurn()
        _ = turn.run(.start(at: 0), vol("wait what about the lantern by the", 1.0), tick(1.9))
        #expect(turn.handle(tick(3.5)) == [.send(text: "wait what about the lantern by the"), .showTranscript(""),
                                           .state(.thinking)])
        // One word more: like any short word while it replies, not a turn.
        #expect(turn.handle(fin("Wait, what about the lantern by the door?", 4.0)) == [])
        #expect(turn.handle(tick(5.5)) == [])
        #expect(turn.state == .thinking)

        var again = makeTurn()
        _ = again.run(.start(at: 0), vol("wait what about the lantern", 1.0), tick(1.9), tick(3.5))
        let late = again.handle(fin("Wait, what about the lantern and the gate by the door?", 4.0))
        #expect(late.contains(.showTranscript("and the gate by the door?")))
        let sent = again.run(tick(4.9)).compactMap { if case .send(let t) = $0 { t } else { nil } }
        #expect(sent == ["and the gate by the door?"])
    }

    @Test func blankResultsAreNotSpeechAndDoNotHoldTheEndOfTurn() {
        var turn = makeTurn()
        _ = turn.handle(.start(at: 0))
        #expect(turn.handle(vol("", 1.0)) == [])
        #expect(turn.state == .listening)
        #expect(turn.handle(fin("What time is it?", 1.5)) == [.state(.userTalking),
                                                               .showTranscript("What time is it?")])
        #expect(turn.handle(vol("", 2.0)) == [])        // the volatile starting again empty is not a newer result
        #expect(turn.handle(tick(2.4)) == [.send(text: "What time is it?"), .showTranscript(""),
                                           .state(.thinking)])
    }

    @Test func aTurnTheTranscriberTakesBackIsDroppedWithoutSending() {
        var turn = makeTurn()
        _ = turn.handle(.start(at: 0))
        #expect(turn.handle(vol("um", 1.0)) == [.state(.userTalking), .showTranscript("um")])
        #expect(turn.handle(vol("", 1.2)) == [.showTranscript("")])
        #expect(turn.handle(tick(3.3)) == [])
        #expect(turn.handle(tick(3.5)) == [.state(.listening)])
    }

    // MARK: Stop words

    @Test func stopSaidAloneWhileThinkingStopsTheReplyWithoutSendingAndItsRestIsNotSpoken() {
        var turn = thinkingTurn()
        #expect(turn.handle(status("model", "thinking", 2.8)) == [])
        #expect(turn.handle(toolStart("read_file", 3.0)) == [.workingSound(true)])
        #expect(turn.handle(fin("Stop.", 4.0)) == [.stopSpeaking, .stopReply, .workingSound(false),
                                                   .state(.listening)])
        #expect(turn.handle(tick(6.0)) == [])
        #expect(turn.handle(draft("It is sunny.", 4.2)) == [])
        #expect(turn.handle(end("stopped by you", 4.5)) == [])
        #expect(turn.handle(.streamClosed(message: nil, at: 4.6)) == [])
        #expect(turn.state == .listening)
    }

    @Test func stopWordsAreCaseAndPunctuationInsensitiveInBothLanguages() {
        for (language, said) in [(VoiceLanguage.en, "Stop!"), (.en, "STOP IT."), (.en, "stop, it"),
                                 (.it, "Basta!"), (.it, "fermati"), (.it, "stop")] {
            var turn = thinkingTurn(language)
            _ = turn.handle(status("model", "thinking", 2.8))
            #expect(turn.handle(fin(said, 3.0)) == [.stopSpeaking, .stopReply, .state(.listening)], "\(said)")
        }
        // Not said alone: an ordinary barge-in, sent as an interjection.
        var turn = thinkingTurn()
        #expect(turn.handle(fin("Stop talking about Milan.", 3.0)) == [
            .stopSpeaking, .state(.userTalking), .showTranscript("Stop talking about Milan.")])
        #expect(turn.handle(tick(4.0)) == [.send(text: "Stop talking about Milan."), .showTranscript(""),
                                           .state(.thinking)])
    }

    @Test func stopSaidWhileListeningIsSentAsOrdinaryText() {
        var turn = makeTurn()
        _ = turn.handle(.start(at: 0))
        #expect(turn.handle(fin("Stop.", 1.0)) == [.state(.userTalking), .showTranscript("Stop.")])
        #expect(turn.handle(tick(2.0)) == [.send(text: "Stop."), .showTranscript(""), .state(.thinking)])
    }

    @Test func stopWhileSpeakingStopsTheSpeechAndTheReply() {
        var turn = thinkingTurn()
        #expect(turn.handle(draft("It is sunny in Milan. It will rain tomorrow.", 3.0)) == [
            .speak(id: 1, text: "It is sunny in Milan.", isStatus: false),
            .speak(id: 2, text: "It will rain tomorrow.", isStatus: false)])
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.handle(fin("basta", 3.8)) == [.stopSpeaking, .stopReply, .state(.listening)])
        #expect(turn.run(finished(1, 3.8), finished(2, 3.8), started(2, 3.9)) == [])
        #expect(turn.state == .listening)
    }

    @Test func stopAfterTheReplyEndedOnlyStopsTheSpeechStillQueued() {
        var turn = thinkingTurn()
        _ = turn.run(draft("It is sunny in Milan. It will rain tomorrow.", 3.0), stepEnd(answer: true, 3.1),
                     answer("It is sunny in Milan. It will rain tomorrow.", 3.2), end(nil, 3.3))
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.handle(fin("Stop.", 4.0)) == [.stopSpeaking, .state(.listening)])
    }

    @Test func aStopBeforeTheReplysFirstEventDropsThatReplyWhenItShowsUp() {
        var turn = thinkingTurn()
        #expect(turn.handle(fin("Stop.", 3.0)) == [.stopSpeaking, .stopReply, .state(.listening)])
        #expect(turn.run(status(waitingPhase, nil, 3.1), draft("It is sunny.", 3.2), answer("It is sunny.", 3.3),
                         end("stopped by you", 3.4)) == [])
        #expect(turn.state == .listening)
    }

    @Test func theCloseOfAStoppedReplyDoesNotEndTheNextTurnsWait() {
        var turn = thinkingTurn()
        _ = turn.handle(status("model", "thinking", 2.8))
        #expect(turn.handle(fin("Stop.", 3.0)) == [.stopSpeaking, .stopReply, .state(.listening)])
        #expect(turn.handle(fin("What about Rome?", 4.0)) == [.state(.userTalking),
                                                               .showTranscript("What about Rome?")])
        #expect(turn.handle(tick(5.0)) == [.send(text: "What about Rome?"), .showTranscript(""),
                                           .state(.thinking)])
        #expect(turn.handle(end("stopped by you", 5.1)) == [])
        #expect(turn.handle(.streamClosed(message: nil, at: 5.2)) == [])
        #expect(turn.state == .thinking)
        #expect(turn.handle(draft("Rome is cloudy.", 6.0, r2)) == [
            .speak(id: 1, text: "Rome is cloudy.", isStatus: false)])
        #expect(turn.handle(started(1, 6.5)) == [.state(.speaking), .latency(1.5)])
    }

    // MARK: Barge-in

    @Test func bargeInWhileSpeakingStopsSpeechAtOnceAndTheInterruptedStepIsNotResumed() {
        var turn = thinkingTurn()
        #expect(turn.handle(draft("It is sunny in Milan. It will", 3.0)) == [
            .speak(id: 1, text: "It is sunny in Milan.", isStatus: false)])
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.handle(vol("wait", 4.0)) == [])                // one word: not yet a barge-in
        #expect(turn.handle(vol("wait actually", 4.2)) == [.stopSpeaking, .state(.userTalking),
                                                           .showTranscript("wait actually")])
        #expect(turn.handle(finished(1, 4.2)) == [])
        // Nothing is spoken while you talk, and the step you cut off stays dropped until its end.
        #expect(turn.handle(draft(" rain tomorrow. And the day after.", 4.4)) == [])
        #expect(turn.handle(fin("Wait, actually make it Rome.", 4.6)) == [
            .showTranscript("Wait, actually make it Rome.")])
        #expect(turn.handle(tick(5.5)) == [.send(text: "Wait, actually make it Rome."), .showTranscript(""),
                                           .state(.thinking)])
        #expect(turn.handle(draft(" Also windy.", 5.6)) == [])
        #expect(turn.handle(stepEnd(answer: false, 5.7)) == [])
        #expect(turn.handle(toolStart("web_search", 5.8)) == [.workingSound(true)])
        #expect(turn.handle(toolEnd("web_search", 6.0)) == [])
        // The next step answers the interjection, from a fresh chunker (no fragment of the dropped step).
        #expect(turn.handle(draft("In Rome it is cloudy.", 6.2)) == [
            .workingSound(false), .speak(id: 2, text: "In Rome it is cloudy.", isStatus: false)])
        #expect(turn.handle(started(2, 6.5)) == [.state(.speaking), .latency(1.0)])
    }

    @Test func bargeInWhileThinkingStopsAndShowsYouTalking() {
        var turn = thinkingTurn()
        #expect(turn.handle(vol("hold on", 3.0)) == [.stopSpeaking, .state(.userTalking),
                                                     .showTranscript("hold on")])
        #expect(turn.handle(fin("Hold on, use Rome.", 3.3)) == [.showTranscript("Hold on, use Rome.")])
        #expect(turn.handle(tick(4.2)) == [.send(text: "Hold on, use Rome."), .showTranscript(""),
                                           .state(.thinking)])
    }

    @Test func bargeInWhileItsLastSentencesAreSpokenAfterTheReplyEnded() {
        var turn = thinkingTurn()
        _ = turn.run(answer("One. Two.", 3.0), end(nil, 3.1))
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.handle(vol("wait a second", 4.0)) == [.stopSpeaking, .state(.userTalking),
                                                           .showTranscript("wait a second")])
        #expect(turn.handle(fin("Wait a second.", 4.2)) == [.showTranscript("Wait a second.")])
        #expect(turn.handle(tick(5.1)) == [.send(text: "Wait a second."), .showTranscript(""), .state(.thinking)])
    }

    @Test func aFewWordsWhileItSpeaksAreNeverSentAsATurn() {
        var turn = thinkingTurn()
        _ = turn.run(draft("It is sunny in Milan.", 3.0), stepEnd(answer: true, 3.1))
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.handle(fin("okay", 3.8)) == [])
        #expect(turn.handle(tick(4.8)) == [])                 // its end of turn: dropped, not sent
        #expect(turn.handle(vol("right", 5.0)) == [])
        #expect(turn.handle(tick(7.5)) == [])                 // unchanged volatile, taken as final: dropped too
        #expect(turn.state == .speaking)
        #expect(turn.run(answer("It is sunny in Milan.", 7.6), end(nil, 7.7)) == [])
        #expect(turn.handle(finished(1, 8.0)) == [.state(.listening)])
    }

    @Test func wordsSaidAsItFinishedSpeakingBecomeYourNextTurn() {
        var turn = thinkingTurn()
        _ = turn.run(answer("Sunny.", 3.0), end(nil, 3.1))
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.handle(fin("and", 3.8)) == [])
        #expect(turn.handle(finished(1, 4.0)) == [.state(.listening)])
        #expect(turn.handle(vol("tomorrow", 4.2)) == [.state(.userTalking), .showTranscript("and tomorrow")])
        #expect(turn.handle(fin("tomorrow?", 4.4)) == [.showTranscript("and tomorrow?")])
        #expect(turn.handle(tick(5.3)) == [.send(text: "and tomorrow?"), .showTranscript(""), .state(.thinking)])
    }

    // MARK: The stream

    @Test func draftsAreSpokenSentenceBySentenceAndTheReplyEndsAfterTheLastQueuedUtterance() {
        var turn = thinkingTurn()
        #expect(turn.handle(draft("It is sun", 3.0)) == [])
        #expect(turn.handle(draft("ny in Milan. Tomor", 3.1)) == [
            .speak(id: 1, text: "It is sunny in Milan.", isStatus: false)])
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.handle(draft("row it will rain.", 3.6)) == [
            .speak(id: 2, text: "Tomorrow it will rain.", isStatus: false)])
        #expect(turn.handle(stepEnd(answer: true, 3.7)) == [])
        #expect(turn.handle(answer("It is sunny in Milan. Tomorrow it will rain.", 3.8)) == [])  // not twice
        #expect(turn.handle(end(nil, 3.9)) == [])               // two utterances still to speak
        #expect(turn.handle(finished(1, 4.0)) == [])
        #expect(turn.handle(started(2, 4.0)) == [])             // latency once per reply
        #expect(turn.handle(finished(2, 5.0)) == [.state(.listening)])
    }

    @Test func narrationIsSpokenAtItsStepEndAndTheWorkingSoundStopsWhenAnswerTextArrives() {
        var turn = thinkingTurn()
        #expect(turn.handle(draft("Let me look", 3.0)) == [])
        #expect(turn.handle(stepEnd(answer: false, 3.1)) == [
            .speak(id: 1, text: "Let me look", isStatus: false)])
        #expect(turn.handle(toolStart("read_file", 3.2)) == [.workingSound(true)])
        #expect(turn.handle(toolEnd("read_file", 4.0)) == [])
        #expect(turn.handle(draft("The file says yes.", 4.1)) == [
            .workingSound(false), .speak(id: 2, text: "The file says yes.", isStatus: false)])
    }

    @Test func aReplyWithNoDraftsIsSpokenFromItsAnswer() {
        var turn = thinkingTurn()
        #expect(turn.handle(answer("Done. The note is saved", 3.0)) == [
            .speak(id: 1, text: "Done.", isStatus: false), .speak(id: 2, text: "The note is saved", isStatus: false)])
        #expect(turn.handle(end(nil, 3.1)) == [])
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.run(finished(1, 4.0), started(2, 4.0)) == [])
        #expect(turn.handle(finished(2, 5.0)) == [.state(.listening)])
    }

    @Test func anEmptyAnswerEndsTheReplyAtOnce() {
        var turn = thinkingTurn()
        #expect(turn.handle(answer("", 3.0)) == [])
        #expect(turn.handle(end(nil, 3.1)) == [.state(.listening)])
    }

    @Test func aFailedReplyShowsItsNoteButStoppedByYouDoesNot() {
        var turn = thinkingTurn()
        #expect(turn.handle(draft("It is sun", 3.0)) == [])
        // The fragment of a failed step is not spoken.
        #expect(turn.handle(end("error: the model timed out", 3.1)) == [
            .showStatus("error: the model timed out"), .state(.listening)])

        var other = thinkingTurn()
        #expect(other.handle(end("stopped by you", 3.0)) == [.state(.listening)])
    }

    @Test func aNormalEndWithoutAStepEndStillSpeaksWhatIsOpen() {
        var turn = thinkingTurn()
        #expect(turn.handle(draft("It is sunny", 3.0)) == [])
        #expect(turn.handle(end(nil, 3.1)) == [.speak(id: 1, text: "It is sunny", isStatus: false)])
        #expect(turn.state == .thinking)
    }

    @Test func aNewReplyIdResetsPerReplyState() {
        // Its drafts flag: the new reply has no drafts, so its answer is spoken.
        var turn = thinkingTurn()
        #expect(turn.handle(draft("Let me check", 3.0)) == [])
        #expect(turn.handle(answer("Here it is.", 3.5, r2)) == [
            .speak(id: 1, text: "Here it is.", isStatus: false)])
        // A late event of the old reply is ignored.
        #expect(turn.handle(draft(" the weather.", 3.6)) == [])

        // Its chunker: no fragment of the old reply's open sentence.
        var fresh = thinkingTurn()
        #expect(fresh.handle(draft("Let me check", 3.0)) == [])
        #expect(fresh.handle(draft("Rome is cloudy.", 3.5, r2)) == [
            .speak(id: 1, text: "Rome is cloudy.", isStatus: false)])

        // Its tools: the old reply's working sound stops.
        var tools = thinkingTurn()
        #expect(tools.handle(toolStart("read_file", 3.0)) == [.workingSound(true)])
        #expect(tools.handle(status("model", "thinking", 3.5, r2)) == [.workingSound(false)])
        #expect(tools.handle(tick(4.5)) == [])                 // no grace carried over from the old reply's tool
    }

    @Test func aNewReplyWhileListeningIsFollowedWithoutASecondLatency() {
        var turn = thinkingTurn()
        _ = turn.run(answer("Yes.", 3.0), end(nil, 3.1))
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.handle(finished(1, 4.0)) == [.state(.listening)])
        #expect(turn.handle(draft("I also checked tomorrow.", 6.0, r2)) == [
            .state(.thinking), .speak(id: 2, text: "I also checked tomorrow.", isStatus: false)])
        #expect(turn.handle(started(2, 6.2)) == [.state(.speaking)])
    }

    // MARK: Working sound

    @Test func theWorkingSoundRunsFromToolStartAndBackToBackToolsLeaveNoGap() {
        var turn = thinkingTurn()
        var log: [VoiceAction] = []
        func step(_ input: VoiceInput, _ expected: [VoiceAction], sourceLocation: SourceLocation = #_sourceLocation) {
            let actions = turn.handle(input)
            log += actions
            #expect(actions == expected, sourceLocation: sourceLocation)
        }
        step(stepEnd(answer: false, 2.9), [])
        step(toolStart("read_file", 3.0), [.workingSound(true)])
        step(tick(3.5), [])
        step(toolEnd("read_file", 4.5), [])
        step(tick(4.7), [])
        step(toolStart("web_search", 4.8), [])       // still on: no gap, no second on
        step(tick(5.5), [])
        step(toolEnd("web_search", 6.3), [])
        step(tick(6.8), [])                          // within the grace
        step(tick(7.0), [.workingSound(false)])
        #expect(workingAlternates(log))
    }

    @Test func theWorkingSoundPlaysWhileTheReplyWaitsForACycle() {
        var turn = thinkingTurn()
        #expect(turn.handle(status(waitingPhase, nil, 2.8)) == [.workingSound(true)])
        #expect(turn.handle(status("model", "thinking", 4.0)) == [.workingSound(false)])
        #expect(turn.handle(status("queue", "pausing a cycle", 4.2)) == [.workingSound(true)])
        #expect(turn.handle(toolStart("read_file", 4.5)) == [])
        #expect(turn.handle(toolEnd("read_file", 5.0)) == [])
        #expect(turn.handle(tick(5.7)) == [.workingSound(false)])
    }

    @Test func theWorkingSoundIsNeverTurnedOnTwiceWithoutAnOffBetween() {
        var turn = thinkingTurn()
        let actions = turn.run(status(waitingPhase, nil, 2.8), toolStart("read_file", 3.0),
                               status(waitingPhase, nil, 3.1), toolStart("web_search", 3.2),
                               toolEnd("read_file", 3.5), toolEnd("web_search", 3.6), tick(4.0),
                               toolStart("read_file", 4.1), tick(4.3), toolEnd("read_file", 4.4), tick(5.1),
                               status(waitingPhase, nil, 5.2), draft("Done.", 5.3), status(waitingPhase, nil, 5.4),
                               end(nil, 5.5))
        #expect(actions == [.workingSound(true), .workingSound(false), .workingSound(true), .workingSound(false),
                            .speak(id: 1, text: "Done.", isStatus: false), .workingSound(true), .workingSound(false)])
        #expect(workingAlternates(actions))
    }

    @Test func theWorkingSoundStopsAtTheEndOfTheReplyAndAtEnd() {
        var turn = thinkingTurn()
        #expect(turn.handle(toolStart("read_file", 3.0)) == [.workingSound(true)])
        #expect(turn.handle(end("error: the tool failed", 3.5)) == [
            .workingSound(false), .showStatus("error: the tool failed"), .state(.listening)])

        var ended = thinkingTurn()
        #expect(ended.handle(toolStart("read_file", 3.0)) == [.workingSound(true)])
        #expect(ended.handle(.end(at: 3.5)) == [.stopSpeaking, .workingSound(false), .state(.ended)])
    }

    @Test func theWorkingSoundSwitchedOffNeverPlaysOrDucks() {
        var turn = thinkingTurn(tuning: VoiceTuning(workingSound: false))
        #expect(turn.run(status(waitingPhase, nil, 2.8), toolStart("read_file", 3.0), toolEnd("read_file", 3.5),
                         toolStart("web_search", 3.6)) == [])
        #expect(turn.handle(vol("hold on", 4.0)) == [.stopSpeaking, .state(.userTalking),
                                                     .showTranscript("hold on")])
    }

    @Test func theWorkingSoundDucksWhileYouTalk() {
        var turn = thinkingTurn()
        #expect(turn.handle(toolStart("web_search", 3.0)) == [.workingSound(true)])
        #expect(turn.handle(vol("hold on", 3.5)) == [.stopSpeaking, .state(.userTalking), .duckWorkingSound(true),
                                                     .showTranscript("hold on")])
        #expect(turn.handle(fin("Hold on, use Rome.", 3.8)) == [.showTranscript("Hold on, use Rome.")])
        #expect(turn.handle(tick(4.7)) == [.send(text: "Hold on, use Rome."), .showTranscript(""),
                                           .duckWorkingSound(false), .state(.thinking)])

        // A tool that starts while you talk plays ducked.
        var later = thinkingTurn()
        #expect(later.handle(vol("hold on", 3.0)) == [.stopSpeaking, .state(.userTalking),
                                                      .showTranscript("hold on")])
        #expect(later.handle(toolStart("web_search", 3.2)) == [.workingSound(true), .duckWorkingSound(true)])
    }

    // MARK: Fillers

    @Test func oneStatusLineAfterSilenceAndTheSameLineIsNotReadTwice() {
        var turn = thinkingTurn()
        #expect(turn.handle(tick(5.4)) == [])
        #expect(turn.handle(tick(5.6)) == [.speak(id: 1, text: "Tundra is thinking", isStatus: true)])
        #expect(turn.handle(started(1, 5.7)) == [])           // a status line: still thinking, no latency
        #expect(turn.state == .thinking)
        #expect(turn.handle(finished(1, 7.0)) == [])
        #expect(turn.handle(tick(15.0)) == [])
        #expect(turn.handle(status("model", "reading ledger.md", 15.1)) == [])
        #expect(turn.handle(tick(15.2)) == [.speak(id: 2, text: "Tundra is reading ledger.md", isStatus: true)])
    }

    @Test func atMostOneFillerEveryThreeFillerS() {
        var turn = thinkingTurn()
        #expect(turn.handle(tick(5.6)) == [.speak(id: 1, text: "Tundra is thinking", isStatus: true)])
        #expect(turn.run(finished(1, 6.5), status("model", "reading ledger.md", 7.0)) == [])
        #expect(turn.handle(tick(14.5)) == [])
        #expect(turn.handle(tick(14.7)) == [.speak(id: 2, text: "Tundra is reading ledger.md", isStatus: true)])
    }

    @Test func noFillerWhileAStatusLineIsStillBeingSpoken() {
        var turn = thinkingTurn()
        #expect(turn.handle(tick(5.6)) == [.speak(id: 1, text: "Tundra is thinking", isStatus: true)])
        #expect(turn.handle(status("model", "reading ledger.md", 7.0)) == [])
        #expect(turn.handle(tick(15.0)) == [])                 // line 1 not finished yet
        #expect(turn.handle(finished(1, 15.1)) == [])
        #expect(turn.handle(tick(15.2)) == [.speak(id: 2, text: "Tundra is reading ledger.md", isStatus: true)])
    }

    @Test func aWaitForACycleIsReadFromItsPhase() {
        var turn = thinkingTurn()
        #expect(turn.handle(status(waitingPhase, nil, 2.8)) == [.workingSound(true)])
        #expect(turn.handle(tick(5.6)) == [.speak(id: 1, text: "Tundra is pausing its cycle for you",
                                                  isStatus: true)])
    }

    /// Bug: the reply's first stream event restarted the silence clock, so a status arriving late pushed the first
    /// filler fillerS past it instead of fillerS past the end of your turn.
    @Test func theFirstFillerCountsFromYourTurnNotFromTheStreamsFirstEvent() {
        var turn = thinkingTurn()
        #expect(turn.handle(status("model", "thinking", 5.0)) == [])
        #expect(turn.handle(tick(5.6)) == [.speak(id: 1, text: "Tundra is thinking", isStatus: true)])
    }

    @Test func aToolRunningLongerThanThreeFillerSIsNamed() {
        var turn = thinkingTurn()
        #expect(turn.handle(toolStart("web_search", 3.0)) == [.workingSound(true)])
        #expect(turn.handle(tick(6.0)) == [])                  // a tool runs: not the silence line
        #expect(turn.handle(tick(11.9)) == [])
        #expect(turn.handle(tick(12.1)) == [.speak(id: 1, text: "Tundra is using web search", isStatus: true)])
    }

    @Test func noFillerOnceAnswerTextHasStarted() {
        var turn = thinkingTurn()
        #expect(turn.handle(draft("Let me look", 3.0)) == [])
        #expect(turn.handle(tick(10.0)) == [])
        #expect(turn.handle(toolStart("web_search", 10.1)) == [.workingSound(true)])
        #expect(turn.handle(tick(30.0)) == [])
    }

    @Test func italianFillersAreSpokenInItalian() {
        var thinking = thinkingTurn(.it)
        #expect(thinking.handle(tick(5.6)) == [.speak(id: 1, text: "Tundra sta pensando", isStatus: true)])

        var tool = thinkingTurn(.it)
        _ = tool.handle(toolStart("web_search", 3.0))
        #expect(tool.handle(tick(12.1)) == [.speak(id: 1, text: "Tundra sta usando web search", isStatus: true)])

        var waiting = thinkingTurn(.it)
        _ = waiting.handle(status(waitingPhase, nil, 2.8))
        #expect(waiting.handle(tick(5.6)) == [
            .speak(id: 1, text: "Tundra sta mettendo in pausa il suo ciclo per te", isStatus: true)])

        // An English detail is not read by the Italian voice.
        var english = thinkingTurn(.it)
        _ = english.handle(status("model", "reading ledger.md", 2.8))
        #expect(english.handle(tick(5.6)) == [.speak(id: 1, text: "Tundra sta pensando", isStatus: true)])
    }

    // MARK: Latency, speaking, ids

    @Test func latencyIsReportedOnceAtTheFirstWordsOfTheInstanceNotAtAStatusLine() {
        var turn = thinkingTurn()
        #expect(turn.handle(tick(5.6)) == [.speak(id: 1, text: "Tundra is thinking", isStatus: true)])
        #expect(turn.run(started(1, 5.7), finished(1, 6.5)) == [])
        #expect(turn.handle(draft("Here it is. And more.", 7.0)) == [
            .speak(id: 2, text: "Here it is.", isStatus: false), .speak(id: 3, text: "And more.", isStatus: false)])
        #expect(turn.handle(started(2, 7.5)) == [.state(.speaking), .latency(5.0)])
        #expect(turn.run(finished(2, 8.0), started(3, 8.0)) == [])
    }

    @Test func utteranceIdsIncreaseFromOneAcrossTheSession() {
        var turn = thinkingTurn()
        #expect(turn.handle(answer("One. Two.", 3.0)) == [.speak(id: 1, text: "One.", isStatus: false),
                                                          .speak(id: 2, text: "Two.", isStatus: false)])
        _ = turn.run(end(nil, 3.1), started(1, 3.5), finished(1, 4.0), started(2, 4.0))
        #expect(turn.handle(finished(2, 4.5)) == [.state(.listening)])
        _ = turn.run(fin("And then?", 6.0))
        #expect(turn.handle(tick(7.0)) == [.send(text: "And then?"), .showTranscript(""), .state(.thinking)])
        #expect(turn.handle(tick(10.1)) == [.speak(id: 3, text: "Tundra is thinking", isStatus: true)])
        #expect(turn.handle(answer("Three.", 11.0, r2)) == [.speak(id: 4, text: "Three.", isStatus: false)])
    }

    @Test func theSendsAnswerIsShownAsAStatusLine() {
        var turn = thinkingTurn()
        #expect(turn.handle(.sendAnswered(said: "sent to tundra", at: 2.6)) == [.showStatus("sent to tundra")])
        #expect(turn.handle(.sendAnswered(said: "", at: 2.7)) == [])
    }

    // MARK: Mute and End

    @Test func muteIgnoresTranscriptsAndSendsNothingWhileSpeechContinues() {
        var turn = thinkingTurn()
        _ = turn.handle(draft("It is sunny in Milan.", 3.0))
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.handle(.mute(true, at: 3.6)) == [])
        #expect(turn.muted)
        #expect(turn.run(vol("hello there friend", 3.7), fin("Hello there friend.", 3.8), tick(5.0)) == [])
        #expect(turn.handle(finished(1, 5.0)) == [])
        #expect(turn.state == .speaking)
        #expect(turn.handle(.mute(false, at: 5.1)) == [])
        #expect(!turn.muted)
        #expect(turn.handle(vol("one more thing", 5.2)) == [.stopSpeaking, .state(.userTalking),
                                                            .showTranscript("one more thing")])
    }

    @Test func mutingWhileYouTalkDropsWhatYouWereSaying() {
        var turn = makeTurn()
        _ = turn.handle(.start(at: 0))
        #expect(turn.handle(vol("send this", 1.0)) == [.state(.userTalking), .showTranscript("send this")])
        #expect(turn.handle(.mute(true, at: 1.2)) == [.showTranscript(""), .state(.listening)])
        #expect(turn.run(fin("Send this now.", 1.5), tick(5.0)) == [])
        #expect(turn.state == .listening)
    }

    @Test func endStopsEverythingAndNothingHappensAfterIt() {
        var turn = thinkingTurn()
        _ = turn.handle(draft("It is sunny.", 3.0))
        #expect(turn.handle(toolStart("read_file", 3.1)) == [.workingSound(true)])
        #expect(turn.handle(.end(at: 3.5)) == [.stopSpeaking, .workingSound(false), .state(.ended)])
        let after: [VoiceInput] = [
            .start(at: 4), vol("hello there friend", 4.1), fin("Hello there friend.", 4.2), tick(10),
            .sendAnswered(said: "sent to tundra", at: 10.1), .sendFailed(message: "failed", at: 10.2),
            draft("More.", 10.3), toolStart("web_search", 10.4), end("error: x", 10.5),
            .streamClosed(message: "lost", at: 10.6), .streamClosed(message: nil, at: 10.7), started(1, 10.8),
            finished(1, 10.9), .mute(true, at: 11), .mute(false, at: 11.1), .end(at: 11.2)]
        for input in after { #expect(turn.handle(input) == [], "\(input)") }
        #expect(turn.state == .ended)
    }

    // MARK: Failures

    @Test func aFailedSendShowsItsLineAndListensAgain() {
        var turn = thinkingTurn()
        #expect(turn.handle(.sendFailed(message: "the API is not reachable", at: 3.0)) == [
            .showStatus("the API is not reachable"), .state(.listening)])
        #expect(turn.handle(tick(10)) == [])                    // no filler for a reply that is not coming
    }

    @Test func aStreamFailureStopsTheWorkingSoundAndLeavesThatReply() {
        var turn = thinkingTurn()
        #expect(turn.handle(status(waitingPhase, nil, 2.8)) == [.workingSound(true)])
        #expect(turn.handle(.streamClosed(message: "the connection was lost", at: 3.0)) == [
            .showStatus("the connection was lost"), .workingSound(false), .state(.listening)])
        #expect(turn.handle(draft("Late text.", 3.2)) == [])
    }

    @Test func aStreamFailureWhileSpeakingListensOnceTheQueuedSpeechIsDone() {
        var turn = thinkingTurn()
        _ = turn.handle(draft("One. Two.", 3.0))
        #expect(turn.handle(started(1, 3.5)) == [.state(.speaking), .latency(1.0)])
        #expect(turn.handle(.streamClosed(message: "the connection was lost", at: 3.6)) == [
            .showStatus("the connection was lost")])
        #expect(turn.run(finished(1, 4.0), started(2, 4.0)) == [])
        #expect(turn.handle(finished(2, 4.5)) == [.state(.listening)])
    }

    @Test func aStreamClosedWithoutAMessageEndsOnlyAReplyThatNeverCame() {
        var turn = thinkingTurn()
        #expect(turn.handle(.streamClosed(message: nil, at: 3.0)) == [.state(.listening)])    // 409 not_replying

        var done = thinkingTurn()
        _ = done.run(answer("Yes.", 3.0), end(nil, 3.1))
        #expect(done.handle(.streamClosed(message: nil, at: 3.2)) == [])
        #expect(done.state == .thinking)                         // still to speak "Yes."
    }
}
