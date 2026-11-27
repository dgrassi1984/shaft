import AVFoundation
import Testing
@testable import SHAFTKit

@MainActor
private final class FakeReadAloudSynth: ReadAloudSynth {
    var batches: [[String]] = []
    var voices: [AVSpeechSynthesisVoice?] = []
    var progress: [@MainActor () -> Void] = []
    var completions: [@MainActor () -> Void] = []
    var startsImmediately = false
    var stops = 0

    func speak(_ utterances: [String], voice: AVSpeechSynthesisVoice?,
               onProgress: @escaping @MainActor () -> Void,
               onFinish: @escaping @MainActor () -> Void) {
        batches.append(utterances)
        voices.append(voice)
        progress.append(onProgress)
        completions.append(onFinish)
        if startsImmediately { onProgress() }
    }

    /// The latest batch speaks a word.
    func speakAWord() { progress.last?() }

    func stop() { stops += 1 }
}

@Suite struct ReadAloudLanguageTests {
    @Test func detectsEnglishAndItalianParagraphs() {
        #expect(ReadAloudLanguage.detect("The morning sunlight filled the kitchen. I opened the window and watched the birds in the garden.") == .en)
        #expect(ReadAloudLanguage.detect("La luce del mattino illuminava la cucina. Ho aperto la finestra e guardato gli uccelli nel giardino.") == .it)
    }

    @Test func emptyAndWhitespaceHaveNoLanguage() {
        #expect(ReadAloudLanguage.detect("") == nil)
        #expect(ReadAloudLanguage.detect(" \n\t ") == nil)
    }
}

