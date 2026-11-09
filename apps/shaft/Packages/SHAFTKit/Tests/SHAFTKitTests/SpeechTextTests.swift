import Foundation
import Testing
@testable import SHAFTKit

// What is spoken (voice M2, lane A). Every text here is invented. The central property: a reply fed in pieces cut
// anywhere gives exactly the sentences it gives fed whole.

/// A small deterministic generator, so a failing seed can be replayed.
private struct SeededRandom: RandomNumberGenerator {
    var state: UInt64
    init(seed: UInt64) { state = seed &* 0x9E37_79B9_7F4A_7C15 &+ 1 }
    mutating func next() -> UInt64 {
        state = state &* 6364136223846793005 &+ 1442695040888963407
        var x = state
        x ^= x >> 33
        x = x &* 0xff51afd7ed558ccd
        x ^= x >> 33
        return x
    }
}

private func whole(_ text: String, _ language: VoiceLanguage = .en) -> [String] {
    var chunker = SpeechChunker(language: language)
    return chunker.feed(text) + chunker.finish()
}

/// `text` cut into pieces of 1...`maxPiece` unicode scalars (so cuts fall inside words, markers, links, even inside an
/// emoji), fed one by one.
private func pieces(_ text: String, seed: UInt64, maxPiece: Int, _ language: VoiceLanguage = .en) -> [String] {
    var random = SeededRandom(seed: seed)
    var chunker = SpeechChunker(language: language)
    var out: [String] = []
    let scalars = Array(text.unicodeScalars)
    var index = 0
    while index < scalars.count {
        let size = min(Int.random(in: 1...maxPiece, using: &random), scalars.count - index)
        var piece = String.UnicodeScalarView()
        piece.append(contentsOf: scalars[index..<(index + size)])
        out += chunker.feed(String(piece))
        index += size
    }
    return out + chunker.finish()
}

private func render(_ markdown: String, _ language: VoiceLanguage = .en) -> String {
    SpokenText.render(markdown, language: language)
}

// MARK: - Invented replies shaped like real ones

private let englishReply = """
# Weekly summary

I checked the *three* open items, and **two** of them are done. The third, per Dr. Rossi, needs a decision (e.g. a \
budget of 3.5 million, i.e. about v0.44.0 scale) before Friday. Is that fine with you? I think so!

Here is what changed:

- Updated `notes/docs/plan.md` and the config at /etc/shaft/app.toml
- Read [the design page](https://example.com/design/page?x=1) and https://status.example.org/health.
  - Nested point about 20:56 sync
  - Another nested point; it has two parts
- Dropped the old script

1. First step is to wait
2. Second step: check the log
3. Third step.

| Name | Score |
|------|-------|
| Ada  | 3.5   |
| Bo   | 4.1   |

After the table comes a sentence. Then some code:

```swift
let a = 1. 2. 3.
print("not a sentence. at all?")
```

She said "I... don't know, really" and left. Wait… really? Done 🎉 and ✅ shipped.

> A quoted line that goes on.
> And another one.

Final line without an end
"""

private let italianReply = """
## Riepilogo della settimana

Ho controllato le *tre* voci aperte, e **due** sono chiuse. La terza, secondo il Sig. Bianchi, richiede una decisione \
(p.es. un budget di 3,5 milioni, ecc.) entro venerdì. Va bene così? Penso di sì!

Ecco cosa è cambiato:

- Aggiornato `notes/docs/piano.md` e il file /etc/shaft/app.toml
- Letta [la pagina di design](https://esempio.it/design) e https://stato.esempio.it/salute.
- Rimosso il vecchio script

1. Primo passo: aspettare
2. Secondo passo, controllare il log

| Nome | Punteggio |
|------|-----------|
| Ada  | 3,5       |

Dopo la tabella arriva una frase. Poi del codice:

~~~python
x = 1.5
~~~

Ha detto "non lo so... forse" e se n'è andata. Fatto 🎉 consegnato.
"""

// MARK: - Same sentences however the reply is cut

@Suite struct SpeechChunkerInvarianceTests {
    @Test(arguments: [englishReply, italianReply])
    func theSameReplyInRandomPiecesGivesTheSentencesItGivesWhole(_ reply: String) {
        let language: VoiceLanguage = reply == italianReply ? .it : .en
        let expected = whole(reply, language)
        #expect(expected.count > 10)
        for seed in UInt64(1)...40 {
            for maxPiece in [1, 2, 3, 5, 9, 17, 40] {
                let got = pieces(reply, seed: seed, maxPiece: maxPiece, language)
                #expect(got == expected, "seed \(seed), pieces up to \(maxPiece) characters")
            }
        }
    }

