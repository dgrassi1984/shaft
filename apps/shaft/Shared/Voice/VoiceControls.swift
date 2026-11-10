import SHAFTKit
import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

// The voice screen's parts (VoiceScreen.swift lays them out): the state, the words, the delay, the controls, the voice
// settings, the setup states and the choose-a-voice step. Each is drawn large on the iPhone's full screen and compact
// in the Mac's panel.

/// Words the voice screen says in more than one place.
enum VoiceLabels {
    static func title(_ state: VoiceState) -> String {
        switch state {
        case .idle: String(localized: "Starting…")
        case .listening: String(localized: "Listening")
        case .userTalking: String(localized: "You're talking")
        case .thinking: String(localized: "Thinking")
        case .speaking: String(localized: "Speaking")
        case .ended: String(localized: "Ended")
        }
    }

    static func language(_ language: VoiceLanguage) -> String {
        switch language {
        case .en: String(localized: "English")
        case .it: String(localized: "Italian")
        }
    }

    static func short(_ language: VoiceLanguage) -> String {
        language.rawValue.uppercased()
    }

    /// "1.8"
    static func seconds(_ value: TimeInterval) -> String {
        value.formatted(.number.precision(.fractionLength(1)))
    }
}

enum VoiceSize {
    case large
    case compact
}

// MARK: The state

/// The state as a large, clear sign: a ring with its symbol that breathes slowly while listening, a little faster
/// while you talk or it speaks; still under Reduce Motion or while muted.
struct VoiceStateIndicator: View {
    let state: VoiceState
    let muted: Bool
    let diameter: CGFloat
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            Circle()
                .fill(tint.opacity(0.14))
                .phaseAnimator(breath) { circle, scale in
                    circle.scaleEffect(scale)
                } animation: { _ in
                    .easeInOut(duration: period)
                }
                .id("\(state.rawValue)-\(animates)")       // a new rhythm starts from rest
            Circle()
                .strokeBorder(tint.opacity(0.4), lineWidth: 2)
            Image(systemName: symbol)
                .font(.system(size: diameter * 0.36, weight: .medium))
                .foregroundStyle(tint)
                .symbolEffect(.variableColor.iterative, isActive: animates && state == .speaking)
                .symbolEffect(.pulse, isActive: animates && state == .thinking)
                .contentTransition(.symbolEffect(.replace))
        }
        .frame(width: diameter, height: diameter)
        .accessibilityHidden(true)
    }

    private var animates: Bool { !reduceMotion && !muted }

    private var tint: Color {
        if muted { return .textTertiary }
        switch state {
        case .idle, .ended: return .textSecondary
        default: return .brand
        }
    }

    private var symbol: String {
        if muted { return "mic.slash.fill" }
        switch state {
        case .idle: return "waveform"
        case .listening: return "ear"
        case .userTalking: return "waveform"
        case .thinking: return "ellipsis"
        case .speaking: return "speaker.wave.2.fill"
        case .ended: return "checkmark"
        }
    }

    /// The ring's scale, in and out: calm, never a flash.
    private var breath: [CGFloat] {
        guard animates else { return [1] }
        switch state {
        case .listening: return [1, 1.05]
        case .userTalking: return [1, 1.1]
        case .thinking: return [1, 1.04]
        case .speaking: return [1, 1.08]
        case .idle, .ended: return [1]
        }
    }

    private var period: Double {
        switch state {
        case .listening: 2.4
        case .userTalking: 1.0
        case .thinking: 1.6
        default: 1.2
        }
    }
}

/// The indicator with the state's name under it (large) or beside it (compact), and Muted while the microphone is off.
struct VoiceStateHeading: View {
    let state: VoiceState
    let muted: Bool
    let instance: String
    let size: VoiceSize
    /// Said instead of the state's name (the Mac's panel before the session runs).
    var title: String?
    @ScaledMetric(relativeTo: .largeTitle) private var large: CGFloat = 148
    @ScaledMetric(relativeTo: .headline) private var compact: CGFloat = 32

