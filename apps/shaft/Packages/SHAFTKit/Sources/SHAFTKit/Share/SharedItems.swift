import Foundation

/// One shared representation, after the extension has copied any borrowed file into its own container.
public enum SharedItem: Sendable, Equatable {
    case text(String)
    case url(URL)
    case file(OutgoingFile)
    case unsupported(String)
}

/// The shared text in host order, with the operator's optional line first, and the files sent by the composer.
public struct SharedItems: Sendable, Equatable {
    public let text: String
    public let files: [OutgoingFile]
    public let unsupportedMessages: [String]

    public init(_ items: [SharedItem], note: String = "") {
        var lines = note.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? [] : [note]
        var files: [OutgoingFile] = []
        var unsupported: [String] = []
        for item in items {
            switch item {
            case .text(let text):
                if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { lines.append(text) }
            case .url(let url): lines.append(url.absoluteString)
            case .file(let file): files.append(file)
            case .unsupported(let description): unsupported.append("Could not share \(description).")
            }
        }
        self.text = lines.joined(separator: "\n")
        self.files = files
        unsupportedMessages = unsupported
    }

    public var isSendable: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !files.isEmpty
    }
}
