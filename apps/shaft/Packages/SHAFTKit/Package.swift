// swift-tools-version: 6.0
// SHAFTKit: what the native chat apps share (docs/plans/PLAN-native-apps.md §2.3) — the /app/v1 models
// (docs/app-api.md), the API client and long-poll, pairing and the Keychain, drafts, the outbox, push registration and
// the notification extensions' body fetch. No UI: the app targets draw it.
import PackageDescription

let package = Package(
    name: "SHAFTKit",
    platforms: [.iOS("26.0"), .macOS("26.0")],
    products: [.library(name: "SHAFTKit", targets: ["SHAFTKit"])],
    targets: [
        .target(name: "SHAFTKit"),
        .testTarget(name: "SHAFTKitTests", dependencies: ["SHAFTKit"], resources: [.copy("Fixtures")]),
    ]
)
