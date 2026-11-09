import AVFoundation
import Foundation

// Speaking and sounding (PLAN-voice §2, §4): the instance's speech rendered to PCM and played on the graph's speech
// player, and the generated working sound and earcons on their own players. All of it leaves through the graph's
// voice-processing output, so echo cancellation hears it and keeps it out of the transcript.

/// The instance's speech: each utterance rendered by AVSpeechSynthesizer.write(_:toBufferCallback:) and played, in
/// the order queued, on the graph's speech player. `onStart` comes when an utterance's first buffer begins playing
/// (once all queued before it have played), `onFinish` when its last buffer has played, or when `stopAll` drops it.
/// An utterance dropped before it began gets `onFinish` only, never `onStart`, so a dropped sentence never reads as
/// speech that started. Callbacks come in order on one serial queue of their own.
public final class EngineSpeechOutput: SpeechOutput, @unchecked Sendable {
    private struct Item {
        let id: Int
        let text: String
        let onStart: @Sendable (Int) -> Void
        let onFinish: @Sendable (Int) -> Void
        var rendering = false
        var rendered = false
        var started = false
        var scheduled = 0
        var played = 0
    }

    private let graph: VoiceAudioGraph
    private let synthesizer = AVSpeechSynthesizer()
    /// Every piece of state, the synthesizer and the speech player are used on this queue only.
    private let queue = DispatchQueue(label: "shaft.voice.speech")
    private let callbacks = DispatchQueue(label: "shaft.voice.speech.callbacks")
    private var items: [Item] = []
    /// Ids of the buffers on the player, in play order; each buffer's completion pops the first.
    private var onPlayer: [Int] = []
    private var converter: PCMStreamConverter?
    private var voice: AVSpeechSynthesisVoice?
    private var generation = 0

    public init(graph: VoiceAudioGraph) {
        self.graph = graph
    }

    /// The voice speaking now (nil: the system default).
    public var voiceIdentifier: String? { queue.sync { voice?.identifier } }

    public func useVoice(identifier: String?, name: String?, language: VoiceLanguage) -> String? {
        if let identifier, let installed = AVSpeechSynthesisVoice(identifier: identifier) {
            queue.sync { voice = installed }
            return nil
        }
        let fallback = AVSpeechSynthesisVoice(language: language.bcp47)
        queue.sync { voice = fallback }
        guard let identifier else { return nil }
        let shown = (name?.isEmpty == false ? name : nil) ?? identifier
        #if os(iOS)
        return "\(shown) is not on this iPhone: Settings → Accessibility → Spoken Content → Voices. "
            + "The system voice speaks until it is installed."
        #else
        return "\(shown) is not on this Mac: System Settings → Accessibility → Spoken Content. "
            + "The system voice speaks until it is installed."
        #endif
    }

    public func speak(id: Int, text: String, onStart: @escaping @Sendable (Int) -> Void,
                      onFinish: @escaping @Sendable (Int) -> Void) {
        do {
            try graph.start()
        } catch {
            callbacks.async { onFinish(id) }
            return
        }
        queue.async { [self] in
            items.append(Item(id: id, text: text, onStart: onStart, onFinish: onFinish))
            renderNext()
        }
    }

    public func stopAll() {
        queue.sync { [self] in
            generation += 1
            let dropped = items
            items.removeAll()
            onPlayer.removeAll()
            converter = nil
            synthesizer.stopSpeaking(at: .immediate)
            graph.speechPlayer.stop()
            for item in dropped { callbacks.async { item.onFinish(item.id) } }
        }
    }

    // MARK: - On the queue

    /// Render the next utterance, if none is rendering.
    private func renderNext() {
        if items.contains(where: { $0.rendering }) { return }
        guard let index = items.firstIndex(where: { !$0.rendered }) else { return }
        items[index].rendering = true
        converter = nil
        let utterance = AVSpeechUtterance(string: items[index].text)
        utterance.voice = voice
        let current = generation
        synthesizer.write(utterance) { [weak self] buffer in
            guard let self else { return }
            // A copy goes to the queue, so nothing depends on whether the synthesizer reuses its buffers.
            nonisolated(unsafe) let copy = Self.copy(buffer)
            self.queue.async { self.received(copy, generation: current) }
        }
    }