    @Test func oneCharacterAtATimeGivesTheWholeReplysSentences() {
        for (reply, language) in [(englishReply, VoiceLanguage.en), (italianReply, .it)] {
            var chunker = SpeechChunker(language: language)
            var got: [String] = []
            for scalar in reply.unicodeScalars { got += chunker.feed(String(scalar)) }
            got += chunker.finish()
            #expect(got == whole(reply, language))
        }
    }

    @Test func aReplyWithWindowsLineBreaksCutBetweenTheReturnAndTheNewlineGivesTheSameSentences() {
        let unix = "First line:\n- one\n- two\n\nThen a closing sentence. Last."
        let windows = unix.replacingOccurrences(of: "\n", with: "\r\n")
        #expect(whole(windows) == whole(unix))
        for seed in UInt64(1)...20 { #expect(pieces(windows, seed: seed, maxPiece: 3) == whole(unix)) }
    }

    @Test func aLongRunOnSentenceIsCutIdenticallyWholeAndInPieces() {
        let clause = "the update touched the scheduler and the mail reader and the notes index"
        let text = Array(repeating: clause, count: 8).joined(separator: ", ") + ". Short end."
        let expected = whole(text)
        for seed in UInt64(1)...20 { #expect(pieces(text, seed: seed, maxPiece: 7) == expected) }
    }
}

// MARK: - Sentence ends

@Suite struct SpeechChunkerSentenceTests {
    @Test func aSentenceIsSpokenOnlyOnceItIsCompleteAndTheNextCharacterConfirmsIt() {
        var chunker = SpeechChunker(language: .en)
        #expect(chunker.feed("Hello th").isEmpty)
        #expect(chunker.feed("ere").isEmpty)
        #expect(chunker.feed(".").isEmpty)                  // "3." could still become "3.5": it waits
        #expect(chunker.feed(" How are") == ["Hello there."])
        #expect(chunker.feed(" you? Fine") == ["How are you?"])
        #expect(chunker.finish() == ["Fine"])
    }

    @Test func stopsQuestionsExclamationsAndEllipsesEndSentences() {
        #expect(whole("One. Two? Three! Four… Five.") == ["One.", "Two?", "Three!", "Four…", "Five."])
        #expect(whole("Wait... then go. Now?! Yes.") == ["Wait... then go.", "Now?!", "Yes."])   // a dot-dot-dot before a lowercase word trails off
    }

    @Test func aColonBeforeALineBreakEndsTheSentenceAndABlankLineEndsTheParagraph() {
        #expect(whole("Here is the list:\nfirst thing\n\nNext paragraph without a stop\n\nLast") ==
                ["Here is the list:", "first thing", "Next paragraph without a stop", "Last"])
    }

    @Test func aTimeIsNotASentenceEndButAColonBeforeALineBreakIs() {
        #expect(whole("We meet at 20:56 today, ok. Note:\nbring it") == ["We meet at 20:56 today, ok.", "Note:", "bring it"])
    }

    @Test func decimalsVersionsFileNamesAndUrlsAreNotSentenceEnds() {
        #expect(whole("It costs 3.5 euros. Version v0.44.0 is out. See notes/plan.md for it. Open https://example.com/a.b?x=1&y=2 now.") ==
                ["It costs 3.5 euros.", "Version v0.44.0 is out.", "See notes/plan.md for it.", "Open https://example.com/a.b?x=1&y=2 now."]
                    .map { SpokenText.render($0, language: .en) })
    }

    @Test(arguments: [
        "Use a tool, e.g. the scheduler, for that.",
        "That is, i.e. the plan, ready.",
        "We need milk, eggs, etc. before noon.",
        "Ask Dr. Rossi about it.",
        "Mr. Smith agreed.",
        "Fast vs. slow is the question.",
    ])
    func englishAbbreviationsDoNotEndASentence(_ text: String) {
        #expect(whole(text).count == 1, "\(text)")
    }

    @Test(arguments: [
        "Servono latte, uova, ecc. prima di sera.",
        "Usa uno strumento, es. lo scheduler, per questo.",
        "Usa uno strumento, p.es. lo scheduler, per questo.",
        "Chiedi al Sig. Bianchi di questo.",
    ])
    func italianAbbreviationsDoNotEndASentence(_ text: String) {
        #expect(whole(text, .it).count == 1, "\(text)")
    }

