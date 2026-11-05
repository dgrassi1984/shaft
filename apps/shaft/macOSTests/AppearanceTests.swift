import AppKit
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
