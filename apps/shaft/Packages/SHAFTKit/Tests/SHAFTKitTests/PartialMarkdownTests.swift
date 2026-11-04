import Foundation
import Testing
@testable import SHAFTKit

/// Bug: a stopped or streaming reply showed "found for **Slow, and I will" and a dot for an empty list item, on the
/// feed and in the sidebar's preview.
struct PartialMarkdownTests {
    @Test func openBoldIsClosedSoNoRawMarksShow() {
        #expect(PartialMarkdown.closed("Here is what I found for **Slow, and I will") ==
                "Here is what I found for **Slow, and I will**")
    }

    @Test func trailingSpaceDoesNotStopTheCloser() {
        #expect(PartialMarkdown.closed("found for **Slow, and ") == "found for **Slow, and**")
    }

    @Test func aMarkWithNothingAfterItIsTakenOff() {
        #expect(PartialMarkdown.closed("Here is what I found for **") == "Here is what I found for")
        #expect(PartialMarkdown.closed("one *") == "one")
    }

    @Test func emphasisAndCodeAndStrikeClose() {
        #expect(PartialMarkdown.closed("so *this") == "so *this*")
        #expect(PartialMarkdown.closed("run `ls -l") == "run `ls -l`")
        #expect(PartialMarkdown.closed("was ~~wrong") == "was ~~wrong~~")
        #expect(PartialMarkdown.closed("***both") == "***both***")
    }

    @Test func aParagraphsIndentStaysAndALoneTickGoes() {
        #expect(PartialMarkdown.closed("  - run `ls") == "  - run `ls`")
        #expect(PartialMarkdown.closed("run `") == "run ")
    }

    /// It runs on every refresh of a streaming reply: a long reply must stay cheap.
    @Test func aLongReplyStaysCheap() {
        let text = String(repeating: "A line of **bold** and `code` and more words here.\n\n", count: 4_000)
        let start = ContinuousClock.now
        for _ in 0..<10 { _ = PartialMarkdown.closed(text) }
        print("PartialMarkdown: \(text.utf8.count) bytes, 10 runs in \(ContinuousClock.now - start)")
        #expect(ContinuousClock.now - start < .seconds(2))
    }

    @Test func balancedTextIsLeftAlone() {
        for text in ["**bold** and *em* and `code`", "2 ** 3 is 8", "a * b * c", "snake_case_name here", "no marks"] {
            #expect(PartialMarkdown.closed(text) == text)
        }
    }

    @Test func marksInsideCodeAreNotMarks() {
        #expect(PartialMarkdown.closed("use `**kwargs` here") == "use `**kwargs` here")
    }

    @Test func anEmptyListItemAtTheEndGoes() {
        #expect(PartialMarkdown.closed("Show the slow one.\n\n- ") == "Show the slow one.")
        #expect(PartialMarkdown.closed("- one\n- two\n- ") == "- one\n- two")
        #expect(PartialMarkdown.closed("1. one\n2.") == "1. one")
    }

    @Test func anOpenFenceIsAlreadyCode() {
        let text = "Look:\n\n```python\nprint('**')\n"
        #expect(PartialMarkdown.closed(text) == text)
    }

    @Test func onlyTheLastParagraphIsLookedAt() {
        #expect(PartialMarkdown.closed("A *lone star\n\nNext **bold") == "A *lone star\n\nNext **bold**")
    }

    @Test func nextCycleHintKeepsTheClauseThatSaysWhen() {
        #expect(NextCycleHint.short("a cycle is running and read its inbox when it began: this starts the next one once it ends")
                == "this starts the next one once it ends")
        #expect(NextCycleHint.short("for wren's next cycle, which this starts now") == "for wren's next cycle, which this starts now")
    }

    @Test func aRefusalReadsAsASentence() {
        let error = APIError.server(status: 409, error: ServerError(code: "refused", message: "refused: notes/docs/ledger.md changed since you looked"))
        #expect(error.message == "notes/docs/ledger.md changed since you looked.")
    }
}
