import AVFoundation
import Observation
import SHAFTKit
import SwiftUI

/// Choose and preview this device's voices. Only an explicit Preview tap produces audio.
///
/// `embedded`: shown inside another screen (the voice screen's first-time choice) rather than as its own sheet: no
/// navigation bar, title or Done of its own, and choosing never dismisses — `dismiss` there is the voice screen's
/// own, and closed voice mode as the voice was chosen (M3, iPhone Simulator). The host acts on `onChosen`.
struct VoicePicker: View {
    let client: APIClient
    let instance: String
    let language: VoiceLanguage
    var embedded = false
    var onChosen: (VoicesResponse) -> Void = { _ in }
    @Environment(\.dismiss) private var dismiss
    @State private var installed: [VoicesResponse.AvailableVoice] = []
    @State private var current: String?
    @State private var loaded = false
    @State private var loadError: String?
    @State private var errors: [String: String] = [:]
    @State private var saving = false
    @State private var preview = VoicePreview()

    var body: some View {
        Group {
            if embedded {
                list
            } else {
                NavigationStack {
                    list
                        .navigationTitle("Choose a voice for \(instance)")
                        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
                }
            }
        }
        .task {
            installed = InstalledVoices.report().filter {
                $0.language.lowercased() == language.rawValue || $0.language.lowercased().hasPrefix(language.rawValue + "-")
            }
            await load()
        }
        .onDisappear { preview.stop() }
    }

    private var list: some View {
            List {
                if let loadError {
                    Text(loadError).foregroundStyle(Color.warning)
                    Button("Try again") { Task { await load() } }
                } else if !loaded {
                    ProgressView("Loading voices…")
                }
                ForEach(["premium", "enhanced", "default"], id: \.self) { quality in
                    let voices = installed.filter { $0.quality == quality }
                    if !voices.isEmpty {
                        Section(quality.capitalized) {
                            ForEach(voices, id: \.identifier) { voice in
                                VStack(alignment: .leading, spacing: Design.Space.s4) {
                                    HStack {
                                        VStack(alignment: .leading, spacing: Design.Space.s4) {
                                            Text(voice.name)
                                            Text(voice.language).font(.caption).foregroundStyle(Color.textSecondary)
                                            if current == voice.identifier {
                                                Label("In use", systemImage: "checkmark").font(.caption)
                                            }
                                        }
                                        Spacer()
                                        Button {
                                            preview.toggle(voice, language: language)
                                        } label: {
                                            Text(preview.identifier == voice.identifier ? "Stop preview" : "Preview")
                                                .quietHitArea()
                                        }
                                        .accessibilityLabel(preview.identifier == voice.identifier ? "Stop preview of \(voice.name)" : "Preview \(voice.name)")
                                        Button { choose(voice) } label: { Text("Choose").quietHitArea() }
                                            .disabled(!loaded || saving)
                                            .accessibilityLabel("Choose \(voice.name)")
                                    }
                                    if let error = errors[voice.identifier] {
                                        Text(error).font(.footnote).foregroundStyle(Color.warning)
                                    }
                                }
                            }
                        }
                    }
                }
                if installed.isEmpty {
                    Text("No voices for this language are installed.").foregroundStyle(Color.textSecondary)
                }
                Section {} footer: {
                    #if os(iOS)
                    Text("Install more voices in Settings → Accessibility → Spoken Content → Voices.")
                    #else
                    Text("Install more voices in System Settings → Accessibility → Spoken Content.")
                    #endif
                }
            }
            .buttonStyle(.borderless)
            .scrollContentBackground(.hidden)
            .background(Color.canvas)
    }

    private func load() async {
        do {
            let answer = try await client.voices(instance)
            current = answer.voices[language.rawValue].flatMap { $0 }?.identifier
            loaded = true
            loadError = nil
        } catch { loadError = APIError.from(error)?.message }
    }

    private func choose(_ voice: VoicesResponse.AvailableVoice) {
        saving = true
        errors[voice.identifier] = nil
        preview.stop()
        Task {
            defer { saving = false }
            do {
                let answer = try await client.setVoice(instance, language: language.rawValue, identifier: voice.identifier)
                onChosen(answer)
                if !embedded { dismiss() }
            } catch { errors[voice.identifier] = APIError.from(error)?.message }
        }
    }
}

/// Retain the synthesizer and clear the preview state when a sample finishes naturally.
@MainActor
@Observable
private final class VoicePreview: NSObject, AVSpeechSynthesizerDelegate {
    private let synthesizer = AVSpeechSynthesizer()
    private(set) var identifier: String?

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func toggle(_ voice: VoicesResponse.AvailableVoice, language: VoiceLanguage) {
        let stopping = identifier == voice.identifier
        stop()
        guard !stopping, let installedVoice = AVSpeechSynthesisVoice(identifier: voice.identifier) else { return }
        let utterance = AVSpeechUtterance(string: language == .en
            ? "Hello. This is the voice I will use." : "Ciao. Questa è la voce che userò.")
        utterance.voice = installedVoice
        identifier = voice.identifier
        synthesizer.speak(utterance)
    }

    func stop() {
        synthesizer.stopSpeaking(at: .immediate)
        identifier = nil
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in
            guard let self, !self.synthesizer.isSpeaking else { return }
            self.identifier = nil
        }
    }
}
