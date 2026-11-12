import Foundation

/// Whether the paired server speaks the `/app/v1` this app needs (docs/app-api.md §19). The server says its version at
/// `GET /app/v1/version`; a server older than that route answers 404, which is api 0. Too old: the app says so once, in
/// place of the session, instead of each call failing on its own.
public enum ServerCompatibility {
    /// The lowest `api` (`app_api.API_VERSION`) the everyday app works with: 0, every server since `/app/v1` (one
    /// without `/version` included). Raise it with a change the everyday app cannot do without.
    public static let minimumServerAPI = 0
    /// The lowest `api` the Mac setup wizard works with: its routes (§18) came with api 1. The wizard runs only against
    /// a server it installed, so this never locks out an existing install.
    public static let setupServerAPI = 1

    public enum Verdict: Equatable, Sendable {
        /// The server is new enough; `server` is its release (git describe).
        case compatible(server: String)
        /// The server is older than this app needs; `server` is its release when it said one (nil: it predates
        /// `/app/v1/version`).
        case needsNewerServer(server: String?)
        /// The check could not be made (the server unreachable, a 500): nothing is blocked, the calls say what fails.
        case unknown(String)
    }

    public static func verdict(_ result: Result<ServerVersion, APIError>,
                               minimum: Int = minimumServerAPI) -> Verdict {
        switch result {
        case .success(let version):
            return version.api >= minimum ? .compatible(server: version.server) : .needsNewerServer(server: version.server)
        case .failure(let error):
            // No /version route: a server from before it, api 0.
            guard error.status == 404 else { return .unknown(error.message) }
            return minimum <= 0 ? .compatible(server: "") : .needsNewerServer(server: nil)
        }
    }

    /// What the app shows in place of the session.
    public static func message(for server: String?) -> String {
        "This app needs a newer SHAFT server" + (server.map { " (the server runs \($0))" } ?? "") + "."
    }
}
