#if os(macOS)
import Foundation
import Testing
@testable import SHAFTKit

// Final review C2: after the Model step saved the provider, the running API (and web) kept the models they read at
// their start, so the welcome failed with 403. The services `not_live` names are restarted as local `shaft` processes,
// web last, then the app waits for the web service. Scripted answers: nothing here runs a real `shaft`.

@MainActor
struct ServiceRestartTests {
    static let ok = ScriptedShaft.lines(0, #"{"ok": true}"#)

    @Test func eachNamedServiceIsRestartedWebLastThenTheAppWaitsForIt() async throws {
        let shaft = ScriptedShaft([Self.ok, Self.ok])
        var said: [String] = []
        var probes = 0
        try await ServiceRestart.restart(["web", "api"], run: { try await shaft.run($0, nil) },
                                         reachable: { probes += 1; return probes >= 3 }, sleep: { _ in },
                                         progress: { said.append($0) })
        #expect(shaft.calls == [["api", "restart", "--json"], ["web", "restart", "--json"]])
        #expect(said == ["Restarting SHAFT's api service…", "Restarting SHAFT's web service…", "Reconnecting…"])
        #expect(probes == 3)
    }

    @Test func theSchedulerDrainsAndTelegramRestartsWithoutAWaitForTheWeb() async throws {
        let shaft = ScriptedShaft([ScriptedShaft.lines(0, "scheduler restart requested"), ScriptedShaft.lines(0, "done")])
        var probes = 0
        try await ServiceRestart.restart(["scheduler", "telegram"], run: { try await shaft.run($0, nil) },
                                         reachable: { probes += 1; return true }, sleep: { _ in })
        #expect(shaft.calls == [["telegram", "restart"], ["autonomy", "restart"]])
        #expect(probes == 0)
    }

    @Test func aRefusalStopsWithTheServersWords() async {
        let said = "wisp is replying in a conversation right now: try again when that reply finishes."
        let shaft = ScriptedShaft([ScriptedShaft.lines(1, #"{"ok": false, "error": "\#(said)"}"#)])
        await #expect(throws: InstallError(said)) {
            try await ServiceRestart.restart(["api", "web"], run: { try await shaft.run($0, nil) },
                                             reachable: { true }, sleep: { _ in })
        }
        #expect(shaft.calls == [["api", "restart", "--json"]])            // web is not restarted after a refusal
    }

    @Test func aWebThatNeverAnswersAgainFailsAfterItsTries() async {
        let shaft = ScriptedShaft([Self.ok])
        let slept = Sleeps()
        await #expect(throws: InstallError.self) {
            try await ServiceRestart.restart(["web"], run: { try await shaft.run($0, nil) }, reachable: { false },
                                             sleep: { _ in slept.add() })
        }
        #expect(slept.count == ServiceRestart.reconnectTries)
    }

    @Test func aServiceTheAppCannotRestartIsSaidNotSkipped() async {
        let shaft = ScriptedShaft([])
        await #expect(throws: InstallError.self) {
            try await ServiceRestart.restart(["model"], run: { try await shaft.run($0, nil) }, reachable: { true })
        }
        #expect(shaft.calls.isEmpty)
    }
}
/// How many times a restart slept.
final class Sleeps: @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    var count: Int { lock.withLock { n } }
    func add() { lock.withLock { n += 1 } }
}
#endif
