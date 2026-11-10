import Foundation
import Observation
import SHAFTKit
import Speech
import SwiftUI

// Starting a voice session (M3 lane V2): this device's voice preferences, the permissions, the speech
// model for the language, and the session built from the engine's parts. Debug builds take two test hooks from the
// environment instead of the microphone and the speakers (apps/shaft/README.md, "Voice against the fixture server").

/// This device's voice preferences, kept in UserDefaults: the voice settings (end-of-turn wait, the working sound and
/// its volume) and the language last used with each instance. Per device, not per instance: they are about the room
/// and the ears, not the instance.
enum VoicePreferences {
    /// The tuning as JSON under one key; the language per instance under `voice.language.<instance>`.
    static let tuningKey = "voice.tuning"
    static func languageKey(_ instance: String) -> String { "voice.language.\(instance)" }

    /// The tuning saved on this device, laid over the defaults: a setting saved by an older build that lacks a newer
    /// field keeps the rest of what was chosen, and the new field takes its default.
    static func tuning(defaults: UserDefaults = .standard) -> VoiceTuning {
        guard let stored = defaults.data(forKey: tuningKey) else { return VoiceTuning() }
        do {
            let base = try JSONSerialization.jsonObject(with: JSONEncoder().encode(VoiceTuning())) as? [String: Any] ?? [:]
            let saved = try JSONSerialization.jsonObject(with: stored) as? [String: Any] ?? [:]
            let merged = base.merging(saved) { _, chosen in chosen }
            return try JSONDecoder().decode(VoiceTuning.self, from: JSONSerialization.data(withJSONObject: merged))
        } catch {
            NSLog("SHAFT voice: the saved voice settings (%@) could not be read, the defaults apply: %@", tuningKey,
                  String(describing: error))
            return VoiceTuning()
        }
    }

    static func save(_ tuning: VoiceTuning, defaults: UserDefaults = .standard) {
        do {
            defaults.set(try JSONEncoder().encode(tuning), forKey: tuningKey)
        } catch {
            NSLog("SHAFT voice: the voice settings could not be saved: %@", String(describing: error))
        }
    }

    /// The language last used with `instance` (English until one is chosen).
    static func language(for instance: String, defaults: UserDefaults = .standard) -> VoiceLanguage {
        defaults.string(forKey: languageKey(instance)).flatMap(VoiceLanguage.init(rawValue:)) ?? .en
    }

    static func setLanguage(_ language: VoiceLanguage, for instance: String, defaults: UserDefaults = .standard) {
        defaults.set(language.rawValue, forKey: languageKey(instance))
    }
}

/// Why voice mode cannot start, said on the voice screen, with what fixes it.
struct VoiceStartProblem: Error, Equatable, LocalizedError {
    var message: String
    var fix: Fix?

    enum Fix: Equatable {
        /// A permission was refused: the system Settings page for this app.
        case openSettings
        /// The language's speech model is not on this device: download it (a system download, only on a tap).
        case downloadModel(VoiceLanguage)
    }

    var errorDescription: String? { message }
}

/// The speech model download under way, for the voice screen's progress: which language and how far (0…1). Set by
/// `VoiceSetup.downloadModel`; nil when none is running.
@MainActor @Observable
final class VoiceModelDownload {
    static let shared = VoiceModelDownload()
    private(set) var language: VoiceLanguage?
    private(set) var fraction: Double?

    fileprivate func update(_ language: VoiceLanguage?, _ fraction: Double?) {
        self.language = language
        self.fraction = fraction
    }
}

@MainActor
enum VoiceSetup {
    /// Ask for the microphone and speech permissions when not yet asked, then build a session for `instance`: the
    /// microphone, the instance's voice and this device's tuning. Debug builds take recorded files instead of the
    /// microphone when SHAFT_DEV_VOICE_FILES is set. Throws `VoiceStartProblem`. The caller calls `start()`.
    static func makeSession(client: APIClient, instance: String, cid: String? = nil,
                            language: VoiceLanguage) async throws -> VoiceSession {
        #if DEBUG
        let hooks = try VoiceDevHooks.read()
        #else
        let hooks = VoiceDevHooks.none
        #endif
        // A test hook stands in for the microphone, and on-device transcription of a file needs no permission (the
        // engine's own tests transcribe with none): nothing is asked, so no prompt stops a scripted run.
        if hooks.files == nil && !hooks.silent { try await askPermissions() }
        try await checkModel(language)

        let graph = VoiceAudioGraph(silent: hooks.silent)
        let transcriber: any TranscriptSource
        if let files = hooks.files {
            transcriber = FileTranscriber(files)
        } else if hooks.silent {
            // A silent graph never opens the microphone: the session hears silence.
            transcriber = FileTranscriber([])
        } else {
            transcriber = MicTranscriber(graph: graph)
        }
        let tuning = VoicePreferences.tuning()
        let sounds = GeneratedVoiceSounds(graph: graph, volume: tuning.workingVolume)
        graph.keepWorkingSound(sounds)
        return VoiceSession(client: client, instance: instance, cid: cid, instanceName: spokenName(instance),
                            language: language,
                            tuning: tuning, transcriber: transcriber, speech: EngineSpeechOutput(graph: graph),
                            sounds: sounds, graph: graph)
    }

