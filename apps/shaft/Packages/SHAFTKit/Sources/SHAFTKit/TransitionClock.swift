import Foundation
import os

/// How long a transition the operator starts takes to show: a tap (New thread, a row) marks `begin`, the screen it
/// leads to marks `shown` when it draws and `loaded` when its content is there. Each mark is logged once per begin
/// (subsystem "SHAFT", category "Transitions"), so `log stream --predicate 'category == "Transitions"'` reads it on a
/// device, a simulator or the Mac, and a signpost interval shows it in Instruments.
@MainActor
public enum TransitionClock {
    private static let logger = Logger(subsystem: "SHAFT", category: "Transitions")
    private static let signposter = OSSignposter(subsystem: "SHAFT", category: "Transitions")
    private static var started: (name: String, at: ContinuousClock.Instant, marks: Set<String>,
                                 state: OSSignpostIntervalState)?

    /// The transition under way, if any (HitchMonitor names it beside a stall).
    public static var current: String? { started?.name }

    /// A transition starts now (it replaces one still going: the last tap is the one being waited on).
    public static func begin(_ name: String) {
        if let started { signposter.endInterval("transition", started.state) }
        started = (name, .now, [], signposter.beginInterval("transition", id: signposter.makeSignpostID()))
        logger.notice("begin \(name, privacy: .public)")
    }

    /// End a transition that failed before its content loaded.
    public static func cancel() {
        guard let current = started else { return }
        signposter.endInterval("transition", current.state)
        logger.notice("\(current.name, privacy: .public) cancelled")
        started = nil
    }

    /// A transition starts now unless one is still going (a New thread tap that ends in a selection is still that one);
    /// one older than ten seconds no longer counts.
    public static func beginIfIdle(_ name: String) {
        if let started, ContinuousClock.now - started.at < .seconds(10) { return }
        begin(name)
    }

    /// The transition reached `mark` ("shown", "loaded"); logged only the first time per transition.
    public static func mark(_ mark: String) {
        guard var current = started, !current.marks.contains(mark) else { return }
        current.marks.insert(mark)
        started = current
        let ms = (ContinuousClock.now - current.at).milliseconds
        logger.notice("\(current.name, privacy: .public) \(mark, privacy: .public) after \(ms) ms")
        if mark == "loaded" {
            signposter.endInterval("transition", current.state)
            started = nil
        }
    }
}

extension Duration {
    fileprivate var milliseconds: Int {
        let (seconds, attoseconds) = components
        return Int(seconds * 1000 + attoseconds / 1_000_000_000_000_000)
    }
}
