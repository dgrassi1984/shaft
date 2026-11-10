import SHAFTKit
import SwiftUI

/// Voice mode for one instance (PLAN-voice §1–§3, §6): what runs it, apart from how it is drawn. It asks for a voice
/// first when the instance has none for the language, builds and starts the session (`VoiceSetup`), turns a start
/// failure into a problem with its fix, and swaps the session when the language changes.
///
/// A session ends only through `close()` (End, the screen closing, the chat changing) or a language switch: nothing
/// here follows the app's phase, so the app going to the background or the iPhone locking keeps it talking.
@MainActor
@Observable
final class VoiceScreenModel {
    enum Phase: Equatable {
        /// Looking up the instance's voice for the language.
        case checking
        /// It has none for the language and none was declined here: "Choose a voice for <instance>" first.
        case choosingVoice
        /// Permissions, the audio, the voice.
        case starting
        /// The language's speech model, on the operator's tap.
        case downloading(VoiceLanguage)
        case running
        case problem(VoiceStartProblem)
    }

    let client: APIClient
    let instance: String
    private(set) var language: VoiceLanguage
    private(set) var phase: Phase = .checking
    /// The running session: set once it started, cleared as it ends.
    private(set) var session: VoiceSession?
    /// Bumped by every start, switch and close: a start that finds it moved on ends the session it made, so a session
    /// being started is never ended halfway (its microphone would open after the end).
    private var generation = 0
    private var began = false
    private var closed = false

    init(client: APIClient, instance: String) {
        self.client = client
        self.instance = instance
        language = VoicePreferences.language(for: instance)
    }

    /// The language toggle is off while a session is being set up (two would hold the audio at once).
    var canSwitchLanguage: Bool {
        switch phase {
        case .checking, .starting, .downloading: false
        case .choosingVoice, .running, .problem: true
        }
    }

    /// Once, as the screen first shows.
    func begin() {
        guard !began else { return }
        began = true
        Task { await open() }
    }

    /// Ask for a voice when there is none for the language, else start. A failed lookup starts anyway: the session
    /// speaks with the system voice and says so.
    func open() async {
        guard !closed else { return }
        generation += 1
        let mine = generation
        phase = .checking
        if !VoiceChoiceMemory.systemVoiceAccepted(instance: instance, language: language),
           let voices = try? await client.voices(instance) {
            guard mine == generation, !closed else { return }
            if (voices.voices[language.rawValue] ?? nil) == nil {
                phase = .choosingVoice
                return
            }
        }
        guard mine == generation, !closed else { return }
        await start(mine)
    }

    /// The picker saved a voice: start with it.
    func voiceChosen(_ answer: VoicesResponse) async {
        guard phase == .choosingVoice, (answer.voices[language.rawValue] ?? nil) != nil, !closed else { return }
        generation += 1
        await start(generation)
    }

    /// "Use the system voice": remembered on this device for this instance and language, so it is not asked again.
    func useSystemVoice() async {
        guard phase == .choosingVoice, !closed else { return }
        VoiceChoiceMemory.acceptSystemVoice(instance: instance, language: language)
        generation += 1
        await start(generation)
    }

    /// The problem's Download: the speech model, then the session.
    func download(_ model: VoiceLanguage) async {
        guard !closed else { return }
        generation += 1
        let mine = generation
        phase = .downloading(model)
        do {
            try await VoiceSetup.downloadModel(model)
        } catch {
            guard mine == generation, !closed else { return }
            phase = .problem(VoiceStartProblem(message: Self.describe(error), fix: .downloadModel(model)))
            return
        }
        guard mine == generation, !closed else { return }
        await start(mine)
    }

    /// EN/IT: the session ends and one starts in the other language, which is remembered for the instance.
    func switchLanguage(to new: VoiceLanguage) async {
        guard new != language, canSwitchLanguage, !closed else { return }
        VoicePreferences.setLanguage(new, for: instance)
        language = new
        generation += 1
        await stopSession()
        await open()
    }

    func setMuted(_ on: Bool) async {
        await session?.setMuted(on)
    }

    /// End, the screen closing, the chat changing: the session ends for good. The chat stays as it is.
    func close() async {
        closed = true
        generation += 1
        await stopSession()
    }

