import Foundation
import Testing

struct MacLayoutPinTests {
    @Test func theMacChatColumnKeepsAWidthOfItsOwn() throws {
        let repo = URL(filePath: #filePath)
            .deletingLastPathComponent()  // SHAFTKitTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // SHAFTKit
            .deletingLastPathComponent()  // Packages
            .deletingLastPathComponent()  // shaft
            .deletingLastPathComponent()  // apps
            .deletingLastPathComponent()  // repository
        let source = try String(contentsOf: repo.appending(path: "apps/shaft/macOS/MacRoot.swift"), encoding: .utf8)
        // AppKit crash: "more Update Constraints in Window passes than there are views in the window", 2026-11-04.
        #expect(source.contains("navigationSplitViewColumnWidth(min: 380"))
    }
}
