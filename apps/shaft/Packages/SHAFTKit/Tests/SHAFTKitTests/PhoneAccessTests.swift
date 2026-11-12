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

    func access(_ shaft: ScriptedShaft, installed: Bool = true) -> PhoneAccess {
        PhoneAccess(run: { try await shaft.run($0, nil) }, hasTailscale: { installed })
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
        let phone = PhoneAccess(run: { try await shaft.run($0, nil) }, hasTailscale: { installed })
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
}
#endif
