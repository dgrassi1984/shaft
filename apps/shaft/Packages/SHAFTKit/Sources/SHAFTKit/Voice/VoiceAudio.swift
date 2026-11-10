import AVFoundation
import Foundation
import Speech

// The voice engine's audio (M2 lane D). One AVAudioEngine carries everything: the microphone with
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
                + "cannot hear you in \(name). Download it from the voice screen (a system download, only when you "
                + "tap Download), or install it from Dictation in the system settings."
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
///
/// The graph keeps itself going while a session runs (M3):
/// - an engine configuration change (the system stopped the engine because a device's format changed: AirPods in or
///   out, another input picked on the Mac) starts the engine again. A microphone tap made with `installMicrophoneTap`
///   is installed again in the input's new format (a tap installed on the input node directly keeps its old one).
/// - on iOS, a route change that adds or removes a device runs the engine again on the new route;
/// - on iOS, an interruption (a call, Siri, an alarm) pauses the graph; when it ends and the system says the app may
///   resume, the graph runs again. When the system says not to, it stays paused until the next `start()` (the next
///   sentence spoken, or the operator's next action).
///
/// What the players had queued: an engine the system stopped has dropped it (each buffer's completion fires, so a
/// sentence cut short counts as played). Those players are stopped too, so `isPlaying` tells the truth, and the
/// handlers added with `addRestartHandler` run so their owners queue again what should still sound (the working
/// loop). An engine the graph only paused (an interruption, a route change it was still running through) keeps it,
/// and plays on from where it was.
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
    /// The graph is meant to run: between `start()` and `stop()`, whatever the system did to the engine meanwhile.
    private var running = false
    /// An interruption paused the graph and the system has not said it may resume.
    private var interrupted = false
    /// The microphone tap the graph keeps installed in the input's current format (`installMicrophoneTap`), and that
    /// format.
    private var microphoneTap: (@Sendable (AVAudioPCMBuffer) -> Void)?
    private var tapFormat: AVAudioFormat?
    /// Restarts done after a configuration or route change, or an interruption's end (for tests and the log).
    private var restartCount = 0
    private var restartHandlers: [@Sendable () -> Void] = []
    /// The system stopped the engine and the players were stopped for it, and the restart handlers have not run since:
    /// kept across a start that failed, so the start that succeeds still runs them.
    private var playersDropped = false
    /// Tests: this many engine starts fail before one is tried for real.
    var failingStarts = 0
    /// System notifications and restart handlers run here, one at a time and never on the thread that posted them or
    /// under the lock: a change can be posted while `start()` holds it.
    private let changes: OperationQueue = {
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        queue.qualityOfService = .userInitiated
        queue.name = "shaft.voice.graph.changes"
        return queue
    }()
    private var observers: [NSObjectProtocol] = []

    public init(silent: Bool = false) {
        self.silent = silent
        observe()
    }

    deinit {
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
    }

    /// Whether the engine runs now.
    public var isRunning: Bool { lock.withLock { running && engine.isRunning } }

    /// Whether an interruption (a call, Siri) holds the graph paused now.
    public var isInterrupted: Bool { lock.withLock { interrupted } }

    /// How many times the graph ran again by itself after a change (configuration, route, an interruption's end).
    public var restarts: Int { lock.withLock { restartCount } }

    /// `handler` runs (on a queue of the graph's own) each time the graph runs again after the system had stopped the
    /// engine, which dropped what the players had queued: queue again what should still sound.
    public func addRestartHandler(_ handler: @escaping @Sendable () -> Void) {
        lock.withLock { restartHandlers.append(handler) }
    }

    /// Start the engine (idempotent). Voice processing is switched on before the first start, as the input node needs.
    /// After an interruption the system did not end with "resume", this is what resumes; after the system stopped
    /// the engine, this runs it again even before its configuration change is handled.
    public func start() throws {
        let dropped: Bool = try lock.withLock {
            if running && !interrupted && engine.isRunning { return false }
            try activateSession()
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
            // Running and not paused by us, yet stopped: the system stopped it, and the players' audio is gone.
            if running && !interrupted && stopDroppedPlayers() { playersDropped = true }
            refreshMicrophoneTap()
            try startEngine()
            running = true
            interrupted = false
            return takeDropped()
        }
        if dropped { runRestartHandlers() }
    }

    /// Stop the engine and every player (idempotent). On iOS the audio session is released for other apps.
    public func stop() {
        lock.withLock {
            guard running else { return }
            for player in [speechPlayer, workingPlayer, earconPlayer] { player.stop() }
            engine.stop()
            running = false
            interrupted = false
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

    /// Hear the microphone: `onBuffer` gets each buffer in the input's own format. The graph keeps this tap and
    /// installs it again, in the new format, whenever it runs again after a device change (AirPods' microphone has
    /// another format than the built-in one), so hearing goes on. A silent graph refuses.
    func installMicrophoneTap(_ onBuffer: @escaping @Sendable (AVAudioPCMBuffer) -> Void) throws {
        let node = try microphoneNode()
        lock.withLock {
            node.removeTap(onBus: 0)
            microphoneTap = onBuffer
            tapFormat = nil
            refreshMicrophoneTap()
        }
    }

    /// Stop hearing the microphone (safe when not hearing).
    func removeMicrophoneTap() {
        lock.withLock {
            guard microphoneTap != nil else { return }
            microphoneTap = nil
            tapFormat = nil
            engine.inputNode.removeTap(onBus: 0)
        }
    }

    // MARK: - Changes the system makes (M3)

    /// The engine stopped itself because a device's format changed: start it again with what is there now.
    func configurationChanged() {
        restart(reason: "configuration change")
    }

    /// A device came or went (iOS route change): run again on the new route. A route change of our own (the category
    /// set at start, an override) needs nothing unless it left the engine stopped.
    func routeChanged(deviceChanged: Bool) {
        let stalled = lock.withLock { running && !interrupted && !engine.isRunning }
        guard deviceChanged || stalled else { return }
        restart(reason: deviceChanged ? "route change" : "route change left the engine stopped")
    }

    /// The system interrupted the audio (a call, Siri): the engine is paused, the players keep their place.
    func interruptionBegan() {
        lock.withLock {
            guard running else { return }
            interrupted = true
            engine.pause()
        }
    }

    /// The interruption ended. `shouldResume`: the system says the app may play again, so the graph runs again.
    /// Otherwise it stays paused until the next `start()`.
    func interruptionEnded(shouldResume: Bool) {
        guard shouldResume else { return }
        let paused = lock.withLock { running && interrupted }
        guard paused else { return }
        restart(reason: "interruption ended", resuming: true)
    }

    /// Run the engine again after a change. An engine still running (a route change it followed) is paused and
    /// started, so the voice-processing I/O takes the new route and the players keep their place; one the system
    /// stopped is started with its players stopped and the restart handlers run (`addRestartHandler`). A start that
    /// fails is tried twice more, 0.3 s apart (a device can still be settling); then the graph stays stopped and the
    /// next `start()` tries again.
    private func restart(reason: String, resuming: Bool = false) {
        for attempt in 0..<3 {
            if attempt > 0 { Thread.sleep(forTimeInterval: 0.3) }
            let outcome: Result<Bool, any Error> = lock.withLock {
                // Stopped by its owner meanwhile, or paused by an interruption that is still on: nothing to do.
                guard running, built, resuming || !interrupted else { return .success(false) }
                do {
                    if resuming { try activateSession() }
                    // Paused by the graph (an interruption) or still running: the players' audio is kept.
                    if !resuming && !engine.isRunning && stopDroppedPlayers() { playersDropped = true }
                    if engine.isRunning { engine.pause() }
                    refreshMicrophoneTap()
                    try startEngine()
                    interrupted = false
                    restartCount += 1
                    return .success(takeDropped())
                } catch {
                    return .failure(error)
                }
            }
            switch outcome {
            case .success(let dropped):
                if dropped { runRestartHandlers() }
                return
            case .failure(let error):
                NSLog("SHAFT voice: running again after a %@ failed (attempt %d): %@", reason, attempt + 1,
                      error.localizedDescription)
            }
        }
    }

    /// Under `lock`, the engine stopped by the system: stop the players that still say they play (their queued audio
    /// is gone), so their owners see they must queue again. True when there was one.
    private func stopDroppedPlayers() -> Bool {
        var any = false
        for player in [speechPlayer, workingPlayer, earconPlayer] where player.isPlaying {
            player.stop()
            any = true
        }
        return any
    }

    /// Under `lock`, after a start that succeeded: whether the restart handlers must run, cleared.
    private func takeDropped() -> Bool {
        defer { playersDropped = false }
        return playersDropped
    }

    private func runRestartHandlers() {
        let handlers = lock.withLock { restartHandlers }
        guard !handlers.isEmpty else { return }
        changes.addOperation { for handler in handlers { handler() } }
    }

    /// Under `lock`: the microphone tap, if any, in the input's format now. A tap in a stale format stops the engine
    /// from starting, so it is installed again whenever the format moved.
    private func refreshMicrophoneTap() {
        guard !silent, let tap = microphoneTap else { return }
        let node = engine.inputNode
        let format = node.outputFormat(forBus: 0)
        if let tapFormat, tapFormat == format { return }
        node.removeTap(onBus: 0)
        node.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in tap(buffer) }
        tapFormat = format
    }

    /// Under `lock`. On iOS: the session's category for this graph, made active.
    private func activateSession() throws {
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
    }

    /// Under `lock`.
    private func startEngine() throws {
        if failingStarts > 0 {
            failingStarts -= 1
            throw VoiceAudioError.engineFailed("a start made to fail (test)")
        }
        connectMixerToOutput()
        engine.prepare()
        do {
            try engine.start()
        } catch {
            throw VoiceAudioError.engineFailed(error.localizedDescription)
        }
    }

    /// The format the main mixer feeds the output in: the microphone's sample rate, the output's channels (stereo when
    /// the output tells none). Nil on a silent graph, or with no microphone to match.
    func mixerOutputFormat() -> AVAudioFormat? {
        guard !silent else { return nil }
        let rate = engine.inputNode.outputFormat(forBus: 0).sampleRate
        guard rate > 0 else { return nil }
        let channels = engine.outputNode.inputFormat(forBus: 0).channelCount
        return AVAudioFormat(standardFormatWithSampleRate: rate, channels: channels > 0 ? channels : 2)
    }

    /// Under `lock`: the main mixer feeds the output at the microphone's sample rate. Voice processing needs both sides
    /// at one rate, or the engine does not start (-10875, "client-side input and output formats do not match"). With
    /// it on, the output node tells no format of its own, so a mixer left to connect itself falls back to 44.1 kHz
    /// against a 48 kHz microphone. Done on every start, so a device that moved the rate (AirPods) is followed.
    private func connectMixerToOutput() {
        guard let format = mixerOutputFormat() else { return }
        let current = engine.mainMixerNode.outputFormat(forBus: 0)
        if current.sampleRate == format.sampleRate && current.channelCount == format.channelCount { return }
        engine.connect(engine.mainMixerNode, to: engine.outputNode, format: format)
    }

    /// Listen for what the system does to the audio: the engine's configuration changes (both platforms), and on iOS
    /// the session's interruptions and route changes.
    private func observe() {
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine,
                                            queue: changes) { [weak self] _ in
            self?.configurationChanged()
        })
        #if os(iOS)
        observers.append(center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil,
                                            queue: changes) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            let reason = raw.flatMap(AVAudioSession.RouteChangeReason.init(rawValue:))
            self?.routeChanged(deviceChanged: reason == .newDeviceAvailable || reason == .oldDeviceUnavailable)
        })
        if #available(iOS 27, *) {
            observers.append(center.addObserver(forName: AVAudioSession.didBecomeInactiveNotification, object: nil,
                                                queue: changes) { [weak self] note in
                let context = note.userInfo?[AVAudioSession.deactivationContextKey] as? AVAudioSession.DeactivationContext
                guard context?.source == .system else { return }
                self?.interruptionBegan()
            })
            observers.append(center.addObserver(forName: AVAudioSession.resumptionRecommendationNotification,
                                                object: nil, queue: changes) { [weak self] note in
                let context = note.userInfo?[AVAudioSession.resumptionContextKey] as? AVAudioSession.ResumptionContext
                self?.interruptionEnded(shouldResume: context?.recommendation == .shouldResume)
            })
        } else {
            observers.append(center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil,
                                                queue: changes) { [weak self] note in
                let rawType = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
                switch rawType.flatMap(AVAudioSession.InterruptionType.init(rawValue:)) {
                case .began:
                    self?.interruptionBegan()
                case .ended:
                    let rawOptions = note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
                    let options = AVAudioSession.InterruptionOptions(rawValue: rawOptions)
                    self?.interruptionEnded(shouldResume: options.contains(.shouldResume))
                default:
                    break
                }
            })
        }
        #endif
    }
}

extension VoiceAudioGraph {
    /// After the system stopped the engine (a device change), the working or waiting sound loops on if it was on: its
    /// player lost the loop with the engine, and VoiceTurn asks for each sound only when it starts.
    public func keepWorkingSound(_ sounds: GeneratedVoiceSounds) {
        addRestartHandler { [weak sounds] in sounds?.resumeLoops() }
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
