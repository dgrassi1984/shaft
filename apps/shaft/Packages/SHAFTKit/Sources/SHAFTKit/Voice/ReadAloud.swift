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

/// One batch in order, with one completion after every utterance finishes or is cancelled.
@MainActor
public protocol ReadAloudSynth: AnyObject {
    func speak(_ utterances: [String], voice: AVSpeechSynthesisVoice?,
               onStart: @escaping @MainActor () -> Void,
               onFinish: @escaping @MainActor () -> Void)
    func stop()
}

@MainActor
public final class SystemReadAloudSynth: NSObject, ReadAloudSynth, AVSpeechSynthesizerDelegate {
    private let synthesizer = AVSpeechSynthesizer()
    private var pending: [AVSpeechUtterance] = []
    private var onStart: (@MainActor () -> Void)?
    private var onFinish: (@MainActor () -> Void)?
    #if os(iOS)
    private var sessionActive = false
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
                      onStart: @escaping @MainActor () -> Void,
                      onFinish: @escaping @MainActor () -> Void) {
        stop()
        self.onStart = onStart
        self.onFinish = onFinish
        guard !utterances.isEmpty else { finish(); return }
        #if os(iOS)
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
            try session.setActive(true)
            sessionActive = true
        } catch {
            finish()
            return
        }
        #endif
        let batch = utterances.map { text in
            let utterance = AVSpeechUtterance(string: text)
            utterance.voice = voice
            return utterance
        }
        pending = batch
        for utterance in batch { synthesizer.speak(utterance) }
    }

    public func stop() {
        synthesizer.stopSpeaking(at: .immediate)
        finish()
    }

    public nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                                             didStart utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        Task { @MainActor [weak self] in self?.started(id) }
    }

    private func started(_ id: ObjectIdentifier) {
        guard pending.contains(where: { ObjectIdentifier($0) == id }) else { return }
        let start = onStart
        onStart = nil
        start?()
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
        onStart = nil
        #if os(iOS)
        if sessionActive {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
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
    private let startTimeout: TimeInterval
    @ObservationIgnored private var startWatchdog: Task<Void, Never>?
    private var generation = 0

    public init(synth: any ReadAloudSynth = SystemReadAloudSynth(), startTimeout: TimeInterval = 5) {
        self.synth = synth
        self.startTimeout = startTimeout
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
        startWatchdog = Task { [weak self, startTimeout] in
            do { try await Task.sleep(for: .seconds(startTimeout)) } catch { return }
            guard !Task.isCancelled, let self, self.generation == mine else { return }
            self.stop()
        }
        synth.speak(utterances, voice: voice, onStart: { [weak self] in
            guard let self, self.generation == mine else { return }
            self.startWatchdog?.cancel()
            self.startWatchdog = nil
        }, onFinish: { [weak self] in
            guard let self, self.generation == mine else { return }
            self.startWatchdog?.cancel()
            self.startWatchdog = nil
            self.playing = nil
        })
    }

    public func stop() {
        generation += 1
        startWatchdog?.cancel()
        startWatchdog = nil
        playing = nil
        synth.stop()
    }
}
