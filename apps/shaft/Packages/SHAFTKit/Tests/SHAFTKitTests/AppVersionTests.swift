import Testing
@testable import SHAFTKit

struct AppVersionTests {
    @Test func gitLabelTakesPrecedence() {
        #expect(AppVersion.label(info: ["SHAFTVersion": "v0.46.0-6-gdf28659f+changes",
                                       "CFBundleShortVersionString": "0.46.0",
                                       "CFBundleVersion": "900"]) == "v0.46.0-6-gdf28659f+changes")
    }

    @Test func testHostFallsBackToBundleVersions() {
        #expect(AppVersion.label(info: ["CFBundleShortVersionString": "0.46.0",
                                       "CFBundleVersion": "900"]) == "0.46.0 (900)")
        #expect(AppVersion.label(info: nil) == nil)
        #expect(AppVersion.label(info: [:]) == nil)
        #expect(AppVersion.label(info: ["CFBundleShortVersionString": "0.46.0"]) == nil)
    }
}
