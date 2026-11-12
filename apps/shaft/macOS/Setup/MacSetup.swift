import Foundation
import Observation
import SHAFTKit

/// How the Mac window reaches a session without a pasted link (SetupGate): the setup wizard when SHAFT is not
/// installed, the local pairing when it is and this app is not paired, the session otherwise. It owns the Installer the
/// wizard and the local pairing share, and the wizard's saved progress (`setup.state`). The app delegate keeps it, so a
/// window closed and opened again finds it as it was.
@MainActor
@Observable
final class MacSetup {
    /// The local pairing screen's state.
    enum Pairing: Equatable {
        /// Waiting for a click: why it is not paired, when the session says.
        case idle(String?)
        case pairing
        /// The pair step's real error, or the session's when keeping the credential failed.
        case failed(String)
    }

    private(set) var route: SetupRoute = .session
    /// The Installer's configuration; nil when the environment cannot be used (`configurationError`).
    private(set) var configuration: Installer.Configuration?
    /// Why SHAFT could not start: a dev run's environment it cannot use (SetupEnvironment).
    private(set) var configurationError: String?
    private(set) var pairing: Pairing = .idle(nil)
    /// The wizard is on screen while unpaired, until a pairing ends it.
    private(set) var wizardOpen: Bool
    /// The wizard goes on after its own pairing (the steps after Install).
    private(set) var wizardContinues: Bool
    /// Where the wizard is and what was typed, kept in `setup.state`.
    private(set) var state = SetupState()
    /// Why `setup.state` could not be read: the wizard offers to start over.
    private(set) var stateError: String?
    /// Why the wizard's progress could not be kept: the wizard goes on, and says so.
    private(set) var saveError: String?
    /// The install's own run, and keeping its pairing in the session.
    private(set) var installing = false
    private(set) var installError: String?

    @ObservationIgnored let states: SetupStateStore
    @ObservationIgnored private var automaticRepairs = 0
    @ObservationIgnored private var pairTask: Task<Void, Never>?
    @ObservationIgnored private var installTask: Task<Void, Never>?
    /// Continue's work (opening the session on the install's pairing, then the next step), owned so closing the
    /// window stops it.
    @ObservationIgnored private(set) var completeTask: Task<Void, Never>?
    /// The install's identity: `stopInstall()` moves it on, and work started under an earlier one changes nothing.
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var shared: (session: ObjectIdentifier, installer: Installer)?
    @ObservationIgnored private let configure: () throws -> Installer.Configuration
    @ObservationIgnored private let runner: any ProcessRunning

    /// `configure` and `runner` are the real ones in the app; tests give a temporary home and a scripted runner.
    init(states: SetupStateStore = SetupStateStore(),
         configure: @escaping () throws -> Installer.Configuration = SetupEnvironment.configuration,
         runner: any ProcessRunning = ProcessRunner()) {
        self.states = states
        self.configure = configure
        self.runner = runner
        wizardOpen = states.hasSaved
        wizardContinues = false
        loadConfiguration()
        loadState()
        #if DEBUG
        // Reopened past Install: the wizard goes on where it was. A Release build ends the wizard at its pairing.
        wizardContinues = wizardOpen && stateError == nil && Self.isAfterInstall(state.step)
        #endif
    }

    // MARK: - The gate

    /// Read the environment again (the Try again of a dev run's startup error).
    func loadConfiguration() {
        do {
            configuration = try configure()
            configurationError = nil
        } catch {
            configuration = nil
            configurationError = "SHAFT could not start: \(error.localizedDescription)"
        }
    }

    /// Decide again what the window shows: the session opened or changed, a pairing came or went.
    func update(_ session: SHAFTSession?) {
        guard let session, let configuration else {
            route = .session        // SessionRoot shows why the session did not open; MacRoot shows a bad environment
            return
        }
        let hasStore = session.store != nil
        if hasStore, wizardOpen, !wizardContinues {
            // Paired (a pasted link from Welcome, a dev run's SHAFT_DEV_PAIR, or the install): the wizard is done.
            endWizard()
        }
        let next = SetupGate.route(hasStore: hasStore, setupNeeded: SetupGate.setupNeeded(home: configuration.home),
                                   wizardOpen: wizardOpen, wizardContinues: wizardContinues)
        if next == .wizard, !wizardOpen {
            wizardOpen = true
            loadState()
        }
        let entering = next == .localPairing && route != .localPairing
        route = next
        // A pairing that just succeeded is not cancelled here: its session may still be registering the push token.
        if entering { enterLocalPairing(session) }
    }

    /// The Installer for this session (its credential store is the session's).
    func installer(for session: SHAFTSession) -> Installer? {
        guard let configuration else { return nil }
        if let shared, shared.session == ObjectIdentifier(session) { return shared.installer }
        let installer = Installer(operatorName: state.operatorName, email: state.email, runner: runner,
                                  store: session.pairing.store, configuration: configuration)
        shared = (ObjectIdentifier(session), installer)
        return installer
    }

    // MARK: - Local pairing

    private func enterLocalPairing(_ session: SHAFTSession) {
        if SetupGate.pairsAutomatically(loss: session.loss, automaticRepairs: automaticRepairs) {
            if session.loss != nil { automaticRepairs += 1 }
            pairLocally(session)
        } else {
            pairing = .idle(session.problem)
        }
    }

