import Foundation

/// The JSON coders for `/app/v1` (docs/app-api.md §1.4–1.6).
///
/// - Keys are snake_case on the wire and camelCase in Swift.
/// - A timestamp is RFC 3339 with the server's UTC offset (`2026-11-03T14:02:11+01:00`, §1.6). One without an offset
///   is refused rather than read in the device's zone: it means a stored local time reached the wire unstamped.
public enum SHAFTJSON {
    public static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let text = try container.decode(String.self)
            guard let date = parseStamp(text) else {
                throw DecodingError.dataCorruptedError(
                    in: container, debugDescription: "not an RFC 3339 time with an offset (docs/app-api.md §1.6): \(text)")
            }
            return date
        }
        return decoder
    }

    public static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(formatStamp(date))
        }
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }

    /// A timestamp as §1.6 sends it; nil when it is not one (no offset, or not a time).
    public static func parseStamp(_ text: String) -> Date? {
        try? Date.ISO8601FormatStyle(timeZoneSeparator: .colon).parse(text)
    }

    /// A date as RFC 3339 in UTC, second precision: how the app stores its own times (drafts, the outbox).
    public static func formatStamp(_ date: Date) -> String {
        date.formatted(Date.ISO8601FormatStyle(timeZoneSeparator: .colon))
    }
}