    @Test func anEllipsisInsideAQuoteDoesNotEndTheSentenceButOneAtTheEndDoes() {
        #expect(whole("She said \"I... don't know\" and left. Then he came back...  The end.") ==
                ["She said \"I... don't know\" and left.", "Then he came back...", "The end."])
    }

    @Test func aClosingQuoteOrBracketStaysWithItsSentence() {
        #expect(whole("He said \"go now.\" She went (quickly!) Then stopped.") ==
                ["He said \"go now.\"", "She went (quickly!)", "Then stopped."])
    }

    @Test func aSentenceBrokenOverTwoLinesIsOneSentence() {
        #expect(whole("The reply goes on\nto a second line. Next.") == ["The reply goes on to a second line.", "Next."])
    }

    @Test func aLongRunOnIsCutAtACommaAfterTwoHundredAndTwentyCharacters() {
        let clause = "and then the scheduler looked at the mail reader"
        let text = Array(repeating: clause, count: 6).joined(separator: ", ") + " and so it ended."
        let sentences = whole(text)
        #expect(sentences.count == 2)
        #expect(sentences[0].hasSuffix(","))
        #expect(sentences[0].count >= 220)
        #expect(sentences[0].count < 220 + clause.count + 2)
        #expect(sentences.joined(separator: " ").contains("so it ended."))
    }

    @Test func aShortSentenceWithManyCommasIsNotCut() {
        #expect(whole("One, two, three, four, five, six, seven.") == ["One, two, three, four, five, six, seven."])
    }

    @Test func finishEmitsWhatIsOpenAndLeavesTheChunkerFresh() {
        var chunker = SpeechChunker(language: .en)
        #expect(chunker.feed("An open **sentence with no end").isEmpty)
        #expect(chunker.finish() == ["An open sentence with no end"])
        #expect(chunker.finish().isEmpty)
        #expect(chunker.feed("Again. ") == ["Again."])
    }

    @Test func nothingToSayGivesNoSentences() {
        #expect(whole("").isEmpty)
        #expect(whole("\n\n  \n").isEmpty)
        #expect(whole("---\n\n***\n").isEmpty)
        #expect(whole("🎉 ✅\n").isEmpty)
        #expect(whole("## \n-\n").isEmpty)
    }
}

// MARK: - Blocks

@Suite struct SpeechChunkerBlockTests {
    @Test func headingsAndListItemsEachEndAtTheirLineBreak() {
        #expect(whole("# Plan\n\nWe start:\n- first item\n- second item, with a comma\n+ third\n") ==
                ["Plan", "We start:", "first item", "second item, with a comma", "third"])
    }

    @Test func numbersOfAnOrderedListAreKeptAndBulletsDropped() {
        #expect(whole("1. Apples\n2. Pears. Ripe ones\n10) Plums\n- Nuts") ==
                ["1. Apples", "2. Pears.", "Ripe ones", "10. Plums", "Nuts"])
    }

    @Test func nestedListItemsAreReadInOrder() {
        #expect(whole("- Outer one\n  - Inner a\n    - Deep b\n- Outer two") == ["Outer one", "Inner a", "Deep b", "Outer two"])
    }

    @Test func aNumberAtTheStartOfALineThatIsNotAListItemIsKept() {
        #expect(whole("3.5 million were sold. 2024 was good.\n") == ["3.5 million were sold.", "2024 was good."])
    }

    @Test func aFencedBlockBecomesOneAnnouncementAndNeverItsContent() {
        let code = "Look:\n```python\nx = 1. 2.\nprint('hi. there')\n```\nDone here."
        #expect(whole(code) == ["Look:", "There's some code in the chat.", "Done here."])
        #expect(whole(code.replacingOccurrences(of: "```python", with: "~~~").replacingOccurrences(of: "\n```", with: "\n~~~"),
                      .it).first == "Look:")
        #expect(whole("```\nx\n```", .it) == ["C'è del codice nella chat."])
    }

    @Test func aFenceIsHeldUntilItClosesAndAFourTickFenceCanHoldThreeTicks() {
        var chunker = SpeechChunker(language: .en)
        #expect(chunker.feed("````\nsome ").isEmpty)
        #expect(chunker.feed("```\nstill inside\n").isEmpty)
        #expect(chunker.feed("````\n") == ["There's some code in the chat."])   // the closing line is whole: it closes
        #expect(chunker.feed("After.").isEmpty)
        #expect(chunker.finish() == ["After."])
    }

    @Test func anUnclosedFenceIsAnnouncedWhenTheReplyEnds() {
        var chunker = SpeechChunker(language: .en)
        #expect(chunker.feed("```js\nlet a = 1\nlet b = 2\n").isEmpty)
        #expect(chunker.finish() == ["There's some code in the chat."])
    }

    @Test func aTableIsHeldWholeAndAnnouncedOnce() {
        let table = "| A | B |\n|---|---|\n| 1.5 | x. y |\n| 2 | z |\n"
        #expect(whole("Before the table.\n\n" + table + "\nAfter it.") ==
                ["Before the table.", "There's a table in the chat.", "After it."])
        #expect(whole(table, .it) == ["C'è una tabella nella chat."])
        var chunker = SpeechChunker(language: .en)
        #expect(chunker.feed("| A | B |\n|---|---|\n| 1 |").isEmpty)
        #expect(chunker.feed(" 2 |\n").isEmpty)           // another row may follow
        #expect(chunker.feed("Text after.") == ["There's a table in the chat."])
    }

    @Test func twoTablesAndABlockEachGetTheirOwnAnnouncement() {
        let text = "| a |\n|---|\n| 1 |\n\n| b |\n|---|\n| 2 |\n\n```\nx\n```\n"
        #expect(whole(text) == ["There's a table in the chat.", "There's a table in the chat.", "There's some code in the chat."])
    }

    @Test func aTableOrFenceCutAtEveryPositionGivesTheSameAnnouncements() {
        let text = "Intro text.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode. here\n```\n\nOutro."
        let expected = whole(text)
        #expect(expected == ["Intro text.", "There's a table in the chat.", "There's some code in the chat.", "Outro."])
        let scalars = Array(text.unicodeScalars)
        for cut in 0...scalars.count {
            var chunker = SpeechChunker(language: .en)
            var first = String.UnicodeScalarView(); first.append(contentsOf: scalars[..<cut])
            var second = String.UnicodeScalarView(); second.append(contentsOf: scalars[cut...])
            let got = chunker.feed(String(first)) + chunker.feed(String(second)) + chunker.finish()
            #expect(got == expected, "cut at \(cut)")
        }
    }

    @Test func aQuoteIsReadWithoutItsMarks() {
        #expect(whole("> First quoted line.\n> Second one goes on\n> and on.\nAfter.") ==
                ["First quoted line.", "Second one goes on and on.", "After."])
    }

    @Test func aPlainLineWithAHashOrDashInsideIsText() {
        #expect(whole("Issue #5 is open - see below. Use a-b and c--d.") == ["Issue #5 is open - see below.", "Use a-b and c--d."])
    }

    @Test func aStrayBacktickOrBracketDoesNotHoldTheSpeechBack() {
        #expect(whole("A stray ` tick here. Then [one bracket there. End.") ==
                ["A stray tick here.", "Then one bracket there.", "End."])
    }
}

