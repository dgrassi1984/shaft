import Foundation
import Testing

struct DesignSystemTests {
    private var app: URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }

    @Test func textTokensMeetContrast() throws {
        for dark in [false, true] {
            for background in ["canvas", "elevated"] {
                let base = try luminance(background, dark: dark)
                for token in ["textPrimary", "textSecondary", "textTertiary"] {
                    let ink = try luminance(token, dark: dark)
                    let ratio = (max(ink, base) + 0.05) / (min(ink, base) + 0.05)
                    #expect(ratio >= 4.5, "\(token) on \(background), dark=\(dark): \(ratio)")
                }
            }
        }
    }

    @Test func screensUseOnlyTheDesignSystem() throws {
        let rules = [
            ("rgb", #"\bColor\s*\(\s*red\s*:"#),
            ("color-hash", #"\bColor\s*\(\s*#"#),
            ("hex", #"(?:#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b|\b0x[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b)"#),
            ("secondary-opacity", #"\b(?:Color\s*\.\s*)?secondary\s*\.\s*opacity\s*\("#),
            ("radius", #"\bcornerRadius\s*(?::|\()\s*[0-9]+(?:\.[0-9]+)?\b"#),
        ]
        var offenders: [String] = []
        for folder in ["Shared", "iOS", "macOS"] {
            let files = try #require(FileManager.default.enumerator(
                at: app.appendingPathComponent(folder), includingPropertiesForKeys: nil))
            for case let url as URL in files where url.pathExtension == "swift" {
                let path = String(url.path.dropFirst(app.path.count + 1))
                guard !path.hasPrefix("Shared/Design/") else { continue }
                let source = try String(contentsOf: url, encoding: .utf8)
                let nsSource = source as NSString
                for (name, pattern) in rules {
                    let regex = try NSRegularExpression(pattern: pattern)
                    for match in regex.matches(in: source, range: NSRange(location: 0, length: nsSource.length)) {
                        let line = nsSource.substring(with: nsSource.lineRange(for: match.range))
                            .trimmingCharacters(in: .whitespacesAndNewlines)
                        offenders.append("\(path):\(name):\(line)")
                    }
                }
            }
        }
        let allowURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("design-allowlist.txt")
        let allowed = try String(contentsOf: allowURL, encoding: .utf8)
            .split(separator: "\n").map(String.init)
        // Compare multisets: deleting even one repeated offender requires shrinking the list.
        #expect(offenders.sorted() == allowed.sorted(),
                "Update screens to use Design; remove stale allow-list entries. Actual:\n\(offenders.sorted().joined(separator: "\n"))")
    }

    private struct ColorSet: Decodable {
        struct Entry: Decodable {
            struct Appearance: Decodable { let appearance: String; let value: String }
            struct Color: Decodable { let components: [String: String] }
            let appearances: [Appearance]?
            let color: Color
        }
        let colors: [Entry]
    }

    private func luminance(_ token: String, dark: Bool) throws -> Double {
        let url = app.appendingPathComponent("Shared/Assets.xcassets/\(token).colorset/Contents.json")
        let set = try JSONDecoder().decode(ColorSet.self, from: Data(contentsOf: url))
        let entry = try #require(set.colors.first { entry in
            let isDark = entry.appearances?.contains { $0.appearance == "luminosity" && $0.value == "dark" } == true
            return isDark == dark
        })
        let channels = try ["red", "green", "blue"].map { channel in
            let raw = try #require(entry.color.components[channel])
            let value: Double
            if raw.hasPrefix("0x") {
                value = Double(try #require(UInt8(raw.dropFirst(2), radix: 16))) / 255
            } else {
                value = try #require(Double(raw))
            }
            return value <= 0.04045 ? value / 12.92 : pow((value + 0.055) / 1.055, 2.4)
        }
        return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
    }
}
