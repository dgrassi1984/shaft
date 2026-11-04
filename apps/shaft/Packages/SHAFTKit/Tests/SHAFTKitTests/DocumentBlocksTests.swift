import Foundation
import Testing
@testable import SHAFTKit

/// The cut behind the fix for documents that froze on opening (a 40 KB one for over 20 s, on the Mac): a page draws a
/// document a block at a time, lazily, so every block must parse alone as it did in the whole.
struct DocumentBlocksTests {
    private func texts(_ source: String) -> [String] {
        DocumentBlocks.split(source).map(\.text)
    }

    @Test func longParagraphsAreCutAtTheirBlankLines() {
        let long = String(repeating: "A long paragraph of **bold** and *italics*. ", count: 50)
        let source = (0..<40).map { "\($0) \(long)" }.joined(separator: "\n\n")
        #expect(texts(source).count == 40)
    }

    @Test func aFenceKeepsItsBlankLines() {
        let fenced = "```python\ndef f():\n\n    return 1\n\n\nf()\n```"
        #expect(texts("Before.\n\n\(fenced)\n\nAfter.") == ["Before.", fenced, "After."])
    }

    @Test func aTildeFenceAndALongerCloserAreRead() {
        let tilde = "~~~\n```\n\nnot a closer\n~~~"
        let long = "````\n```\n\nstill in\n`````"
        #expect(texts("\(tilde)\n\n\(long)") == [tilde, long])
    }

    @Test func anUnclosedFenceRunsToTheEnd() {
        let open = "```\ncode\n\nmore code"
        #expect(texts("Before.\n\n\(open)") == ["Before.", open])
    }

    @Test func aFenceAfterTextWithNoBlankLineStillHoldsItsBlankLines() {
        let source = "Text.\n```\na\n\nb\n```\nMore.\n\nNext."
        #expect(texts(source) == ["Text.\n```\na\n\nb\n```\nMore.", "Next."])
    }

    @Test func aTableIsOneBlock() throws {
        let table = "| Name | Total |\n| --- | ---: |\n| One | 12 |\n| Two | 3.5 |"
        let blocks = DocumentBlocks.split("Before.\n\n\(table)\n\nAfter.")
        #expect(blocks.map(\.text) == ["Before.", table, "After."])
        let block = try #require(blocks.dropFirst().first)
        #expect(block.first == .table && block.last == .table)
    }

    @Test func aListGoesOnAcrossABlankLineAndAnIndentedLine() {
        let list = "- one\n\n  still the first item\n\n- two\n\n      indented code in it\n- three"
        #expect(texts("\(list)\n\nAfter.") == [list, "After."])
    }

    @Test func anOrderedListKeepsItsItemsAndTheirWidth() {
        let list = "1. first\n\n   its second paragraph\n\n2. second\n\n10. tenth\n\n    its paragraph"
        #expect(texts("\(list)\n\nAfter.") == [list, "After."])
    }

    @Test func aFenceInAListItemKeepsItsBlankLines() {
        let list = "- item\n\n  ```\n  a\n\n  b\n  ```\n\n- next"
        #expect(texts("\(list)\n\nAfter.") == [list, "After."])
    }

    @Test func aLineLessIndentedThanTheItemEndsTheList() {
        #expect(texts("10. item\n\n  Not in it.") == ["10. item", "  Not in it."])
    }

    @Test func aRuleIsNotAListItem() throws {
        let blocks = DocumentBlocks.split("- item\n\n- - -\n\n* * *\n\nText.")
        #expect(blocks.map(\.first) == [.list, .rule, .rule, .paragraph])
    }

    @Test func aDocumentWithNoBlankLinesIsOneBlock() throws {
        let source = (0..<230).map { "A line of a document with no blank line, number \($0)." }.joined(separator: "\n")
        let block = try #require(DocumentBlocks.split(source).first)
        #expect(DocumentBlocks.split(source).count == 1)
        #expect(block.text == source)
    }

    @Test func blankLinesAtBothEndsDrawNothing() {
        #expect(texts("") == [])
        #expect(texts("\n\n  \n") == [])
        #expect(texts("\n\nOne.\n\n\n\nTwo.\n\n") == ["One.", "Two."])
    }

    @Test func crlfLinesCutToo() {
        #expect(texts("One.\r\n\r\nTwo.\r\nStill two.") == ["One.", "Two.\nStill two."])
    }

    @Test func indentedCodeKeepsItsBlankLines() {
        #expect(texts("Text.\n\n    a\n\n    b\n\nAfter.") == ["Text.", "    a\n\n    b", "After."])
    }

    @Test func anHTMLCommentKeepsItsBlankLines() {
        let comment = "<!-- a note\n\nof two parts -->"
        #expect(texts("\(comment)\n\nAfter.") == [comment, "After."])
    }

    @Test func aLinkDefinitionReachesEveryBlockAndDrawsNothing() {
        let source = "See [the plan][p].\n\nNo links here.\n\nAnd [again][p].\n\n[p]: https://example.com \"Plan\"\n"
        #expect(texts(source) == ["See [the plan][p].\n\n[p]: https://example.com \"Plan\"", "No links here.",
                                  "And [again][p].\n\n[p]: https://example.com \"Plan\""])
    }

    @Test func aNoteWithWordsIsAParagraphNotADefinition() {
        #expect(texts("[^1]: a note in words") == ["[^1]: a note in words"])
    }

    @Test func aBlockKnowsWhatItOpensAndClosesWith() {
        let source = "# One\n\nText.\n\nTwo\n===\n\n> A quote.\n\n- a\n- b\n\n---\n\n```\ncode\n```\n\n    indented"
        let kinds = DocumentBlocks.split(source).map(\.first)
        #expect(kinds == [.heading(1), .paragraph, .heading(1), .quote, .list, .rule, .code, .code])
        #expect(DocumentBlocks.split("###### Six\ntext").first?.last == .paragraph)
        #expect(DocumentBlocks.split("Text.\n```\ncode\n```").first?.last == .code)
    }

    @Test func aRepeatedBlockHasAnIdOfItsOwnAndAnUnchangedOneKeepsIt() {
        let one = DocumentBlocks.split("Same.\n\nSame.\n\nOther.")
        #expect(Set(one.map(\.id)).count == 3)
        let two = DocumentBlocks.split("Same.\n\nSame.\n\nOther.\n\nNew.")
        #expect(Array(two.prefix(3)).map(\.id) == one.map(\.id))
    }

    /// Text survives the cut: the blocks read the document, line for line, once its blank lines are set aside.
    @Test func nothingIsLostOrMoved() {
        let source = """
        # Title

        Para one
        goes on.

        - item

          more

        ```
        code

        code
        ```

        | a | b |
        | - | - |
        | 1 | 2 |

        > quote

        Last.
        """
        let joined = texts(source).joined(separator: "\n").split(separator: "\n").filter { !$0.isEmpty }
        #expect(joined == source.split(separator: "\n").filter { !$0.isEmpty })
    }
}