// MARK: - Markdown to speech

@Suite struct SpokenTextTests {
    @Test func emphasisHeadingAndQuoteMarksAreDropped() {
        #expect(render("## A **bold** and *slanted* and ~~struck~~ __word__ _end_") == "A bold and slanted and struck word end.")
        #expect(render("> quoted words") == "quoted words.")
        #expect(render("snake_case_name stays; 2 * 3 too") == "snake_case_name stays; 2 3 too.")
    }

    @Test func listItemsReadInOrderNumbersKeptBulletsDropped() {
        #expect(render("1. Apples\n2. Pears\n- Nuts\n  * Walnuts") == "1. Apples. 2. Pears. Nuts. Walnuts.")
        #expect(render("- [x] Done thing\n- [ ] Open thing") == "Done thing. Open thing.")
    }

    @Test func aLinkIsReadByItsTextAndAnImageByItsDescription() {
        #expect(render("Read [the design page](https://example.com/p) now.") == "Read the design page now.")
        #expect(render("![a chart](https://example.com/c.png) shows it.") == "a chart shows it.")
        #expect(render("See [docs (v2)](https://example.com/a_(b)) too.") == "See docs (v2) too.")
        #expect(render("Plain [brackets] stay words.") == "Plain brackets stay words.")
    }

    @Test func aBareUrlIsReadAsItsHost() {
        #expect(render("Open https://status.example.org/health?x=1 now.") == "Open a link to status.example.org now.")
        #expect(render("Go to www.example.com.") == "Go to a link to www.example.com.")
        #expect(render("Apri https://Esempio.it:8080/a/b.", .it) == "Apri un link a esempio.it.")
        #expect(render("<https://example.com/x>") == "a link to example.com.")
        #expect(render("[https://example.com/x](https://example.com/x)") == "a link to example.com.")
    }

    @Test func aPathIsReadByItsFileNameButOrdinarySlashesStay() {
        #expect(render("See notes/docs/plan.md for it.") == "See plan.md for it.")
        #expect(render("The file /etc/shaft/app.toml, and ~/notes/today.md.") == "The file app.toml, and today.md.")
        #expect(render("Use and/or, TCP/IP, 24/7 or 12/10/2026.") == "Use and/or, TCP/IP, 24/7 or 12/10/2026.")
        #expect(render("It runs at v0.44.0 or 3.5.") == "It runs at v0.44.0 or 3.5.")
    }

