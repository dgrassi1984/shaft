import Foundation
import SHAFTKit

/// Debug builds only: the app run from a terminal against the fixture server (apps/shaft/README.md, "Try it against a
/// fixture server"), unsigned or ad-hoc signed. Such a build has no Keychain access group (that needs the team's
/// signing), so with `SHAFT_DEV_DIR` set it keeps the pairing, drafts and outbox in that folder instead, and with
/// `SHAFT_DEV_PAIR=<link>` it pairs at launch through the same `pair` the pairing screen calls. It asks for
/// notifications only with `SHAFT_DEV_NOTIFY=1` (an ad-hoc build gets no push token anyway). `SHAFT_DEV_PRESENCE`
/// (`front` or `shown`) says whether the window counts as in front or shown behind, for a screen that is locked or
/// asleep, where every window counts as covered (MacPresence). A Release build, and a Debug build started without
/// `SHAFT_DEV_DIR` (Xcode, Finder), use the Keychain as always.
enum MacDevSession {
    static var directory: URL? {
        #if DEBUG
        guard let dir = ProcessInfo.processInfo.environment["SHAFT_DEV_DIR"], !dir.isEmpty else { return nil }
        return URL(fileURLWithPath: dir, isDirectory: true)
        #else
        return nil
        #endif
    }

    /// The app's model over `SHAFT_DEV_DIR`, nil when it is not set. A folder it cannot use stops the app with the
    /// reason rather than falling back to the Keychain.
    @MainActor
    static func model() -> AppModel? {
        guard let root = directory else { return nil }
        do {
            let session = SHAFTSession(pairing: PairingStore(store: FileCredentialStore(url: root.appending(path: "credential.json"))),
                                       outbox: try Outbox(directory: root.appending(path: "outbox", directoryHint: .isDirectory)),
                                       drafts: DraftStore(directory: root.appending(path: "drafts", directoryHint: .isDirectory)))
            return AppModel(session: session)
        } catch {
            fatalError("SHAFT_DEV_DIR=\(root.path) cannot be used: \(error)")
        }
    }

    /// Whether to ask for notifications: always, except in a dev run without `SHAFT_DEV_NOTIFY=1`.
    static var asksForNotifications: Bool {
        directory == nil || ProcessInfo.processInfo.environment["SHAFT_DEV_NOTIFY"] == "1"
    }

    /// Pair with `SHAFT_DEV_PAIR` at launch when not paired yet.
    @MainActor
    static func pairIfAsked(_ model: AppModel) {
        guard directory != nil, let link = ProcessInfo.processInfo.environment["SHAFT_DEV_PAIR"], !link.isEmpty,
              let session = model.session, !session.isPaired else { return }
        Task { await session.pair(pasted: link) }
    }
}

/// The pairing credential as a JSON file (a dev run's `SHAFT_DEV_DIR`): what `KeychainStore` keeps in the Keychain.
struct FileCredentialStore: CredentialStore {
    let url: URL

    func load() throws -> ServerCredential? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        let credential = try JSONDecoder().decode(ServerCredential.self, from: Data(contentsOf: url))
        try checkLoopback(credential)
        return credential
    }

    func save(_ credential: ServerCredential) throws {
        try checkLoopback(credential)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(credential).write(to: url, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    private func checkLoopback(_ credential: ServerCredential) throws {
        guard let host = credential.baseURL.host?.lowercased(),
              ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host) else {
            throw APIError.decoding(path: "SHAFT_DEV_DIR",
                                    message: "SHAFT_DEV_DIR only permits loopback servers (127.0.0.1, localhost, ::1). Use the Keychain for other servers.")
        }
    }

    func delete() throws {
        if FileManager.default.fileExists(atPath: url.path) { try FileManager.default.removeItem(at: url) }
    }
}
