import AVFoundation
import Foundation
import Speech

// The voice engine's audio (PLAN-voice §2, §6; M2 lane D). One AVAudioEngine carries everything: the microphone with
// voice processing (echo cancellation), the instance's speech, the working sound and the earcons. Speech and sounds
// go out through the same voice-processing output the microphone listens past, so echo cancellation removes them from
// what the transcriber hears. This file: the graph, its errors, a buffer converter the other audio files share, the
// permissions and the device's voice list. VoiceAudioSources.swift hears; VoiceAudioOutput.swift speaks and sounds.

/// Why the voice audio could not do what was asked, in words for the voice screen.
public enum VoiceAudioError: Error, LocalizedError, Equatable, Sendable {
    /// The on-device speech model for this language is not installed; nothing is downloaded behind the operator's back.
    case transcriberModelMissing(VoiceLanguage)
    /// This device has no on-device transcriber at all.
    case transcriberUnavailable
    /// The transcriber offered no audio format to feed it.
    case noAnalyzerFormat
    /// A silent graph (tests) never touches the microphone.
    case silentGraphHasNoMicrophone
    /// A recorded file for the test hook could not be read.
    case unreadableAudio(String)
    /// The audio engine would not start.
    case engineFailed(String)

    public var errorDescription: String? {
        switch self {
        case .transcriberModelMissing(let language):
            let name = language == .en ? "English" : "Italian"
            return "The on-device speech model for \(name) (\(language.localeIdentifier)) is not installed, so voice "
                + "cannot hear you in \(name). Install it from Dictation in the system settings (pick \(name)); "
                + "SHAFT does not download it by itself."
        case .transcriberUnavailable:
            return "On-device speech recognition is not available on this device."
        case .noAnalyzerFormat:
            return "The speech recogniser offered no audio format to listen in."
        case .silentGraphHasNoMicrophone:
            return "This audio graph is silent (a test graph): it never uses the microphone."
        case .unreadableAudio(let path):
            return "Could not read the audio file \(path)."
        case .engineFailed(let message):
            return "The audio engine did not start: \(message)"
        }
    }
}

/// The one audio engine of a voice session: the microphone input with voice processing (echo cancellation), and three
/// players into the main mixer — the instance's speech, the working sound, the earcons. `silent` (tests): the main
/// mixer's output volume is 0 and the input node is never touched, so nothing is heard and no microphone permission
/// is asked. On iOS the audio session is `.playAndRecord` / `.voiceChat` while the graph runs.
public final class VoiceAudioGraph: @unchecked Sendable {
    /// Every player plays this format: mono, 48 kHz, float. Speech and sounds are converted or generated in it.
    static let playerFormat = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!

    public let silent: Bool
    let engine = AVAudioEngine()
    let speechPlayer = AVAudioPlayerNode()
    let workingPlayer = AVAudioPlayerNode()
    let earconPlayer = AVAudioPlayerNode()

    private let lock = NSLock()
    private var built = false
    private var running = false

    public init(silent: Bool = false) {
        self.silent = silent
    }

    /// Whether the engine runs now.
    public var isRunning: Bool { lock.withLock { running && engine.isRunning } }

    /// Start the engine (idempotent). Voice processing is switched on before the first start, as the input node needs.
    public func start() throws {
        try lock.withLock {
            if running && engine.isRunning { return }
            #if os(iOS)
            let session = AVAudioSession.sharedInstance()
            do {
                if silent {
                    try session.setCategory(.ambient, mode: .default, options: [.mixWithOthers])
                } else {
                    try session.setCategory(.playAndRecord, mode: .voiceChat,
                                            options: [.allowBluetoothHFP, .allowBluetoothA2DP, .defaultToSpeaker])
                }
                try session.setActive(true)
            } catch {
                throw VoiceAudioError.engineFailed("audio session: \(error.localizedDescription)")
            }
            #endif
            if !built {
                if !silent {
                    do {
                        try engine.inputNode.setVoiceProcessingEnabled(true)
                    } catch {
                        throw VoiceAudioError.engineFailed("voice processing: \(error.localizedDescription)")
                    }
                }
                for player in [speechPlayer, workingPlayer, earconPlayer] {
                    engine.attach(player)
                    engine.connect(player, to: engine.mainMixerNode, format: Self.playerFormat)
                }
                built = true
            }
            engine.mainMixerNode.outputVolume = silent ? 0 : 1
            engine.prepare()
            do {
                try engine.start()
            } catch {
                throw VoiceAudioError.engineFailed(error.localizedDescription)
            }
            running = true
        }
    }

    /// Stop the engine and every player (idempotent). On iOS the audio session is released for other apps.
    public func stop() {
        lock.withLock {
            guard running else { return }
            for player in [speechPlayer, workingPlayer, earconPlayer] { player.stop() }
            engine.stop()
            running = false
            #if os(iOS)
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            #endif
        }
    }

