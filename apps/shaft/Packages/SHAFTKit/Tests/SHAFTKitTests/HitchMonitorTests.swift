import Foundation
import Testing
@testable import SHAFTKit

/// HitchMonitor (main-thread stalls): it starts once, and names the transition under way.
struct HitchMonitorTests {
    @Test @MainActor func aTransitionUnderWayIsNamedAndEndsWithItsLoad() {
        TransitionClock.begin("open chat")
        #expect(TransitionClock.current == "open chat")
        TransitionClock.mark("loaded")
        #expect(TransitionClock.current == nil)
    }

    @Test @MainActor func startingTwiceAddsOneObserver() {
        HitchMonitor.start()
        HitchMonitor.start()
        #expect(HitchMonitor.thresholdNanoseconds == 100_000_000)
    }
}
