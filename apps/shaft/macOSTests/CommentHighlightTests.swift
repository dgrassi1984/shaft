import Foundation
import SHAFTKit
import Testing

/// The commented passages marked in a document's Markdown. The page's text must never change; a passage that cannot
/// be marked loses its highlight, not its text.
struct CommentHighlightTests {
    private func thread(_ id: String, _ exact: String, in source: String, state: String = "open") throws -> CommentThread {
        let rendered = RenderedText.plain(source) as NSString
        let range = rendered.range(of: exact)
        let anchor = Anchoring.anchor(in: rendered as String, range: range)
            ?? CommentAnchor(exact: exact, prefix: "", suffix: "", start: 0)
        return try make(id, anchor: anchor, state: state)
    }

    private func make(_ id: String, anchor: CommentAnchor?, state: String = "open") throws -> CommentThread {
        var row: [String: Any] = ["id": id, "doc": "a.md", "by": "operator", "state": state,
                                  "created": "2026-11-04T10:00:00+01:00", "doc_changed": false, "posts": [],
                                  "unsectioned": []]
        if let anchor {
            row["anchor"] = ["exact": anchor.exact, "prefix": anchor.prefix, "suffix": anchor.suffix,
                             "start": anchor.start]
        }
        return try SHAFTJSON.decoder().decode(CommentThread.self, from: JSONSerialization.data(withJSONObject: row))
    }

    private func check(_ source: String, _ exact: String) throws -> String {
        let out = CommentHighlight.marked(source, threads: [try thread("c1", exact, in: source)])
        #expect(RenderedText.plain(out) == RenderedText.plain(source), "text changed: \(out)")
        return out
    }

    @Test func aPassageInAParagraphIsMarked() throws {
        let out = try check("# Title\n\nNet totals are rounded once, at the end.\n", "rounded once")
        #expect(out == "# Title\n\nNet totals are [~~rounded once~~](shaft-comment:c1), at the end.\n")
    }

    @Test func aPassageInATableCellIsMarked() throws {
        let source = "| Account | Total |\n|---|---:|\n| Operating | 12,401.19 |\n| Reserve | 3,050.00 |\n"
        let out = try check(source, "12,401.19")
        #expect(out.contains("| [~~12,401.19~~](shaft-comment:c1) |"))
        #expect(out.contains("|---|---:|"))
    }

    @Test func aPassageAcrossTableCellsMarksEachCell() throws {
        let source = "| A | B |\n|---|---|\n| one | two |\n"
        let rendered = RenderedText.plain(source) as NSString
        let range = rendered.range(of: "one\ntwo")
        #expect(range.location != NSNotFound)
        let anchor = try #require(Anchoring.anchor(in: rendered as String, range: range))
        let out = CommentHighlight.marked(source, threads: [try make("c1", anchor: anchor)])
        #expect(out.contains("[~~one~~](shaft-comment:c1) | [~~two~~](shaft-comment:c1)"))
    }

    @Test func aPassageInAListItemIsMarked() throws {
        let out = try check("- [x] reconcile the ledger\n- [ ] publish it\n1. first thing\n", "publish")
        #expect(out.contains("- [ ] [~~publish~~](shaft-comment:c1) it"))
        let numbered = try check("1. first thing\n2. second thing\n", "second")
        #expect(numbered.contains("2. [~~second~~](shaft-comment:c1) thing"))
    }

    @Test func anchorsAcrossNormalizedAgentBulletsAndPointsStillMarkTheirText() throws {
        let bullets = try check("AGENDA\n• first\n• second", "first\nsecond")
        #expect(bullets.contains("- [~~first~~](shaft-comment:c1)"))
        #expect(bullets.contains("- [~~second~~](shaft-comment:c1)"))
        let points = try check("Lead: a · b · c", "Lead:\na\nb\nc")
        #expect(points.contains("[~~Lead:~~](shaft-comment:c1)"))
        for point in ["a", "b", "c"] {
            #expect(points.contains("- [~~\(point)~~](shaft-comment:c1)"))
        }
        let literal = try check("A literal • inside a sentence.", "literal • inside")
        #expect(literal.contains("[~~literal • inside~~](shaft-comment:c1)"))
    }

    @Test func aPassageAcrossBoldStaysBalanced() throws {
        let out = try check("A word **in bold** and after.\n", "word in bold and")
        #expect(out == "A [~~word&#32;~~](shaft-comment:c1)**[~~in bold~~](shaft-comment:c1)**[~~&#32;and~~](shaft-comment:c1) after.\n")
    }

    @Test func aPassageEndingInsideBoldStaysBalanced() throws {
        let out = try check("Plain **bold words** end.\n", "Plain bold")
        #expect(RenderedText.plain(out) == "Plain bold words end.")
    }

