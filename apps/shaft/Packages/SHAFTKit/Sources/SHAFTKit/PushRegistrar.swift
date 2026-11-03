import Foundation

/// Which APNs host a token belongs to (§11.1). A token from a development-signed build (Xcode) only works with the
/// sandbox; one from a distribution build only with production.
public enum PushEnvironment: String, Sendable, Equatable {
    case sandbox
    case production

    /// This build's environment, read from the `aps-environment` its provisioning profile grants: `development` is
    /// the sandbox. A build with no embedded profile (App Store) is production.
    public static func current(bundle: Bundle = .main) -> PushEnvironment {
        #if os(macOS)
        let url = bundle.bundleURL.appending(path: "Contents/embedded.provisionprofile")
        #else
        let url = bundle.bundleURL.appending(path: "embedded.mobileprovision")
        #endif
        guard let data = try? Data(contentsOf: url) else { return .production }
        return fromProfile(data) ?? .production
    }

    /// The environment a provisioning profile (CMS-signed plist) grants, nil when it grants none.
    static func fromProfile(_ data: Data) -> PushEnvironment? {
        guard let start = data.range(of: Data("<?xml".utf8)),
              let end = data.range(of: Data("</plist>".utf8), in: start.lowerBound..<data.endIndex),
              let plist = try? PropertyListSerialization.propertyList(
                  from: data[start.lowerBound..<end.upperBound], format: nil) as? [String: Any],
              let entitlements = plist["Entitlements"] as? [String: Any] else { return nil }
        let value = (entitlements["aps-environment"] ?? entitlements["com.apple.developer.aps-environment"]) as? String
        switch value {
        case "development": return .sandbox
        case "production": return .production
        default: return nil
        }
    }
}

/// Sends this device's APNs token to the server (§11.1): at every launch, since APNs may change it.
public struct PushRegistrar: Sendable {
    public let client: APIClient
    public let environment: PushEnvironment
    /// The bundle id: what the server's `SHAFT_APNS_TOPIC` must be.
    public let topic: String
    public let platform: String

    public init(client: APIClient, environment: PushEnvironment = .current(),
                topic: String? = Bundle.main.bundleIdentifier, platform: String = Platform.current) throws {
        guard let topic, !topic.isEmpty else {
            throw APIError.badLink("This build has no bundle identifier to register for push.")
        }
        self.client = client
        self.environment = environment
        self.topic = topic
        self.platform = platform
    }

    /// The token as the server takes it: lowercase hex.
    public static func hex(_ token: Data) -> String {
        token.map { String(format: "%02x", $0) }.joined()
    }

    public func body(for token: Data) -> PushRegisterRequest {
        PushRegisterRequest(token: Self.hex(token), env: environment.rawValue, topic: topic, platform: platform)
    }

    /// Register the token. `503 push_off` (the server has no APNs key) comes back as an `APIError` whose message
    /// names what is missing.
    public func register(token: Data) async throws -> PushRegisterResponse {
        try await client.registerPush(body(for: token))
    }

    public func unregister() async throws -> RemovedResponse {
        try await client.unregisterPush()
    }
}
