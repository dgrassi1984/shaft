import Foundation
import Testing
@testable import SHAFTKit

struct AgentMarkdownTests {
    struct Example: Sendable {
        let source: String
        let expected: String

        init(_ source: String, _ expected: String? = nil) {
            self.source = source
            self.expected = expected ?? source
        }
    }

    private static let examples: [String: [Example]] = {
        var groups: [String: [Example]] = [:]
        groups["bulletsAfterAHeadingLineBecomeMarkdownBullets"] = [
            Example("AGENDA\n• a\n• b", "AGENDA\n- a\n- b"),
            Example("•\n• "),
        ]
        do {
            var cases: [Example] = []
            let source = "NEWS\nSoftware\n1. Item one text.\nhttps://example.com/one\n\nResearch\n2. Item two text.\nhttps://example.com/two\n\nTools\n3. Item three text."
            cases.append(Example(source, source
                .replacingOccurrences(of: "Research\n2.", with: "Research\n\n2.")
                .replacingOccurrences(of: "Tools\n3.", with: "Tools\n\n3.")))
            groups["numberedSectionsKeepTheirWrittenSequence"] = cases
        }
        groups["aNumberWithNoPredecessorStaysInItsParagraph"] = [
            Example("In 2026\n3. not a list"),
            Example("In 2026\n3. not a list\n\nResearch\n4. still text"),
        ]
        do {
            var cases: [Example] = []
            for source in ["1. a\n2. b", "5. five\n6. six", "1. a\n   continuation\n2. b", "1. a\nlazy continuation\n2. b"] {
                cases.append(Example(source))
            }
            groups["contiguousNumberedListsStayTightAtAnyStartingNumber"] = cases
        }
        do {
            var cases: [Example] = []
            for fence in ["```", "~~~~"] {
                let source = "\(fence)\n• a\nLead: a · b · c\n1. code\n\(fence)\nResearch\n2. still text"
                cases.append(Example(source))
                cases.append(Example("\(fence)\n• a\nLead: a · b · c"))
            }
            cases.append(Example("````\n```\n• a\n````"))
            groups["fencesShieldBulletsPointsAndNumberedPredecessors"] = cases
        }
        do {
            var cases: [Example] = []
            for source in ["    • code", "    Lead: a · b · c", "| Lead: a · b · c |\n|---|", "    1. code\n\nResearch\n2. text"] {
                cases.append(Example(source))
            }
            groups["indentedCodeAndTableRowsStayUntouched"] = cases
        }
        do {
            var cases: [Example] = []
            cases.append(Example("Lead: a · b · c", "Lead:\n\n- a\n- b\n- c"))
            cases.append(Example("Lead: a | b · c · d", "Lead:\n\n- a | b\n- c\n- d"))
            for source in ["Lead: a · b", "a · b · c", "## Lead: a · b · c"] {
                cases.append(Example(source))
            }
            cases.append(Example("- Lead: a · b · c", "- Lead:\n  - a\n  - b\n  - c"))
            cases.append(Example("  5. Lead: a · b · c", "  5. Lead:\n     - a\n     - b\n     - c"))
            groups["pointsBecomeALeadAndAtLeastThreeBullets"] = cases
        }
        do {
            var cases: [Example] = []
            for point in ["`a · b`", "(a · (b · c))", "[a · b](https://example.com)", "<https://example.com/a>"] {
                cases.append(Example("Lead: \(point) · two · three", "Lead:\n\n- \(point)\n- two\n- three"))
            }
            cases.append(Example("Lead: `a · b · c`"))
            cases.append(Example("Lead: (a · b · c)"))
            cases.append(Example("Lead: [a · b · c](https://example.com)"))
            groups["codeLinksAndNestedAsidesDoNotCutPoints"] = cases
        }
        groups["quotePrefixesStayOnEveryGeneratedLine"] = [
            Example("> AGENDA\n> • a", "> AGENDA\n> - a"),
            Example("> > Lead: a · b · c", "> > Lead:\n> > \n> > - a\n> > - b\n> > - c"),
            Example("> 1. a\n>\n> Research\n> 2. b", "> 1. a\n>\n> Research\n> \n> 2. b"),
            Example("> ```\n> • a\n> ```"),
        ]
        do {
            var cases: [Example] = []
            for source in ["", "Just **text**.\nAnother line.\n", "The date is 2026.", "a • b"] {
                cases.append(Example(source))
            }
            groups["emptyAndOrdinaryTextStayIdentical"] = cases
        }
        groups["expandedPointsOpenAListOnTheFirstPass"] = [
            Example("Lead: a · b · c\n    • x", "Lead:\n\n- a\n- b\n- c\n    - x"),
        ]
        do {
            var cases: [Example] = []
            for source in [
                "- text\n\n    ```\n    • a\n    ```",
                "10. text\n    ```\n    • a\n    ```",
                "1. item\n\n        • bullet in code",
                "- item\n\n        Lead: a · b · c",
                "10. text\n    ```\n    1. code\n    ```\n\nResearch\n2. still text",
                "- item\n\n        1. this is code\n\nLater\n2. real"
            ] {
                cases.append(Example(source))
            }
            groups["fencesAndIndentedCodeUseTheListContentColumn"] = cases
        }
        do {
            var cases: [Example] = []
            for source in [
                "```\n> ```\n• hidden\n```",
                "1. before\n\n```\n> ```\nparagraph\n2. inside\n```",
                "> ```\n> > ```\n> • hidden\n> ```",
                "> > ```\n> > • hidden\n> • visible",
                "> ```\n• visible"
            ] {
                let expected = source.replacingOccurrences(of: "• visible", with: "- visible")
                cases.append(Example(source, expected))
            }
            groups["openFencesUseTheirOpeningQuoteDepth"] = cases
        }
        do {
            var cases: [Example] = []
            for source in ["<div>\n1. stays\n</div>", "Intro\n1. First"] {
                cases.append(Example(source))
            }
            groups["oneAlreadyInterruptsAParagraphWithoutAnInsertedBlank"] = cases
        }
        groups["equalsOnlyFormsASetextUnderlineAfterPlainText"] = [
            Example("1. first\n\n====\n2. second", "1. first\n\n====\n\n2. second"),
            Example("1. first\n\nTitle\n====\n2. second"),
        ]
        groups["carriageReturnsAreNormalizedOnlyWhenScanning"] = [
            Example("• a\r\n• b\r• c", "- a\n- b\n- c"),
            Example("Intro\r\n1. First", "Intro\n1. First"),
            Example("Just text\r\nMore text\r"),
        ]
        groups["leadingTabsCountAsFourIndentColumns"] = [
            Example("\tLead: a · b · c"),
            Example("\t• code"),
        ]
        groups["backticksInTheInfoStringPreventABacktickFence"] = [
            Example("``` foo `bar\n• a", "``` foo `bar\n- a"),
            Example("~~~ foo `bar\n• a\n~~~"),
        ]
        do {
            var cases: [Example] = []
            for source in [
                "[Lead: a · b · c]: https://example.com",
                "[foo]: https://example.com \"title: one · two · three\""
            ] {
                cases.append(Example(source))
            }
            groups["linkReferenceDefinitionsAreNeverPointsLines"] = cases
        }
        groups["pointsThenANumberKeepTheirExistingSeparation"] = [
            Example("1. first\n\nLead: a · b · c\n2. second", "1. first\n\nLead:\n\n- a\n- b\n- c\n2. second"),
        ]
        groups["longPlainRepliesAndNestedAsidesStayCheap"] = [
            Example(String(repeating: "Just a plain paragraph.\n\n", count: 4_000)),
            Example("Lead: " + String(repeating: "(", count: 4_000) + "a · b · c" + String(repeating: ")", count: 4_000))
        ]
        return groups
    }()