    private func start(_ mine: Int) async {
        phase = .starting
        let made: VoiceSession
        do {
            made = try await VoiceSetup.makeSession(client: client, instance: instance, language: language)
        } catch {
            guard mine == generation, !closed else { return }
            phase = .problem(Self.problem(error))
            return
        }
        guard mine == generation, !closed else {
            await made.end()                         // made, never started: whatever it holds lets go
            return
        }
        do {
            try await made.start()
        } catch {
            await made.end()                         // what did start (the audio graph) stops again
            guard mine == generation, !closed else { return }
            phase = .problem(Self.problem(error))
            return
        }
        guard mine == generation, !closed else {
            await made.end()
            return
        }
        session = made
        VoiceDebug.current = made
        phase = .running
    }

    private func stopSession() async {
        guard let ending = session else { return }
        session = nil
        if VoiceDebug.current === ending { VoiceDebug.current = nil }
        await ending.end()
    }

    /// What a start failure says, with its fix where one is known: `VoiceSetup`'s own problems as they are, a missing
    /// speech model with Download.
    static func problem(_ error: any Error) -> VoiceStartProblem {
        if let problem = error as? VoiceStartProblem { return problem }
        if case .transcriberModelMissing(let model)? = error as? VoiceAudioError {
            return VoiceStartProblem(message: describe(error), fix: .downloadModel(model))
        }
        return VoiceStartProblem(message: describe(error), fix: nil)
    }

    private static func describe(_ error: any Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}

/// "Use the system voice", remembered per device, instance and language: the choose-a-voice step is asked once.
enum VoiceChoiceMemory {
    private static func key(_ instance: String, _ language: VoiceLanguage) -> String {
        "voice.systemVoice.\(instance).\(language.rawValue)"
    }

    static func systemVoiceAccepted(instance: String, language: VoiceLanguage) -> Bool {
        UserDefaults.standard.bool(forKey: key(instance, language))
    }

    static func acceptSystemVoice(instance: String, language: VoiceLanguage) {
        UserDefaults.standard.set(true, forKey: key(instance, language))
    }
}

/// The voice screen: on an iPhone or iPad a full-screen cover over the chat, on the Mac a panel where the composer
/// was, with the feed above it still updating. It shows the state, your words, the sentence being spoken, the status
/// and substitution lines, the last delay, and Mute, End, EN/IT and the voice settings. Closing it ends the session.
struct VoiceScreen: View {
    enum Style {
        case fullScreen
        case panel
    }

    let style: Style
    /// Closes the screen (End, Cancel on the voice choice): the presenter's own flag.
    var close: () -> Void
    @State private var model: VoiceScreenModel
    @Environment(\.scenePhase) private var scenePhase

    init(client: APIClient, instance: String, style: Style, close: @escaping () -> Void) {
        self.style = style
        self.close = close
        _model = State(initialValue: VoiceScreenModel(client: client, instance: instance))
    }

    var body: some View {
        Group {
            switch style {
            case .fullScreen: VoiceFullScreen(model: model, end: end)
            case .panel: VoicePanel(model: model, end: end)
            }
        }
        .onAppear { model.begin() }
        // The screen gone (closed, the chat changed, an earlier chat opened over it): the session ends. The app going
        // to the background or the iPhone locking removes no view, so it does not end there.
        .onDisappear {
            Task { await model.close() }
            close()
        }
        // Back from the system Settings with the permission given: try again without a tap.
        .onChange(of: scenePhase) { _, phase in
            if phase == .active, case .problem(let problem) = model.phase, problem.fix == .openSettings {
                Task { await model.open() }
            }
        }
    }

