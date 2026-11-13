import Darwin
import Foundation
import SHAFTKit
import Testing

struct PastedTextTests {
    @Test func characterBoundaryKeeps2000InlineAndAttaches2001() {
        #expect(!PastedText.belongsInFile(String(repeating: "a", count: 2000)))
        #expect(PastedText.belongsInFile(String(repeating: "a", count: 2001)))
    }

    @Test func lineBoundaryKeeps20InlineAndAttaches21() {
        #expect(!PastedText.belongsInFile(Array(repeating: "line", count: 20).joined(separator: "\n")))
        #expect(PastedText.belongsInFile(Array(repeating: "line", count: 21).joined(separator: "\n")))
    }

    @Test func trailingNewlineDoesNotAddALine() {
        #expect(!PastedText.belongsInFile(String(repeating: "line\n", count: 20)))
        #expect(!PastedText.belongsInFile(String(repeating: "line\r\n", count: 20)))
    }

    @Test func whitespaceOnlyStaysInline() {
        #expect(!PastedText.belongsInFile(String(repeating: " ", count: 5000)))
        #expect(!PastedText.belongsInFile(String(repeating: "\n", count: 5000)))
    }

    @Test func unicodeWhiteSpaceStaysInlineButZeroWidthSpacesAttach() {
        #expect(!PastedText.belongsInFile(String(repeating: "\u{0085}", count: 5000)))
        #expect(PastedText.belongsInFile(String(repeating: "\u{200B}", count: 2001)))
    }

    @Test func fileNameUsesLocalTimeWithoutAnExtension() throws {
        let date = Date(timeIntervalSince1970: 0)
        let previous = getenv("TZ").map { String(cString: $0) }
        setenv("TZ", "Etc/GMT-1", 1)
        tzset()
        NSTimeZone.resetSystemTimeZone()
        defer {
            if let previous { setenv("TZ", previous, 1) } else { unsetenv("TZ") }
            tzset()
            NSTimeZone.resetSystemTimeZone()
        }
        #expect(PastedText.fileName(at: date) == "pasted-text-19700101-010000")
    }
}
