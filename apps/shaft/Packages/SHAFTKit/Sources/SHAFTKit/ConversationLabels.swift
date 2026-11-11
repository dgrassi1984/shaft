import Foundation

/// The text shared by the conversation header and the instance's Main row.
public enum ConversationLabels {
    public static func mainSubline(_ row: InstanceRow, main: FromRow?, pendingNotes: Int) -> String {
        // A topic thread replying must not replace Main's preview.
        let replying = main?.state.map { $0 == "replying" } ?? row.replying
        if replying { return String(localized: "replying…") }
        if pendingNotes > 0 { return String(localized: "☾ \(pendingNotes) for next cycle") }
        return main?.line ?? row.line
    }

    public static func header(instance: String, state: String, replying: Bool, inThread: Bool) -> String {
        let status = replying ? String(localized: "replying") : state
        return inThread ? "\(instance) · \(status)" : status
    }
}