    @Test func shortInlineCodeIsReadAsItsTextAndLongInlineCodeIsAnnounced() {
        #expect(render("Run `shaft status` first.") == "Run shaft status first.")
        #expect(render("Open `notes/docs/plan.md` first.") == "Open plan.md first.")
        let long = "`" + String(repeating: "x", count: 41) + "`"
        #expect(render("Try " + long + " now.") == "Try some code now.")
        #expect(render("Prova " + long + " ora.", .it) == "Prova del codice ora.")
        #expect(render("Use ``a ` b`` there.") == "Use a ` b there.")
    }

    @Test func aTableIsOneAnnouncementAndACodeBlockIsOne() {
        #expect(render("| a | b |\n|---|---|\n| 1 | 2 |") == "There's a table in the chat.")
        #expect(render("| a | b |\n|---|---|\n| 1 | 2 |", .it) == "C'è una tabella nella chat.")
        #expect(render("```\ncode\nmore code\n```") == "There's some code in the chat.")
        #expect(render("```\ncode\n```", .it) == "C'è del codice nella chat.")
    }

    @Test func emojiAreDropped() {
        #expect(render("Shipped 🎉 and tested ✅ with 👍🏽 ok 1️⃣ done ⚠️") == "Shipped and tested with ok done.")
    }

    @Test func blocksAreSeparatedAndGetAPauseWhenTheyHaveNoStopOfTheirOwn() {
        #expect(render("# Title\n\nBody text here. More?\n\n- one\n- two") == "Title. Body text here. More? one. two.")
    }

    @Test func nothingToSayRendersAsEmpty() {
        #expect(render("") == "")
        #expect(render("   \n\n") == "")
        #expect(render("---") == "")
        #expect(render("**") == "")
        #expect(render("🎉") == "")
        #expect(render("![](https://example.com/a.png)") == "")
    }

    @Test func escapedMarksAreKeptAsText() {
        #expect(render(#"A \*star\* and a \_dash\_."#) == "A *star* and a _dash_.")
    }

    @Test func aHtmlTagIsDroppedAndALoneLessThanStays() {
        #expect(render("Line<br>break and <b>bold</b> or 2 < 3.") == "Line break and bold or 2 < 3.")
    }
}

// MARK: - Whole replies

@Suite struct SpeechChunkerRealisticReplyTests {
    @Test func theEnglishReplyIsSpokenSentenceBySentence() {
        let expected = [
            "Weekly summary",
            "I checked the three open items, and two of them are done.",
            "The third, per Dr. Rossi, needs a decision (e.g. a budget of 3.5 million, i.e. about v0.44.0 scale) before Friday.",
            "Is that fine with you?",
            "I think so!",
            "Here is what changed:",
            "Updated plan.md and the config at app.toml",
            "Read the design page and a link to status.example.org.",
            "Nested point about 20:56 sync",
            "Another nested point; it has two parts",
            "Dropped the old script",
            "1. First step is to wait",
            "2. Second step: check the log",
            "3. Third step.",
            "There's a table in the chat.",
            "After the table comes a sentence.",
            "Then some code:",
            "There's some code in the chat.",
            "She said \"I... don't know, really\" and left.",
            "Wait… really?",
            "Done and shipped.",
            "A quoted line that goes on.",
            "And another one.",
            "Final line without an end",
        ]
        #expect(whole(englishReply) == expected)
    }

    @Test func theItalianReplyIsSpokenSentenceBySentence() {
        let expected = [
            "Riepilogo della settimana",
            "Ho controllato le tre voci aperte, e due sono chiuse.",
            "La terza, secondo il Sig. Bianchi, richiede una decisione (p.es. un budget di 3,5 milioni, ecc.) entro venerdì.",
            "Va bene così?",
            "Penso di sì!",
            "Ecco cosa è cambiato:",
            "Aggiornato piano.md e il file app.toml",
            "Letta la pagina di design e un link a stato.esempio.it.",
            "Rimosso il vecchio script",
            "1. Primo passo: aspettare",
            "2. Secondo passo, controllare il log",
            "C'è una tabella nella chat.",
            "Dopo la tabella arriva una frase.",
            "Poi del codice:",
            "C'è del codice nella chat.",
            "Ha detto \"non lo so... forse\" e se n'è andata.",
            "Fatto consegnato.",
        ]
        #expect(whole(italianReply, .it) == expected)
    }
}