    @Test func aPassageAcrossASoftBreakIsMarked() throws {
        let source = "one two\nthree four\n"
        let out = try check(source, "two three")
        #expect(out.contains("[~~two~~](shaft-comment:c1)\n[~~three~~](shaft-comment:c1)"))
    }

    @Test func aPassageInInlineCodeMarksTheWholeSpan() throws {
        let out = try check("Run `make test` first.\n", "make")
        #expect(out == "Run [~~`make test`~~](shaft-comment:c1) first.\n")
    }

    @Test func aPassageInAFencedBlockKeepsTheDocumentAsItIs() throws {
        let source = "Before\n\n```python\nprint(\"hi\")\n- not a list\n```\n\nAfter words\n"
        #expect(try check(source, "print") == source)
    }

    @Test func aPassageAfterAFencedBlockStillLandsRight() throws {
        let source = "Before\n\n```python\nprint(\"hi\")\n- not a list\n```\n\nAfter words\n"
        #expect(try check(source, "After").contains("[~~After~~](shaft-comment:c1) words"))
    }

    @Test func aPassageInARealLinkKeepsTheLink() throws {
        let source = "See [the guide](https://example.com/g) now.\n"
        #expect(try check(source, "the guide") == source)
        #expect(try check(source, "now").contains("[the guide](https://example.com/g) [~~now~~](shaft-comment:c1)."))
    }

    @Test func aPassageAfterALinkWhoseAddressHoldsItsLettersLandsRight() throws {
        let source = "A [x](https://h.example/hello) hello there\n"
        #expect(try check(source, "hello") .contains("/hello) [~~hello~~](shaft-comment:c1) there"))
    }

    @Test func entitiesAndEscapesStayWhole() throws {
        let out = try check("Fish &amp; chips \\* 2 are &lt;good&gt; today.\n", "good")
        #expect(out.contains("[~~good~~](shaft-comment:c1)"))
        _ = try check("Fish &amp; chips here.\n", "Fish & chips")
        _ = try check("Literal \\[one\\] here.\n", "[one]")
    }

    @Test func aRepeatedWordMarksTheOneCommentedOn() throws {
        let source = "alpha beta\n\nalpha gamma\n"
        let rendered = RenderedText.plain(source) as NSString
        let second = rendered.range(of: "alpha", range: NSRange(location: 3, length: rendered.length - 3))
        let anchor = try #require(Anchoring.anchor(in: rendered as String, range: second))
        let out = CommentHighlight.marked(source, threads: [try make("c1", anchor: anchor)])
        #expect(out == "alpha beta\n\n[~~alpha~~](shaft-comment:c1) gamma\n")
    }

    @Test func overlappingThreadsOpenBoth() throws {
        let source = "One two three.\n"
        let out = CommentHighlight.marked(source, threads: [try thread("c1", "One two", in: source),
                                                            try thread("c2", "two three", in: source)])
        #expect(RenderedText.plain(out) == RenderedText.plain(source))
        #expect(out.contains("shaft-comment:c1,c2") || out.contains("c1%2Cc2"))
    }

    @Test func aMissingOrResolvedOrWholeDocumentThreadMarksNothing() throws {
        let source = "One two three.\n"
        let gone = CommentAnchor(exact: "no such text", prefix: "", suffix: "", start: 0)
        #expect(CommentHighlight.marked(source, threads: [try make("c1", anchor: gone)]) == source)
        #expect(CommentHighlight.marked(source, threads: [try make("c1", anchor: nil)]) == source)
        #expect(CommentHighlight.marked(source, threads: [try thread("c1", "two", in: source, state: "resolved")]) == source)
    }

    @Test func oneUnmarkablePassageDoesNotCostTheOthers() throws {
        let source = "Word one.\n\n    indented - code here\n\nWord two.\n"
        let out = CommentHighlight.marked(source, threads: [try thread("c1", "indented", in: source),
                                                            try thread("c2", "two", in: source)])
        #expect(RenderedText.plain(out) == RenderedText.plain(source))
        #expect(out.contains("[~~two~~](shaft-comment:c2)"))
    }

    @Test func aRealStrikethroughStays() throws {
        let out = try check("Keep ~~old~~ and new.\n", "new")
        #expect(out.contains("~~old~~ and [~~new~~](shaft-comment:c1)"))
    }

    @Test func aTappedPassageNamesItsThreads() {
        #expect(CommentHighlight.threads(in: URL(string: "shaft-comment:c1,c12")) == ["c1", "c12"])
        #expect(CommentHighlight.threads(in: URL(string: "https://example.com/c1")) == nil)
        #expect(CommentHighlight.threads(in: nil) == nil)
    }
}