    /// Run the Installer's pair step alone, then open the session on its credential (also the Try again).
    func pairLocally(_ session: SHAFTSession) {
        guard let installer = installer(for: session) else { return }
        pairTask?.cancel()
        pairing = .pairing
        pairTask = Task { [weak self] in
            await installer.run(only: .pair)
            guard let self, !Task.isCancelled else { return }
            switch installer.steps[.pair] {
            case .done:
                guard let credential = installer.credential else {
                    pairing = .failed("The pair step finished without a credential.")
                    return
                }
                if await !session.pair(credential: credential) {
                    pairing = .failed(session.problem ?? "Keeping the pairing failed.")
                }
            case .failed(let message):
                pairing = .failed(message)
            case .pending, .running, nil:
                pairing = .idle(nil)      // cancelled: the window closed
            }
        }
    }

    // MARK: - The wizard

    /// "Run setup" from the local pairing screen.
    func openWizard(_ session: SHAFTSession) {
        shared?.installer.stop()
        pairTask?.cancel()
        wizardOpen = true
        loadState()
        update(session)
    }

    /// The wizard is done: its progress is forgotten and the window shows the session.
    func finishWizard(_ session: SHAFTSession) {
        wizardContinues = false
        endWizard()
        update(session)
    }

    private func endWizard() {
        installTask?.cancel()
        installTask = nil
        wizardOpen = false
        wizardContinues = false
        states.clear()
        state = SetupState()
    }

    func go(to step: SetupWizardStep) {
        state.step = step
        save()
    }

    func setOperator(name: String, email: String) {
        state.operatorName = name
        state.email = email
        save()
    }

    /// Forget a saved progress that cannot be read, and start at Welcome.
    func startOver() {
        states.clear()
        state = SetupState()
        stateError = nil
    }

    private func loadState() {
        do {
            state = try states.load()
            stateError = nil
        } catch {
            state = SetupState()
            stateError = "The setup's saved progress (\(SetupStateStore.key)) cannot be read: \(error.localizedDescription)"
        }
    }

    private func save() {
        do {
            try states.save(state)
            saveError = nil
        } catch {
            saveError = "The setup's progress could not be kept, so reopening the app starts it over: \(error.localizedDescription)"
        }
    }

    static func isAfterInstall(_ step: SetupWizardStep) -> Bool {
        let all = SetupWizardStep.allCases
        return all.firstIndex(of: step)! > all.firstIndex(of: .install)!
    }

    // MARK: - The Install step

    /// Run every install step not done yet (also each row's Try again, through `retry`).
    func install(_ session: SHAFTSession, retrying step: InstallStep? = nil) {
        guard let installer = installer(for: session), !installer.isRunning else { return }
        installer.operatorName = state.operatorName
        installer.email = state.email
        installError = nil
        installTask = Task {
            if let step { await installer.retry(step) } else { await installer.run() }
        }
    }

    /// The window closed: the running command and Continue's work stop at once. The command's later lines, and
    /// whatever the stopped work would still do after its next suspension, change nothing.
    func stopInstall() {
        generation &+= 1
        if let shared { shared.installer.stop() }
        installTask?.cancel()
        installTask = nil
        completeTask?.cancel()
        completeTask = nil
        installing = false
    }

    /// Continue after every row is done (and its Try again): `completeInstall`, owned so `stopInstall` stops it.
    @discardableResult
    func continueInstall(_ session: SHAFTSession) -> Task<Void, Never>? {
        guard !installing else { return nil }
        let mine = generation
        installing = true
        installError = nil
        let task = Task { [weak self] in
            await self?.completeInstall(session, generation: mine)
            guard let self, self.generation == mine else { return }
            self.installing = false
            self.completeTask = nil
        }
        completeTask = task
        return task
    }

    /// The session opens on the install's own pairing (no link), then the wizard goes on to Model when the server has
    /// no instance yet (Debug: Model's own step comes in a later build) or ends in the app. After every suspension it
    /// checks that it was not stopped (the window closed) before opening the session, moving the saved step or
    /// clearing the progress.
    private func completeInstall(_ session: SHAFTSession, generation mine: Int) async {
        func current() -> Bool { !Task.isCancelled && generation == mine }
        guard let installer = installer(for: session) else { return }
        if (try? session.pairing.current()) == nil {
            // The credential was dropped since the pair row finished (401 unpaired): pair again first.
            await installer.run(only: .pair)
            guard current() else { return }
            if case .failed(let message) = installer.steps[.pair] {
                installError = message
                return
            }
        }
        guard let credential = installer.credential else {
            installError = "The install finished without pairing this app."
            return
        }
        // The wizard stays on screen while the session opens (it would otherwise give way to the app at the store's
        // first appearance, mid-pairing); it ends or goes on below.
        wizardContinues = true
        if session.credential != credential || session.store == nil {
            guard await session.pair(credential: credential) else {
                wizardContinues = false
                installError = session.problem ?? "Keeping the pairing failed."
                return
            }
            guard current() else { return }
        }
        #if DEBUG
        if let store = session.store {
            await store.refresh()
            guard current() else { return }
            if store.loaded, store.instances.isEmpty {
                go(to: .model)
                update(session)
                return
            }
        }
        #endif
        finishWizard(session)
    }
}
