import Foundation
import SHAFTKit

/// What the Mac window shows when the session has opened: the setup wizard, the local pairing (an install that exists,
/// an app not paired with it), or the session's own screens (paired, or `SessionRoot`'s pasted-link pairing).
enum SetupRoute: Equatable {
    case wizard
    case localPairing
    case session
}

/// The decisions behind the Mac app's first screen, kept free of views so the SHAFTMacTests bundle checks them.
enum SetupGate {
    /// No SHAFT is installed in `<home>/SHAFT`: it has no `.git`. A folder that is there but is not a checkout counts
    /// as not installed; the wizard's Install step says what to do with it.
    static func setupNeeded(home: URL) -> Bool {
        !FileManager.default.fileExists(atPath: home.appending(path: "SHAFT/.git").path)
    }

    /// Paired: the session, unless the wizard goes on past its own pairing (`wizardContinues`: the steps after
    /// Install). Not paired: the wizard while it is open (an install half done already has a `.git`, and no pairing
    /// yet) or when SHAFT is not installed; else pair with the install there, no link.
    static func route(hasStore: Bool, setupNeeded: Bool, wizardOpen: Bool, wizardContinues: Bool) -> SetupRoute {
        if hasStore { return wizardContinues ? .wizard : .session }
        return wizardOpen || setupNeeded ? .wizard : .localPairing
    }

    /// Whether the local pairing screen runs the pair step without a click. Not paired yet in this run: yes. The local
    /// server dropped this app's pairing (`401 unpaired`, a reinstall or a revoke): yes, once; a second loss shows the
    /// error instead of pairing in a loop. The operator unpaired, or a remote server dropped it: no, they choose.
    static func pairsAutomatically(loss: PairingLoss?, automaticRepairs: Int) -> Bool {
        guard let loss else { return true }
        return loss.isLocal && automaticRepairs == 0
    }

    /// Welcome's Continue: only once the operator has ticked that they understand the risks.
    static func welcomeCanContinue(understood: Bool) -> Bool {
        understood
    }

    /// A step's place in the sidebar beside the step on screen.
    enum StepStatus: Equatable {
        case done, current, pending
    }

    static func status(of step: SetupWizardStep, current: SetupWizardStep) -> StepStatus {
        let all = SetupWizardStep.allCases
        guard let index = all.firstIndex(of: step), let now = all.firstIndex(of: current) else { return .pending }
        return index < now ? .done : index == now ? .current : .pending
    }
}

/// The Installer's configuration for this run of the app. A Release build reads nothing from the environment: the
/// newest tagged release goes into the user's own `~/SHAFT`. A Debug build may redirect both, so a dev run never touches
/// the real `~/SHAFT` and can install a branch before it is tagged (apps/shaft/README.md, "The setup wizard in a dev run").
enum SetupEnvironment {
    struct Problem: Error, Equatable, LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// The configuration, or why the environment cannot be used (Debug only: a partial server source, a relative home).
    static func configuration() throws -> Installer.Configuration {
        #if DEBUG
        try configuration(env: ProcessInfo.processInfo.environment)
        #else
        Installer.Configuration()
        #endif
    }

    #if DEBUG
    static let home = "SHAFT_DEV_HOME"
    static let sourceKeys = ["SHAFT_DEV_SERVER_REPO", "SHAFT_DEV_SERVER_REF", "SHAFT_DEV_TEMPLATE_REF"]

    /// `SHAFT_DEV_HOME`: the home the server goes into (`<it>/SHAFT`), an absolute path. A dev run (`SHAFT_DEV_DIR`)
    /// without it uses `<SHAFT_DEV_DIR>/home`: such a run never reaches the real `~/SHAFT`, where the gate would pair
    /// with the operator's own install on its own. `SHAFT_DEV_SERVER_REPO`, `SHAFT_DEV_SERVER_REF` and
    /// `SHAFT_DEV_TEMPLATE_REF`: the origin, the branch or tag to clone, and the ref that becomes the `template` branch,
    /// all three or none.
    static func configuration(env: [String: String]) throws -> Installer.Configuration {
        var configuration = Installer.Configuration()
        if let path = value(home, env) {
            guard path.hasPrefix("/") else {
                throw Problem(message: "\(home) must be an absolute path, not \(path).")
            }
            configuration.home = URL(fileURLWithPath: path, isDirectory: true)
        } else if let dir = value("SHAFT_DEV_DIR", env) {
            configuration.home = URL(fileURLWithPath: dir, isDirectory: true).appending(path: "home", directoryHint: .isDirectory)
        }
        let given = sourceKeys.compactMap { key in value(key, env).map { (key, $0) } }
        if !given.isEmpty {
            let missing = sourceKeys.filter { key in !given.contains { $0.0 == key } }
            guard missing.isEmpty else {
                throw Problem(message: "\(given.map(\.0).joined(separator: ", ")) set without \(missing.joined(separator: " and ")): "
                                       + "the server source needs all of \(sourceKeys.joined(separator: ", ")).")
            }
            let values = Dictionary(uniqueKeysWithValues: given)
            configuration.serverSource = ServerSource(url: values[sourceKeys[0]]!, ref: values[sourceKeys[1]]!,
                                                      templateRef: values[sourceKeys[2]]!)
        }
        return configuration
    }

    private static func value(_ key: String, _ env: [String: String]) -> String? {
        guard let text = env[key]?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else { return nil }
        return text
    }
    #endif
}
