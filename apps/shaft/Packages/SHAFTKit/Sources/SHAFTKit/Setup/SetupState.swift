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
    /// The instance model the provider suggested (`POST /setup/provider`'s `model`): the new instance's `LLM_MODEL`.
    public var model = ""
    public var instanceName = ""
    /// The instance `POST /setup/instance` created, once it has: "Your instance" shows it, never creates a second.
    public var createdInstance: String?
    /// An instance whose `new` succeeded while a later step of its creation was refused: its name is kept, and the
    /// same request again finishes it (the server skips `new` for it).
    public var startedInstance: String?
    /// The drive card picked (`GET /setup/drives` id), nil until one is picked.
    public var drive: String?
    /// The drive's editable text, which becomes `drive.md`.
    public var driveText = ""
    /// `POST /setup/instance`'s `autonomy`: `own` (works on its own after the first reply) or `talk`.
    public var autonomy = "own"
    /// How far the Google setup (Access → Set up Google…) got: its stages done and the address typed. Never the
    /// client's JSON or anything in it: that goes to the server's `.env` on `shaft google client`'s stdin.
    public var google = GoogleSetupProgress()

    public init() {}

    private enum CodingKeys: String, CodingKey {
        case step, operatorName, email, provider, model, instanceName, createdInstance, startedInstance, drive, driveText
        case autonomy, google
    }

    /// A state saved by an older app lacks the newer fields: they take their defaults. An unknown step is an error.
    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        step = try c.decode(SetupWizardStep.self, forKey: .step)
        operatorName = try c.decodeIfPresent(String.self, forKey: .operatorName) ?? ""
        email = try c.decodeIfPresent(String.self, forKey: .email) ?? ""
        provider = try c.decodeIfPresent(String.self, forKey: .provider)
        model = try c.decodeIfPresent(String.self, forKey: .model) ?? ""
        instanceName = try c.decodeIfPresent(String.self, forKey: .instanceName) ?? ""
        createdInstance = try c.decodeIfPresent(String.self, forKey: .createdInstance)
        startedInstance = try c.decodeIfPresent(String.self, forKey: .startedInstance)
        drive = try c.decodeIfPresent(String.self, forKey: .drive)
        driveText = try c.decodeIfPresent(String.self, forKey: .driveText) ?? ""
        autonomy = try c.decodeIfPresent(String.self, forKey: .autonomy) ?? "own"
        google = try c.decodeIfPresent(GoogleSetupProgress.self, forKey: .google) ?? GoogleSetupProgress()
    }
}

/// The Google setup's stages, in order: five cards in Google Cloud (a project, the APIs, the audience and its
/// publishing status, the Data Access scopes, a Desktop app client), the client's JSON handed to the server, the
/// sign-in, and the grants.
public enum GoogleSetupStage: String, Codable, CaseIterable, Sendable {
    case project, apis, audience, scopes, client, json, signIn, grants

    /// The cards the operator follows in Google Cloud and marks done: the app cannot see that console.
    public static let cards: [GoogleSetupStage] = [.project, .apis, .audience, .scopes, .client]
}

/// What the Google setup keeps in `setup.state`, so a reopened app resumes at the first stage not done.
public struct GoogleSetupProgress: Codable, Equatable, Sendable {
    /// The stages done, in their order.
    public private(set) var done: [GoogleSetupStage] = []
    /// The Google address typed for the sign-in.
    public var address = ""

    public init() {}

    /// Where the setup resumes: the first stage not done, in order (a card skipped stays the place to resume, even
    /// when a later one was marked); nil once every stage is done.
    public var resume: GoogleSetupStage? {
        GoogleSetupStage.allCases.first { !done.contains($0) }
    }

    public func isDone(_ stage: GoogleSetupStage) -> Bool { done.contains(stage) }

    public mutating func finish(_ stage: GoogleSetupStage) {
        guard !done.contains(stage) else { return }
        done = GoogleSetupStage.allCases.filter { done.contains($0) || $0 == stage }
    }

    public mutating func reopen(_ stage: GoogleSetupStage) {
        done.removeAll { $0 == stage }
    }

    /// The server kept a client from a new JSON: a login made with an earlier client no longer refreshes, so the
    /// sign-in (and the grants that follow it) are to do again.
    public mutating func clientSaved() {
        finish(.json)
        reopen(.signIn)
        reopen(.grants)
    }

    /// The server already has the account signed in (`google_accounts`), and this setup never handed it a client
    /// itself: the cards, the client and the sign-in were done before (another run, the CLI). Nothing changes once
    /// this setup sent a JSON of its own, whose sign-in is still to come.
    public mutating func adopt(signedIn: Bool) {
        guard signedIn, !isDone(.json), !isDone(.signIn) else { return }
        for stage in GoogleSetupStage.cards + [.json, .signIn] { finish(stage) }
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

    /// Whether a wizard's progress is kept: the wizard was started and not finished.
    public var hasSaved: Bool {
        defaults.object(forKey: Self.key) != nil
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
