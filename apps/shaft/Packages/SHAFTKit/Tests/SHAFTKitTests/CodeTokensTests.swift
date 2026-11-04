import Testing
@testable import SHAFTKit

struct CodeTokensTests {
    @Test func aStringWithAHashIsNotAComment() {
        let text = "print(\"a # inside\") # outside"
        let tokens = CodeTokens.tokens(in: text, language: "Python")
        #expect(pieces(text, tokens, .string) == ["\"a # inside\""])
        #expect(pieces(text, tokens, .comment) == ["# outside"])
    }

    @Test func aHashInShellIsAComment() {
        let text = "echo \"# quoted\" \\#literal # comment\necho 'it\\' # after single quote"
        let tokens = CodeTokens.tokens(in: text, language: "bash")
        #expect(pieces(text, tokens, .string) == ["\"# quoted\"", "'it\\'"])
        #expect(pieces(text, tokens, .comment) == ["# comment", "# after single quote"])
    }

    @Test func anUnknownLanguageIsPlain() {
        let text = "let x = \"#\" // 42 🧑🏽‍💻"
        for language: String? in [nil, "unknown", ""] {
            #expect(CodeTokens.tokens(in: text, language: language) == [
                CodeToken(range: text.startIndex..<text.endIndex, kind: .plain),
            ])
        }
    }

    @Test func tokensCoverTheWholeTextInOrder() {
        let samples = [
            ("py", "def café():\n    return \"🧑🏽‍💻 #\" + 42 # fin\n"),
            ("swift", "let café = \"🧑🏽‍💻\" /* outer /* nested */ end */ + 42\r\n"),
            ("js", "const café = `🧑🏽‍💻`; // note\n42"),
            ("ts", "const café: string = '🧑🏽‍💻'; /* note */ 42"),
            ("sh", "if true; then echo '🧑🏽‍💻'; fi # note\n42"),
            ("json", "{\"café\": true, \"emoji\": \"🧑🏽‍💻\", \"n\": 42}"),
            ("yml", "café: true # 🧑🏽‍💻\nn: 42\n"),
            ("sql", "SELECT '🧑🏽‍💻', 42 FROM café -- note\n"),
            ("diff", "@@ -1 +1 @@\n-old 🧑🏽‍💻\n+new café\n context\n"),
        ]
        for (language, text) in samples {
            let tokens = CodeTokens.tokens(in: text, language: language)
            var end = text.startIndex
            for token in tokens {
                #expect(token.range.lowerBound == end)
                #expect(token.range.lowerBound < token.range.upperBound)
                end = token.range.upperBound
            }
            #expect(end == text.endIndex)
            #expect(tokens.map { String(text[$0.range]) }.joined() == text)
            #expect(tokens.contains { $0.kind != .plain })
        }
    }

    @Test func anUnterminatedStringEndsAtTheEnd() {
        for (language, text) in [
            ("py", "x = \"unterminated # still a string"),
            ("swift", "let x = \"\"\"multiline\n# still a string"),
            ("js", "const x = `unterminated\n// still a string"),
            ("sh", "echo 'unterminated # still a string"),
            ("json", "{\"key\": \"unterminated"),
            ("yml", "key: 'unterminated"),
            ("sql", "SELECT 'unterminated -- still a string"),
        ] {
            let tokens = CodeTokens.tokens(in: text, language: language)
            #expect(tokens.last?.kind == .string)
            #expect(tokens.last?.range.upperBound == text.endIndex)
            #expect(!tokens.contains { $0.kind == .comment })
        }
    }

    @Test func emptyTextHasNoTokens() {
        for language: String? in [nil, "unknown", "py", "swift", "js", "sh", "json", "yml", "sql", "diff"] {
            #expect(CodeTokens.tokens(in: "", language: language).isEmpty)
        }
    }

    @Test func aLongBlockStaysLinear() {
        let line = "const answer = \"# string\"; /* comment */ 42;\n"
        let text = String(repeating: line, count: 200_000 / line.utf8.count + 1)
        let clock = ContinuousClock()
        let start = clock.now
        let tokens = CodeTokens.tokens(in: text, language: "ts")
        let elapsed = start.duration(to: clock.now)
        #expect(text.utf8.count >= 200_000)
        #expect(tokens.last?.range.upperBound == text.endIndex)
        #expect(elapsed < .milliseconds(750))
        print("CodeTokens: \(text.utf8.count) bytes in \(elapsed)")
    }

    @Test func aliasesAndCaseAreRecognised() {
        for tags in [
            ["python", "py", "python3", "py3"], ["swift"],
            ["javascript", "js", "jsx", "typescript", "ts", "tsx"],
            ["shell", "sh", "bash", "zsh", "shellscript"],
            ["json"], ["yaml", "yml"], ["sql"], ["diff", "patch"],
        ] {
            let text = "if true 42 \"text\" # comment\n+added\n"
            let expected = CodeTokens.tokens(in: text, language: tags[0])
            for tag in tags {
                #expect(CodeTokens.tokens(in: text, language: tag.uppercased()) == expected)
            }
        }
    }

    @Test func recognisedSyntaxHasExpectedKinds() {
        for (language, text, keywords, strings, comments, numbers) in [
            ("py", "return \"a\\\"#b\" + 1.5e-2 # end", ["return"], ["\"a\\\"#b\""], ["# end"], ["1.5e-2"]),
            ("swift", "let s = \"\"\"a\nb\"\"\" /* a /* b */ c */ 0xFF", ["let"], ["\"\"\"a\nb\"\"\""], ["/* a /* b */ c */"], ["0xFF"]),
            ("ts", "const s = `a\nb`; /* c */ 42", ["const"], ["`a\nb`"], ["/* c */"], ["42"]),
            ("json", "{\"n\": null, \"b\": true, \"v\": -42}", ["null", "true"], ["\"n\"", "\"b\"", "\"v\""], [], ["42"]),
            ("yaml", "value: 'it''s # text' # c\nn: 42", [], ["'it''s # text'"], ["# c"], ["42"]),
            ("sql", "SELECT 'it''s -- text', 42 FROM t -- c", ["SELECT", "FROM"], ["'it''s -- text'"], ["-- c"], ["42"]),
            ("diff", "@@ header\n-old\n+new\n context", ["-old\n"], ["+new\n"], ["@@ header\n"], []),
        ] {
            let tokens = CodeTokens.tokens(in: text, language: language)
            #expect(pieces(text, tokens, .keyword) == keywords)
            #expect(pieces(text, tokens, .string) == strings)
            #expect(pieces(text, tokens, .comment) == comments)
            #expect(pieces(text, tokens, .number) == numbers)
        }
    }

    private func pieces(_ text: String, _ tokens: [CodeToken], _ kind: CodeTokenKind) -> [String] {
        tokens.filter { $0.kind == kind }.map { String(text[$0.range]) }
    }
}