    private func check(_ name: String) {
        for example in Self.examples[name]! {
            #expect(AgentMarkdown.normalized(example.source) == example.expected)
        }
    }

    @Test func bulletsAfterAHeadingLineBecomeMarkdownBullets() { check("bulletsAfterAHeadingLineBecomeMarkdownBullets") }
    @Test func numberedSectionsKeepTheirWrittenSequence() { check("numberedSectionsKeepTheirWrittenSequence") }
    @Test func aNumberWithNoPredecessorStaysInItsParagraph() { check("aNumberWithNoPredecessorStaysInItsParagraph") }
    @Test func contiguousNumberedListsStayTightAtAnyStartingNumber() { check("contiguousNumberedListsStayTightAtAnyStartingNumber") }
    @Test func fencesShieldBulletsPointsAndNumberedPredecessors() { check("fencesShieldBulletsPointsAndNumberedPredecessors") }
    @Test func indentedCodeAndTableRowsStayUntouched() { check("indentedCodeAndTableRowsStayUntouched") }
    @Test func pointsBecomeALeadAndAtLeastThreeBullets() { check("pointsBecomeALeadAndAtLeastThreeBullets") }
    @Test func codeLinksAndNestedAsidesDoNotCutPoints() { check("codeLinksAndNestedAsidesDoNotCutPoints") }
    @Test func quotePrefixesStayOnEveryGeneratedLine() { check("quotePrefixesStayOnEveryGeneratedLine") }
    @Test func emptyAndOrdinaryTextStayIdentical() { check("emptyAndOrdinaryTextStayIdentical") }
    @Test func expandedPointsOpenAListOnTheFirstPass() { check("expandedPointsOpenAListOnTheFirstPass") }
    @Test func fencesAndIndentedCodeUseTheListContentColumn() { check("fencesAndIndentedCodeUseTheListContentColumn") }
    @Test func openFencesUseTheirOpeningQuoteDepth() { check("openFencesUseTheirOpeningQuoteDepth") }
    @Test func oneAlreadyInterruptsAParagraphWithoutAnInsertedBlank() { check("oneAlreadyInterruptsAParagraphWithoutAnInsertedBlank") }
    @Test func equalsOnlyFormsASetextUnderlineAfterPlainText() { check("equalsOnlyFormsASetextUnderlineAfterPlainText") }
    @Test func carriageReturnsAreNormalizedOnlyWhenScanning() { check("carriageReturnsAreNormalizedOnlyWhenScanning") }
    @Test func leadingTabsCountAsFourIndentColumns() { check("leadingTabsCountAsFourIndentColumns") }
    @Test func backticksInTheInfoStringPreventABacktickFence() { check("backticksInTheInfoStringPreventABacktickFence") }
    @Test func linkReferenceDefinitionsAreNeverPointsLines() { check("linkReferenceDefinitionsAreNeverPointsLines") }
    @Test func pointsThenANumberKeepTheirExistingSeparation() { check("pointsThenANumberKeepTheirExistingSeparation") }

    @Test func longPlainRepliesAndNestedAsidesStayCheap() {
        let start = ContinuousClock.now
        check("longPlainRepliesAndNestedAsidesStayCheap")
        #expect(ContinuousClock.now - start < .seconds(2))
    }

    @Test func normalizationIsIdempotentForEveryTestInput() {
        for example in Self.examples.values.flatMap({ $0 }) {
            let once = AgentMarkdown.normalized(example.source)
            #expect(AgentMarkdown.normalized(once) == once, "Input: \(example.source)")
        }
    }
}
