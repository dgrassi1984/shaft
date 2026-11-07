import Foundation

/// A file that reads as Markdown: drawn as a document is, where Quick Look would show its source. `.md` and `.markdown`,
/// as the web's documents panel (`web_views.MARKDOWN_TYPES`).
public enum MarkdownFile {
    /// What a document may weigh on the server (`docs.MAX_BYTES`), so what the Markdown view is known to draw. Past it a
    /// file opens as source in Quick Look, which stays quick at any size.
    public static let maxBytes = 256 * 1024

    public static func matches(name: String) -> Bool {
        ["md", "markdown"].contains((name as NSString).pathExtension.lowercased())
    }

    /// The text to draw; nil when the file opens as any other does: not Markdown, past `maxBytes`, not UTF-8 or unreadable.
    public static func text(at url: URL) -> String? {
        guard matches(name: url.lastPathComponent),
              let size = (try? FileManager.default.attributesOfItem(atPath: url.path))?[.size] as? Int,
              size <= maxBytes,
              let data = try? Data(contentsOf: url),
              var text = String(data: data, encoding: .utf8) else { return nil }
        if text.unicodeScalars.first == "\u{FEFF}" { text.unicodeScalars.removeFirst() }
        return text
    }
}
