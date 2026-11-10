import SHAFTKit
import SwiftUI

// M3 contract stub (lane V2 owns this file and replaces the bodies; the names and signatures stay).

/// This device's voice preferences, kept in UserDefaults: the voice settings (end-of-turn wait, the working sound and
/// its volume) and the language last used with each instance. Per device, not per instance: they are about the room
/// and the ears, not the instance.
enum VoicePreferences {
    static func tuning() -> VoiceTuning { VoiceTuning() }
    static func save(_ tuning: VoiceTuning) {}
    static func language(for instance: String) -> VoiceLanguage { .en }
    static func setLanguage(_ language: VoiceLanguage, for instance: String) {}
}

/// Why voice mode cannot start, said on the voice screen, with what fixes it.
struct VoiceStartProblem: Error, Equatable {
    var message: String
    var fix: Fix?

    enum Fix: Equatable {
        /// A permission was refused: the system Settings page for this app.
        case openSettings
        /// The language's speech model is not on this device: download it (a system download, only on a tap).
        case downloadModel(VoiceLanguage)
    }
}

@MainActor
enum VoiceSetup {
    /// Ask for the microphone and speech permissions when not yet asked, then build a session for `instance`: the
    /// microphone, the instance's voice and this device's tuning. Debug builds take recorded files instead of the
    /// microphone when SHAFT_DEV_VOICE_FILES is set. Throws `VoiceStartProblem`. The caller calls `start()`.
    static func makeSession(client: APIClient, instance: String, language: VoiceLanguage) async throws -> VoiceSession {
        throw VoiceStartProblem(message: "Voice mode is not built yet.", fix: nil)
    }

    /// Download `language`'s speech model (the operator tapped Download).
    static func downloadModel(_ language: VoiceLanguage) async throws {}
}

/// The voice session on screen, for the Mac's scripted driver (Debug) to read. Set by the voice screen.
@MainActor
enum VoiceDebug {
    static weak var current: VoiceSession?
}
