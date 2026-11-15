import AppKit
import SHAFTKit
import SwiftUI
import Testing

struct AppearanceTests {
    @Test func appearanceValuesRoundTripAndUnknownValuesUseSystem() {
        for theme in AppTheme.allCases {
            #expect(AppTheme(rawValue: theme.rawValue) == theme)
            #expect(AppTheme.stored(theme.rawValue) == theme)
        }
        for size in ChatTextSize.allCases {
            #expect(ChatTextSize(rawValue: size.rawValue) == size)
            #expect(ChatTextSize.stored(size.rawValue) == size)
        }
        for unknown in ["", "future-value", "LIGHT"] {
            #expect(AppTheme.stored(unknown) == .system)
            #expect(ChatTextSize.stored(unknown) == .system)
        }
        #expect(AppTheme.system.colorScheme == nil)
        #expect(AppTheme.light.colorScheme == .light)
        #expect(AppTheme.dark.colorScheme == .dark)
    }

    private let markdown = "# Heading\n\nBody with `inline`.\n\n```swift\nlet value = 1\n```\n\n| Column | Count |\n| --- | --- |\n| Cell | 123 |\n\nTail."

    private func font(_ word: String, in rendered: MarkdownAttributed.Rendered) throws -> NSFont {
        let range = (rendered.text.string as NSString).range(of: word)
        try #require(range.location != NSNotFound)
        return try #require(rendered.text.attribute(.font, at: range.location, effectiveRange: nil) as? NSFont)
    }

    @Test @MainActor func everyTextSizeScalesBodyHeadingsCodeAndTables() throws {
        let body = NSFont.preferredFont(forTextStyle: .body).pointSize
        let table = NSFont.preferredFont(forTextStyle: .subheadline).pointSize
        let scales: [ChatTextSize: CGFloat] = [.small: 0.9, .system: 1, .large: 1.12, .extraLarge: 1.25, .huge: 1.4]
        for size in ChatTextSize.allCases {
            let scale = try #require(scales[size])
            #expect(size.scale == scale)
            let rendered = MarkdownAttributed.render(markdown, scale: size.scale)
            #expect(rendered.textScale == scale)
            #expect(abs(try font("Body", in: rendered).pointSize - body * scale) < 0.01)
            #expect(try font("Heading", in: rendered).pointSize == (body * scale * 1.25).rounded())
            for code in ["inline", "let value"] {
                #expect(abs(try font(code, in: rendered).pointSize - body * scale * 0.88) < 0.01)
            }
            #expect(abs(try font("Cell", in: rendered).pointSize - table * scale) < 0.01)
            let own = MarkdownAttributed.operatorText("Body\n```\ncode\n```", scale: size.scale)
            #expect(abs(try font("Body", in: own).pointSize - body * scale) < 0.01)
            #expect(abs(try font("code", in: own).pointSize - body * scale * 0.88) < 0.01)
        }
    }

    @Test @MainActor func aLargerTextSizeMakesTheSameMessageTaller() throws {
        let small = MarkdownAttributed.render(markdown, scale: ChatTextSize.small.scale)
        let large = MarkdownAttributed.render(markdown, scale: ChatTextSize.huge.scale)
        for word in ["Heading", "Body", "inline", "let value", "Cell"] {
            #expect(try font(word, in: large).pointSize > font(word, in: small).pointSize)
        }
        let previous = MarkdownAttributed.Measure(small)
        for width in [CGFloat(280), 620] {
            let before = previous.layout(width: width)
            let fresh = MarkdownAttributed.Measure(large).layout(width: width)
            let reused = MarkdownAttributed.Measure(large, after: previous).layout(width: width)
            #expect(fresh.size.height > before.size.height)
            #expect(reused == fresh)
        }
    }

    @Test @MainActor func theDefaultTextSizeRendersAsBefore() throws {
        let source = "# Heading\n\nBody with `inline`.\n\n```swift\nlet value = 1\n```"
        let rendered = MarkdownAttributed.render(source, scale: ChatTextSize.system.scale)
        #expect(rendered.text.isEqual(to: MarkdownAttributed.render(source).text))
        #expect(rendered.text.string == "Heading\nBody with inline.\nlet value = 1")
        let body = NSFont.preferredFont(forTextStyle: .body)
        #expect(try font("Body", in: rendered) == body)
        #expect(try font("Heading", in: rendered) == NSFont.systemFont(ofSize: (body.pointSize * 1.25).rounded(), weight: .bold))
        #expect(try font("inline", in: rendered) == NSFont.monospacedSystemFont(ofSize: body.pointSize * 0.88, weight: .regular))
        #expect(try font("let value", in: rendered) == NSFont.monospacedSystemFont(ofSize: body.pointSize * 0.88, weight: .regular))
        let paragraph = try #require(rendered.text.attribute(.paragraphStyle, at: 8, effectiveRange: nil) as? NSParagraphStyle)
        #expect(paragraph.lineSpacing == 4)
        #expect(paragraph.paragraphSpacing == 10)
        let own = "Body\n```\ncode\n```"
        #expect(MarkdownAttributed.operatorText(own, scale: 1).text.isEqual(to: MarkdownAttributed.operatorText(own).text))
    }
}

