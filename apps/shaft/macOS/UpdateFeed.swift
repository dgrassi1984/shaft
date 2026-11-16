import Foundation

/// Whether this build may update itself, read from its Info.plist alone: only the Developer ID download carries a
/// feed (`Scripts/release-mac.sh` passes `SHAFT_UPDATE_FEED`; every other build drops the empty key, project.yml).
/// A build that would check a feed it cannot trust is refused rather than started. Kept free of Sparkle so the Mac
/// tests can link it (`macOSTests/UpdateFeedTests.swift`).
enum UpdateFeed: Equatable {
    /// Sparkle may start, checking this feed.
    case on(URL)
    /// An everyday or Debug build: no feed, so no checks.
    case noFeed
    /// A feed is named but the build is not fit to use it; the reason says why.
    case refused(String)

    static func availability(info: [String: Any]?) -> UpdateFeed {
        let feed = (info?["SUFeedURL"] as? String)?.trimmingCharacters(in: .whitespaces) ?? ""
        guard !feed.isEmpty else { return .noFeed }
        guard let url = URL(string: feed), allowed(url) else {
            return .refused("the update feed \(feed) is neither https nor this Mac")
        }
        let key = (info?["SUPublicEDKey"] as? String)?.trimmingCharacters(in: .whitespaces) ?? ""
        guard !key.isEmpty else { return .refused("the build has no public update key") }
        // Both are on in project.yml: the archive's signature is checked before Sparkle unpacks it, and the feed itself
        // must be signed. A build without them was not made by project.yml, so it checks nothing.
        for flag in ["SUVerifyUpdateBeforeExtraction", "SURequireSignedFeed"] where info?[flag] as? Bool != true {
            return .refused("the build does not set \(flag)")
        }
        return .on(url)
    }

    /// https anywhere; plain http only to this Mac, for testing an update from a local server.
    static func allowed(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), let host = url.host(percentEncoded: false)?.lowercased(),
              !host.isEmpty, url.user == nil, url.password == nil else { return false }
        switch scheme {
        case "https": return true
        case "http": return host == "127.0.0.1" || host == "localhost"
        default: return false
        }
    }

    /// The menu's and Settings' line when the build does not update itself.
    var unavailableNote: String? {
        switch self {
        case .on: nil
        case .noFeed: "This build does not update itself: only the signed download from the SHAFT releases does."
        case .refused(let reason): "Updates are off: \(reason)."
        }
    }
}
