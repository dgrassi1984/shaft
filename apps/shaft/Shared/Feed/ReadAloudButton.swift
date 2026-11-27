import Observation
import SHAFTKit
import SwiftUI

/// Serializes voice lookups as well as playback across chat screens.
@MainActor
@Observable
final class ReadAloudRequests {
    static let shared = ReadAloudRequests()
    @ObservationIgnored private var lookup: Task<Void, Never>?
    @ObservationIgnored private var timeout: Task<Void, Never>?
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var owner: ObjectIdentifier?
    private(set) var requested: String?
    @ObservationIgnored private var voiceOwners: Set<ObjectIdentifier> = []

    func isActive(_ id: String) -> Bool {
        ReadAloudPlayer.shared.playing == id || requested == id
    }

    func beginVoice(_ owner: VoiceScreenModel) {
        voiceOwners.insert(ObjectIdentifier(owner))
        stop()
    }

    func endVoice(_ owner: VoiceScreenModel) {
        voiceOwners.remove(ObjectIdentifier(owner))
    }

    /// The voice voice mode speaks with for `instance` in `language` (nil: the system voice), as `client` last heard.
    private func identifier(_ client: APIClient, _ instance: String, _ language: VoiceLanguage) -> String? {
        (client.knownVoices(instance)?.voices[language.rawValue] ?? nil)?.identifier
    }

    /// Read the message in voice mode's voice for this instance: voice mode's language on this device and the voice
    /// chosen for it, whatever language the message is in. Like voice mode, it asks the server for the current choice;
    /// a server slower than 2 s gets the voice this client last heard for the instance (the system voice when none).
    func toggle(id: String, source: String, screen: ChatScreenModel, client: APIClient) {
        #if os(iOS)
        guard IOSAudioUse.shared.owner != .meeting else { return }
        #endif
        let stopping = isActive(id)
        stop()
        guard !stopping, !screen.showVoice, voiceOwners.isEmpty else { return }
        let instance = screen.instance
        let language = VoicePreferences.language(for: instance)
        owner = ObjectIdentifier(screen)
        requested = id
        let mine = generation
        lookup = Task {
            _ = try? await client.voices(instance)
            finish(mine, id: id, source: source, identifier: identifier(client, instance, language), language: language,
                   screen: screen)
        }
        timeout = Task {
            do { try await Task.sleep(for: .seconds(2)) } catch { return }
            finish(mine, id: id, source: source, identifier: identifier(client, instance, language), language: language,
                   screen: screen)
        }
    }

    private func finish(_ mine: Int, id: String, source: String, identifier: String?,
                        language: VoiceLanguage, screen: ChatScreenModel) {
        guard generation == mine else { return }
        generation += 1
        lookup?.cancel()
        timeout?.cancel()
        lookup = nil
        timeout = nil
        requested = nil
        guard !screen.showVoice, voiceOwners.isEmpty else { owner = nil; return }
        ReadAloudPlayer.shared.toggle(id: id, markdown: source, voiceIdentifier: identifier, language: language)
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
    private var playing: Bool { ReadAloudRequests.shared.isActive(id) }
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
