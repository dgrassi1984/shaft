import Foundation

/// Long pastes travel as text files so the composer stays readable.
public enum PastedText {
    public static let maxInlineCharacters = 2000
    public static let maxInlineLines = 20

    public static func belongsInFile(_ text: String) -> Bool {
        guard !text.allSatisfy(\.isWhitespace) else { return false }
        let segments = text.components(separatedBy: "\n")
        let lines = segments.count - (segments.last == "" ? 1 : 0)
        return text.count > maxInlineCharacters || lines > maxInlineLines
    }

    public static func fileName(at date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = .current
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return "pasted-text-" + formatter.string(from: date)
    }
}
