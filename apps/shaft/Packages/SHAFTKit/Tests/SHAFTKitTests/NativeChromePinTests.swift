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

    /// Bug: thumbs showed only on the last answer, so a message sent proactively had none; and a turn that failed with no
    /// text asked for a rating of nothing.
    @Test func theThumbsAreInEveryFooterWithSomethingToRate() throws {
        let source = try shared("Shared/Feed/FeedItemViews.swift")
        let footer = try #require(source.range(of: "struct AnswerFooter"))
        let body = source[footer.lowerBound...]
        let thumb = try #require(body.range(of: #"ThumbButton(thumb: "up""#))
        let before = body[..<thumb.lowerBound]
        #expect(!before.contains("if keepActions || rating != nil {"))
        #expect(before.hasSuffix("if rateable {\n                    // Always here, on every message and answer: never only on hover or in a menu (2026-11-04).\n                    "))
        #expect(source.contains("rateable: !answer.content.isEmpty"))
    }

    /// Bug: on the Mac the last turn had Copy and Reply before the thumbs and every other turn after them.
    @Test func theMacsFooterIsInOneOrder() throws {
        let source = try shared("Shared/Feed/FeedItemViews.swift")
        let footer = try #require(source.range(of: "struct AnswerFooter"))
        let body = source[footer.lowerBound...]
        let thumb = try #require(body.range(of: #"ThumbButton(thumb: "up""#))
        let hover = try #require(body.range(of: "HoverActions(source: source, shown: hovering"))
        #expect(thumb.lowerBound < hover.lowerBound)
        let row = body[..<thumb.lowerBound]
        #expect(row.contains("#if os(iOS)\n                if keepActions && !source.isEmpty {"))
        #expect(!body.contains("if !keepActions && !source.isEmpty { HoverActions"))
    }

    /// Bug: the title sat bare on the scrolling text, then in a capsule with its text in the curve.
    @Test func theTitleHasItsOwnCapsule() throws {
        let source = try shared("Shared/Chat/ChatScreen.swift")
        #expect(source.contains(".frame(minHeight: Design.Header.height)"))
        #expect(source.contains(".glassSurface(in: Capsule())"))
        #expect(source.contains(".padding(.horizontal, Design.Header.titleInset)"))
        #expect(try shared("Shared/Design/Design.swift").contains("static let titleInset: CGFloat = 16"))
    }

    /// Bug: the system's edge left a blurred half line under the bar, its opaque band sliced the last line beside Latest,
    /// and Reduce Transparency hit a hard cut. One plain fade, the same everywhere.
    @Test func theFeedEndsInOnePlainFade() throws {
        let screen = try shared("Shared/Chat/ChatScreen.swift")
        #expect(screen.contains(".scrollEdgeEffectHidden(true, for: .all)"))
        #expect(!screen.contains("scrollEdgeEffectStyle"))
        #expect(!screen.contains("scrollEdgeEffectHidden(reduceTransparency"))
        #expect(!screen.contains(".safeAreaBar"))
        #expect(screen.contains(".feedEdges {"))
        #expect(screen.contains(".feedFloor()"))
        let design = try shared("Shared/Design/Design.swift")
        #expect(design.contains("static let fade: CGFloat = 32"))
        // At rest the last line stands clear of the fade.
        let feed = try shared("Shared/Feed/FeedView.swift")
        #expect(feed.contains(".padding(.top, Design.Header.fade)"))
        #expect(feed.contains(".padding(.bottom, Design.Header.fade - Design.Space.turn)"))
    }

    /// Bug: the 3 pt bar ran past the time and icons to the end of their 28 pt target.
    @Test func anAccentBarStopsAtItsContent() throws {
        let source = try shared("Shared/Feed/FeedItemViews.swift")
        #expect(source.contains(".padding(.bottom, -Design.Footer.slack)"))
        #expect(source.contains(".padding(.vertical, -Design.Footer.slack)"))
    }

    /// Bug: the shortcuts line appeared with focus and moved the composer and the feed.
    @Test func theComposersShortcutsAreATooltip() throws {
        let source = try shared("Shared/Composer/ComposerView.swift")
        #expect(source.contains(#".help("Return sends · Shift-Return new line · ⌘-Return queues after the reply")"#))
        #expect(!source.contains("HardwareKeyboard"))
    }

    /// Bug: a change of text size with the keyboard up left an empty field's line at the old size: the capsule stayed tall,
    /// or the placeholder and the caret stood sliced at its top. Reproduced by the frames in the 2026-11-04 polish.
    @Test func anEmptyFieldFollowsTheTextSize() throws {
        let source = try shared("Shared/Platform/TextViews.swift")
        #expect(source.contains("registerForTraitChanges([UITraitPreferredContentSizeCategory.self])"))
        #expect(source.contains("guard let self, self.text.isEmpty else { return }"))
        #expect(source.contains("super.contentOffset = isScrollEnabled ? newValue : .zero"))
        #expect(source.contains("placeholder.topAnchor.constraint(equalTo: frameLayoutGuide.topAnchor"))
    }

    /// Bug: iOS's code and tables cut off at the block's edge with nothing to say more lay beyond.
    @Test func sidewaysBlocksFadeAtAnEdgeWithMore() throws {
        let source = try shared("Shared/Markdown/MarkdownView.swift")
        #expect(source.components(separatedBy: ".sidewaysFade()").count == 4)
    }

    /// Bugs: a sheet taller than its text; the chat showing through Earlier chats; the error twice on the iPad.
    @Test func sheetsFitAndCoverAndTheErrorShowsOnce() throws {
        #expect(try shared("Shared/Documents/DocumentDetailView.swift").contains(".presentationDetents(textHeight.map"))
        #expect(try shared("Shared/Chat/ChatSheets.swift").contains(".presentationBackground(Color.canvas)"))
        #expect(try shared("Shared/Root/SessionViews.swift").contains("if store.loaded, let error = store.error {"))
    }

    /// Bug: without the sidebar's toggle, documents open in portrait left the iPad no way back to its chats.
    @Test func thePadKeepsTheSidebarsToggle() throws {
        #expect(try !shared("iOS/IOSRoot.swift").contains(".toolbar(removing:"))
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

    /// Bug: the Latest pill was an overlay on the scroll view and covered table cells and footers; then, in the bar, it
    /// raised an opaque band. It sits in the fade, at its own width.
    @Test func latestSitsInTheFade() throws {
        #expect(try !shared("Shared/Feed/FeedView.swift").contains(".overlay(alignment: .bottom)"))
        let screen = try shared("Shared/Chat/ChatScreen.swift")
        let edges = try #require(screen.range(of: ".feedEdges {"))
        let pill = try #require(screen.range(of: "LatestPillButton(pill: pill)"))
        let bar = try #require(screen.range(of: ".safeAreaInset(edge: .bottom"))
        #expect(edges.lowerBound < pill.lowerBound && pill.lowerBound < bar.lowerBound)
        let button = try shared("Shared/Feed/FeedView.swift")
        let start = try #require(button.range(of: "struct LatestPillButton"))
        let body = button[start.lowerBound...]
        let end = try #require(body.range(of: "\n}\n"))
        #expect(!body[..<end.lowerBound].contains(".frame(maxWidth: .infinity)"))
    }
}