    /// Download `language`'s speech model (the operator tapped Download). A system download, shared with other apps
    /// and kept by the system; `VoiceModelDownload.shared` follows its progress. Throws `VoiceStartProblem`.
    static func downloadModel(_ language: VoiceLanguage) async throws {
        let download = VoiceModelDownload.shared
        download.update(language, 0)
        defer { download.update(nil, nil) }
        do {
            guard let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber(language)])
            else { return }                                              // already installed
            let watch = Task { @MainActor in
                while !Task.isCancelled {
                    download.update(language, request.progress.fractionCompleted)
                    try? await Task.sleep(for: .milliseconds(250))
                }
            }
            defer { watch.cancel() }
            try await request.downloadAndInstall()
        } catch {
            throw VoiceStartProblem(message: "The speech model for \(languageName(language)) did not download: "
                                        + "\(error.localizedDescription)",
                                    fix: .downloadModel(language))
        }
        guard await installed(language) else {
            throw VoiceStartProblem(message: "The speech model for \(languageName(language)) downloaded, but "
                                        + "\(deviceName) does not list it as installed yet. Try again in a moment.",
                                    fix: .downloadModel(language))
        }
    }

    // MARK: - Permissions

    /// The microphone and speech recognition: asked when not yet asked, a refusal said with Open Settings.
    private static func askPermissions() async throws {
        switch VoicePermissions.microphone() {
        case .granted:
            break
        case .notDetermined:
            guard await VoicePermissions.requestMicrophone() else { throw microphoneRefused }
        case .denied, .restricted:
            throw microphoneRefused
        }
        var speech = VoicePermissions.speechRecognition()
        if speech == .notDetermined { speech = await VoicePermissions.requestSpeechRecognition() }
        switch speech {
        case .granted:
            break
        case .restricted:
            throw VoiceStartProblem(message: "Speech recognition is restricted on \(deviceName) (Screen Time or a "
                                        + "device profile), so voice cannot hear you.", fix: nil)
        case .denied, .notDetermined:
            throw VoiceStartProblem(message: "SHAFT may not use speech recognition, so voice cannot hear you. Allow "
                                        + "it in \(settingsPath("Speech Recognition")).", fix: .openSettings)
        }
    }

    private static var microphoneRefused: VoiceStartProblem {
        VoiceStartProblem(message: "SHAFT may not use the microphone, so voice cannot hear you. Allow it in "
                              + "\(settingsPath("Microphone")).", fix: .openSettings)
    }

    // MARK: - The speech model

    /// The transcriber module as the engine makes it (TranscriberPipeline), so the assets checked and downloaded are
    /// the ones it uses.
    private static func transcriber(_ language: VoiceLanguage) -> SpeechTranscriber {
        SpeechTranscriber(locale: Locale(identifier: language.localeIdentifier), transcriptionOptions: [],
                          reportingOptions: [.volatileResults, .fastResults], attributeOptions: [])
    }

    private static func installed(_ language: VoiceLanguage) async -> Bool {
        let wanted = Locale(identifier: language.localeIdentifier).identifier(.bcp47).lowercased()
        return await SpeechTranscriber.installedLocales.contains { $0.identifier(.bcp47).lowercased() == wanted }
    }

    /// The model for `language` is here, or the problem says why not and whether Download fixes it. Nothing is
    /// downloaded here.
    private static func checkModel(_ language: VoiceLanguage) async throws {
        let name = languageName(language)
        guard SpeechTranscriber.isAvailable else {
            throw VoiceStartProblem(message: "On-device speech recognition is not available on \(deviceName).",
                                    fix: nil)
        }
        if await installed(language) { return }
        switch await AssetInventory.status(forModules: [transcriber(language)]) {
        case .installed:
            return
        case .supported:
            throw VoiceStartProblem(message: "The speech model for \(name) is not on \(deviceName) yet. Download it "
                                        + "to talk in \(name): it stays on the device, and what you say never leaves "
                                        + "it as audio.", fix: .downloadModel(language))
        case .downloading:
            throw VoiceStartProblem(message: "The speech model for \(name) is downloading.",
                                    fix: .downloadModel(language))
        case .unsupported:
            throw VoiceStartProblem(message: "Speech recognition on \(deviceName) does not offer \(name).", fix: nil)
        @unknown default:
            throw VoiceStartProblem(message: "The speech model for \(name) is not available on \(deviceName).",
                                    fix: nil)
        }
    }

    // MARK: - Words

    /// The instance's name as status lines speak it ("Tundra is using web search").
    static func spokenName(_ instance: String) -> String {
        instance.prefix(1).uppercased() + instance.dropFirst()
    }

    private static func languageName(_ language: VoiceLanguage) -> String {
        language == .en ? "English" : "Italian"
    }

    private static var deviceName: String {
        #if os(iOS)
        "this iPhone"
        #else
        "this Mac"
        #endif
    }

    private static func settingsPath(_ pane: String) -> String {
        #if os(iOS)
        "Settings → Privacy & Security → \(pane)"
        #else
        "System Settings → Privacy & Security → \(pane)"
        #endif
    }
}