    var body: some View {
        Group {
            switch size {
            case .large:
                VStack(spacing: Design.Space.s16) {
                    VoiceStateIndicator(state: state, muted: muted, diameter: min(large, 200))
                    VStack(spacing: Design.Space.s4) {
                        Text(name)
                            .font(.title2.weight(.semibold))
                            .foregroundStyle(Color.textPrimary)
                        if muted {
                            Text("Microphone off: \(instance) keeps speaking")
                                .font(.footnote)
                                .foregroundStyle(Color.textSecondary)
                                .multilineTextAlignment(.center)
                        }
                    }
                }
            case .compact:
                HStack(spacing: Design.Space.s8) {
                    VoiceStateIndicator(state: state, muted: muted, diameter: min(compact, 44))
                    Text(name)
                        .font(.headline)
                        .foregroundStyle(Color.textPrimary)
                        .lineLimit(1)
                    if muted {
                        Text("Muted").font(.footnote).foregroundStyle(Color.textSecondary)
                    }
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(muted ? String(localized: "\(name), microphone off") : name)
        .accessibilityAddTraits(.updatesFrequently)
    }

    private var name: String { title ?? VoiceLabels.title(state) }
}

// MARK: The words

/// Your words as you say them; the sentence being spoken, the instance's own words beside its accent bar and a status
/// line (a filler it did not write) smaller, in italics and marked as status; the voice standing in for the chosen one,
/// for as long as it does; and the session's latest status.
struct VoiceLines: View {
    let session: VoiceSession
    let instance: String
    let size: VoiceSize

    private var words: Font { size == .large ? .title3 : .body }

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s12) {
            if !session.transcript.isEmpty {
                VStack(alignment: .leading, spacing: Design.Space.s4) {
                    Text("You").font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                    Text(session.transcript)
                        .font(words)
                        .foregroundStyle(Color.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
            }
            if let spoken = session.speaking {
                if spoken.isStatus {
                    Label {
                        Text(spoken.text).italic().fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "info.circle")
                    }
                    .font(.callout)
                    .foregroundStyle(Color.textSecondary)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel("Status: \(spoken.text)")
                } else {
                    VStack(alignment: .leading, spacing: Design.Space.s4) {
                        Text(instance).font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                        Text(spoken.text)
                            .font(words)
                            .foregroundStyle(Color.textPrimary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.leading, Design.Space.s12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(alignment: .leading) { AccentBar().clipShape(.capsule) }
                    .accessibilityElement(children: .combine)
                }
            }
            if let substitution = session.voiceSubstitution {
                line(substitution, symbol: "exclamationmark.triangle", colour: .warning)
            }
            if !session.statusLine.isEmpty, session.statusLine != session.voiceSubstitution {
                line(session.statusLine, symbol: "text.bubble", colour: .textSecondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func line(_ text: String, symbol: String, colour: Color) -> some View {
        Label {
            Text(text).fixedSize(horizontal: false, vertical: true)
        } icon: {
            Image(systemName: symbol)
        }
        .font(.footnote)
        .foregroundStyle(colour)
    }
}

/// The last turn's delay: from the end of your words to the first spoken word of the reply.
struct VoiceDelay: View {
    let latency: TimeInterval?

    var body: some View {
        if let latency {
            Text("Last delay \(VoiceLabels.seconds(latency)) s")
                .font(Design.Text.metadata)
                .foregroundStyle(Color.textSecondary)
                .accessibilityLabel("The last reply began \(VoiceLabels.seconds(latency)) seconds after you stopped")
        }
    }
}

// MARK: Setting up

/// What shows until the session runs: starting, downloading the speech model, or why it cannot start with its fix.
struct VoiceSetupState: View {
    let model: VoiceScreenModel
    let compact: Bool

    var body: some View {
        switch model.phase {
        case .checking, .starting:
            if !compact {
                VoiceStateHeading(state: .idle, muted: false, instance: model.instance, size: .large)
            }
        case .downloading(let language):
            HStack(spacing: Design.Space.s8) {
                // The system's own progress when it reports one (VoiceModelDownload), else a spinner.
                if let fraction = VoiceModelDownload.shared.fraction, VoiceModelDownload.shared.language == language {
                    ProgressView(value: fraction).frame(maxWidth: compact ? 80 : 160)
                } else {
                    ProgressView().controlSize(compact ? .small : .regular)
                }
                Text("Downloading the \(VoiceLabels.language(language)) speech model…")
                    .font(compact ? .footnote : .body)
                    .foregroundStyle(Color.textSecondary)
            }
            .accessibilityElement(children: .combine)
        case .problem(let problem):
            VoiceProblemView(model: model, problem: problem, compact: compact)
        case .choosingVoice, .running:
            EmptyView()
        }
    }
}

/// Why voice mode cannot start, and its fix as a button: Open Settings for a refused permission, Download for a
/// missing speech model. Try again for anything else, or after the fix.
struct VoiceProblemView: View {
    let model: VoiceScreenModel
    let problem: VoiceStartProblem
    let compact: Bool
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(alignment: compact ? .leading : .center, spacing: Design.Space.s12) {
            Label {
                Text(problem.message).fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "exclamationmark.triangle.fill")
            }
            .font(compact ? .footnote : .body)
            .foregroundStyle(Color.danger)
            HStack(spacing: Design.Space.s12) {
                switch problem.fix {
                case .openSettings:
                    fix("Open Settings") {
                        if let url = VoiceSystemSettings.url { openURL(url) }
                    }
                    .accessibilityHint("Opens this app's permissions in Settings")
                case .downloadModel(let language):
                    fix("Download") { Task { await model.download(language) } }
                        .accessibilityHint("Downloads the \(VoiceLabels.language(language)) speech model to this device")
                case nil:
                    EmptyView()
                }
                Button("Try again") { Task { await model.open() } }
                    .frame(minHeight: 44)
            }
        }
        .frame(maxWidth: .infinity, alignment: compact ? .leading : .center)
    }

    @ViewBuilder
    private func fix(_ title: LocalizedStringKey, action: @escaping () -> Void) -> some View {
        if compact {
            Button(title, action: action).compactPrimary()
        } else {
            Button(title, action: action).buttonStyle(ProminentAction())
        }
    }
}

/// The system Settings page where the microphone and speech permissions are given back.
enum VoiceSystemSettings {
    @MainActor static var url: URL? {
        #if os(iOS)
        URL(string: UIApplication.openSettingsURLString)
        #else
        URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
        #endif
    }
}

/// The first time voice mode opens for an instance with no voice for the language: one is chosen first (the picker
/// saves it on the server, for the iPhone and the Mac alike), or the system voice is taken and not asked about again.
struct VoiceChooseStep: View {
    let model: VoiceScreenModel
    /// Cancel closes voice mode (the Mac's sheet); the iPhone's screen has End for that.
    var cancel: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s12) {
            Text("Choose a voice for \(model.instance)")
                .font(.title3.weight(.semibold))
                .foregroundStyle(Color.textPrimary)
                .accessibilityAddTraits(.isHeader)
            Text("\(model.instance) has no \(VoiceLabels.language(model.language)) voice yet. The one you choose speaks on your iPhone and your Mac.")
                .font(.footnote)
                .foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            VoicePicker(client: model.client, instance: model.instance, language: model.language,
                        onChosen: { answer in Task { await model.voiceChosen(answer) } })
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            HStack(spacing: Design.Space.s12) {
                if let cancel {
                    Button("Cancel", action: cancel)
                        .keyboardShortcut(.cancelAction)
                        .frame(minHeight: 44)
                }
                Spacer(minLength: 0)
                Button("Use the system voice") { Task { await model.useSystemVoice() } }
                    .frame(minHeight: 44)
                    .accessibilityHint("Speaks with this device's own \(VoiceLabels.language(model.language)) voice")
            }
            .padding(.bottom, Design.Space.s8)
        }
    }
}

// MARK: Controls

/// A voice control: a capsule on `elevated`, as wide as the bar on the iPhone, its own size in the Mac's panel.
struct VoiceControlStyle: ButtonStyle {
    enum Tone {
        case plain
        case on
        case danger
    }

    var tone: Tone = .plain
    let compact: Bool
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(compact ? .callout.weight(.medium) : .body.weight(.semibold))
            .foregroundStyle(!enabled ? Color.textTertiary : tone == .danger ? Color.danger : Color.textPrimary)
            .padding(.horizontal, compact ? Design.Space.s12 : Design.Space.s20)
            .frame(maxWidth: compact ? nil : .infinity, minHeight: compact ? Design.Header.height : 52)
            .background(tone == .on ? Color.hairline : Color.elevated, in: Capsule())
            .overlay { Capsule().strokeBorder(Color.hairline, lineWidth: 1) }
            .contentShape(Capsule())
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

/// Mute: the microphone off, the instance still speaking. Pressed again, listening again.
struct VoiceMuteButton: View {
    let model: VoiceScreenModel
    let session: VoiceSession
    let compact: Bool

    var body: some View {
        Button { Task { await model.setMuted(!session.muted) } } label: {
            Label(session.muted ? "Unmute" : "Mute", systemImage: session.muted ? "mic.slash.fill" : "mic.fill")
        }
        .buttonStyle(VoiceControlStyle(tone: session.muted ? .on : .plain, compact: compact))
        .accessibilityHint(session.muted ? "Turns the microphone on" : "Turns the microphone off; it keeps speaking")
    }
}

/// End: voice mode closes; the chat stays open as it is.
struct VoiceEndButton: View {
    let compact: Bool
    var end: () -> Void

    var body: some View {
        Button(action: end) {
            Label("End", systemImage: "xmark.circle.fill")
        }
        .buttonStyle(VoiceControlStyle(tone: .danger, compact: compact))
        .accessibilityHint("Ends voice mode; the chat stays open")
    }
}

/// The iPhone's foot: Mute and End side by side, one over the other at the largest text sizes.
struct VoiceBottomBar: View {
    let model: VoiceScreenModel
    var end: () -> Void

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: Design.Space.s12) { buttons }
            VStack(spacing: Design.Space.s8) { buttons }
        }
        .padding(.horizontal, Design.Space.gutter)
        .padding(.vertical, Design.Space.s12)
        .frame(maxWidth: Design.measure)
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private var buttons: some View {
        if model.phase == .running, let session = model.session {
            VoiceMuteButton(model: model, session: session, compact: false)
        }
        VoiceEndButton(compact: false, end: end)
    }
}

/// EN / IT: switching ends the session and starts one in the other language, remembered for the instance.
struct VoiceLanguageToggle: View {
    let model: VoiceScreenModel

    var body: some View {
        HStack(spacing: 0) {
            ForEach(VoiceLanguage.allCases, id: \.self) { language in
                let selected = language == model.language
                Button {
                    Task { await model.switchLanguage(to: language) }
                } label: {
                    Text(VoiceLabels.short(language))
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(selected ? Color.onAccent : Color.textPrimary)
                        .frame(minWidth: 44, minHeight: Design.Header.height)
                        .background(selected ? Color.brand : Color.clear, in: Capsule())
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(VoiceLabels.language(language))
                .accessibilityAddTraits(selected ? .isSelected : [])
            }
        }
        .padding(2)
        .background(Color.elevated, in: Capsule())
        .disabled(!model.canSwitchLanguage)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Language")
        .accessibilityHint("Switching starts again in the other language")
    }
}

/// The voice settings, behind a small button: they apply from the next session.
struct VoiceTuningButton: View {
    @State private var showing = false

    var body: some View {
        Button { showing = true } label: {
            Image(systemName: "slider.horizontal.3")
                .font(.body.weight(.medium))
                .foregroundStyle(Color.textPrimary)
                .frame(minWidth: 44, minHeight: Design.Header.height)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Voice settings")
        .popover(isPresented: $showing) {
            VoiceTuningForm()
                .presentationCompactAdaptation(.popover)
        }
    }
}

/// This device's voice settings (`VoicePreferences`): how long a pause ends your turn, and the working sound with its
/// volume. Saved as they change; a session already running keeps the ones it started with.
struct VoiceTuningForm: View {
    @State private var tuning = VoicePreferences.tuning()

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            Text("Voice settings")
                .font(.headline)
                .foregroundStyle(Color.textPrimary)
                .accessibilityAddTraits(.isHeader)
            VStack(alignment: .leading, spacing: Design.Space.s4) {
                HStack {
                    Text("End-of-turn wait").foregroundStyle(Color.textPrimary)
                    Spacer()
                    Text("\(VoiceLabels.seconds(tuning.endpointS)) s")
                        .monospacedDigit()
                        .foregroundStyle(Color.textSecondary)
                }
                Slider(value: Binding(get: { tuning.endpointS },
                                      set: { tuning.endpointS = ($0 * 10).rounded() / 10 }),
                       in: 0.5...2.0, step: 0.1)
                    .labelsHidden()
                    .accessibilityLabel("End-of-turn wait")
                    .accessibilityValue("\(VoiceLabels.seconds(tuning.endpointS)) seconds")
                Text("How long you pause before your words are sent.")
                    .font(.footnote)
                    .foregroundStyle(Color.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Toggle("Working sound", isOn: $tuning.workingSound)
                .foregroundStyle(Color.textPrimary)
                .frame(minHeight: 44)
            VStack(alignment: .leading, spacing: Design.Space.s4) {
                HStack {
                    Text("Volume").foregroundStyle(Color.textPrimary)
                    Spacer()
                    Text("\(Int((tuning.workingVolume * 100).rounded())) %")
                        .monospacedDigit()
                        .foregroundStyle(Color.textSecondary)
                }
                Slider(value: Binding(get: { Double(tuning.workingVolume) },
                                      set: { tuning.workingVolume = Float(($0 * 20).rounded() / 20) }),
                       in: 0...1)
                    .labelsHidden()
                    .accessibilityLabel("Working sound volume")
                    .accessibilityValue("\(Int((tuning.workingVolume * 100).rounded())) percent")
            }
            .disabled(!tuning.workingSound)
            Text("Applies from the next session.")
                .font(.footnote)
                .foregroundStyle(Color.textSecondary)
        }
        .padding(Design.Space.s16)
        .frame(width: 300)
        .onChange(of: tuning) { _, new in VoicePreferences.save(new) }
    }
}
