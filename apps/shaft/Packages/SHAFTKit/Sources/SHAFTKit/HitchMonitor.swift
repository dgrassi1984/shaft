import Foundation
import os

/// Main-thread stalls, in every build: one pass of the main run loop that works for `threshold` or longer before it
/// waits again is logged (subsystem "SHAFT", category "Hitches", with the transition under way if any) and marked as a
/// signpost interval for Instruments. A click, a key or a scroll that lands during such a pass waits for its end, so
/// these are the waits the operator feels. Read them with
/// `log show --predicate 'subsystem == "SHAFT" AND category == "Hitches"'` (`/usr/bin/log` in zsh).
///
/// A nested run loop (a menu being tracked, a modal sheet) starts its own passes: each "after waiting" restarts the
/// clock, so the outer pass that holds a menu open is not counted as one long stall.
@MainActor
public enum HitchMonitor {
    private static let logger = Logger(subsystem: "SHAFT", category: "Hitches")
    private static let signposter = OSSignposter(subsystem: "SHAFT", category: "Hitches")
    private static var observer: CFRunLoopObserver?
    nonisolated(unsafe) private static var passStarted: UInt64 = 0
    /// Stalls at least this long are logged (100 ms: a click answered later than that reads as slow).
    public static let thresholdNanoseconds: UInt64 = 100_000_000

    /// Start watching the main run loop (once; later calls do nothing).
    public static func start() {
        guard observer == nil else { return }
        let limit = thresholdNanoseconds
        let activities = CFRunLoopActivity.afterWaiting.rawValue | CFRunLoopActivity.beforeWaiting.rawValue
        let observer = CFRunLoopObserverCreateWithHandler(nil, activities, true, 0) { _, activity in
            let now = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
            if activity == .afterWaiting {
                passStarted = now
            } else if passStarted != 0 {
                let spent = now - passStarted
                passStarted = 0
                if spent >= limit {
                    MainActor.assumeIsolated { report(milliseconds: Int(spent / 1_000_000), endedAt: now) }
                }
            }
        }
        CFRunLoopAddObserver(CFRunLoopGetMain(), observer, .commonModes)
        self.observer = observer
    }

    private static func report(milliseconds: Int, endedAt: UInt64) {
        let during = TransitionClock.current.map { " during \($0)" } ?? ""
        logger.notice("main thread busy \(milliseconds) ms\(during, privacy: .public)")
        signposter.emitEvent("hitch", "\(milliseconds) ms\(during, privacy: .public)")
    }
}