/// What stands in for the microphone and the speakers in a test run. Release builds never read the environment.
struct VoiceDevHooks {
    /// Recorded files played into the transcriber instead of the microphone, each at its offset in seconds.
    var files: [(URL, at: TimeInterval)]?
    /// The silent graph: nothing is heard, the microphone is never opened.
    var silent = false

    static let none = VoiceDevHooks()

    #if DEBUG
    /// Debug builds only:
    /// - `SHAFT_DEV_VOICE_FILES=<path>@<seconds>[,<path>@<seconds>…]`: `FileTranscriber` instead of the microphone, no
    ///   permission asked. A relative path is taken from `SHAFT_DEV_DIR` when set (the Mac's dev run), else
    ///   from the app's Documents folder (the simulator: `xcrun simctl get_app_container booted <bundle id> data`).
    /// - `SHAFT_DEV_VOICE_SILENT=1`: the silent graph. Alone, the session hears silence (no microphone).
    /// In the iPhone Simulator they come as `SIMCTL_CHILD_SHAFT_DEV_VOICE_…` to `xcrun simctl launch`. A value that
    /// cannot be used stops the session with what is wrong with it, never falls back to the microphone.
    static func read(_ environment: [String: String] = ProcessInfo.processInfo.environment) throws -> VoiceDevHooks {
        var hooks = VoiceDevHooks()
        if let silent = environment["SHAFT_DEV_VOICE_SILENT"], !silent.isEmpty {
            guard silent == "1" || silent == "0" else {
                throw VoiceStartProblem(message: "SHAFT_DEV_VOICE_SILENT=\(silent): use 1 (or 0).", fix: nil)
            }
            hooks.silent = silent == "1"
        }
        if let value = environment["SHAFT_DEV_VOICE_FILES"], !value.isEmpty {
            hooks.files = try value.split(separator: ",").map { try clip(String($0), environment: environment) }
        }
        if hooks.files != nil || hooks.silent {
            let clips = hooks.files.map { $0.map { "\($0.0.path)@\($0.at)" }.joined(separator: ", ") } ?? "none"
            NSLog("SHAFT voice: test hooks: files %@; silent %@", clips, hooks.silent ? "yes" : "no")
        }
        return hooks
    }

    private static func clip(_ item: String, environment: [String: String]) throws -> (URL, at: TimeInterval) {
        let trimmed = item.trimmingCharacters(in: .whitespaces)
        guard let at = trimmed.lastIndex(of: "@"), let seconds = TimeInterval(trimmed[trimmed.index(after: at)...]),
              seconds >= 0, at > trimmed.startIndex else {
            throw VoiceStartProblem(message: "SHAFT_DEV_VOICE_FILES: \"\(trimmed)\" is not <path>@<seconds>.", fix: nil)
        }
        let path = String(trimmed[..<at])
        let url: URL
        if path.hasPrefix("/") {
            url = URL(fileURLWithPath: path)
        } else if let dir = environment["SHAFT_DEV_DIR"], !dir.isEmpty {
            url = URL(fileURLWithPath: dir, isDirectory: true).appending(path: path)
        } else {
            url = URL.documentsDirectory.appending(path: path)
        }
        guard FileManager.default.isReadableFile(atPath: url.path) else {
            throw VoiceStartProblem(message: "SHAFT_DEV_VOICE_FILES: no readable file at \(url.path).", fix: nil)
        }
        return (url, at: seconds)
    }
    #endif
}

/// The voice session on screen, for the Mac's scripted driver (Debug) to read. Set by the voice screen.
@MainActor
enum VoiceDebug {
    static weak var current: VoiceSession?
}