    /// One buffer from the synthesizer: an empty one ends the utterance.
    private func received(_ buffer: AVAudioBuffer, generation: Int) {
        guard generation == self.generation, let index = items.firstIndex(where: { $0.rendering }) else { return }
        if let pcm = buffer as? AVAudioPCMBuffer, pcm.frameLength > 0 {
            if converter == nil || converter?.from != pcm.format {
                converter = PCMStreamConverter(from: pcm.format, to: VoiceAudioGraph.playerFormat)
            }
            if let out = converter?.convert(pcm) { enqueue(out, index: index, generation: generation) }
            settle()
            return
        }
        if let tail = converter?.flush() { enqueue(tail, index: index, generation: generation) }
        items[index].rendering = false
        items[index].rendered = true
        settle()
        renderNext()
    }

    /// Put one converted buffer on the speech player; its completion settles the queue. With the graph stopped
    /// under it, the buffer counts as played: there is nothing to play it on.
    private func enqueue(_ pcm: AVAudioPCMBuffer, index: Int, generation: Int) {
        items[index].scheduled += 1
        guard graph.isRunning else {
            items[index].played += 1
            return
        }
        onPlayer.append(items[index].id)
        let player = graph.speechPlayer
        player.scheduleBuffer(pcm, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self else { return }
            self.queue.async { self.played(generation: generation) }
        }
        if !player.isPlaying { player.play() }
    }

    /// An empty buffer (the end of an utterance) as is, else a copy of the synthesizer's buffer.
    private static func copy(_ buffer: AVAudioBuffer) -> AVAudioBuffer {
        guard let pcm = buffer as? AVAudioPCMBuffer, pcm.frameLength > 0,
              let copy = AVAudioPCMBuffer(pcmFormat: pcm.format, frameCapacity: pcm.frameLength) else { return buffer }
        copy.frameLength = pcm.frameLength
        let from = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: pcm.audioBufferList))
        let to = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
        for (source, target) in zip(from, to) {
            guard let data = source.mData, let destination = target.mData else { continue }
            memcpy(destination, data, Int(min(source.mDataByteSize, target.mDataByteSize)))
        }
        return copy
    }

    private func played(generation: Int) {
        guard generation == self.generation, !onPlayer.isEmpty else { return }
        let id = onPlayer.removeFirst()
        if let index = items.firstIndex(where: { $0.id == id }) { items[index].played += 1 }
        settle()
    }

    /// Fire what is now true, in queue order: the head starts when its first buffer is first on the player (or it
    /// rendered to nothing), and finishes when it is rendered and all its buffers have played.
    private func settle() {
        while let head = items.first {
            if !head.started {
                let firstOnPlayer = onPlayer.first == head.id
                let empty = head.rendered && head.scheduled == 0
                guard firstOnPlayer || empty else { return }
                items[0].started = true
                callbacks.async { head.onStart(head.id) }
            }
            guard head.rendered, head.played == head.scheduled else { return }
            items.removeFirst()
            callbacks.async { head.onFinish(head.id) }
        }
    }
}

/// The working sound and the state earcons, generated as PCM (no audio assets): a soft low pulse that loops without a
/// seam while a tool runs, ducked by 12 dB while the operator talks, and a short distinct earcon per state.
public final class GeneratedVoiceSounds: VoiceSounds, @unchecked Sendable {
    /// One loop of the working sound, seconds.
    static let loopSeconds: Double = 1.2
    /// −12 dB while the operator talks.
    static let duckGain: Float = 0.251_188_64

    private let graph: VoiceAudioGraph
    /// State and the two players are used on this queue only.
    private let queue = DispatchQueue(label: "shaft.voice.sounds")
    private var volume: Float
    private var ducked = false
    private var on = false
    private let loop = GeneratedVoiceSounds.workingLoop(sampleRate: VoiceAudioGraph.playerFormat.sampleRate)

    public init(graph: VoiceAudioGraph, volume: Float = VoiceTuning().workingVolume) {
        self.graph = graph
        self.volume = min(max(volume, 0), 1)
    }

