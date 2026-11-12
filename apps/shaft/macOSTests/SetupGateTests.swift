import Foundation
import SHAFTKit
import Testing

/// The Mac app's first screen (macOS/Setup/SetupGate.swift): the wizard when SHAFT is not installed, the local pairing
/// when it is, and the rules for pairing again on its own.
struct SetupGateTests {
    private func home(withCheckout: Bool) throws -> URL {
        let home = FileManager.default.temporaryDirectory.appending(path: "setup-gate-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        if withCheckout {
            try FileManager.default.createDirectory(at: home.appending(path: "SHAFT/.git"), withIntermediateDirectories: true)
        }
        return home
    }

    @Test func setupIsNeededUntilTheHomeHoldsASHAFTCheckout() throws {
        let empty = try home(withCheckout: false)
        let installed = try home(withCheckout: true)
        defer {
            try? FileManager.default.removeItem(at: empty)
            try? FileManager.default.removeItem(at: installed)
        }
        #expect(SetupGate.setupNeeded(home: empty))
        // A ~/SHAFT that is not a checkout is not an install.
        try FileManager.default.createDirectory(at: empty.appending(path: "SHAFT/notes"), withIntermediateDirectories: true)
        #expect(SetupGate.setupNeeded(home: empty))
        #expect(!SetupGate.setupNeeded(home: installed))
    }

    @Test func theGateOpensTheWizardOnlyWithoutAnInstall() {
        func route(store: Bool, needed: Bool, open: Bool = false, continues: Bool = false) -> SetupRoute {
            SetupGate.route(hasStore: store, setupNeeded: needed, wizardOpen: open, wizardContinues: continues)
        }
        // Not paired, nothing installed: the wizard (not the pasted-link screen).
        #expect(route(store: false, needed: true) == .wizard)
        // Not paired, SHAFT installed: pair with it, no wizard.
        #expect(route(store: false, needed: false) == .localPairing)
        // Paired: the app, installed or not (a remote server paired with a link).
        #expect(route(store: true, needed: false) == .session)
        #expect(route(store: true, needed: true) == .session)
        // An open wizard stays open while unpaired: the clone makes ~/SHAFT/.git long before the pair step.
        #expect(route(store: false, needed: false, open: true) == .wizard)
        // A pairing ends it (a pasted link from Welcome, or the install's own), unless it goes on past Install.
        #expect(route(store: true, needed: false, open: true) == .session)
        #expect(route(store: true, needed: false, open: true, continues: true) == .wizard)
    }

    @Test func aStaleLocalCredentialIsRepairedOnceThenTheErrorShows() {
        let local = PairingLoss.server(URL(string: "http://127.0.0.1:8090")!)
        // Never paired in this run (an install found at launch): pair without a click.
        #expect(SetupGate.pairsAutomatically(loss: nil, automaticRepairs: 0))
        // The local server forgot this app: pair again on its own, once.
        #expect(SetupGate.pairsAutomatically(loss: local, automaticRepairs: 0))
        #expect(!SetupGate.pairsAutomatically(loss: local, automaticRepairs: 1))
        #expect(SetupGate.pairsAutomatically(loss: .server(URL(string: "http://localhost:8090")!), automaticRepairs: 0))
        // A remote server's loss, or the operator's own Unpair: never on its own.
        #expect(!SetupGate.pairsAutomatically(loss: .server(URL(string: "https://mac.tail1.ts.net:8444")!), automaticRepairs: 0))
        #expect(!SetupGate.pairsAutomatically(loss: .operatorChose, automaticRepairs: 0))
    }

    @Test func welcomeContinuesOnlyOnceTheRisksAreUnderstood() {
        #expect(!SetupGate.welcomeCanContinue(understood: false))
        #expect(SetupGate.welcomeCanContinue(understood: true))
    }

    @Test func theSidebarMarksStepsBeforeTheCurrentOneDone() {
        #expect(SetupGate.status(of: .welcome, current: .install) == .done)
        #expect(SetupGate.status(of: .install, current: .install) == .current)
        #expect(SetupGate.status(of: .model, current: .install) == .pending)
        #expect(SetupGate.status(of: .hello, current: .welcome) == .pending)
    }

    @Test func aDevRunRedirectsTheHomeAndTheServerSource() throws {
        let plain = try SetupEnvironment.configuration(env: [:])
        #expect(plain.home == FileManager.default.homeDirectoryForCurrentUser)
        #expect(plain.serverSource == nil)
        let configuration = try SetupEnvironment.configuration(env: [
            "SHAFT_DEV_HOME": "/tmp/shaft-dev-home",
            "SHAFT_DEV_SERVER_REPO": "/tmp/shaft.git",
            "SHAFT_DEV_SERVER_REF": "claude/onboarding",
            "SHAFT_DEV_TEMPLATE_REF": "template",
        ])
        #expect(configuration.home.path == "/tmp/shaft-dev-home")
        #expect(configuration.server.path == "/tmp/shaft-dev-home/SHAFT")
        #expect(configuration.serverSource == ServerSource(url: "/tmp/shaft.git", ref: "claude/onboarding", templateRef: "template"))
        // A dev run against the fixture (SHAFT_DEV_DIR) never reaches the real ~/SHAFT, even without SHAFT_DEV_HOME.
        let devRun = try SetupEnvironment.configuration(env: ["SHAFT_DEV_DIR": "/tmp/shaft-mac"])
        #expect(devRun.server.path == "/tmp/shaft-mac/home/SHAFT")
        #expect(devRun.serverSource == nil)
    }

    @Test func aPartialServerSourceIsAStartupErrorNamingWhatIsMissing() {
        do {
            _ = try SetupEnvironment.configuration(env: ["SHAFT_DEV_SERVER_REPO": "/tmp/shaft.git",
                                                         "SHAFT_DEV_SERVER_REF": "main"])
            Issue.record("a partial server source was accepted")
        } catch {
            let message = error.localizedDescription
            #expect(message.contains("SHAFT_DEV_TEMPLATE_REF"))
            #expect(!message.contains("without SHAFT_DEV_SERVER_REPO"))
        }
        #expect(throws: SetupEnvironment.Problem.self) {
            _ = try SetupEnvironment.configuration(env: ["SHAFT_DEV_HOME": "relative/home"])
        }
    }
}
