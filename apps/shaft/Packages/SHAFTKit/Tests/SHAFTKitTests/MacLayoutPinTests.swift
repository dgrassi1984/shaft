import Foundation
import Testing

struct MacLayoutPinTests {
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

    @Test func theMacChatColumnKeepsAWidthOfItsOwn() throws {
        let source = try source("macOS/MacRoot.swift")
        // AppKit crash: "more Update Constraints in Window passes than there are views in the window", 2026-11-04.
        #expect(source.contains("navigationSplitViewColumnWidth(min: MacRoot.splitChatMinimum, ideal:"))
        #expect(source.contains("static let splitChatMinimum: CGFloat = ChatShell.chatMinimum - ChatShell.inspectorWidth"))
    }

    /// The split views count an open inspector twice: a chat column minimum of the full 380 ran all three columns off
    /// a window under 1400 pt wide (2026-11-04). The fixed minimum is what makes the sum come out at what they take.
    @Test func openDocumentsFitTheWindow() throws {
        let shell = try source("Shared/Chat/ChatShell.swift")
        #expect(shell.contains(".inspectorColumnWidth(min: ChatShell.inspectorWidth, ideal: ChatShell.inspectorWidth,"))
        #expect(shell.contains("static let inspectorWidth: CGFloat = 320"))
    }

    /// AppKit scrolled a code block by itself when a live resize ended (documents sliding open): "SELECT" lost its S.
    @Test func onlyTheOperatorScrollsACodeBlock() throws {
        #expect(try source("macOS/MessageTextView.swift").contains("override func viewDidEndLiveResize()"))
    }

    /// AppKit's floating Writing Tools tile sat on the composer's + and stayed behind outside the window.
    @Test func theTextViewsHaveNoFloatingWritingToolsTile() throws {
        #expect(try source("macOS/MacTextViews.swift").contains("textView.writingToolsBehavior = .none"))
    }

    /// Bug: at 780 the title stood 4 to 6 pt from the buttons beside it.
    @Test func theTitleKeepsItsGapFromTheToolbar() throws {
        let chrome = try source("macOS/MacChrome.swift")
        #expect(chrome.contains("column - 2 * (max(leading, trailing) + Design.Header.titleGap)"))
        // Meetings and Talk widen the right end only: subtracted from the whole column, they also shrank the title when
        // the collapsed sidebar's controls were the wider end (review of the Meetings button, 2026-11-26).
        #expect(chrome.contains("let trailing = trailingControls + CGFloat(extraButtons) * Design.Header.height"))
        #expect(try source("Shared/Design/Design.swift").contains("static let titleGap: CGFloat = 8"))
    }

    /// Bug: the blue caret stood on the first letter of the placeholder.
    @Test func thePlaceholderClearsTheCaret() throws {
        #expect(try source("macOS/MacTextViews.swift").contains("+ ComposerTextView.placeholderInset"))
    }

    /// Bug: a fence with no language had an empty header on the Mac and "text" on iOS.
    @Test func aCodeBlockHasTheSameHeaderEverywhere() throws {
        #expect(try source("macOS/MessageTextView.swift").contains(#"CodeColours.language(block?.language) ?? String(localized: "text")"#))
    }

    /// Bug: Select text's buttons sat in the sheet's system bar, white on a `canvas` sheet.
    @Test func selectTextHasItsOwnFooter() throws {
        for path in ["Shared/Chat/ChatSheets.swift", "Shared/Documents/DocumentDetailView.swift"] {
            #expect(try source(path).contains("SheetFooter {"), "\(path)")
        }
    }

    /// Bug: Settings' Unpair and Forget were tinted pills inside a row. Paused instances' Resume is text too.
    @Test func settingsActionsAreText() throws {
        let settings = try source("Shared/Root/SessionViews.swift")
        #expect(settings.components(separatedBy: ".rowTextButton()").count == 4)
    }
}
