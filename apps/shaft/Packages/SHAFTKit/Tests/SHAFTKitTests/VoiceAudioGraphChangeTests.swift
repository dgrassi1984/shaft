import AVFoundation
import Foundation
@testable import SHAFTKit
import XCTest

// What the voice graph does when the system changes the audio under it (M3 lane V2): an engine configuration change
// (a device's format changed: AirPods in or out, another input on the Mac), a route change on iOS, an interruption (a
// call, Siri). All on a silent graph: the main mixer's output volume is 0, the input node is never touched, nothing is
// heard. The system's own act is imitated — the engine stopped under the graph, then the notification posted — since
// no test can plug AirPods in; the interruption and route handlers are called as the iOS observers call them.

/// Collects the samples a tap on a node sees.
private final class Samples: @unchecked Sendable {
    private let lock = NSLock()
    private var samples: [Float] = []

    func add(_ buffer: AVAudioPCMBuffer) {
        guard let channel = buffer.floatChannelData?[0] else { return }
        let piece = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
        lock.withLock { samples.append(contentsOf: piece) }
    }
    var count: Int { lock.withLock { samples.count } }
    func rms(from start: Int) -> Float {
        let tail = lock.withLock { Array(samples[min(start, samples.count)...]) }
        guard !tail.isEmpty else { return 0 }
        return (tail.reduce(0) { $0 + $1 * $1 } / Float(tail.count)).squareRoot()
    }
}

private final class Events: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [String] = []
    func add(_ value: String) { lock.withLock { stored.append(value) } }
    var values: [String] { lock.withLock { stored } }
}

private func waitUntil(_ timeout: TimeInterval, _ condition: () -> Bool) async -> Bool {
    let deadline = VoiceClock.now() + timeout
    while VoiceClock.now() < deadline {
        if condition() { return true }
        try? await Task.sleep(for: .milliseconds(20))
    }
    return condition()
}

final class VoiceAudioGraphChangeTests: XCTestCase {
    /// What the system does on a device change: it stops the engine, then posts the configuration change.
    private func systemChangesConfiguration(of graph: VoiceAudioGraph) {
        graph.engine.stop()
        NotificationCenter.default.post(name: .AVAudioEngineConfigurationChange, object: graph.engine)
    }

    /// Sound kept coming from the working player for `seconds` after `start` frames (a loop at volume 0.25 is about
    /// 0.05 RMS; silence is 0).
    private func workingSoundFlows(_ samples: Samples, after seconds: TimeInterval) async throws -> Bool {
        let from = samples.count
        try await Task.sleep(for: .seconds(seconds))
        return samples.count - from > Int(0.5 * seconds * VoiceAudioGraph.playerFormat.sampleRate)
            && samples.rms(from: from) > 0.02
    }

    func testAConfigurationChangeStartsTheEngineAgainAndTheWorkingSoundLoopsOn() async throws {
        let graph = VoiceAudioGraph(silent: true)
        defer { graph.stop() }
        let sounds = GeneratedVoiceSounds(graph: graph, volume: 0.25)
        graph.keepWorkingSound(sounds)
        let samples = Samples()
        sounds.working(true)
        graph.workingPlayer.installTap(onBus: 0, bufferSize: 1024, format: nil) { buffer, _ in samples.add(buffer) }
        defer { graph.workingPlayer.removeTap(onBus: 0) }
        let before = try await workingSoundFlows(samples, after: 0.4)
        XCTAssertTrue(before, "the working sound did not play before the change")

        systemChangesConfiguration(of: graph)
        let back = await waitUntil(3) { graph.isRunning && graph.workingPlayer.isPlaying }
        XCTAssertTrue(back, "the graph and its working sound did not run again after a configuration change")
        XCTAssertEqual(graph.restarts, 1)
        XCTAssertTrue(sounds.isWorking)
        // A stopped engine drops the loop the player had queued (a player still "playing" plays silence): without
        // the loop queued again, the sound stays off until the next tool starts.
        let after = try await workingSoundFlows(samples, after: 0.5)
        XCTAssertTrue(after, "the working sound stayed silent after the configuration change")
    }

