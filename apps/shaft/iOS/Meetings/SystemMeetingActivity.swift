import ActivityKit
import Foundation

/// The Lock Screen and Dynamic Island follow the recorder, even when the meeting screen is closed.
@MainActor
final class SystemMeetingActivity: IOSMeetingActivity {
    private let activity: Activity<MeetingActivity>
    /// Updates run one after another, and Stop waits for the last (Codex review 2026-11-27: an unawaited end could
    /// leave "Recording" on the Lock Screen when the app was suspended right after Stop).
    private var last: Task<Void, Never>?

    private init(_ activity: Activity<MeetingActivity>) {
        self.activity = activity
    }

    static func start(at date: Date) -> SystemMeetingActivity? {
        endOrphans()
        guard ActivityAuthorizationInfo().areActivitiesEnabled,
              let activity = try? Activity.request(attributes: MeetingActivity(started: date),
                                                   content: .init(state: .recording, staleDate: nil)) else { return nil }
        return SystemMeetingActivity(activity)
    }

    /// End every meeting activity this process does not own: one left by a crash would otherwise show a Stop that
    /// stops the next meeting (Codex review 2026-11-27). Called at launch and before a new one starts.
    static func endOrphans() {
        for orphan in Activity<MeetingActivity>.activities {
            nonisolated(unsafe) let orphan = orphan
            Task { await orphan.end(nil, dismissalPolicy: .immediate) }
        }
    }

    func setInterrupted(_ interrupted: Bool) {
        let timestamp = Date()
        // Activity is not Sendable; it is only ever touched from here, one update at a time.
        nonisolated(unsafe) let activity = activity
        let previous = last
        last = Task {
            await previous?.value
            await activity.update(.init(state: interrupted ? .interrupted : .recording, staleDate: nil),
                                  timestamp: timestamp)
        }
    }

    func end(at date: Date, problem: String?) {
        let timestamp = Date()
        nonisolated(unsafe) let activity = activity
        let previous = last
        last = Task {
            await previous?.value
            await activity.end(.init(state: .ended(at: date, problem: problem), staleDate: nil),
                               dismissalPolicy: problem == nil ? .immediate : .after(date.addingTimeInterval(900)),
                               timestamp: timestamp)
        }
    }

    func settle() async {
        await last?.value
    }
}
