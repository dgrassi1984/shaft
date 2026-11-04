import Foundation

/// One row of a chat as the apps draw it, in order: the feed's items with the day separators the app draws itself
/// (§1.6: they are not sent), then what follows the conversation in the HTML feed (§14): the reply running now, the
/// queued messages, pending feedback, pending edits and the messages not sent yet.
public enum FeedRow: Sendable, Equatable, Identifiable {
    /// "Show the N earlier messages" (`more`).
    case earlier(Int)
    /// A day separator (`day-<date>`): the start of that day in the device's zone.
    case day(Date)
    case item(FeedItem)
    /// The reply running now (`live`, `act`, `wait`/`ans`).
    case live(Reply)
    case queued(QueuedMessage)
    case pendingFeedback(PendingFeedback)
    /// "✎ Your changes to … go to <n> with your next message" (`pend`).
    case pendingEdits([String])
    /// A message on its way or not sent (the outbox), shown after the chat's own.
    case unsent(OutboxEntry)

    public var id: String {
        switch self {
        case .earlier: "more"
        case .day(let date): "day-\(Int(date.timeIntervalSince1970))"
        case .item(let item): item.key
        case .live: "live"
        case .queued(let message): message.key
        case .pendingFeedback(let feedback): feedback.key
        case .pendingEdits: "pend"
        case .unsent(let entry): "unsent-\(entry.mid)"
        }
    }
}

public enum FeedRows {
    /// The rows of `feed` (full) with `unsent` after them. Day separators go before the first item of each day in
    /// `calendar`'s zone (the device's); the reply and what follows it get one when they start a new day.
    public static func rows(_ feed: Feed, unsent: [OutboxEntry] = [], calendar: Calendar = .current) -> [FeedRow] {
        guard let content = feed.content else { return [] }
        var rows: [FeedRow] = []
        if content.earlier > 0 { rows.append(.earlier(content.earlier)) }
        var day: Date?
        func dayRow(_ date: Date?) {
            guard let date else { return }
            let start = calendar.startOfDay(for: date)
            if start != day {
                day = start
                rows.append(.day(start))
            }
        }
        for item in content.items {
            dayRow(item.ts)
            rows.append(.item(item))
        }
        if let reply = feed.reply {
            dayRow(reply.started)
            rows.append(.live(reply))
        }
        if !content.archived {
            rows += content.queued.map(FeedRow.queued)
            rows += content.pendingFeedback.map(FeedRow.pendingFeedback)
            if !content.pendingEdits.isEmpty { rows.append(.pendingEdits(content.pendingEdits)) }
            let held = Set(content.mids)
            rows += unsent.filter { !held.contains($0.mid) }.map(FeedRow.unsent)
        }
        return rows
    }

    /// The key of the newest thing the chat shows from the instance or the operator: what must be on screen before the
    /// app marks the chat seen (§7.5). Nil for an empty chat.
    public static func newestKey(_ feed: Feed) -> String? {
        if feed.reply != nil { return "live" }
        return feed.content?.items.last?.key
    }

    /// The answers (and messages) that `newer` holds and `older` did not: what to announce for VoiceOver ("thelos
    /// replied"). Empty on the first load (`older` nil) and for the operator's own items.
    public static func arrivals(from older: Feed?, to newer: Feed) -> [FeedItem] {
        guard let old = older?.content, let new = newer.content else { return [] }
        let seen = Set(old.items.map(\.key))
        return new.items.filter { item in
            guard !seen.contains(item.key) else { return false }
            switch item {
            case .assistant, .sent: return true
            default: return false
            }
        }
    }
}

/// The text a send's quotes become, as the server makes it (§6, `app_api.quote_text`): the outbox shows a message not
/// sent as the chat would. The server stays the one that builds what is sent.
public enum QuoteText {
    public static func compose(_ text: String, quotes: [Quote]) -> String {
        let blocks = quotes.map { q -> String in
            var lines: [String] = []
            if !q.doc.isEmpty { lines.append("> (from notes/docs/\(q.doc))") }
            let body = q.quote.replacingOccurrences(of: "\r\n", with: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            for line in body.split(separator: "\n", omittingEmptySubsequences: false) {
                lines.append(trimTrailing("> " + line))
            }
            let quote = lines.joined(separator: "\n")
            let answer = q.answer.trimmingCharacters(in: .whitespacesAndNewlines)
            return answer.isEmpty ? quote : quote + "\n\n" + answer
        }
        let main = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let answered = quotes.contains { !$0.answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        let pieces = answered ? [main] + blocks : blocks + [main]
        return pieces.filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    private static func trimTrailing(_ line: String) -> String {
        var line = line
        while line.last?.isWhitespace == true { line.removeLast() }
        return line
    }
}

/// How the operator's own text reads (§4.4 `user.text`): not Markdown. Lines starting `> ` are a quote block, a fenced
/// block (```) is code, the rest is plain text with its line breaks (`web_views._my_text`).
public enum OperatorText {
    public enum Block: Sendable, Equatable {
        case quote(String)
        case code(String)
        case text(String)
    }

    public static func blocks(_ text: String) -> [Block] {
        var blocks: [Block] = []
        var kind: Character = " "          // q, c, t
        var lines: [String] = []
        func flush() {
            guard !lines.isEmpty else { return }
            let body = lines.joined(separator: "\n")
            switch kind {
            case "q": blocks.append(.quote(body))
            case "c": blocks.append(.code(body))
            default:
                let trimmed = body.trimmingCharacters(in: .newlines)
                if !trimmed.isEmpty { blocks.append(.text(trimmed)) }
            }
            lines = []
        }
        var inCode = false
        for raw in text.replacingOccurrences(of: "\r\n", with: "\n").split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw)
            if inCode {
                if line.trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    flush()
                    inCode = false
                    kind = " "
                } else {
                    lines.append(line)
                }
                continue
            }
            if line.trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                flush()
                inCode = true
                kind = "c"
                continue
            }
            let quoted = quoteBody(line)
            let wanted: Character = quoted != nil ? "q" : "t"
            if wanted != kind {
                flush()
                kind = wanted
            }
            if let quoted {
                lines.append(quoted)
            } else {
                lines.append(line)
            }
        }
        flush()
        return blocks
    }

    /// The line without its quote mark (`^ {0,3}> ?`, as `web_views._QUOTE`), nil when it is not quoted.
    static func quoteBody(_ line: String) -> String? {
        var rest = Substring(line)
        var spaces = 0
        while rest.first == " " && spaces < 3 {
            rest = rest.dropFirst()
            spaces += 1
        }
        guard rest.first == ">" else { return nil }
        rest = rest.dropFirst()
        if rest.first == " " { rest = rest.dropFirst() }
        return String(rest)
    }
}
