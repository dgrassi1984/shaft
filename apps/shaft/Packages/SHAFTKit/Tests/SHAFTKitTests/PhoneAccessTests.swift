#if os(macOS)
import Foundation
import Testing
@testable import SHAFTKit

// "Use SHAFT from iPhone/iPad" (Settings → Services): the row's state machine against scripted `shaft` answers. No
// Tailscale, no real `shaft`, nothing on the network.

@MainActor
struct PhoneAccessTests {
    static let link = "https://mac.example.ts.net:8444/pair?t=abc"
    static let okLine = ScriptedShaft.lines(0, #"{"ok": true}"#)
    static let linkLine = ScriptedShaft.lines(0, #"{"ok": true, "link": "\#(link)", "expires_minutes": 10}"#)

    func access(_ shaft: ScriptedShaft, installed: Bool = true, reachable: @escaping @MainActor () async -> Bool = { true },
                sleep: @escaping @Sendable (Duration) async throws -> Void = { _ in }) -> PhoneAccess {
        PhoneAccess(run: { try await shaft.run($0, nil) }, reachable: reachable, hasTailscale: { installed }, sleep: sleep)
    }

    @Test func issue39FindsTailscaleInHomeApplications() {
        let systemApp = URL(fileURLWithPath: "/Applications/Tailscale.app")
        let homeApp = URL(fileURLWithPath: "/Users/example/Applications/Tailscale.app")
        let found = PhoneAccess.resolveTailscaleApp(systemApp: systemApp, homeApp: homeApp) { $0 == homeApp }
        #expect(found == homeApp)
    }

    @Test func withoutTheTailscaleAppItSaysSoAndRunsNothing() async {
        let shaft = ScriptedShaft([])
        let phone = access(shaft, installed: false)
        phone.start()
        await phone.finished()
        #expect(phone.state == .missingApp)
        #expect(shaft.calls.isEmpty)
    }

    @Test func checkingAgainAfterInstallingGoesOn() async {
        let shaft = ScriptedShaft([Self.okLine, Self.okLine, Self.linkLine])
        var installed = false
        let phone = PhoneAccess(run: { try await shaft.run($0, nil) }, reachable: { true }, hasTailscale: { installed })
        phone.start()
        #expect(phone.state == .missingApp)
        installed = true
        phone.start()
        await phone.finished()
        #expect(phone.state == .link(Self.link, expiresMinutes: 10))
    }

    @Test func setupRunsApiThenWebThenMakesTheLink() async {
        let shaft = ScriptedShaft([Self.okLine, Self.okLine, Self.linkLine])
        let phone = access(shaft)
        phone.start()
        #expect(phone.state == .working("Connecting this Mac to your tailnet…"))
        await phone.finished()
        #expect(shaft.calls == [["api", "on", "--json"], ["web", "on", "--json"],
                                ["web", "pair", "--name", "iphone", "--json"]])
        #expect(phone.state == .link(Self.link, expiresMinutes: 10))
        // The Mac's own pairing is never asked for or touched: no --local anywhere.
        #expect(!shaft.calls.joined().contains("--local"))
    }

