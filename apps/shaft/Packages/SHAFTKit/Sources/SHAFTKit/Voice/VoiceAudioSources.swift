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

    /// Feed one buffer of audio, in any PCM format (converted to the analyzer's). Call from one thread at a time.
    func feed(_ buffer: AVAudioPCMBuffer) {
        let converted: AVAudioPCMBuffer? = lock.withLock {
            if stopped { return nil }
            if converter == nil || converter?.from != buffer.format {
                converter = PCMStreamConverter(from: buffer.format, to: analyzerFormat)
            }
            return converter?.convert(buffer)
        }
        if let converted { input.yield(AnalyzerInput(buffer: converted)) }
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
            let node = try graph.microphoneNode()
            let format = node.outputFormat(forBus: 0)
            node.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
                pipeline.feed(buffer)
            }
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
        graph.engine.inputNode.removeTap(onBus: 0)
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