    /// Whether the working sound is playing.
    public var isWorking: Bool { queue.sync { on } }

    /// The working player's gain now: the volume, less 12 dB while ducked.
    public var workingGain: Float { queue.sync { gain } }

    public func working(_ on: Bool) {
        if on, (try? graph.start()) == nil { return }
        queue.sync {
            let player = graph.workingPlayer
            if on {
                // Already looping: nothing to do. On but silent (the graph was stopped under it): start again.
                if self.on && player.isPlaying { return }
                self.on = true
                player.volume = gain
                player.scheduleBuffer(loop, at: nil, options: [.loops, .interrupts])
                player.play()
            } else {
                guard self.on else { return }
                self.on = false
                player.stop()
            }
        }
    }

    public func duckWorking(_ ducked: Bool) {
        queue.sync {
            self.ducked = ducked
            graph.workingPlayer.volume = gain
        }
    }

    public func setWorkingVolume(_ volume: Float) {
        queue.sync {
            self.volume = min(max(volume, 0), 1)
            graph.workingPlayer.volume = gain
        }
    }

    private var gain: Float { volume * (ducked ? Self.duckGain : 1) }

    public func earcon(_ state: VoiceState) {
        guard let buffer = Self.earconBuffer(state, sampleRate: VoiceAudioGraph.playerFormat.sampleRate),
              (try? graph.start()) != nil else { return }
        queue.sync {
            let player = graph.earconPlayer
            player.scheduleBuffer(buffer, at: nil, options: [.interrupts])
            if !player.isPlaying { player.play() }
        }
    }

    // MARK: - Generated audio

    /// One loop of the working sound: a 110 Hz tone with a soft octave, its loudness swelling and easing once per
    /// loop and never reaching silence. Whole cycles of every partial and of the swell fit the loop, so its end runs
    /// into its start without a click.
    static func workingLoop(sampleRate: Double) -> AVAudioPCMBuffer {
        let frames = Int((loopSeconds * sampleRate).rounded())
        let buffer = AVAudioPCMBuffer(pcmFormat: VoiceAudioGraph.playerFormat, frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = AVAudioFrameCount(frames)
        let channel = buffer.floatChannelData![0]
        for frame in 0..<frames {
            let t = Double(frame) / sampleRate
            let swell = 0.65 - 0.35 * cos(2 * Double.pi * t / loopSeconds)
            let tone = sin(2 * Double.pi * 110 * t) + 0.3 * sin(2 * Double.pi * 220 * t)
            channel[frame] = Float(0.3 * swell * tone / 1.3)
        }
        return buffer
    }

    /// A short earcon per state (nil for idle): rising for listening, a soft tick for you talking, a low pair for
    /// thinking, a single bell-like note for speaking, falling for ended.
    static func earconBuffer(_ state: VoiceState, sampleRate: Double) -> AVAudioPCMBuffer? {
        let notes: [(hz: Double, seconds: Double)]
        switch state {
        case .idle: return nil
        case .listening: notes = [(660, 0.09), (880, 0.12)]
        case .userTalking: notes = [(990, 0.05)]
        case .thinking: notes = [(440, 0.08), (392, 0.12)]
        case .speaking: notes = [(784, 0.14)]
        case .ended: notes = [(880, 0.09), (587, 0.16)]
        }
        let total = notes.reduce(0) { $0 + Int(($1.seconds * sampleRate).rounded()) }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: VoiceAudioGraph.playerFormat,
                                            frameCapacity: AVAudioFrameCount(total)),
              let channel = buffer.floatChannelData?[0] else { return nil }
        buffer.frameLength = AVAudioFrameCount(total)
        var offset = 0
        for note in notes {
            let frames = Int((note.seconds * sampleRate).rounded())
            for frame in 0..<frames {
                let t = Double(frame) / sampleRate
                // A quick rise and an easing fall, so no note clicks.
                let envelope = min(1, t / 0.008) * pow(1 - Double(frame) / Double(frames), 2)
                channel[offset + frame] = Float(0.25 * envelope * sin(2 * Double.pi * note.hz * t))
            }
            offset += frames
        }
        return buffer
    }
}
