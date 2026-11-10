import AVFoundation
import Foundation
import Speech

// Hearing (PLAN-voice §2, §6): the on-device transcriber fed from the microphone (MicTranscriber), or from recorded
// files played into it at real time (FileTranscriber, the Debug test hook). Both go through the same pipeline, so a
// test run hears exactly as a live one does, minus the microphone.

/// One SpeechAnalyzer with a SpeechTranscriber for a language, fed PCM buffers of any format as they come. Results,
/// volatile and final, go to `onResult` with the VoiceClock time they arrived; none after `stop` returns.
final class TranscriberPipeline: @unchecked Sendable {
    let analyzerFormat: AVAudioFormat
    private let analyzer: SpeechAnalyzer
    private let input: AsyncStream<AnalyzerInput>.Continuation
    private let lock = NSLock()
    private var converter: PCMStreamConverter?
    private var resultsTask: Task<Void, Never>?
    private var stopped = false

    private init(analyzer: SpeechAnalyzer, analyzerFormat: AVAudioFormat,
                 input: AsyncStream<AnalyzerInput>.Continuation) {
        self.analyzer = analyzer
        self.analyzerFormat = analyzerFormat
        self.input = input
    }

    /// Start hearing in `language`. Throws, naming the language, when its model is not installed: the model is a
    /// system download the operator starts, never this code.
    static func start(language: VoiceLanguage,
                      onResult: @escaping @Sendable (TranscriptEvent) -> Void) async throws -> TranscriberPipeline {
        guard SpeechTranscriber.isAvailable else { throw VoiceAudioError.transcriberUnavailable }
        guard await isInstalled(language) else { throw VoiceAudioError.transcriberModelMissing(language) }
        // Fast results: measured on generated speech (M2), the first volatile result came 0.6 s after the speech
        // began with them and 3.5 s without (after a 2.8 s sentence had ended), too late for barge-in.
        let transcriber = SpeechTranscriber(locale: Locale(identifier: language.localeIdentifier),
                                            transcriptionOptions: [],
                                            reportingOptions: [.volatileResults, .fastResults],
                                            attributeOptions: [])
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw VoiceAudioError.noAnalyzerFormat
        }
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        let pipeline = TranscriberPipeline(analyzer: analyzer, analyzerFormat: format, input: continuation)
        try await analyzer.prepareToAnalyze(in: format)
        try await analyzer.start(inputSequence: stream)
        pipeline.resultsTask = Task { [weak pipeline] in
            do {
                for try await result in transcriber.results {
                    let text = String(result.text.characters)
                    // An empty result says nothing was heard; passing it on would read as the operator talking.
                    if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { continue }
                    let event = TranscriptEvent(text: text, isFinal: result.isFinal, at: VoiceClock.now())
                    guard let pipeline, !pipeline.isStopped else { return }
                    onResult(event)
                }
            } catch {
                // The analyzer was cancelled (stop) or failed; either way hearing has ended.
            }
        }
        return pipeline
    }

    /// Whether `language`'s transcriber model is installed on this device.
    static func isInstalled(_ language: VoiceLanguage) async -> Bool {
        let wanted = Locale(identifier: language.localeIdentifier).identifier(.bcp47).lowercased()
        return await SpeechTranscriber.installedLocales.contains { $0.identifier(.bcp47).lowercased() == wanted }
    }

    private var isStopped: Bool { lock.withLock { stopped } }

    /// Quiet this long after speech, and what was heard is finalized at once. The transcriber alone can hold its
    /// final back for seconds (M2 end-to-end: 6 s once), and the end of your turn counts from the final. Measured on
    /// the audio, never on the transcript: volatile text can stand still for a second while you are still talking,
    /// and finalizing then cut a sentence in two ("to." / "…the land?", M2). M3 tunes it with a real voice.
    static let finalizeAfterQuietS: TimeInterval = 0.5
    /// A buffer is speech when it is this far above the noise floor, and above `speechFloorDB` whatever the floor.
    static let speechAboveFloorDB: Float = 12
    static let speechFloorDB: Float = -50

    /// Voice activity, from the level of each buffer fed (under `lock`).
    private var noiseFloorDB: Float = -60
    private var heardSpeech = false
    private var quietFor: TimeInterval = 0

    /// Feed one buffer of audio, in any PCM format (converted to the analyzer's). Call from one thread at a time.
    func feed(_ buffer: AVAudioPCMBuffer) {
        var finalizeNow = false
        let converted: AVAudioPCMBuffer? = lock.withLock {
            if stopped { return nil }
            if converter == nil || converter?.from != buffer.format {
                converter = PCMStreamConverter(from: buffer.format, to: analyzerFormat)
            }
            finalizeNow = hearQuiet(buffer)
            return converter?.convert(buffer)
        }
        if let converted { input.yield(AnalyzerInput(buffer: converted)) }
        if finalizeNow {
            Task { [analyzer] in try? await analyzer.finalize(through: nil) }
        }
    }

    /// Track speech and quiet in `buffer`; true when the quiet after speech has just reached `finalizeAfterQuietS`.
    private func hearQuiet(_ buffer: AVAudioPCMBuffer) -> Bool {
        let seconds = Double(buffer.frameLength) / buffer.format.sampleRate
        let level = Self.levelDB(buffer)
        // The floor follows the quietest level at once and rises slowly (3 dB a second), so steady noise stops
        // counting as speech while speech never raises it much.
        noiseFloorDB = min(level, noiseFloorDB + Float(seconds) * 3)
        if level > max(Self.speechFloorDB, noiseFloorDB + Self.speechAboveFloorDB) {
            heardSpeech = true
            quietFor = 0
            return false
        }
        guard heardSpeech else { return false }
        quietFor += seconds
        if quietFor >= Self.finalizeAfterQuietS {
            heardSpeech = false
            return true
        }
        return false
    }

    /// The buffer's RMS level in dBFS (first channel; -120 for digital silence).
    static func levelDB(_ buffer: AVAudioPCMBuffer) -> Float {
        let n = Int(buffer.frameLength)
        guard n > 0 else { return -120 }
        var sum: Double = 0
        if let samples = buffer.floatChannelData?[0] {
            for i in 0..<n { sum += Double(samples[i]) * Double(samples[i]) }
        } else if let samples = buffer.int16ChannelData?[0] {
            for i in 0..<n { let v = Double(samples[i]) / 32768; sum += v * v }
        } else {
            return 0                                     // a format with no level to read: count it as sound
        }
        let rms = (sum / Double(n)).squareRoot()
        return rms > 1e-6 ? Float(20 * log10(rms)) : -120
    }

    /// Stop hearing now: input ends, the analysis is cancelled, and no result is delivered after this returns.
    func stop() async {
        let task: Task<Void, Never>? = lock.withLock {
            if stopped { return nil }
            stopped = true
            return resultsTask
        }
        input.finish()
        await analyzer.cancelAndFinishNow()
        task?.cancel()
        await task?.value
    }
}