    func testAfterTheSystemStoppedTheEngineNoPlayerSaysItPlaysWhatItLost() async throws {
        let graph = VoiceAudioGraph(silent: true)
        defer { graph.stop() }
        let sounds = GeneratedVoiceSounds(graph: graph, volume: 0.25)
        let handled = Events()
        graph.addRestartHandler { handled.add("restart") }
        sounds.working(true)
        XCTAssertTrue(graph.workingPlayer.isPlaying)
        systemChangesConfiguration(of: graph)
        let back = await waitUntil(3) { graph.isRunning }
        XCTAssertTrue(back)
        XCTAssertFalse(graph.workingPlayer.isPlaying, "a player that lost its loop still says it plays")
        let ran = await waitUntil(2) { handled.values == ["restart"] }
        XCTAssertTrue(ran, "the restart handler ran \(handled.values)")
        // `working(true)` takes a silent player as off and queues the loop again (what keepWorkingSound does).
        sounds.working(true)
        XCTAssertTrue(graph.workingPlayer.isPlaying)
    }

    func testStartingBeforeTheConfigurationChangeIsHandledStillQueuesTheWorkingSoundAgain() async throws {
        let graph = VoiceAudioGraph(silent: true)
        defer { graph.stop() }
        let sounds = GeneratedVoiceSounds(graph: graph, volume: 0.25)
        graph.keepWorkingSound(sounds)
        let samples = Samples()
        sounds.working(true)
        graph.workingPlayer.installTap(onBus: 0, bufferSize: 1024, format: nil) { buffer, _ in samples.add(buffer) }
        defer { graph.workingPlayer.removeTap(onBus: 0) }
        // The system stopped the engine; a sentence starts the graph before the notification arrives.
        graph.engine.stop()
        try graph.start()
        XCTAssertTrue(graph.isRunning)
        let flows = try await workingSoundFlows(samples, after: 0.6)
        XCTAssertTrue(flows, "the working sound stayed silent after a start that found the engine stopped")
    }

