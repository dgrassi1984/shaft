import SHAFTKit
import SwiftUI

/// Serializes voice lookups as well as playback across chat screens.
@MainActor
final class ReadAloudRequests {
    static let shared = ReadAloudRequests()
    private var lookup: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var generation = 0
    private var owner: ObjectIdentifier?
    private var requested: String?
    private var voiceOwners: Set<ObjectIdentifier> = []

    func beginVoice(_ owner: VoiceScreenModel) {
        voiceOwners.insert(ObjectIdentifier(owner))
        stop()
    }

    func endVoice(_ owner: VoiceScreenModel) {
        voiceOwners.remove(ObjectIdentifier(owner))
    }

    func toggle(id: String, source: String, screen: ChatScreenModel, client: APIClient) {
        let stopping = requested == id || ReadAloudPlayer.shared.playing == id
        stop()
        guard !stopping, !screen.showVoice, voiceOwners.isEmpty else { return }
        owner = ObjectIdentifier(screen)
        requested = id
        let mine = generation
        let fallback = VoicePreferences.language(for: screen.instance)
        let language = ReadAloudLanguage.detect(source) ?? fallback
        lookup = Task {
            let voices = try? await client.voices(screen.instance)
            finish(mine, id: id, source: source, identifier: (voices?.voices[language.rawValue] ?? nil)?.identifier,
                   fallback: fallback, screen: screen)
        }
        timeout = Task {
            do { try await Task.sleep(for: .seconds(2)) } catch { return }
            finish(mine, id: id, source: source, identifier: nil, fallback: fallback, screen: screen)
        }
    }

    private func finish(_ mine: Int, id: String, source: String, identifier: String?,
                        fallback: VoiceLanguage, screen: ChatScreenModel) {
        guard generation == mine else { return }
        generation += 1
        lookup?.cancel()
        timeout?.cancel()
        lookup = nil
        timeout = nil
        requested = nil
        guard !screen.showVoice, voiceOwners.isEmpty else { owner = nil; return }
        ReadAloudPlayer.shared.toggle(id: id, markdown: source, voiceIdentifier: identifier, fallback: fallback)
    }

    func stop(owner screen: ChatScreenModel? = nil) {
        if let screen, owner != ObjectIdentifier(screen) { return }
        generation += 1
        lookup?.cancel()
        timeout?.cancel()
        lookup = nil
        timeout = nil
        requested = nil
        owner = nil
        ReadAloudPlayer.shared.stop()
    }
}

struct ReadAloudButton: View {
    let id: String
    let source: String
    @Environment(ChatScreenModel.self) private var screen
    @Environment(InstancesStore.self) private var store
    private var playing: Bool { ReadAloudPlayer.shared.playing == id }
    private var label: LocalizedStringKey { playing ? "Stop reading" : "Read aloud" }

    var body: some View {
        Button {
            ReadAloudRequests.shared.toggle(id: id, source: source, screen: screen, client: store.client)
        } label: {
            QuietIcon(symbol: playing ? "stop.fill" : "play")
        }
        .buttonStyle(.plain)
        .accessibilityLabel(Text(label))
        .disabled(screen.showVoice)
        #if os(macOS)
        .help(Text(label))
        #endif
    }
}

extension ChatScreenModel {
    func readAloudID(_ key: String) -> String {
        "\(instance)/\(chat.ref.cid ?? "current")/\(key)"
    }

    func stopReadAloud() { ReadAloudRequests.shared.stop(owner: self) }

    func stopReadAloudForVoice() { ReadAloudRequests.shared.stop() }
}