/// The microphone, through the graph's voice-processing input (echo cancellation), into the on-device transcriber.
/// Needs the microphone permission (VoicePermissions); a silent graph refuses it.
public final class MicTranscriber: TranscriptSource, @unchecked Sendable {
    private let graph: VoiceAudioGraph
    private let lock = NSLock()
    private var pipeline: TranscriberPipeline?

    public init(graph: VoiceAudioGraph) {
        self.graph = graph
    }

    public func start(language: VoiceLanguage, onResult: @escaping @Sendable (TranscriptEvent) -> Void) async throws {
        await stop()
        if graph.silent { throw VoiceAudioError.silentGraphHasNoMicrophone }
        let pipeline = try await TranscriberPipeline.start(language: language, onResult: onResult)
        do {
            // Through the graph, which installs the tap again in the input's new format after a route or
            // configuration change (AirPods in or out): a tap installed here directly would stop hearing then.
            try graph.installMicrophoneTap { pipeline.feed($0) }
        } catch {
            await pipeline.stop()
            throw error
        }
        lock.withLock { self.pipeline = pipeline }
    }

    public func stop() async {
        let running: TranscriberPipeline? = lock.withLock {
            defer { pipeline = nil }
            return pipeline
        }
        guard let running else { return }
        graph.removeMicrophoneTap()
        await running.stop()
    }
}