    /// The input node, for the microphone tap. Never called on a silent graph.
    func microphoneNode() throws -> AVAudioInputNode {
        if silent { throw VoiceAudioError.silentGraphHasNoMicrophone }
        try start()
        return engine.inputNode
    }
}

/// Converts a stream of PCM buffers from one format to another, keeping the resampler's state between buffers so a
/// stream cut into pieces converts as it would whole. Not thread-safe: one stream, one caller at a time.
final class PCMStreamConverter {
    let from: AVAudioFormat
    let to: AVAudioFormat
    private let converter: AVAudioConverter?

    init?(from: AVAudioFormat, to: AVAudioFormat) {
        self.from = from
        self.to = to
        if from == to {
            converter = nil
        } else {
            guard let made = AVAudioConverter(from: from, to: to) else { return nil }
            converter = made
        }
    }

    /// The buffer in the target format (nil: nothing came out yet, or the conversion failed).
    func convert(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let converter else { return buffer.frameLength > 0 ? buffer : nil }
        return run(converter, input: buffer, capacityFrames: buffer.frameLength)
    }

    /// What the resampler still holds at the end of the stream.
    func flush() -> AVAudioPCMBuffer? {
        guard let converter else { return nil }
        return run(converter, input: nil, capacityFrames: 4096)
    }

    private func run(_ converter: AVAudioConverter, input: AVAudioPCMBuffer?,
                     capacityFrames: AVAudioFrameCount) -> AVAudioPCMBuffer? {
        let ratio = to.sampleRate / from.sampleRate
        let capacity = AVAudioFrameCount((Double(capacityFrames) * ratio).rounded(.up)) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: to, frameCapacity: capacity) else { return nil }
        let feed = InputOnce(input)
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, outStatus in
            if let next = feed.take() {
                outStatus.pointee = .haveData
                return next
            }
            outStatus.pointee = feed.ended ? .endOfStream : .noDataNow
            return nil
        }
        if status == .error || out.frameLength == 0 { return nil }
        return out
    }

    /// Hands the converter its one input buffer, then "no data now" (or end of stream when there was none).
    private final class InputOnce: @unchecked Sendable {
        private var buffer: AVAudioPCMBuffer?
        let ended: Bool
        init(_ buffer: AVAudioPCMBuffer?) {
            self.buffer = buffer
            ended = buffer == nil
        }
        func take() -> AVAudioPCMBuffer? {
            defer { buffer = nil }
            return buffer
        }
    }
}

/// The microphone and speech-recognition permissions voice mode needs, read and asked for. Reading never prompts;
/// the request functions do (the app calls them when voice mode first opens, never in tests).
public enum VoicePermissions {
    public enum Status: String, Sendable, Equatable {
        case notDetermined, denied, restricted, granted
    }

    /// The microphone permission now.
    public static func microphone() -> Status {
        switch AVAudioApplication.shared.recordPermission {
        case .granted: return .granted
        case .denied: return .denied
        default: return .notDetermined
        }
    }

    /// Ask for the microphone (prompts once); true when granted.
    public static func requestMicrophone() async -> Bool {
        await AVAudioApplication.requestRecordPermission()
    }

    /// The speech-recognition permission now.
    public static func speechRecognition() -> Status {
        status(SFSpeechRecognizer.authorizationStatus())
    }

    /// Ask for speech recognition (prompts once).
    public static func requestSpeechRecognition() async -> Status {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: status($0)) }
        }
    }

    private static func status(_ value: SFSpeechRecognizerAuthorizationStatus) -> Status {
        switch value {
        case .authorized: return .granted
        case .denied: return .denied
        case .restricted: return .restricted
        default: return .notDetermined
        }
    }
}

/// The speaking voices installed on this device, as reported to the server (§17) for the instance to choose from:
/// English and Italian only, novelty voices (Bells, Zarvox…) left out since nobody holds a conversation in them.
public enum InstalledVoices {
    public static func report() -> [VoicesResponse.AvailableVoice] {
        AVSpeechSynthesisVoice.speechVoices()
            .filter { isReported($0.language) && !$0.voiceTraits.contains(.isNoveltyVoice) }
            .map {
                VoicesResponse.AvailableVoice(identifier: $0.identifier, name: $0.name, language: $0.language,
                                              quality: quality($0.quality))
            }
            .sorted { ($0.language, $0.name, $0.identifier) < ($1.language, $1.name, $1.identifier) }
    }

    /// en-* and it-* (BCP 47 tags as AVSpeechSynthesisVoice gives them).
    static func isReported(_ language: String) -> Bool {
        let lower = language.lowercased()
        return lower == "en" || lower == "it" || lower.hasPrefix("en-") || lower.hasPrefix("it-")
    }

    static func quality(_ quality: AVSpeechSynthesisVoiceQuality) -> String {
        switch quality {
        case .premium: return "premium"
        case .enhanced: return "enhanced"
        default: return "default"
        }
    }
}
