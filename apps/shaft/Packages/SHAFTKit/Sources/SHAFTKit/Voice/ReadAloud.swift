import AVFoundation
import Foundation
import NaturalLanguage
import Observation

public enum ReadAloudLanguage {
    public static func detect(_ text: String) -> VoiceLanguage? {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        let recognizer = NLLanguageRecognizer()
        recognizer.languageConstraints = [.english, .italian]
        recognizer.processString(text)
        switch recognizer.dominantLanguage {
        case .english: return .en
        case .italian: return .it
        default: return nil
        }
    }
}

/// One batch in order, with one completion after every utterance finishes or is cancelled. `onProgress` comes when
/// speech starts and again as each word is spoken: the player's sign that the reading is really going on.
@MainActor
public protocol ReadAloudSynth: AnyObject {
    func speak(_ utterances: [String], voice: AVSpeechSynthesisVoice?,
               onProgress: @escaping @MainActor () -> Void,
               onFinish: @escaping @MainActor () -> Void)
    func stop()
}

@MainActor
public final class SystemReadAloudSynth: NSObject, ReadAloudSynth, AVSpeechSynthesizerDelegate {
    private let synthesizer = AVSpeechSynthesizer()
    private var pending: [AVSpeechUtterance] = []
    private var onProgress: (@MainActor () -> Void)?
    private var onFinish: (@MainActor () -> Void)?
    #if os(iOS)
    private var sessionActive = false
    /// The audio session is switched on and off here, never on the main thread: `setActive` can block for a while
    /// (the system logs it as a hang risk). One serial queue keeps the order: a reading's switch-off always lands
    /// before the next reading's switch-on, so a late switch-off can't cut the next reading short.
    private static let sessionQueue = DispatchQueue(label: "shaft.read-aloud.audio-session")
    /// The reading whose session is being switched on: a stop meanwhile changes it, and that reading never starts.
    private var starting: UUID?
    #endif

    public override init() {
        super.init()
        synthesizer.delegate = self
        #if os(iOS)
        NotificationCenter.default.addObserver(self, selector: #selector(interrupted(_:)),
                                               name: AVAudioSession.interruptionNotification, object: nil)
        #endif
    }

    deinit {
        #if os(iOS)
        NotificationCenter.default.removeObserver(self)
        #endif
    }

    #if os(iOS)
    @objc private nonisolated func interrupted(_ notification: Notification) {
        guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              AVAudioSession.InterruptionType(rawValue: raw) == .began else { return }
        Task { @MainActor [weak self] in
            guard let self, !self.pending.isEmpty else { return }
            self.stop()
        }
    }
    #endif

    public func speak(_ utterances: [String], voice: AVSpeechSynthesisVoice?,
                      onProgress: @escaping @MainActor () -> Void,
                      onFinish: @escaping @MainActor () -> Void) {
        stop()
        self.onProgress = onProgress
        self.onFinish = onFinish
        guard !utterances.isEmpty else { finish(); return }
        let batch = utterances.map { text in
            let utterance = AVSpeechUtterance(string: text)
            utterance.voice = voice
            return utterance
        }
        pending = batch
        #if os(iOS)
        let reading = UUID()
        starting = reading
        Self.sessionQueue.async {
            let ready: Bool
            do {
                let session = AVAudioSession.sharedInstance()
                try session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
                try session.setActive(true)
                ready = true
            } catch {
                ready = false
            }
            Task { @MainActor [weak self] in self?.sessionReady(ready, for: reading) }
        }
        #else
        for utterance in batch { synthesizer.speak(utterance) }
        #endif
    }

    #if os(iOS)
    private func sessionReady(_ ready: Bool, for reading: UUID) {
        guard starting == reading else {
            // Stopped while the session was switching on: switch it off again, in order — unless a newer reading is
            // starting or speaking, which uses this same session (its switch-on was queued after this one).
            if ready, starting == nil, !sessionActive { Self.deactivateSession() }
            return
        }
        starting = nil
        guard ready else { finish(); return }
        sessionActive = true
        for utterance in pending { synthesizer.speak(utterance) }
    }