/// The Debug test hook (plan §6): recorded audio files played into the same transcriber pipeline at real time from
/// `start`, each at its own offset, with silence between and after them until `stop` — as a microphone would hear
/// them. A whole voice turn, and a barge-in at a chosen moment, runs with no microphone and nothing played aloud.
public final class FileTranscriber: TranscriptSource, @unchecked Sendable {
    /// Audio is fed in pieces this long, each when its time has come.
    public static let chunkSeconds: TimeInterval = 0.1

    private let clips: [(url: URL, at: TimeInterval)]
    private let lock = NSLock()
    private var pipeline: TranscriberPipeline?
    private var feeder: Task<Void, Never>?
    private var zero: TimeInterval?

    public init(_ clips: [(URL, at: TimeInterval)]) {
        self.clips = clips.map { (url: $0.0, at: $0.at) }
    }

    /// The VoiceClock time of the timeline's zero (when `start` began feeding): a clip at offset `at` begins being
    /// heard at `timelineStart + at`. Nil before `start`.
    public var timelineStart: TimeInterval? { lock.withLock { zero } }

    public func start(language: VoiceLanguage, onResult: @escaping @Sendable (TranscriptEvent) -> Void) async throws {
        await stop()
        let pipeline = try await TranscriberPipeline.start(language: language, onResult: onResult)
        let rate = pipeline.analyzerFormat.sampleRate
        let placed: [(start: Int, samples: [Float])]
        do {
            placed = try clips.map { (start: Int(($0.at * rate).rounded()), samples: try Self.readMono($0.url, rate: rate)) }
        } catch {
            await pipeline.stop()
            throw error
        }
        guard let chunkFormat = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1) else {
            await pipeline.stop()
            throw VoiceAudioError.noAnalyzerFormat
        }
        let chunkFrames = Int((Self.chunkSeconds * rate).rounded())
        let started = VoiceClock.now()
        let clock = ContinuousClock()
        let origin = clock.now
        let feeder = Task.detached {
            var index = 0
            while !Task.isCancelled {
                // A microphone hands over a piece once it has been heard: wait for the end of this piece's time.
                let due = origin + .seconds(Double(index + 1) * Self.chunkSeconds)
                try? await Task.sleep(until: due, clock: clock)
                if Task.isCancelled { return }
                guard let buffer = AVAudioPCMBuffer(pcmFormat: chunkFormat,
                                                    frameCapacity: AVAudioFrameCount(chunkFrames)),
                      let channel = buffer.floatChannelData?[0] else { return }
                buffer.frameLength = AVAudioFrameCount(chunkFrames)
                let first = index * chunkFrames
                for frame in 0..<chunkFrames { channel[frame] = 0 }
                for clip in placed {
                    let lower = max(first, clip.start)
                    let upper = min(first + chunkFrames, clip.start + clip.samples.count)
                    if lower >= upper { continue }
                    for position in lower..<upper {
                        channel[position - first] += clip.samples[position - clip.start]
                    }
                }
                pipeline.feed(buffer)
                index += 1
            }
        }
        lock.withLock {
            self.pipeline = pipeline
            self.feeder = feeder
            self.zero = started
        }
    }

    public func stop() async {
        let (running, feeding): (TranscriberPipeline?, Task<Void, Never>?) = lock.withLock {
            defer {
                pipeline = nil
                feeder = nil
            }
            return (pipeline, feeder)
        }
        feeding?.cancel()
        await feeding?.value
        await running?.stop()
    }

    /// A whole audio file as mono float samples at `rate`.
    static func readMono(_ url: URL, rate: Double) throws -> [Float] {
        let file: AVAudioFile
        do {
            file = try AVAudioFile(forReading: url)
        } catch {
            throw VoiceAudioError.unreadableAudio(url.path)
        }
        let source = file.processingFormat
        guard let whole = AVAudioPCMBuffer(pcmFormat: source, frameCapacity: AVAudioFrameCount(file.length)),
              (try? file.read(into: whole)) != nil,
              let target = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1),
              let converter = PCMStreamConverter(from: source, to: target) else {
            throw VoiceAudioError.unreadableAudio(url.path)
        }
        var samples: [Float] = []
        for piece in [converter.convert(whole), converter.flush()] {
            guard let piece, let channel = piece.floatChannelData?[0] else { continue }
            samples.append(contentsOf: UnsafeBufferPointer(start: channel, count: Int(piece.frameLength)))
        }
        return samples
    }
}