struct SidebarRowPresentationTests {
    @Test func sidebarStampStaysShortSoTitlesAreNotTruncated() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "Europe/Rome"))
        func date(_ year: Int, _ month: Int, _ day: Int) throws -> Date {
            try #require(calendar.date(from: DateComponents(year: year, month: month, day: day, hour: 10)))
        }
        let now = try date(2026, 11, 15)
        let dates = try [now, date(2026, 11, 14), date(2026, 11, 11), date(2026, 10, 1), date(2025, 11, 14)]
        let stamps = dates.map { SidebarStamp.text($0, now: now, calendar: calendar) }
        let time = Date.FormatStyle(calendar: calendar, timeZone: calendar.timeZone).hour().minute()
        #expect(stamps[0] == now.formatted(time))
        #expect(stamps[1] == "Yesterday")
        #expect(stamps[2].count <= 4)
        #expect(!stamps[3].contains("2026"))
        #expect(stamps[4].contains("2025"))
        for (index, stamp) in stamps.enumerated() {
            #expect(stamp.count <= 11)
            #expect(!stamp.contains(" at "))
            if index != 0 { #expect(!stamp.contains(":")) }
        }
    }

    private func instance(replying: Bool = false) throws -> SHAFTKit.InstanceRow {
        let json = """
        {"name":"example","state":"idle","since":"","model":"example","usdToday":0,
         "line":"Main preview","replying":\(replying),"unread":false,"nextCycle":"","voiceMode":"off",
         "unreadThreads":2}
        """
        return try JSONDecoder().decode(SHAFTKit.InstanceRow.self, from: Data(json.utf8))
    }

    @Test func mainStateOverridesOtherThreadsActivity() throws {
        let row = try instance(replying: true)
        var main = SHAFTKit.FromRow(line: "Main preview", last: nil, unread: false)
        main.state = "idle"
        let state = ConversationRowState(row: row, main: main)
        #expect(state.state == "idle")
        #expect(state.label == "idle")
        #expect(ConversationRowState(row: row, main: nil).state == "replying")
    }

    @Test func mainFailureAndWaitingAreAnnounced() throws {
        let row = try instance()
        var main = SHAFTKit.FromRow(line: "Main preview", last: nil, unread: false)
        main.state = "idle"
        main.failed = true
        #expect(ConversationRowState(row: row, main: main).failed)
        #expect(SidebarRowLabels.instance(row, main: main, collapsed: false, subline: "Main preview")
                == "example, reply failed, Main preview")
        main.waiting = "answering in another thread"
        #expect(ConversationRowState(row: row, main: main).label == "waiting: answering in another thread")
    }

    @Test func collapsedMainAnnouncesUnreadThreadCount() throws {
        let row = try instance()
        #expect(SidebarRowLabels.instance(row, main: nil, collapsed: true, subline: "Main preview")
                == "example, idle, Main preview, 2 unread threads")
        #expect(SidebarRowLabels.instance(row, main: nil, collapsed: false, subline: "Main preview")
                == "example, idle, Main preview")
        #expect(SidebarRowLabels.disclosure("example", expanded: false) == "Expand threads for example")
        #expect(SidebarRowLabels.disclosure("example", expanded: true) == "Collapse threads for example")
    }

    @Test func threadAnnouncesItsWaitingSentenceAndFailure() {
        var thread = SHAFTKit.ThreadRow(id: "example", title: "A topic", created: .distantPast, last: nil,
                                        line: "", state: "idle", waiting: "answering Main", unread: true)
        #expect(SidebarRowLabels.thread(thread) == "A topic, waiting: answering Main, unread")
        thread.waiting = nil
        thread.failed = true
        #expect(SidebarRowLabels.thread(thread) == "A topic, reply failed, unread")
    }

    @Test func movesAreAvailableOnlyWithinTheRowsOwnOrder() {
        let ids = ["first", "middle", "last"]
        #expect(!SidebarMoveAvailability(id: "first", ids: ids).up)
        #expect(SidebarMoveAvailability(id: "first", ids: ids).down)
        #expect(SidebarMoveAvailability(id: "middle", ids: ids).up)
        #expect(SidebarMoveAvailability(id: "middle", ids: ids).down)
        #expect(SidebarMoveAvailability(id: "last", ids: ids).up)
        #expect(!SidebarMoveAvailability(id: "last", ids: ids).down)
        for rows in [[], ["only"]] {
            #expect(!SidebarMoveAvailability(id: "only", ids: rows).up)
            #expect(!SidebarMoveAvailability(id: "only", ids: rows).down)
        }
        #expect(!SidebarMoveAvailability(id: "missing", ids: ids).up)
        #expect(!SidebarMoveAvailability(id: "missing", ids: ids).down)
    }
}