    func testSpeechGoesOnAcrossAConfigurationChange() async throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        defer { graph.stop() }
        let output = EngineSpeechOutput(graph: graph)
        let events = Events()
        let long = "This sentence is long enough to be still playing when the device changes, by a wide margin."
        output.speak(id: 1, text: long, onStart: { events.add("start \($0)") }, onFinish: { events.add("finish \($0)") })
        output.speak(id: 2, text: "And then one more.", onStart: { events.add("start \($0)") },
                     onFinish: { events.add("finish \($0)") })
        let started = await waitUntil(20) { events.values.contains("start 1") }
        XCTAssertTrue(started)
        try await Task.sleep(for: .milliseconds(400))
        systemChangesConfiguration(of: graph)
        let done = await waitUntil(30) { events.values.contains("finish 2") }
        XCTAssertTrue(done, "speech stalled after a configuration change: \(events.values)")
        XCTAssertEqual(events.values, ["start 1", "finish 1", "start 2", "finish 2"])
        XCTAssertEqual(graph.restarts, 1)
    }

    func testAConfigurationChangeAfterTheSessionStoppedLeavesTheGraphStopped() async throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        graph.stop()
        NotificationCenter.default.post(name: .AVAudioEngineConfigurationChange, object: graph.engine)
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertFalse(graph.isRunning, "a stopped graph started itself on a configuration change")
        XCTAssertFalse(graph.engine.isRunning)
        XCTAssertEqual(graph.restarts, 0)
    }

    func testAnotherEnginesConfigurationChangeIsNotThisGraphs() async throws {
        let graph = VoiceAudioGraph(silent: true)
        let other = VoiceAudioGraph(silent: true)
        try graph.start()
        try other.start()
        defer {
            graph.stop()
            other.stop()
        }
        systemChangesConfiguration(of: other)
        let otherBack = await waitUntil(3) { other.isRunning }
        XCTAssertTrue(otherBack)
        XCTAssertEqual(graph.restarts, 0)
        XCTAssertEqual(other.restarts, 1)
    }

    func testAnInterruptionPausesTheGraphAndItsEndWithResumeRunsItAgain() async throws {
        let graph = VoiceAudioGraph(silent: true)
        defer { graph.stop() }
        let sounds = GeneratedVoiceSounds(graph: graph, volume: 0.25)
        let samples = Samples()
        sounds.working(true)
        graph.workingPlayer.installTap(onBus: 0, bufferSize: 1024, format: nil) { buffer, _ in samples.add(buffer) }
        defer { graph.workingPlayer.removeTap(onBus: 0) }

        graph.interruptionBegan()
        XCTAssertTrue(graph.isInterrupted)
        XCTAssertFalse(graph.isRunning, "an interrupted graph still counts as running")
        // A configuration change during the interruption must not start the engine under the call.
        NotificationCenter.default.post(name: .AVAudioEngineConfigurationChange, object: graph.engine)
        try await Task.sleep(for: .milliseconds(400))
        XCTAssertFalse(graph.engine.isRunning, "the engine ran during the interruption")
        XCTAssertEqual(graph.restarts, 0)

        graph.interruptionEnded(shouldResume: true)
        XCTAssertFalse(graph.isInterrupted)
        XCTAssertTrue(graph.isRunning, "the graph did not resume when the system said it may")
        XCTAssertEqual(graph.restarts, 1)
        let flows = try await workingSoundFlows(samples, after: 0.5)
        XCTAssertTrue(flows, "the working sound did not come back after the interruption")
    }

    func testAnInterruptionEndedWithoutResumeStaysPausedUntilTheNextStart() throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        defer { graph.stop() }
        graph.interruptionBegan()
        graph.interruptionEnded(shouldResume: false)
        XCTAssertTrue(graph.isInterrupted, "the graph resumed though the system said not to")
        XCTAssertFalse(graph.isRunning)
        XCTAssertEqual(graph.restarts, 0)
        // The next sentence or sound starts the graph (each calls `start()`).
        try graph.start()
        XCTAssertTrue(graph.isRunning)
        XCTAssertFalse(graph.isInterrupted)
    }

    func testAnInterruptionOfAStoppedGraphChangesNothing() {
        let graph = VoiceAudioGraph(silent: true)
        graph.interruptionBegan()
        XCTAssertFalse(graph.isInterrupted)
        graph.interruptionEnded(shouldResume: true)
        XCTAssertFalse(graph.isRunning, "an interruption's end started a graph nobody started")
        XCTAssertEqual(graph.restarts, 0)
    }

    func testARouteChangeOfADeviceRestartsTheGraphAndOneOfOurOwnDoesNot() throws {
        let graph = VoiceAudioGraph(silent: true)
        try graph.start()
        defer { graph.stop() }
        graph.routeChanged(deviceChanged: true)
        XCTAssertTrue(graph.isRunning)
        XCTAssertEqual(graph.restarts, 1)
        // A route change of our own (the category set at start) with the engine running: left alone.
        graph.routeChanged(deviceChanged: false)
        XCTAssertEqual(graph.restarts, 1)
        // The same with the engine left stopped by it: run again.
        graph.engine.stop()
        graph.routeChanged(deviceChanged: false)
        XCTAssertTrue(graph.isRunning, "a route change that stopped the engine left it stopped")
        XCTAssertEqual(graph.restarts, 2)
        // During an interruption a route change waits for its end.
        graph.interruptionBegan()
        graph.routeChanged(deviceChanged: true)
        XCTAssertFalse(graph.isRunning)
        XCTAssertEqual(graph.restarts, 2)
    }

    func testASilentGraphRefusesAMicrophoneTap() {
        let graph = VoiceAudioGraph(silent: true)
        XCTAssertThrowsError(try graph.installMicrophoneTap { _ in }) { error in
            XCTAssertEqual(error as? VoiceAudioError, .silentGraphHasNoMicrophone)
        }
        graph.removeMicrophoneTap()  // nothing installed: no effect, no trap
    }
}
