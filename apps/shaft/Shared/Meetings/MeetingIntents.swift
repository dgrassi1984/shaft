import AppIntents

/// The app supplies these actions so shortcuts and controls use the same recorder as the meeting screen.
struct MeetingIntentActions: Sendable {
    let start: @MainActor @Sendable (Bool) async -> IntentDialog
    let stop: @MainActor @Sendable () async -> IntentDialog
}

#if os(macOS)
enum MeetingRecordingMode: String, AppEnum {
    case online
    case inPerson

    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Recording Mode"
    static let caseDisplayRepresentations: [Self: DisplayRepresentation] = [
        .online: "Online",
        .inPerson: "In Person"
    ]
}
#endif

struct RecordMeetingIntent: AppIntent {
    static let title: LocalizedStringResource = "Record Meeting"
    static let supportedModes: IntentModes = .foreground(.immediate)
    @available(iOS 27.0, macOS 27.0, *)
    static let allowedExecutionTargets: IntentExecutionTargets = .main

    @Dependency private var meetings: MeetingIntentActions

    #if os(macOS)
    @Parameter(title: "Mode", default: .online) var mode: MeetingRecordingMode

    static var parameterSummary: some ParameterSummary { Summary("Record meeting (\(\.$mode))") }
    #endif

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        #if os(iOS)
        let dialog = await meetings.start(false)
        #else
        let dialog = await meetings.start(mode == .online)
        #endif
        return .result(dialog: dialog)
    }
}

struct StopMeetingRecordingIntent: AppIntent {
    static let title: LocalizedStringResource = "Stop Meeting Recording"
    static let supportedModes: IntentModes = .background
    @available(iOS 27.0, macOS 27.0, *)
    static let allowedExecutionTargets: IntentExecutionTargets = .main

    @Dependency private var meetings: MeetingIntentActions

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        .result(dialog: await meetings.stop())
    }
}

#if os(iOS)
// The controls and Live Activity share the intents with the app. Only the app owns the recorder.
extension RecordMeetingIntent: LiveActivityIntent {}
extension StopMeetingRecordingIntent: LiveActivityIntent {}
#endif
