import Foundation
import Testing

/// Bug, 2026-11-20: opening or selecting a chat or thread left the keyboard off the composer (the Mac window's first
/// responder stayed the window). Traced in a dev-driven run: the field did take the keyboard, then the switch from
/// Loading to the feed made the composer anew (its insets sat on the if/else), and the new field took the request as
/// already handled.
struct ComposerFocusPinTests {
    private func source(_ path: String) throws -> String {
        let repo = URL(filePath: #filePath)
            .deletingLastPathComponent()  // SHAFTKitTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // SHAFTKit
            .deletingLastPathComponent()  // Packages
            .deletingLastPathComponent()  // shaft
            .deletingLastPathComponent()  // apps
            .deletingLastPathComponent()  // repository
        return try String(contentsOf: repo.appending(path: "apps/shaft/" + path), encoding: .utf8)
    }

    @Test func openingAChatRequestsComposerFocus() throws {
        let source = try source("Shared/Chat/ChatScreen.swift")
        #expect(source.contains("screen.focusComposerOnOpen = true"))
        #expect(source.contains("focusesComposerOnOpen"))
        #expect(!source.contains("screen.focusComposerRequest += 1"))
    }

    /// The composer is an inset on the feed's container: on the if/else itself, the feed's arrival remade it.
    @Test func theFeedsArrivalKeepsTheComposer() throws {
        let source = try source("Shared/Chat/ChatScreen.swift")
        #expect(source.contains("ZStack { content }"))
    }

    /// An on-screen keyboard would cover the chat just opened: iOS focuses only with a hardware keyboard.
    @Test func iosFocusesOnlyWithAHardwareKeyboard() throws {
        #expect(try source("Shared/Chat/ChatScreen.swift").contains("GCKeyboard.coalesced != nil"))
    }

    @Test func composerRequestsFocusOnceItsFieldExists() throws {
        let source = try source("Shared/Composer/ComposerView.swift")
        #expect(source.contains(".onChange(of: screen.focusComposerOnOpen, initial: true)"))
    }

    @Test func macComposerWaitsForItsWindow() throws {
        let source = try source("macOS/MacTextViews.swift")
        #expect(source.contains("override func viewDidMoveToWindow()"))
        #expect(source.contains("focusWhenInWindow"))
    }

    @Test func iosComposerWaitsForItsWindow() throws {
        let source = try source("Shared/Platform/TextViews.swift")
        #expect(source.contains("override func didMoveToWindow()"))
        #expect(source.contains("focusWhenInWindow"))
    }
}