    private func end() {
        Task { await model.close() }
        close()
    }
}

/// The iPhone's and iPad's voice screen: the instance, EN/IT and the settings at the top; the state large in the
/// middle with the words under it; Mute and End at the thumb.
private struct VoiceFullScreen: View {
    let model: VoiceScreenModel
    var end: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            topBar
            Group {
                switch model.phase {
                case .choosingVoice:
                    VoiceChooseStep(model: model, cancel: nil)
                        .padding(.horizontal, Design.Space.gutter)
                case .running:
                    if let session = model.session {
                        ScrollView {
                            VStack(spacing: Design.Space.s24) {
                                VoiceStateHeading(state: session.state, muted: session.muted, instance: model.instance,
                                                  size: .large)
                                VoiceLines(session: session, instance: model.instance, size: .large)
                                VoiceDelay(latency: session.latency)
                            }
                            .padding(.horizontal, Design.Space.gutter)
                            .padding(.vertical, Design.Space.s24)
                            .frame(maxWidth: Design.measure)
                            .frame(maxWidth: .infinity)
                        }
                        .scrollBounceBehavior(.basedOnSize)
                    }
                default:
                    ScrollView {
                        VoiceSetupState(model: model, compact: false)
                            .padding(.horizontal, Design.Space.gutter)
                            .padding(.vertical, Design.Space.s24)
                            .frame(maxWidth: Design.measure)
                            .frame(maxWidth: .infinity)
                    }
                    .scrollBounceBehavior(.basedOnSize)
                }
            }
            .frame(maxHeight: .infinity)
            VoiceBottomBar(model: model, end: end)
        }
        .background(Color.canvas.ignoresSafeArea())
    }

    private var topBar: some View {
        HStack(spacing: Design.Space.s12) {
            Text(model.instance)
                .font(.headline)
                .foregroundStyle(Color.textPrimary)
                .lineLimit(1)
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: Design.Space.s8)
            VoiceLanguageToggle(model: model)
            VoiceTuningButton()
        }
        .padding(.horizontal, Design.Space.gutter)
        .padding(.vertical, Design.Space.s8)
    }
}

/// The Mac's voice panel, where the composer was: one glass block with the state and its controls in a row, the words
/// under them. The feed stays above it.
private struct VoicePanel: View {
    let model: VoiceScreenModel
    var end: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s12) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: Design.Space.s12) {
                    heading
                    Spacer(minLength: Design.Space.s8)
                    controls
                }
                VStack(alignment: .leading, spacing: Design.Space.s8) {
                    heading
                    HStack(spacing: Design.Space.s12) {
                        Spacer(minLength: 0)
                        controls
                    }
                }
            }
            switch model.phase {
            case .running:
                if let session = model.session {
                    VoiceLines(session: session, instance: model.instance, size: .compact)
                }
            case .choosingVoice:
                Text("Choose a voice for \(model.instance) to start.")
                    .font(.footnote)
                    .foregroundStyle(Color.textSecondary)
            default:
                VoiceSetupState(model: model, compact: true)
            }
        }
        .padding(Design.Space.s12)
        .background {
            Color.clear.glassSurface(in: RoundedRectangle(cornerRadius: Design.Radius.composer, style: .continuous))
        }
        .frame(maxWidth: Design.measure)
        .padding(.horizontal, Design.Space.gutter)
        .padding(.vertical, Design.Space.s8)
        .frame(maxWidth: .infinity)
        .sheet(isPresented: Binding(get: { model.phase == .choosingVoice },
                                    set: { shown in if !shown, model.phase == .choosingVoice { end() } })) {
            VoiceChooseStep(model: model, cancel: end)
                .padding(.horizontal, Design.Space.gutter)
                .padding(.top, Design.Space.s16)
                .background(Color.canvas)
                .sheetFrame(minWidth: 480, minHeight: 520)
        }
    }

    private var heading: some View {
        HStack(spacing: Design.Space.s12) {
            VoiceStateHeading(state: model.session?.state ?? .idle, muted: model.session?.muted ?? false,
                              instance: model.instance, size: .compact, title: setupTitle)
            if let session = model.session {
                VoiceDelay(latency: session.latency)
            }
        }
    }

    /// The heading's words before the session runs (the indicator rests then).
    private var setupTitle: String? {
        switch model.phase {
        case .checking, .starting, .running: nil
        case .choosingVoice: String(localized: "Choose a voice")
        case .downloading: String(localized: "Downloading")
        case .problem: String(localized: "Not started")
        }
    }

    private var controls: some View {
        HStack(spacing: Design.Space.s8) {
            VoiceLanguageToggle(model: model)
            VoiceTuningButton()
            if model.phase == .running, let session = model.session {
                VoiceMuteButton(model: model, session: session, compact: true)
            }
            VoiceEndButton(compact: true, end: end)
                .keyboardShortcut(.cancelAction)
        }
    }
}
