import Foundation
import Security

/// A pasted pairing link (`https://<mac>.<tailnet>.ts.net:8444/pair?t=<token>`, §2). The base URL is everything before
/// `/pair`.
public struct PairingLink: Sendable, Equatable {
    public let link: String
    public let baseURL: URL
    public let token: String

    public init(_ pasted: String) throws {
        let text = pasted.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: text), let scheme = components.scheme?.lowercased(),
              scheme == "https" || scheme == "http", let host = components.host, !host.isEmpty else {
            throw APIError.badLink("That is not a pairing link. Run `shaft web pair --name <device>` on the Mac and paste the link it prints.")
        }
        guard components.path.hasSuffix("/pair") else {
            throw APIError.badLink("That link is not a pairing link (it should end in /pair?t=…).")
        }
        guard let token = components.queryItems?.first(where: { $0.name == "t" })?.value, !token.isEmpty else {
            throw APIError.badLink("The pairing link has no token (t=…). Copy the whole link.")
        }
        var base = components
        base.path = String(components.path.dropLast("/pair".count))
        base.query = nil
        base.fragment = nil
        guard let baseURL = base.url else {
            throw APIError.badLink("Could not read the server's address from the link.")
        }
        self.link = text
        self.baseURL = baseURL
        self.token = token
    }
}

/// Where the pairing credential is kept. The app and its notification extensions share it (`KeychainStore`); tests use
/// `MemoryCredentialStore`.
public protocol CredentialStore: Sendable {
    func load() throws -> ServerCredential?
    func save(_ credential: ServerCredential) throws
    func delete() throws
}

/// The pairing credential in the Keychain, in an access group the app and its extensions share, so a notification
/// extension can fetch the body with the app's bearer (§11.4). One item: the credential as JSON.
public struct KeychainStore: CredentialStore {
    public static let service = "io.github.dgrassi1984.shaft.pairing"
    static let account = "server"

    /// `<team>.io.github.dgrassi1984.shaft`: each target's Info.plist names it (`SHAFTKeychainAccessGroup`, set from
    /// `$(AppIdentifierPrefix)` in project.yml), so the team id is written once.
    public let accessGroup: String

    public init(accessGroup: String) {
        self.accessGroup = accessGroup
    }

    /// The store for this target, from its Info.plist. Throws when the key is missing: a target built without it would
    /// keep the credential where the extensions cannot read it.
    public static func forMainBundle(_ bundle: Bundle = .main) throws -> KeychainStore {
        guard let group = bundle.object(forInfoDictionaryKey: "SHAFTKeychainAccessGroup") as? String,
              !group.isEmpty, !group.hasPrefix("$(") else {
            throw KeychainError(status: errSecMissingEntitlement,
                                what: "SHAFTKeychainAccessGroup is not set in \(bundle.bundleIdentifier ?? "this target")'s Info.plist")
        }
        return KeychainStore(accessGroup: group)
    }

    private var query: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: Self.account,
            kSecAttrAccessGroup as String: accessGroup,
            kSecUseDataProtectionKeychain as String: true,
        ]
    }

    public func load() throws -> ServerCredential? {
        var query = query
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw KeychainError(status: status, what: "reading the pairing")
        }
        return try JSONDecoder().decode(ServerCredential.self, from: data)
    }

    public func save(_ credential: ServerCredential) throws {
        let data = try JSONEncoder().encode(credential)
        // After the first unlock: a notification extension runs while the phone is locked.
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(query.merging(attributes) { $1 } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw KeychainError(status: status, what: "keeping the pairing") }
    }

    public func delete() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError(status: status, what: "removing the pairing")
        }
    }
}

public struct KeychainError: Error, Sendable, Equatable, LocalizedError {
    public let status: OSStatus
    public let what: String

    public var errorDescription: String? {
        let reason = SecCopyErrorMessageString(status, nil) as String? ?? "OSStatus \(status)"
        return "Keychain: \(what) failed: \(reason)"
    }
}

/// A credential store in memory: tests and previews.
public final class MemoryCredentialStore: CredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var credential: ServerCredential?

    public init(_ credential: ServerCredential? = nil) {
        self.credential = credential
    }

    public func load() throws -> ServerCredential? { lock.withLock { credential } }
    public func save(_ credential: ServerCredential) throws { lock.withLock { self.credential = credential } }
    public func delete() throws { lock.withLock { credential = nil } }
}

/// Pairing for the app: takes the pasted link, pairs with the server it names (`POST /app/v1/pair`) and keeps the
/// credential in the shared Keychain item.
public struct PairingStore: Sendable {
    public let store: any CredentialStore
    let session: URLSession

    public init(store: any CredentialStore, session: URLSession = .shared) {
        self.store = store
        self.session = session
    }

    public func current() throws -> ServerCredential? {
        try store.load()
    }

    /// Pair with the link pasted (§2). On success the credential is stored and returned.
    @discardableResult
    public func pair(pasted: String, platform: String = Platform.current) async throws -> ServerCredential {
        let link = try PairingLink(pasted)
        let credential = try await APIClient.pair(link: link, platform: platform, session: session)
        try store.save(credential)
        return credential
    }

    /// Unpair: tell the server (best effort: a device it already forgot answers `401`), then forget it here.
    public func unpair() async throws {
        if let credential = try store.load() {
            do {
                _ = try await APIClient(credential: credential, session: session).unpair()
            } catch let error as APIError where error.isUnpaired {
                // Already unpaired on the server: forgetting it here is all that is left.
            }
        }
        try store.delete()
    }

    /// Forget the pairing on this device only: when the server cannot be reached to unpair. `shaft web unpair` then
    /// revokes it on the Mac.
    public func forget() throws {
        try store.delete()
    }
}