    private static func deactivateSession() {
        sessionQueue.async {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }
    #endif

    public func stop() {
        synthesizer.stopSpeaking(at: .immediate)
        finish()
    }

    public nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                                             didStart utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        Task { @MainActor [weak self] in self?.progressed(id) }
    }

    public nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                                             willSpeakRangeOfSpeechString characterRange: NSRange,
                                             utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        Task { @MainActor [weak self] in self?.progressed(id) }
    }

    private func progressed(_ id: ObjectIdentifier) {
        guard pending.contains(where: { ObjectIdentifier($0) == id }) else { return }
        onProgress?()
    }

    public nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                                             didFinish utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        Task { @MainActor [weak self] in self?.completed(id) }
    }

    public nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                                             didCancel utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        Task { @MainActor [weak self] in self?.completed(id) }
    }

    private func completed(_ id: ObjectIdentifier) {
        guard let index = pending.firstIndex(where: { ObjectIdentifier($0) == id }) else { return }
        pending.remove(at: index)
        guard pending.isEmpty else { return }
        finish()
    }

    private func finish() {
        pending.removeAll()
        onProgress = nil
        #if os(iOS)
        starting = nil
        if sessionActive {
            Self.deactivateSession()
            sessionActive = false
        }
        #endif
        let completion = onFinish
        onFinish = nil
        completion?()
    }
}

@MainActor
@Observable
public final class ReadAloudPlayer {
    public static let shared = ReadAloudPlayer()
    public private(set) var playing: String?
    private let synth: any ReadAloudSynth
    /// A reading with no sign of speech (no start, no spoken word) for this long is over: the ■ goes back to ▶. Speech
    /// that can't play (no audio device, a refused audio queue) may start and then never finish; the button must not
    /// say a message is being read when nothing is heard.
    private let stallTimeout: TimeInterval
    @ObservationIgnored private var watchdog: Task<Void, Never>?
    private var generation = 0

    public init(synth: any ReadAloudSynth = SystemReadAloudSynth(), stallTimeout: TimeInterval = 5) {
        self.synth = synth
        self.stallTimeout = stallTimeout
    }

    public func toggle(id: String, markdown: String, voiceIdentifier: String?, fallback: VoiceLanguage) {
        let wasPlaying = playing == id
        stop()
        guard !wasPlaying else { return }
        let language = ReadAloudLanguage.detect(markdown) ?? fallback
        var chunker = SpeechChunker(language: language)
        let utterances = chunker.feed(markdown) + chunker.finish()
        guard !utterances.isEmpty else { return }
        let voice = voiceIdentifier.flatMap { AVSpeechSynthesisVoice(identifier: $0) }
            ?? AVSpeechSynthesisVoice(language: language.bcp47)
        let mine = generation
        playing = id
        armWatchdog(for: mine)
        synth.speak(utterances, voice: voice, onProgress: { [weak self] in
            guard let self, self.generation == mine else { return }
            self.armWatchdog(for: mine)
        }, onFinish: { [weak self] in
            guard let self, self.generation == mine else { return }
            self.watchdog?.cancel()
            self.watchdog = nil
            self.playing = nil
        })
    }

    /// (Re)start the stall countdown for reading `mine`: it stops the reading unless speech shows progress first.
    private func armWatchdog(for mine: Int) {
        watchdog?.cancel()
        watchdog = Task { [weak self, stallTimeout] in
            do { try await Task.sleep(for: .seconds(stallTimeout)) } catch { return }
            guard !Task.isCancelled, let self, self.generation == mine else { return }
            self.stop()
        }
    }

    public func stop() {
        generation += 1
        watchdog?.cancel()
        watchdog = nil
        playing = nil
        synth.stop()
    }
}
