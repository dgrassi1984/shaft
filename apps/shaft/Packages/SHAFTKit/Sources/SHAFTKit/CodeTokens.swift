public enum CodeTokenKind: Sendable {
    case keyword, string, comment, number, plain
}

public struct CodeToken: Sendable, Equatable {
    public let range: Range<String.Index>
    public let kind: CodeTokenKind
}

public enum CodeTokens {
    public static func tokens(in text: String, language: String?) -> [CodeToken] {
        guard !text.isEmpty else { return [] }
        guard let language, let syntax = Syntax(tag: language.lowercased()) else {
            return [CodeToken(range: text.startIndex..<text.endIndex, kind: .plain)]
        }
        let keywords = syntax.keywords
        var result: [CodeToken] = []
        var index = text.startIndex

        func starts(_ prefix: String) -> Bool { text[index...].hasPrefix(prefix) }
        func advance() { text.formIndex(after: &index) }
        func emit(from start: String.Index, kind: CodeTokenKind) {
            if let last = result.last, last.kind == kind {
                result[result.count - 1] = CodeToken(range: last.range.lowerBound..<index, kind: kind)
            } else {
                result.append(CodeToken(range: start..<index, kind: kind))
            }
        }

        while index < text.endIndex {
            let start = index
            let character = text[index]
            var kind: CodeTokenKind = .plain
            if syntax == .diff {
                if character == "+" { kind = .string }
                else if character == "-" { kind = .keyword }
                else if starts("@@") { kind = .comment }
                while index < text.endIndex, !text[index].isNewline { advance() }
                if index < text.endIndex { advance() }
            } else if (syntax.hashComments && character == "#")
                || (syntax.slashComments && starts("//"))
                || (syntax == .sql && starts("--")) {
                kind = .comment
                while index < text.endIndex, !text[index].isNewline { advance() }
            } else if syntax.blockComments && starts("/*") {
                kind = .comment
                advance()
                advance()
                var depth = 1
                while index < text.endIndex {
                    if starts("*/") {
                        advance()
                        advance()
                        depth -= 1
                        if depth == 0 { break }
                    } else if syntax == .swift && starts("/*") {
                        advance()
                        advance()
                        depth += 1
                    } else {
                        advance()
                    }
                }
            } else if character == "\"" || (character == "'" && syntax != .json)
                || (character == "`" && syntax == .javascript) {
                kind = .string
                let triple = (syntax == .python || syntax == .swift)
                    && starts(String(repeating: String(character), count: 3))
                let delimiter = String(repeating: String(character), count: triple ? 3 : 1)
                for _ in delimiter { advance() }
                while index < text.endIndex {
                    if starts(delimiter) {
                        for _ in delimiter { advance() }
                        // SQL and YAML escape quotes by doubling them.
                        if (syntax == .sql || syntax == .yaml), starts(delimiter) {
                            for _ in delimiter { advance() }
                            continue
                        }
                        break
                    }
                    let escapes = !(syntax == .shell && character == "'")
                        && !(syntax == .yaml && character == "'") && syntax != .sql
                    if escapes && text[index] == "\\" {
                        advance()
                        if index < text.endIndex { advance() }
                    } else {
                        advance()
                    }
                }
            } else if character.isASCIIDigit {
                kind = .number
                if starts("0x") || starts("0X") || starts("0b") || starts("0B")
                    || starts("0o") || starts("0O") {
                    advance()
                    advance()
                    while index < text.endIndex,
                          text[index].isHexDigit || text[index] == "_" { advance() }
                } else {
                    while index < text.endIndex,
                          text[index].isASCIIDigit || text[index] == "_" { advance() }
                    if index < text.endIndex, text[index] == "." {
                        let next = text.index(after: index)
                        if next < text.endIndex, text[next].isASCIIDigit {
                            advance()
                            while index < text.endIndex,
                                  text[index].isASCIIDigit || text[index] == "_" { advance() }
                        }
                    }
                    if index < text.endIndex, text[index] == "e" || text[index] == "E" {
                        var exponent = text.index(after: index)
                        if exponent < text.endIndex, text[exponent] == "+" || text[exponent] == "-" {
                            text.formIndex(after: &exponent)
                        }
                        if exponent < text.endIndex, text[exponent].isASCIIDigit {
                            index = text.index(after: exponent)
                            while index < text.endIndex, text[index].isASCIIDigit { advance() }
                        }
                    }
                }
            } else if character.isLetter || character == "_" || character == "$" {
                advance()
                while index < text.endIndex {
                    let next = text[index]
                    guard next.isLetter || next.isNumber || next == "_" || next == "$" else { break }
                    advance()
                }
                let word = String(text[start..<index])
                if keywords.contains(syntax == .sql ? word.lowercased() : word) { kind = .keyword }
            } else {
                // An escaped shell hash is literal, even outside quotes.
                if syntax == .shell && character == "\\" {
                    advance()
                    if index < text.endIndex { advance() }
                } else {
                    advance()
                }
            }
            emit(from: start, kind: kind)
        }
        return result
    }

    private enum Syntax {
        case python, swift, javascript, shell, json, yaml, sql, diff

        init?(tag: String) {
            switch tag {
            case "python", "py", "python3", "py3": self = .python
            case "swift": self = .swift
            case "javascript", "js", "jsx", "typescript", "ts", "tsx": self = .javascript
            case "shell", "sh", "bash", "zsh", "shellscript": self = .shell
            case "json": self = .json
            case "yaml", "yml": self = .yaml
            case "sql": self = .sql
            case "diff", "patch": self = .diff
            default: return nil
            }
        }

        var hashComments: Bool { self == .python || self == .shell || self == .yaml }
        var slashComments: Bool { self == .swift || self == .javascript }
        var blockComments: Bool { slashComments || self == .sql }

        var keywords: Set<String> {
            let words: String
            switch self {
            case .python:
                words = "False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case"
            case .swift:
                words = "actor any as associatedtype async await break case catch class continue default defer deinit didSet do else enum extension fallthrough false fileprivate for func guard if import in init inout internal is isolated lazy let mutating nil nonisolated nonmutating open operator override private protocol public repeat required rethrows return self Self sending set some static struct subscript super switch throw throws true try typealias var weak where while willSet"
            case .javascript:
                words = "abstract any as async await boolean break case catch class const constructor continue debugger declare default delete do else enum export extends false finally for from function get if implements import in instanceof interface keyof let namespace never new null number of package private protected public readonly return satisfies set static string super switch symbol this throw true try type typeof undefined unknown var void while with yield"
            case .shell:
                words = "if then else elif fi for while until do done case esac in function select time coproc break continue return exit export local readonly unset source alias unalias"
            case .json:
                words = "true false null"
            case .yaml:
                words = "true false null True False Null TRUE FALSE NULL yes no on off"
            case .sql:
                words = "select from where insert into values update set delete create alter drop table index view join inner outer left right full cross on as and or not null true false is in like between exists distinct all union having group by order asc desc limit offset case when then else end with recursive primary foreign key references default constraint begin commit rollback returning"
            case .diff:
                words = ""
            }
            return Set(words.split(separator: " ").map(String.init))
        }
    }
}

private extension Character {
    var isASCIIDigit: Bool { self >= "0" && self <= "9" }
}
