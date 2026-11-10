import AVFoundation
@testable import SHAFTKit
import XCTest

// The real graph, on this machine's own microphone and output, with voice processing on: the one path the silent graph
// of the other tests never takes. Skipped where there is no microphone. Nothing is played, so nothing is heard.
final class VoiceAudioGraphHardwareTests: XCTestCase {
    private func skipWithoutMicrophone() throws {
        let probe = AVAudioEngine()
        if probe.inputNode.inputFormat(forBus: 0).sampleRate == 0 { throw XCTSkip("no microphone on this machine") }
    }

    /// Bug: with voice processing on, the output node tells no format, so the main mixer connected itself at 44.1 kHz
    /// against a 48 kHz microphone and the engine refused to start: "The audio engine did not start … error -10875"
    /// (a Mac mini with a headset-jack microphone, build 1135). The mixer now feeds the output at the microphone's rate.
    func testTheGraphWithVoiceProcessingStartsAndRunsAgainAtTheMicrophonesRate() throws {
        try skipWithoutMicrophone()
        let graph = VoiceAudioGraph(silent: false)
        defer { graph.stop() }
        XCTAssertNoThrow(try graph.start(), "the voice-processing engine must start")
        XCTAssertTrue(graph.isRunning)
        let micRate = graph.engine.inputNode.outputFormat(forBus: 0).sampleRate
        XCTAssertGreaterThan(micRate, 0)
        XCTAssertEqual(graph.engine.mainMixerNode.outputFormat(forBus: 0).sampleRate, micRate,
                       "the mixer must feed the output at the microphone's rate")
        graph.stop()
        XCTAssertNoThrow(try graph.start(), "and start again after a stop")
        XCTAssertTrue(graph.isRunning)
    }

    /// The microphone tap the transcriber uses goes on through the same start.
    func testTheMicrophoneTapInstallsOnTheRunningGraph() throws {
        try skipWithoutMicrophone()
        let graph = VoiceAudioGraph(silent: false)
        defer { graph.removeMicrophoneTap(); graph.stop() }
        XCTAssertNoThrow(try graph.installMicrophoneTap { _ in })
        XCTAssertTrue(graph.isRunning)
    }
}
