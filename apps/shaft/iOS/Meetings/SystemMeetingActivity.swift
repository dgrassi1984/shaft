import ActivityKit
import Foundation

/// The Lock Screen and Dynamic Island follow the recorder, even when the meeting screen is closed.
@MainActor
final class SystemMeetingActivity: IOSMeetingActivity {
    private let activity: Activity<MeetingActivity>

    private init(_ activity: Activity<MeetingActivity>) {
        self.activity = activity
    }

    static func start(at date: Date) -> SystemMeetingActivity? {
        guard ActivityAuthorizationInfo().areActivitiesEnabled,
              let activity = try? Activity.request(attributes: MeetingActivity(started: date),
                                                   content: .init(state: .recording, staleDate: nil)) else { return nil }
        return SystemMeetingActivity(activity)
    }

    func setInterrupted(_ interrupted: Bool) {
        let timestamp = Date()
        // Activity is not Sendable; it is only ever touched from here, one update at a time.
        nonisolated(unsafe) let activity = activity
        Task {
            await activity.update(.init(state: interrupted ? .interrupted : .recording, staleDate: nil),
                                  timestamp: timestamp)
        }
    }

    func end(at date: Date, problem: String?) {
        let timestamp = Date()
        nonisolated(unsafe) let activity = activity
        Task {
            await activity.end(.init(state: .ended(at: date, problem: problem), staleDate: nil),
                               dismissalPolicy: problem == nil ? .immediate : .after(date.addingTimeInterval(900)),
                               timestamp: timestamp)
        }
    }
}
