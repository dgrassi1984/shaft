import Foundation

/// The setup wizard's steps, in the order the sidebar shows them.
public enum SetupWizardStep: String, Codable, CaseIterable, Sendable {
    case welcome, install, model, instance, access, alone, hello
}

/// Where the wizard is and what the operator typed, so reopening the app resumes there. Never a secret: keys, tokens
/// and the pairing bearer go to the server's `.env` (on stdin) or the Keychain, never here.
public struct SetupState: Codable, Equatable, Sendable {
    public var step: SetupWizardStep = .welcome
    /// The operator's name: the git identity of the install and the instance's `OPERATOR_NAME`.
    public var operatorName = ""
    /// Optional: the repository's git email (`shaft install --email`).
    public var email = ""
    /// The chosen model provider (`openrouter`, `codex`, `custom`), nil until one is picked.
    public var provider: String?
    public var instanceName = ""
    /// The drive card picked (`GET /setup/drives` id), nil until one is picked.
    public var drive: String?
    /// The drive's editable text, which becomes `drive.md`.
    public var driveText = ""
    /// `POST /setup/instance`'s `autonomy`: `own` (works on its own after the first reply) or `talk`.
    public var autonomy = "own"

    public init() {}

    private enum CodingKeys: String, CodingKey {
        case step, operatorName, email, provider, instanceName, drive, driveText, autonomy
    }

    /// A state saved by an older app lacks the newer fields: they take their defaults. An unknown step is an error.
    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        step = try c.decode(SetupWizardStep.self, forKey: .step)
        operatorName = try c.decodeIfPresent(String.self, forKey: .operatorName) ?? ""
        email = try c.decodeIfPresent(String.self, forKey: .email) ?? ""
        provider = try c.decodeIfPresent(String.self, forKey: .provider)
        instanceName = try c.decodeIfPresent(String.self, forKey: .instanceName) ?? ""
        drive = try c.decodeIfPresent(String.self, forKey: .drive)
        driveText = try c.decodeIfPresent(String.self, forKey: .driveText) ?? ""
        autonomy = try c.decodeIfPresent(String.self, forKey: .autonomy) ?? "own"
    }
}

/// `SetupState` in `UserDefaults` under `setup.state`, as JSON.
public struct SetupStateStore: @unchecked Sendable {
    public static let key = "setup.state"
    private let defaults: UserDefaults

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    /// The saved state, or a new one at Welcome when nothing is saved. A stored value that is not the saved state (not
    /// data, or data that does not decode) throws: the caller shows it rather than silently restarting the wizard.
    public func load() throws -> SetupState {
        guard let stored = defaults.object(forKey: Self.key) else { return SetupState() }
        guard let data = stored as? Data else {
            throw SetupStateError("\(Self.key) holds a \(type(of: stored)), not the setup wizard's saved state.")
        }
        return try JSONDecoder().decode(SetupState.self, from: data)
    }

    public func save(_ state: SetupState) throws {
        defaults.set(try JSONEncoder().encode(state), forKey: Self.key)
    }

    /// Forget the wizard's progress: setup finished, or the operator starts over.
    public func clear() {
        defaults.removeObject(forKey: Self.key)
    }
}

public struct SetupStateError: Error, Sendable, Equatable, LocalizedError {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}
