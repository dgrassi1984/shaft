import Foundation
import Testing

/// The operator's three decisions of 2026-11-04 live in view code a unit test cannot draw, so these pin the source: each
/// one fails if the old behaviour comes back.
struct NativeChromePinTests {
    private func shared(_ path: String) throws -> String {
        let app = URL(filePath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: app.appending(path: path), encoding: .utf8)
    }

    /// Bug: thumbs showed only on the last answer, so a message sent proactively had none.
    @Test func theThumbsAreInEveryFooterUnconditionally() throws {
        let source = try shared("Shared/Feed/FeedItemViews.swift")
        let footer = try #require(source.range(of: "struct AnswerFooter"))
        let body = source[footer.lowerBound...]
        let thumb = try #require(body.range(of: #"ThumbButton(thumb: "up""#))
        let before = body[..<thumb.lowerBound]
        #expect(!before.hasSuffix("if keepActions || rating != nil {\n                    "))
        #expect(before.hasSuffix("// Always here, on every message and answer: never only on hover or in a menu (2026-11-04).\n                "))
    }

    /// Bug: the title sat bare on the scrolling text: it has its own capsule, as tall as the header's buttons.
    @Test func theTitleHasItsOwnCapsule() throws {
        let source = try shared("Shared/Chat/ChatScreen.swift")
        #expect(source.contains(".frame(minHeight: Design.Header.height)"))
        #expect(source.contains(".glassSurface(in: Capsule())"))
        #expect(source.contains(".headerEdge()"))
        #expect(source.contains(".footerEdge()"))
    }

    /// Bug: the selected row was the system's accent, with its grey text unreadable on it.
    @Test func aSelectedRowIsNotTheSystemsAccent() throws {
        let source = try shared("Shared/Root/SessionViews.swift")
        #expect(source.contains(".fill(row.name == selection ? Color.rowSelected : Color.clear)"))
        #expect(source.contains(".listRowBackground(rowFill(row))"))
        #expect(source.contains("table.selectionHighlightStyle = .none"))
    }

    /// Bug: opening + or Send's menu morphed the glass it sat in, and the whole composer went with it.
    @Test func theComposersGlassIsBehindItsMenus() throws {
        let source = try shared("Shared/Composer/ComposerView.swift")
        #expect(source.contains("Color.clear.glassSurface(in: RoundedRectangle"))
    }

    /// Bug: the Latest pill was an overlay on the scroll view and covered table cells and footers.
    @Test func latestLivesInTheBarAboveTheComposer() throws {
        #expect(try !shared("Shared/Feed/FeedView.swift").contains(".overlay(alignment: .bottom)"))
        #expect(try shared("Shared/Chat/ChatScreen.swift").contains("LatestPillButton(pill: pill)"))
    }
}