    @Test func aRefusalShowsTheServersWordsAndTryAgainResumes() async {
        let said = "Tailscale is not signed in: open Tailscale, log in, then try again"
        let shaft = ScriptedShaft([
            ScriptedShaft.lines(1, #"{"ok": false, "error": "\#(said)"}"#),
            Self.okLine, Self.okLine, Self.linkLine])
        let phone = access(shaft)
        phone.start()
        await phone.finished()
        #expect(phone.state == .failed(said))
        #expect(shaft.calls.count == 1)               // web on and the link were not tried
        phone.start()                                  // Try again
        await phone.finished()
        #expect(phone.state == .link(Self.link, expiresMinutes: 10))
        #expect(shaft.calls.count == 4)
    }

    @Test func aFailureWithoutJSONShowsWhatTheCommandSaid() async {
        let shaft = ScriptedShaft([{ _ in failed(1, "Traceback: boom") }])
        let phone = access(shaft)
        phone.start()
        await phone.finished()
        guard case .failed(let message) = phone.state else { Issue.record("expected a failure"); return }
        #expect(message.contains("exit 1") && message.contains("boom"))
        #expect(!message.contains("shaft "))           // no terminal command in the copy
    }

    @Test func aNewLinkRunsThePairingStepAlone() async {
        let shaft = ScriptedShaft([Self.okLine, Self.okLine, Self.linkLine,
                                   ScriptedShaft.lines(0, #"{"ok": true, "link": "https://x/pair?t=2", "expires_minutes": 10}"#)])
        let phone = access(shaft)
        phone.start()
        await phone.finished()
        phone.newLink()
        await phone.finished()
        #expect(shaft.calls.last == ["web", "pair", "--name", "iphone", "--json"])
        #expect(shaft.calls.count == 4)
        #expect(phone.state == .link("https://x/pair?t=2", expiresMinutes: 10))
    }

    @Test func stoppingDropsTheAnswerOfTheRunThatWasCut() async {
        let shaft = ScriptedShaft([Self.okLine, Self.okLine, Self.linkLine])
        let phone = access(shaft)
        phone.start()
        phone.stop()
        await Task.yield()
        #expect(phone.state == .idle)
    }

    /// A box the tests flip: whether the restarted web service answers.
    @MainActor final class Service { var up = false; var asked = 0 }

    @Test func theLinkIsAskedForOnlyOnceTheRestartedServiceAnswers() async {
        let service = Service()
        let shaft = ScriptedShaft([Self.okLine, Self.okLine, Self.linkLine])
        let phone = access(shaft, reachable: { service.asked += 1; return service.asked >= 3 },
                           sleep: { _ in })
        phone.start()
        await phone.finished()
        #expect(service.asked == 3)                    // it polled until the service answered
        #expect(phone.state == .link(Self.link, expiresMinutes: 10))
        #expect(shaft.calls.map { $0[0] + " " + $0[1] } == ["api on", "web on", "web pair"])
    }

    @Test func aServiceThatNeverAnswersFailsAndTryAgainOnlyWaitsAgain() async {
        let service = Service()
        let shaft = ScriptedShaft([Self.okLine, Self.okLine, Self.linkLine])
        let phone = access(shaft, reachable: { service.asked += 1; return service.up })
        phone.start()
        await phone.finished()
        guard case .failed(let message) = phone.state else { Issue.record("expected a failure, got \(phone.state)"); return }
        #expect(message.contains("not answered"))
        #expect(!message.contains("shaft "))
        #expect(shaft.calls.count == 2)                // no link asked for while the service is down
        service.up = true
        phone.start()                                  // Try again: nothing is restarted a second time
        await phone.finished()
        #expect(phone.state == .link(Self.link, expiresMinutes: 10))
        #expect(shaft.calls.count == 3)
        #expect(shaft.calls.last?[0] == "web" && shaft.calls.last?[1] == "pair")
    }

    @Test func leavingTheSessionWhileReconnectingEndsTheRun() async {
        let service = Service()
        let shaft = ScriptedShaft([Self.okLine, Self.okLine, Self.linkLine])
        // the wait never ends by itself: a sleep that only a cancel breaks
        let phone = access(shaft, reachable: { false }, sleep: { _ in try await Task.sleep(for: .seconds(60)) })
        phone.start()
        for _ in 0..<400 where shaft.calls.count < 2 { try? await Task.sleep(for: .milliseconds(5)) }
        for _ in 0..<400 where phone.state != .working("Reconnecting…") { try? await Task.sleep(for: .milliseconds(5)) }
        #expect(phone.state == .working("Reconnecting…"))
        phone.stop()
        #expect(phone.state == .idle)
        await phone.finished()
        #expect(phone.state == .idle)                  // the cancelled wait did not turn into a failure or a link
        #expect(shaft.calls.count == 2)
        _ = service
    }
}
#endif
