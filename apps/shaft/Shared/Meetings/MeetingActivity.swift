#if os(iOS)
import ActivityKit
import Foundation

struct MeetingActivity: ActivityAttributes {
    enum ContentState: Codable, Hashable {
        case recording
        case interrupted
        case ended(at: Date, problem: String?)
    }

    let started: Date
}
#endif