@Suite @MainActor struct ReadAloudPlayerTests {
    /// Waits for the stall timeout to act, up to 5 s. A fixed 0.2 s sleep failed when the full suite ran in parallel on
    /// a loaded machine (2026-11-15): the main actor ran the timeout late, not never.
    private func waitUntil(_ done: () -> Bool) async throws {
        let deadline = ContinuousClock.now + .seconds(5)
        while !done(), ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(10)) }
    }

    @Test func speechThatNeverStartsClearsPlayingAfterTheStallTimeout_20261114() async throws {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth, stallTimeout: 0.05)
        player.toggle(id: "a", markdown: "First message.", voiceIdentifier: nil, language: .en)
        #expect(player.playing == "a")
        let before = synth.stops
        try await waitUntil { player.playing == nil }
        #expect(player.playing == nil)
        #expect(synth.stops == before + 1)
    }

    /// The iPhone simulator, 2026-11-14: the synthesizer said it started, its audio queue failed, and it never said it
    /// finished. The ■ stayed for good although nothing was heard.
    @Test func speechThatStartsThenGoesSilentClearsPlaying_20261114() async throws {
        let synth = FakeReadAloudSynth()
        synth.startsImmediately = true
        let player = ReadAloudPlayer(synth: synth, stallTimeout: 0.05)
        player.toggle(id: "a", markdown: "First message.", voiceIdentifier: nil, language: .en)
        #expect(player.playing == "a")
        let before = synth.stops
        try await waitUntil { player.playing == nil }
        #expect(player.playing == nil, "no word for longer than the stall timeout: the reading is over")
        #expect(synth.stops == before + 1)
    }

    @Test func speechThatKeepsSpeakingIsNotStoppedByTheWatchdog() async throws {
        let synth = FakeReadAloudSynth()
        synth.startsImmediately = true
        let player = ReadAloudPlayer(synth: synth, stallTimeout: 0.1)
        player.toggle(id: "a", markdown: "First message.", voiceIdentifier: nil, language: .en)
        let before = synth.stops
        for _ in 0..<10 {                     // 0.3 s of speech, three times the stall timeout
            try await Task.sleep(for: .seconds(0.03))
            synth.speakAWord()
        }
        #expect(player.playing == "a")
        #expect(synth.stops == before)
    }

    @Test func staleWatchdogDoesNotStopANewerMessage() async throws {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth, stallTimeout: 0.1)
        player.toggle(id: "a", markdown: "First message.", voiceIdentifier: nil, language: .en)
        synth.startsImmediately = true
        player.toggle(id: "b", markdown: "Second message.", voiceIdentifier: nil, language: .en)
        let before = synth.stops
        for _ in 0..<10 {
            try await Task.sleep(for: .seconds(0.03))
            synth.speakAWord()
        }
        #expect(player.playing == "b")
        #expect(synth.stops == before)
    }

    @Test func aWordFromAnEarlierReadingDoesNotKeepANewerOneAlive() async throws {
        let synth = FakeReadAloudSynth()
        synth.startsImmediately = true
        let player = ReadAloudPlayer(synth: synth, stallTimeout: 0.1)
        player.toggle(id: "a", markdown: "First message.", voiceIdentifier: nil, language: .en)
        let first = try #require(synth.progress.last)
        player.toggle(id: "b", markdown: "Second message.", voiceIdentifier: nil, language: .en)
        for _ in 0..<10 {
            try await Task.sleep(for: .seconds(0.03))
            first()                            // late words of the reading that was replaced
        }
        #expect(player.playing == nil, "b spoke nothing: it stalled, whatever a said")
    }

    @Test func speaksChunkerSentencesWithoutMarkupOrCodeContents() {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        let markdown = "This is **bold**. Read the next sentence.\n\n```swift\nlet secret = 42\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
        var chunker = SpeechChunker(language: .en)
        let expected = chunker.feed(markdown) + chunker.finish()
        #expect(expected == ["This is bold.", "Read the next sentence.", "There's some code in the chat.", "There's a table in the chat."])
        player.toggle(id: "example/a1", markdown: markdown, voiceIdentifier: nil, language: .en)
        #expect(synth.batches == [expected])
        #expect(player.playing == "example/a1")
    }

    @Test(.enabled(if: !AVSpeechSynthesisVoice.speechVoices().isEmpty, "No speech voices are installed on this device."))
    func installedIdentifierIsUsed() throws {
        let installed = try #require(AVSpeechSynthesisVoice.speechVoices().first)
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        player.toggle(id: "a", markdown: "The message is ready to read.", voiceIdentifier: installed.identifier, language: .en)
        #expect(synth.voices.count == 1)
        #expect(synth.voices[0]?.identifier == installed.identifier)
    }

    /// The bug (2026-11-27): ▶ read a message in the voice of the language the text was detected in, so an Italian
    /// reply to an instance whose voice mode speaks English (or the reverse) came out in another voice than voice mode's.
    /// Voice mode's language wins over the text's: a missing or nil identifier falls back to that language's system
    /// voice, never to the detected one's.
    @Test(.enabled(if: AVSpeechSynthesisVoice(language: VoiceLanguage.en.bcp47) != nil
                       && AVSpeechSynthesisVoice(language: VoiceLanguage.it.bcp47) != nil,
                   "English and Italian system speech voices are needed on this device."))
    func voiceModeLanguageWinsOverTheTextsLanguage_20261127() {
        let english = AVSpeechSynthesisVoice(language: VoiceLanguage.en.bcp47)?.identifier
        #expect(english != AVSpeechSynthesisVoice(language: VoiceLanguage.it.bcp47)?.identifier)
        let identifiers: [String?] = ["example.voice.does-not-exist", nil]
        for identifier in identifiers {
            let synth = FakeReadAloudSynth()
            let player = ReadAloudPlayer(synth: synth)
            player.toggle(id: "a", markdown: "La luce del mattino illuminava la cucina. Ho aperto la finestra per guardare il giardino.",
                          voiceIdentifier: identifier, language: .en)
            #expect(synth.voices.count == 1)
            #expect(synth.voices[0]?.identifier == english)
        }
    }

    @Test func sameIDStopsAndDoesNotSpeakAgain() {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        player.toggle(id: "a", markdown: "Hello there.", voiceIdentifier: nil, language: .en)
        let before = synth.stops
        player.toggle(id: "a", markdown: "Hello there.", voiceIdentifier: nil, language: .en)
        #expect(synth.stops == before + 1)
        #expect(synth.batches == [["Hello there."]])
        #expect(player.playing == nil)
    }

    @Test func anotherIDStopsFirstAndStaleFinishCannotClearSecond() {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        player.toggle(id: "a", markdown: "First message.", voiceIdentifier: nil, language: .en)
        let before = synth.stops
        player.toggle(id: "b", markdown: "Second message.", voiceIdentifier: nil, language: .en)
        #expect(synth.stops == before + 1)
        #expect(synth.batches == [["First message."], ["Second message."]])
        synth.completions[0]()
        #expect(player.playing == "b")
        synth.completions[1]()
        #expect(player.playing == nil)
    }

    @Test func explicitStopClearsPlayingAndInvalidatesFinish() {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        player.toggle(id: "a", markdown: "Hello there.", voiceIdentifier: nil, language: .en)
        player.stop()
        #expect(player.playing == nil)
        synth.completions[0]()
        #expect(player.playing == nil)
    }

    @Test func unspokenMarkupDoesNotStartPlayback() {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        player.toggle(id: "a", markdown: "---\n\n", voiceIdentifier: nil, language: .it)
        #expect(synth.batches.isEmpty)
        #expect(player.playing == nil)
    }
}
