import AVFoundation
import Foundation
@testable import SHAFTKit
import XCTest

// The voice audio (M2 lane D), run on a silent graph: the main mixer's output volume is 0, the input node is never
// touched, no microphone is used and nothing is heard. Test speech is made with `/usr/bin/say -o <temp file>` and
// only ever fed to the transcriber, never played.

/// Collects what callbacks report, from any thread, with the VoiceClock time each came.
private final class Recorder<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [(value: Value, at: TimeInterval)] = []

    func add(_ value: Value) { lock.withLock { stored.append((value, VoiceClock.now())) } }
    var values: [Value] { lock.withLock { stored.map(\.value) } }
    var timed: [(value: Value, at: TimeInterval)] { lock.withLock { stored } }
}

/// Collects the samples a tap on a node sees.
private final class SampleTap: @unchecked Sendable {
    private let lock = NSLock()
    private var samples: [Float] = []

    func add(_ buffer: AVAudioPCMBuffer) {
        guard let channel = buffer.floatChannelData?[0] else { return }
        let piece = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
        lock.withLock { samples.append(contentsOf: piece) }
    }
    var count: Int { lock.withLock { samples.count } }
    func slice(from start: Int) -> [Float] { lock.withLock { Array(samples[min(start, samples.count)...]) } }
}

private enum SpeechEvent: Equatable, Sendable {
    case start(Int), finish(Int)
}

private func rms(_ samples: ArraySlice<Float>) -> Float {
    guard !samples.isEmpty else { return 0 }
    return (samples.reduce(0) { $0 + $1 * $1 } / Float(samples.count)).squareRoot()
}

/// Wait until `condition` holds, up to `timeout` seconds; returns whether it did.
private func waitUntil(_ timeout: TimeInterval, _ condition: () -> Bool) async -> Bool {
    let deadline = VoiceClock.now() + timeout
    while VoiceClock.now() < deadline {
        if condition() { return true }
        try? await Task.sleep(for: .milliseconds(20))
    }
    return condition()
}

/// English test speech in a temporary AIFF, made with `say` (written to the file, never played).
/// Each attempt is bounded because a hung `say` once blocked the whole suite.
private func spokenFile(_ text: String) throws -> URL {
    #if os(macOS)
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent("shaft-voice-\(UUID().uuidString).aiff")
    var timedOut = false
    for voice in [["-v", "Samantha"], []] {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        process.arguments = voice + ["-o", url.path, text]
        let exited = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in exited.signal() }
        try process.run()
        if exited.wait(timeout: .now() + 30) == .timedOut {
            timedOut = true
            process.terminate()
            if exited.wait(timeout: .now() + 2) == .timedOut, process.isRunning {
                kill(process.processIdentifier, SIGKILL)
            }
            process.waitUntilExit()
            try? FileManager.default.removeItem(at: url)
            continue
        }
        process.waitUntilExit()
        if process.terminationStatus == 0, FileManager.default.fileExists(atPath: url.path) { return url }
    }
    throw XCTSkip(timedOut
        ? "/usr/bin/say timed out after 30 s writing test speech"
        : "/usr/bin/say could not write test speech")
    #else
    throw XCTSkip("test speech is made with /usr/bin/say, which only the Mac has")
    #endif
}

private func seconds(of url: URL) throws -> TimeInterval {
    let file = try AVAudioFile(forReading: url)
    return Double(file.length) / file.fileFormat.sampleRate
}

final class VoiceAudioTests: XCTestCase {
    // MARK: - The graph

