import AVFoundation
import Testing
@testable import SHAFTKit

@MainActor
private final class FakeReadAloudSynth: ReadAloudSynth {
    var batches: [[String]] = []
    var voices: [AVSpeechSynthesisVoice?] = []
    var completions: [@MainActor () -> Void] = []
    var stops = 0

    func speak(_ utterances: [String], voice: AVSpeechSynthesisVoice?,
               onFinish: @escaping @MainActor () -> Void) {
        batches.append(utterances)
        voices.append(voice)
        completions.append(onFinish)
    }

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
    @Test func speaksChunkerSentencesWithoutMarkupOrCodeContents() {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        let markdown = "This is **bold**. Read the next sentence.\n\n```swift\nlet secret = 42\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
        var chunker = SpeechChunker(language: .en)
        let expected = chunker.feed(markdown) + chunker.finish()
        #expect(expected == ["This is bold.", "Read the next sentence.", "There's some code in the chat.", "There's a table in the chat."])
        player.toggle(id: "example/a1", markdown: markdown, voiceIdentifier: nil, fallback: .en)
        #expect(synth.batches == [expected])
        #expect(player.playing == "example/a1")
    }

    @Test(.enabled(if: !AVSpeechSynthesisVoice.speechVoices().isEmpty, "No speech voices are installed on this device."))
    func installedIdentifierIsUsed() throws {
        let installed = try #require(AVSpeechSynthesisVoice.speechVoices().first)
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        player.toggle(id: "a", markdown: "The message is ready to read.", voiceIdentifier: installed.identifier, fallback: .en)
        #expect(synth.voices.count == 1)
        #expect(synth.voices[0]?.identifier == installed.identifier)
    }

    @Test(.enabled(if: AVSpeechSynthesisVoice(language: VoiceLanguage.it.bcp47) != nil,
                   "No Italian system speech voice is installed on this device."))
    func missingAndNilIdentifiersUseDetectedLanguageSystemVoice() {
        let identifiers: [String?] = ["example.voice.does-not-exist", nil]
        for identifier in identifiers {
            let synth = FakeReadAloudSynth()
            let player = ReadAloudPlayer(synth: synth)
            player.toggle(id: "a", markdown: "La luce del mattino illuminava la cucina. Ho aperto la finestra per guardare il giardino.",
                          voiceIdentifier: identifier, fallback: .en)
            #expect(synth.voices.count == 1)
            #expect(synth.voices[0]?.identifier == AVSpeechSynthesisVoice(language: VoiceLanguage.it.bcp47)?.identifier)
        }
    }

    @Test func sameIDStopsAndDoesNotSpeakAgain() {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        player.toggle(id: "a", markdown: "Hello there.", voiceIdentifier: nil, fallback: .en)
        let before = synth.stops
        player.toggle(id: "a", markdown: "Hello there.", voiceIdentifier: nil, fallback: .en)
        #expect(synth.stops == before + 1)
        #expect(synth.batches == [["Hello there."]])
        #expect(player.playing == nil)
    }

    @Test func anotherIDStopsFirstAndStaleFinishCannotClearSecond() {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        player.toggle(id: "a", markdown: "First message.", voiceIdentifier: nil, fallback: .en)
        let before = synth.stops
        player.toggle(id: "b", markdown: "Second message.", voiceIdentifier: nil, fallback: .en)
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
        player.toggle(id: "a", markdown: "Hello there.", voiceIdentifier: nil, fallback: .en)
        player.stop()
        #expect(player.playing == nil)
        synth.completions[0]()
        #expect(player.playing == nil)
    }

    @Test func unspokenMarkupDoesNotStartPlayback() {
        let synth = FakeReadAloudSynth()
        let player = ReadAloudPlayer(synth: synth)
        player.toggle(id: "a", markdown: "---\n\n", voiceIdentifier: nil, fallback: .it)
        #expect(synth.batches.isEmpty)
        #expect(player.playing == nil)
    }
}
