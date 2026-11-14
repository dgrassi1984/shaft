import AVFoundation
import Observation

/// Reads a reply aloud on the watch: the sentences the iPhone prepared (markup already turned into speech), with the
/// instance's voice for the reply's language when the watch has it installed, else the watch's voice for that language.
@MainActor
@Observable
final class WatchSpeaker: NSObject {
    private(set) var speaking = false
    @ObservationIgnored private let synthesizer = AVSpeechSynthesizer()
    /// Utterances still to finish; when the last one ends (or is cancelled) speaking is over.
    @ObservationIgnored private var pending = Set<ObjectIdentifier>()
    /// No sign of speech (no start, no spoken word) for this long and the reading is over, so ■ never stays on while
    /// nothing is heard (an audio queue that fails after the synthesizer said it started never finishes).
    @ObservationIgnored private var watchdog: Task<Void, Never>?
    private static let stallTimeout: Duration = .seconds(5)
    // No `setActive` here: it can block the main thread (the system logs it as a hang risk). The synthesizer switches
    // the session on itself, with the category set in `speak`, and nothing needs switching off: the watch's speech
    // ducks no other audio.

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ reply: WatchReplyPayload) {
        stop()
        let sentences = reply.spoken.isEmpty ? [reply.text] : reply.spoken
        let voice = reply.voice.flatMap { AVSpeechSynthesisVoice(identifier: $0) }
            ?? AVSpeechSynthesisVoice(language: reply.language)
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
        for sentence in sentences where !sentence.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            let utterance = AVSpeechUtterance(string: sentence)
            utterance.voice = voice
            pending.insert(ObjectIdentifier(utterance))
            synthesizer.speak(utterance)
        }
        speaking = !pending.isEmpty
        if speaking { armWatchdog() }
    }

    private func armWatchdog() {
        watchdog?.cancel()
        watchdog = Task { [weak self] in
            do { try await Task.sleep(for: Self.stallTimeout) } catch { return }
            guard !Task.isCancelled else { return }
            self?.stop()
        }
    }

    private func progressed(_ id: ObjectIdentifier) {
        guard pending.contains(id) else { return }
        armWatchdog()
    }

    func stop() {
        watchdog?.cancel()
        watchdog = nil
        pending.removeAll()
        speaking = false
        synthesizer.stopSpeaking(at: .immediate)
    }

    private func done(_ id: ObjectIdentifier) {
        guard pending.remove(id) != nil else { return }
        guard pending.isEmpty else { armWatchdog(); return }
        watchdog?.cancel()
        watchdog = nil
        speaking = false
    }
}

extension WatchSpeaker: AVSpeechSynthesizerDelegate {
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didStart utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        Task { @MainActor in self.progressed(id) }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, willSpeakRangeOfSpeechString characterRange: NSRange,
                                       utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        Task { @MainActor in self.progressed(id) }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        Task { @MainActor in self.done(id) }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        Task { @MainActor in self.done(id) }
    }
}