    func testSilentGraphHasItsOutputAtZeroAndRefusesTheMicrophone() async throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        defer { graph.stop() }
        XCTAssertTrue(graph.isRunning)
        XCTAssertEqual(graph.engine.mainMixerNode.outputVolume, 0)
        do {
            try await MicTranscriber(graph: graph).start(language: .en) { _ in }
            XCTFail("a silent graph must refuse the microphone")
        } catch let error as VoiceAudioError {
            XCTAssertEqual(error, .silentGraphHasNoMicrophone)
        }
    }

    // MARK: - Speech output

    func testSpeechOutputStartsAndFinishesEachUtteranceInQueueOrderAfterItPlays() async throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        defer { graph.stop() }
        let tap = SampleTap()
        graph.speechPlayer.installTap(onBus: 0, bufferSize: 1024, format: nil) { buffer, _ in tap.add(buffer) }
        defer { graph.speechPlayer.removeTap(onBus: 0) }
        let output = EngineSpeechOutput(graph: graph)
        XCTAssertNil(output.useVoice(identifier: nil, name: nil, language: .en))
        let events = Recorder<SpeechEvent>()
        let sentences = ["The first sentence is here.", "Then a second one.", "And a third to close."]
        for (offset, sentence) in sentences.enumerated() {
            output.speak(id: offset + 1, text: sentence, onStart: { events.add(.start($0)) },
                         onFinish: { events.add(.finish($0)) })
        }
        let done = await waitUntil(30) { events.values.contains(.finish(3)) }
        XCTAssertTrue(done, "speech never finished: \(events.values)")
        XCTAssertEqual(events.values, [.start(1), .finish(1), .start(2), .finish(2), .start(3), .finish(3)])
        // Each finish comes after the utterance has played, not merely rendered: a spoken sentence lasts > 0.5 s.
        let timed = events.timed
        for id in 1...3 {
            let start = try XCTUnwrap(timed.first { $0.value == .start(id) }?.at)
            let finish = try XCTUnwrap(timed.first { $0.value == .finish(id) }?.at)
            XCTAssertGreaterThan(finish - start, 0.5, "utterance \(id) finished \(finish - start) s after it started")
        }
        // The speech went through the graph's speech player (where echo cancellation hears it).
        XCTAssertGreaterThan(rms(tap.slice(from: 0)[...]), 0.001)
    }

    func testStopAllFinishesTheCurrentAndQueuedUtterancesAndDropsTheirStart() async throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        defer { graph.stop() }
        let output = EngineSpeechOutput(graph: graph)
        let events = Recorder<SpeechEvent>()
        let long = "This sentence is long enough to be still playing when the stop arrives, by a wide margin."
        for id in 1...3 {
            output.speak(id: id, text: long, onStart: { events.add(.start($0)) }, onFinish: { events.add(.finish($0)) })
        }
        let started = await waitUntil(20) { events.values.contains(.start(1)) }
        XCTAssertTrue(started)
        try await Task.sleep(for: .milliseconds(300))
        output.stopAll()
        let finished = await waitUntil(2) { events.values.contains(.finish(3)) }
        XCTAssertTrue(finished, "stopAll left utterances unfinished: \(events.values)")
        XCTAssertEqual(events.values, [.start(1), .finish(1), .finish(2), .finish(3)])
        // Nothing of the dropped utterances comes later.
        try await Task.sleep(for: .milliseconds(800))
        XCTAssertEqual(events.values.count, 4, "events after stopAll: \(events.values)")
        // The output still speaks after a stop.
        output.speak(id: 4, text: "Still here.", onStart: { events.add(.start($0)) },
                     onFinish: { events.add(.finish($0)) })
        let again = await waitUntil(20) { events.values.contains(.finish(4)) }
        XCTAssertTrue(again, "no speech after stopAll: \(events.values)")
        XCTAssertEqual(Array(events.values.suffix(2)), [.start(4), .finish(4)])
    }

    func testAnEmptyUtteranceStillStartsAndFinishesInItsTurn() async throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        defer { graph.stop() }
        let output = EngineSpeechOutput(graph: graph)
        let events = Recorder<SpeechEvent>()
        output.speak(id: 1, text: "", onStart: { events.add(.start($0)) }, onFinish: { events.add(.finish($0)) })
        output.speak(id: 2, text: "Done.", onStart: { events.add(.start($0)) }, onFinish: { events.add(.finish($0)) })
        let done = await waitUntil(20) { events.values.contains(.finish(2)) }
        XCTAssertTrue(done, "an empty utterance stalled the queue: \(events.values)")
        XCTAssertEqual(events.values, [.start(1), .finish(1), .start(2), .finish(2)])
    }

    func testUseVoiceWithAnUnknownIdentifierNamesTheVoiceAndFallsBackToTheSystemVoice() throws {
        let output = EngineSpeechOutput(graph: VoiceAudioGraph(silent: true))
        let line = try XCTUnwrap(output.useVoice(identifier: "com.example.voice.missing", name: "Ava (Premium)",
                                                 language: .en))
        XCTAssertTrue(line.hasPrefix("Ava (Premium) is not on this "), line)
        XCTAssertTrue(line.contains("Spoken Content"), line)
        XCTAssertEqual(output.voiceIdentifier, AVSpeechSynthesisVoice(language: "en-US")?.identifier)
        // With no name, the line names the identifier.
        let unnamed = try XCTUnwrap(output.useVoice(identifier: "com.example.voice.missing", name: nil, language: .it))
        XCTAssertTrue(unnamed.hasPrefix("com.example.voice.missing is not on this "), unnamed)
        // An installed voice is used as is, with nothing to show.
        let installed = try XCTUnwrap(AVSpeechSynthesisVoice(language: "en-US"))
        XCTAssertNil(output.useVoice(identifier: installed.identifier, name: installed.name, language: .en))
        XCTAssertEqual(output.voiceIdentifier, installed.identifier)
    }

    // MARK: - Working sound and earcons

    func testWorkingSoundLoopsWithoutAGapAndStops() async throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        defer { graph.stop() }
        let tap = SampleTap()
        graph.workingPlayer.installTap(onBus: 0, bufferSize: 1024, format: nil) { buffer, _ in tap.add(buffer) }
        defer { graph.workingPlayer.removeTap(onBus: 0) }
        let sounds = GeneratedVoiceSounds(graph: graph, volume: 0.25)
        let before = tap.count
        sounds.working(true)
        sounds.working(true)  // a second "on" changes nothing
        XCTAssertTrue(sounds.isWorking)
        try await Task.sleep(for: .seconds(3.2))
        let playing = tap.slice(from: before)
        let rate = VoiceAudioGraph.playerFormat.sampleRate
        // Skip the first 0.2 s (the player starting), keep at least two whole loops.
        let window = Int(0.02 * rate)
        let body = playing.dropFirst(Int(0.2 * rate))
        XCTAssertGreaterThan(body.count, Int(2 * GeneratedVoiceSounds.loopSeconds * rate),
                             "the tap saw only \(body.count) frames")
        var quietest = Float.infinity
        var index = body.startIndex
        while index + window <= body.endIndex {
            quietest = min(quietest, rms(body[index..<index + window]))
            index += window
        }
        // The loop's quietest 20 ms is about 0.05 RMS; a gap between loops would show as silence.
        XCTAssertGreaterThan(quietest, 0.02, "a 20 ms window fell silent while the working sound looped")

        sounds.working(false)
        XCTAssertFalse(sounds.isWorking)
        try await Task.sleep(for: .milliseconds(300))
        let afterStop = tap.count
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertLessThan(rms(tap.slice(from: afterStop)[...]), 0.000_1, "the working sound kept playing after off")
    }

    func testWorkingSoundAndSpeechComeBackAfterTheGraphWasStoppedUnderThem() async throws {
        let graph = VoiceAudioGraph(silent: true)
        let sounds = GeneratedVoiceSounds(graph: graph)
        sounds.working(true)
        XCTAssertTrue(graph.workingPlayer.isPlaying)
        graph.stop()
        XCTAssertFalse(graph.workingPlayer.isPlaying)
        sounds.working(true)  // still "on", but silent: it must play again, not be taken as already playing
        XCTAssertTrue(graph.workingPlayer.isPlaying, "the working sound stayed silent after the graph restarted")
        sounds.working(false)
        let output = EngineSpeechOutput(graph: graph)
        let events = Recorder<SpeechEvent>()
        output.speak(id: 1, text: "Before the stop.", onStart: { events.add(.start($0)) },
                     onFinish: { events.add(.finish($0)) })
        _ = await waitUntil(20) { events.values.contains(.start(1)) }
        graph.stop()
        output.stopAll()
        output.speak(id: 2, text: "After the restart.", onStart: { events.add(.start($0)) },
                     onFinish: { events.add(.finish($0)) })
        let done = await waitUntil(20) { events.values.contains(.finish(2)) }
        XCTAssertTrue(done, "no speech after the graph restarted: \(events.values)")
        XCTAssertEqual(events.values, [.start(1), .finish(1), .start(2), .finish(2)])
        graph.stop()
    }

    func testWorkingLoopRunsIntoItsStartWithoutAClick() {
        let loop = GeneratedVoiceSounds.workingLoop(sampleRate: 48_000)
        let samples = Array(UnsafeBufferPointer(start: loop.floatChannelData![0], count: Int(loop.frameLength)))
        let largestStep = zip(samples, samples.dropFirst()).map { abs($1 - $0) }.max() ?? 0
        let seam = abs(samples[0] - samples[samples.count - 1])
        XCTAssertLessThanOrEqual(seam, largestStep * 1.01, "the loop's seam jumps by \(seam)")
        XCTAssertEqual(Double(loop.frameLength) / 48_000, GeneratedVoiceSounds.loopSeconds, accuracy: 0.001)
    }

    func testTheWaitingLoopIsOneSoftTickThenQuietAndLoopsWithoutAClick() {
        let rate = 48_000.0
        let loop = GeneratedVoiceSounds.waitingLoop(sampleRate: rate)
        let samples = Array(UnsafeBufferPointer(start: loop.floatChannelData![0], count: Int(loop.frameLength)))
        XCTAssertEqual(Double(loop.frameLength) / rate, GeneratedVoiceSounds.waitingLoopSeconds, accuracy: 0.001)
        let tick = samples.prefix(Int(0.12 * rate))
        XCTAssertGreaterThan(tick.map(abs).max() ?? 0, 0.1, "the tick is inaudible")
        XCTAssertLessThan(tick.map(abs).max() ?? 1, 0.6, "the tick is louder than meant")
        XCTAssertEqual(samples.dropFirst(tick.count).map(abs).max() ?? 1, 0, "the rest of the loop is not quiet")
        XCTAssertLessThan(abs(samples[0]), 0.01, "the tick starts with a click")
        // Quieter at the player's volume than an earcon at full volume: it is a background sound.
        let earcon = GeneratedVoiceSounds.earconBuffer(.thinking, sampleRate: rate)!
        let earconPeak = UnsafeBufferPointer(start: earcon.floatChannelData![0], count: Int(earcon.frameLength))
            .map(abs).max() ?? 0
        XCTAssertLessThan((tick.map(abs).max() ?? 0) * VoiceTuning().workingVolume, earconPeak)
    }

    func testTheWorkingSoundWinsOverTheWaitingSoundInEitherOrder() throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        defer { graph.stop() }
        let sounds = GeneratedVoiceSounds(graph: graph)
        sounds.waiting(true)
        XCTAssertEqual(sounds.playingLoop, .waiting)
        XCTAssertTrue(graph.workingPlayer.isPlaying)
        sounds.working(true)                       // a tool starts, then the waiting sound is switched off
        XCTAssertEqual(sounds.playingLoop, .working)
        sounds.waiting(false)
        XCTAssertEqual(sounds.playingLoop, .working, "switching the waiting sound off stopped the working sound")
        XCTAssertTrue(graph.workingPlayer.isPlaying)
        sounds.waiting(true)                       // on while the tool runs: the working sound keeps playing
        XCTAssertEqual(sounds.playingLoop, .working)
        sounds.working(false)                      // the tool ends: back to waiting
        XCTAssertEqual(sounds.playingLoop, .waiting)
        XCTAssertTrue(graph.workingPlayer.isPlaying)
        sounds.waiting(false)
        XCTAssertNil(sounds.playingLoop)
        XCTAssertFalse(graph.workingPlayer.isPlaying)
    }

    func testDuckLowersTheWorkingSoundTwelveDecibelsAndRestoresIt() throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        defer { graph.stop() }
        let sounds = GeneratedVoiceSounds(graph: graph, volume: 0.4)
        sounds.working(true)
        defer { sounds.working(false) }
        XCTAssertEqual(graph.workingPlayer.volume, 0.4, accuracy: 0.000_1)
        sounds.duckWorking(true)
        XCTAssertEqual(20 * log10(sounds.workingGain / 0.4), -12, accuracy: 0.01)
        XCTAssertEqual(graph.workingPlayer.volume, sounds.workingGain, accuracy: 0.000_1)
        sounds.setWorkingVolume(0.2)
        XCTAssertEqual(graph.workingPlayer.volume, 0.2 * GeneratedVoiceSounds.duckGain, accuracy: 0.000_1)
        sounds.duckWorking(false)
        XCTAssertEqual(graph.workingPlayer.volume, 0.2, accuracy: 0.000_1)
    }

    func testEachStateHasItsOwnShortEarcon() throws {
        var seen: [[Float]] = []
        for state in VoiceState.allCases {
            guard let buffer = GeneratedVoiceSounds.earconBuffer(state, sampleRate: 48_000) else {
                XCTAssertEqual(state, .idle)
                continue
            }
            XCTAssertLessThan(Double(buffer.frameLength) / 48_000, 0.4, "\(state)'s earcon is long")
            let samples = Array(UnsafeBufferPointer(start: buffer.floatChannelData![0], count: Int(buffer.frameLength)))
            XCTAssertFalse(seen.contains(samples), "\(state)'s earcon repeats another's")
            seen.append(samples)
        }
        XCTAssertEqual(seen.count, VoiceState.allCases.count - 1)
        let graph = VoiceAudioGraph(silent: true)
        defer { graph.stop() }
        GeneratedVoiceSounds(graph: graph).earcon(.listening)  // plays on the silent graph without trouble
        XCTAssertTrue(graph.isRunning)
    }

    // MARK: - Hearing

    func testFileTranscriberHearsGeneratedSpeechWithVolatileThenFinalResultsInTimeOrder() async throws {
        guard await TranscriberPipeline.isInstalled(.en) else {
            throw XCTSkip("The English (en_US) speech model is not installed on this Mac; not downloading it.")
        }
        let offset: TimeInterval = 0.5
        let audio = try spokenFile("Could you check the weather for tomorrow morning in Milan")
        defer { try? FileManager.default.removeItem(at: audio) }
        let length = try seconds(of: audio)
        let source = FileTranscriber([(audio, at: offset)])
        let results = Recorder<TranscriptEvent>()
        try await source.start(language: .en) { results.add($0) }
        let finalText: () -> String = { results.values.filter(\.isFinal).map(\.text).joined().lowercased() }
        let heard = await waitUntil(length + 15) { finalText().contains("tomorrow") && finalText().contains("milan") }
        await source.stop()
        let events = results.values
        XCTAssertTrue(heard, "final text: \(finalText()); all: \(events)")
        XCTAssertTrue(finalText().contains("weather"), finalText())
        let firstVolatile = try XCTUnwrap(events.first { !$0.isFinal }, "no volatile result")
        let firstFinal = try XCTUnwrap(events.first(where: \.isFinal))
        XCTAssertLessThan(firstVolatile.at, firstFinal.at, "the first volatile result came after the first final")
        for (earlier, later) in zip(events, events.dropFirst()) {
            XCTAssertLessThan(earlier.at, later.at, "result times go backwards")
        }
        // No result before the speech began being heard.
        let zero = try XCTUnwrap(source.timelineStart)
        XCTAssertGreaterThan(firstVolatile.at, zero + offset)
        // Volatile results come while the speech is still being heard (fast results on), so a barge-in can stop the
        // instance mid-sentence rather than after the operator has finished.
        XCTAssertLessThan(firstVolatile.at, zero + offset + length, "the first volatile result waited for the end")
        let lastFinal = try XCTUnwrap(events.last(where: \.isFinal))
        print(String(format: "VoiceAudio timing: audio %.2f s of speech at +%.2f s; first volatile %.2f s after speech "
                     + "began; last final %.2f s after speech ended; %d results (%d final)",
                     length, offset, firstVolatile.at - zero - offset, lastFinal.at - zero - offset - length,
                     events.count, events.filter(\.isFinal).count))
    }

    func testFileTranscriberPlaysEachClipAtItsOffsetAndStopsDelivering() async throws {
        guard await TranscriberPipeline.isInstalled(.en) else {
            throw XCTSkip("The English (en_US) speech model is not installed on this Mac; not downloading it.")
        }
        let first = try spokenFile("Tell me about the garden")
        let second = try spokenFile("Stop please wait a moment")
        defer {
            try? FileManager.default.removeItem(at: first)
            try? FileManager.default.removeItem(at: second)
        }
        let later: TimeInterval = 4.5
        let source = FileTranscriber([(first, at: 0.2), (second, at: later)])
        let results = Recorder<TranscriptEvent>()
        try await source.start(language: .en) { results.add($0) }
        let zero = try XCTUnwrap(source.timelineStart)
        let heardBoth = await waitUntil(later + 12) {
            let text = results.values.map(\.text).joined().lowercased()
            return text.contains("garden") && text.contains("moment")
        }
        XCTAssertTrue(heardBoth, "heard: \(results.values)")
        let firstMoment = try XCTUnwrap(results.timed.first { $0.value.text.lowercased().contains("moment") })
        XCTAssertGreaterThan(firstMoment.at, zero + later, "the second clip was heard before its offset")
        let firstGarden = try XCTUnwrap(results.timed.first { $0.value.text.lowercased().contains("garden") })
        XCTAssertLessThan(firstGarden.at, zero + later, "the first clip was heard only after the second's offset")
        await source.stop()
        let count = results.values.count
        try await Task.sleep(for: .milliseconds(600))
        XCTAssertEqual(results.values.count, count, "results came after stop returned")
    }

    /// Each sentence's final is whole and comes within 1.5 s of its end. Bug (M3): finalizing early after 0.5 s of
    /// quiet lost words at a pause mid-sentence — "Wait, what about the lantern by the door" came back as ". what
    /// about the lantern by the door?", and the Mac app sent "to. Wait, …". The second sentence's final must start
    /// with its first word.
    func testEachSentencesFinalComesSoonAfterItEndsWithTheWholeSentence() async throws {
        guard await TranscriberPipeline.isInstalled(.en) else {
            throw XCTSkip("The English (en_US) speech model is not installed on this Mac; not downloading it.")
        }
        let first = try spokenFile("Tell me the long story now")
        let second = try spokenFile("Wait, what about the lantern by the door")
        defer {
            try? FileManager.default.removeItem(at: first)
            try? FileManager.default.removeItem(at: second)
        }
        let at: [TimeInterval] = [0.5, 6]
        let ends = try [at[0] + seconds(of: first), at[1] + seconds(of: second)]
        let source = FileTranscriber([(first, at: at[0]), (second, at: at[1])])
        let results = Recorder<TranscriptEvent>()
        try await source.start(language: .en) { results.add($0) }
        let zero = try XCTUnwrap(source.timelineStart)
        let done = await waitUntil(ends[1] + 10) {
            results.values.filter(\.isFinal).map(\.text).joined().lowercased().contains("door")
        }
        await source.stop()
        XCTAssertTrue(done, "no final with 'door': \(results.values)")
        let finals = results.values.filter(\.isFinal)
        for (end, word) in zip(ends, ["story", "door"]) {
            let final = try XCTUnwrap(finals.first { $0.text.lowercased().contains(word) }, "no final with '\(word)'")
            XCTAssertLessThan(final.at - zero - end, 1.5, "the final of the sentence ending '\(word)' came late")
        }
        let second_ = finals.filter { $0.at > zero + at[1] }.map(\.text).joined(separator: " ").lowercased()
        XCTAssertTrue(second_.contains("lantern") && second_.contains("door"), "split or partial: \(second_)")
        XCTAssertTrue(second_.trimmingCharacters(in: .whitespaces).hasPrefix("wait"), "words lost or added: \(second_)")
    }

    func testAMissingTranscriberModelThrowsNamingTheLanguage() async throws {
        guard await !TranscriberPipeline.isInstalled(.it) else {
            throw XCTSkip("The Italian model is installed here, so the missing-model error cannot be provoked.")
        }
        let audio = try spokenFile("hello")
        defer { try? FileManager.default.removeItem(at: audio) }
        do {
            try await FileTranscriber([(audio, at: 0)]).start(language: .it) { _ in }
            XCTFail("starting without the Italian model must throw")
        } catch let error as VoiceAudioError {
            XCTAssertEqual(error, .transcriberModelMissing(.it))
            let message = try XCTUnwrap(error.errorDescription)
            XCTAssertTrue(message.contains("Italian") && message.contains("it_IT"), message)
            XCTAssertTrue(message.contains("not installed"), message)
        }
    }

    func testStreamConverterCutIntoPiecesKeepsEveryFrame() throws {
        // A resampler restarted on every piece loses frames at each cut; a streaming one keeps the whole length.
        let source = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 22_050, channels: 1))
        let target = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1))
        let converter = try XCTUnwrap(PCMStreamConverter(from: source, to: target))
        let pieceFrames = 2_205
        var out = 0
        for _ in 0..<20 {
            let piece = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: source, frameCapacity: AVAudioFrameCount(pieceFrames)))
            piece.frameLength = AVAudioFrameCount(pieceFrames)
            for frame in 0..<pieceFrames { piece.floatChannelData![0][frame] = sin(Float(frame) * 0.1) * 0.5 }
            out += Int(converter.convert(piece)?.frameLength ?? 0)
        }
        out += Int(converter.flush()?.frameLength ?? 0)
        XCTAssertEqual(Double(out), 2.0 * 16_000, accuracy: 16, "2 s at 22.05 kHz became \(out) frames at 16 kHz")
    }

    // MARK: - Voices

    func testInstalledVoicesReportsOnlyEnglishAndItalianVoicesWithTheirQuality() {
        let voices = InstalledVoices.report()
        XCTAssertFalse(voices.isEmpty, "no English or Italian voice reported on this device")
        for voice in voices {
            let language = voice.language.lowercased()
            XCTAssertTrue(language.hasPrefix("en") || language.hasPrefix("it"), "\(voice.name): \(voice.language)")
            XCTAssertTrue(["default", "enhanced", "premium"].contains(voice.quality), voice.quality)
        }
        XCTAssertTrue(voices.contains { $0.language.hasPrefix("en") })
        let novelty = AVSpeechSynthesisVoice.speechVoices().filter { $0.voiceTraits.contains(.isNoveltyVoice) }
        XCTAssertFalse(voices.contains { voice in novelty.contains { $0.identifier == voice.identifier } },
                       "a novelty voice was reported")
        XCTAssertEqual(Set(voices.map(\.identifier)).count, voices.count, "a voice is reported twice")
    }
}
