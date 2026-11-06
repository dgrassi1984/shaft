import Foundation
import Testing
@testable import SHAFTKit

/// Every JSON fixture decodes into the Kit's models, and the models keep every field the fixture has.
///
/// The fixtures are written by the server's own serialiser (docs/app-api.md §13.5) in
/// `supervisor/tests/app_api_fixtures/`, or `$SHAFT_APP_API_FIXTURES` when set.
/// A fixture is matched to its model by its shape (its top-level keys), so a new one needs no code here. A fixture
/// that matches no model fails: either the contract grew or the fixture is not an /app/v1 answer.
struct FixtureTests {
    @Test func sentIdentityUsesTheServerShapeAndIsOptionalForOlderServers() throws {
        let fixture = try #require(JSONSerialization.jsonObject(with: DecodingTests.fixture("sent")) as? [String: Any])
        let items = try #require(fixture["items"] as? [[String: Any]])
        var sent = try #require(items.first { $0["type"] as? String == "sent" })
        #expect(sent["read"] is Bool)
        #expect(sent["kind"] is String)
        let identity = try #require(sent["file"] as? String)
        let current = try SHAFTJSON.decoder().decode(SentItem.self, from: JSONSerialization.data(withJSONObject: sent))
        #expect(current.file == identity)
        sent.removeValue(forKey: "file")
        let old = try SHAFTJSON.decoder().decode(SentItem.self, from: JSONSerialization.data(withJSONObject: sent))
        #expect(old.file == nil)
    }

    static var fixtureDirectory: URL {
        if let folder = ProcessInfo.processInfo.environment["SHAFT_APP_API_FIXTURES"] {
            return URL(filePath: folder)
        }
        let repo = URL(filePath: #filePath)
            .deletingLastPathComponent()  // SHAFTKitTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // SHAFTKit
            .deletingLastPathComponent()  // Packages
            .deletingLastPathComponent()  // shaft
            .deletingLastPathComponent()  // apps
            .deletingLastPathComponent()  // repository
        return repo.appending(path: "supervisor/tests/app_api_fixtures")
    }

    static func fixtureFiles() -> [URL] {
        ((try? FileManager.default.contentsOfDirectory(at: fixtureDirectory, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "json" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    @Test func theServerFixturesAreThere() {
        let names = Set(Self.fixtureFiles().map(\.lastPathComponent))
        for name in ["idle", "failed", "live", "live-partial", "queued", "quotes", "attachments", "sent", "feedback",
                     "earlier", "ended", "instances", "docs", "doc-comments", "push-latest", "errors", "pair", "chats",
                     "send", "feedback-response", "retried", "plan", "cycles", "cycle"] {
            #expect(names.contains("\(name).json"), "missing fixture \(name)")
        }
    }

    @Test func cycleFieldsKeepTheirWireMeaning() throws {
        let planData = try DecodingTests.fixture("plan")
        var object = try #require(JSONSerialization.jsonObject(with: planData) as? [String: Any])
        object["now"] = ["state": "idle", "since": "12:05", "goal": NSNull()] as [String: Any]
        let plan = try SHAFTJSON.decoder().decode(
            CyclePlan.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(plan.now.since == "12:05")
        #expect(plan.queue.rows[0].dueAt == SHAFTJSON.parseStamp("2099-11-04T12:00:00+01:00"))
        #expect(plan.queue.rows[0].dueAtIsDate == false)
        #expect(plan.queue.rows[1].dueAt == SHAFTJSON.parseStamp("2099-11-05T00:00:00Z"))
        #expect(plan.queue.rows[1].dueAtIsDate)
        #expect(plan.queue.rows[2].dueAt == nil)
        #expect(plan.queue.rows[2].dueAtIsDate == false)
        #expect(plan.queue.rows[2].dueAtText == "when ready")
        let page = try SHAFTJSON.decoder().decode(CycleDaysResponse.self, from: DecodingTests.fixture("cycles"))
        #expect(page.days[0].date == "2026-11-03")
        #expect(page.days[0].rows[2].id == "2026-11-03T13:58:40")
        #expect(page.days[0].rows[2].at == SHAFTJSON.parseStamp("2026-11-03T13:58:40+01:00"))
        #expect(page.days[0].rows[2].claim == "yes")
        #expect(page.days[0].rows[2].verdict == "partial")
        let detail = try SHAFTJSON.decoder().decode(CycleDetail.self, from: DecodingTests.fixture("cycle"))
        #expect(detail.did.usd == nil)
        #expect(detail.result?.claim == "yes")
        #expect(detail.result?.judge?.verdict == "partial")
        #expect(detail.result?.judge?.firstError == "The totals were not checked before sending.")
        #expect(detail.result?.doubts == ["step: 1; what: Check again"])
    }

    @Test func unreadableImprovementIsNotDecodedAsNoPlan() throws {
        var object = try #require(JSONSerialization.jsonObject(with: DecodingTests.fixture("plan")) as? [String: Any])
        object["improvement"] = NSNull()
        object["improvement_error"] = "could not read memory/journal.md: denied"
        let plan = try SHAFTJSON.decoder().decode(
            CyclePlan.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(plan.improvement == nil)
        #expect(plan.improvementError == "could not read memory/journal.md: denied")
    }

    @Test func aDayWithOnlySkippedLinesStillDecodesForDisplay() throws {
        var object = try #require(JSONSerialization.jsonObject(with: DecodingTests.fixture("cycles")) as? [String: Any])
        var days = try #require(object["days"] as? [[String: Any]])
        days[0]["rows"] = []
        days[0]["skipped_lines"] = 2
        object["days"] = days
        let page = try SHAFTJSON.decoder().decode(
            CycleDaysResponse.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(page.days.count == 1)
        #expect(page.days[0].rows.isEmpty)
        #expect(page.days[0].skippedLines == 2)
    }

    @Test(arguments: ["2099-11-05", "2099-11-05T00:00:00Z"])
    func queueDueDatesKeepTheirWireFormat(_ due: String) throws {
        let object: [String: Any] = ["id": 1, "task": "Check", "due_at": due, "overdue": false]
        let row = try SHAFTJSON.decoder().decode(
            CyclePlan.Queue.Row.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(row.dueAt == SHAFTJSON.parseStamp("2099-11-05T00:00:00Z"))
        #expect(row.dueAtIsDate == (due == "2099-11-05"))
    }

    @Test(arguments: fixtureFiles())
    func fixtureDecodes(_ url: URL) throws {
        let data = try Data(contentsOf: url)
        let json = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        let checked = try FixtureShape.check(json, name: url.lastPathComponent)
        #expect(checked > 0, "\(url.lastPathComponent): nothing in it was an /app/v1 answer")
    }
}

/// Decodes a fixture by its shape and checks the model kept every non-null field.
enum FixtureShape {
    struct Unmatched: Error, CustomStringConvertible {
        let description: String
    }

    /// Check `json` (an answer, or a list or map of answers) and return how many answers it held.
    static func check(_ json: Any, name: String) throws -> Int {
        if let list = json as? [Any] {
            return try list.enumerated().reduce(0) { $0 + (try check($1.element, name: "\(name)[\($1.offset)]")) }
        }
        guard let object = json as? [String: Any] else {
            throw Unmatched(description: "\(name): not a JSON object or list")
        }
        if let decode = decoder(for: Set(object.keys)) {
            try decode(object, name)
            return 1
        }
        // A map of named answers, including error fixtures wrapped in {status, body}.
        if !object.isEmpty, object.values.allSatisfy({ $0 is [String: Any] || $0 is [Any] }) {
            return try object.keys.sorted().reduce(0) { $0 + (try check(object[$1]!, name: "\(name).\($1)")) }
        }
        throw Unmatched(description: "\(name): matches no /app/v1 model (keys: \(object.keys.sorted()))")
    }

    typealias Decode = ([String: Any], String) throws -> Void

    static func decoder(for keys: Set<String>) -> Decode? {
        func has(_ wanted: String...) -> Bool { wanted.allSatisfy(keys.contains) }
        if has("instance", "partial", "version", "live") { return roundTrip(Feed.self) }
        if has("instances", "badge") { return roundTrip(InstancesResponse.self) }
        if has("now", "check", "queue") { return roundTrip(CyclePlan.self) }
        if has("days", "next", "judge_error") { return roundTrip(CycleDaysResponse.self) }
        if has("id", "at", "did") { return roundTrip(CycleDetail.self) }
        if has("chats") { return roundTrip(ChatsResponse.self) }
        if has("dir", "max_bytes", "docs") { return roundTrip(DocsList.self) }
        if has("path", "text", "sha", "threads") { return roundTrip(DocumentDetail.self) }
        if has("events", "badge") { return roundTrip(PushLatest.self) }
        if has("status", "body") {
            return { object, name in
                let status = try #require(object["status"] as? Int, "\(name): missing integer status")
                let body = try #require(object["body"] as? [String: Any], "\(name): missing error body")
                try roundTrip(ErrorBody.self)(body, "\(name).body")
                let data = try JSONSerialization.data(withJSONObject: body)
                let decoded = try SHAFTJSON.decoder().decode(ErrorBody.self, from: data)
                // docs/app-api.md §1.5: the status belonging to each error code.
                let statuses = [
                    "bad_request": 400, "unpaired": 401,
                    "login": 403, "host": 403, "pair_expired": 403,
                    "not_found": 404, "method": 405,
                    "refused": 409, "stale": 409, "too_big": 409, "stale_version": 409,
                    "too_large": 413, "internal": 500, "push_off": 503,
                ]
                let expected = try #require(statuses[decoded.error.code], "\(name): undocumented error code")
                let error = APIError.server(status: status, error: decoded.error)
                #expect(error.status == expected, "\(name): wrong status for \(decoded.error.code)")
            }
        }
        if has("error") { return roundTrip(ErrorBody.self) }
        if has("device", "secret") { return roundTrip(PairResponse.self) }
        if has("push") { return roundTrip(PushRegisterResponse.self) }
        if has("on", "reason", "problem") { return roundTrip(PushStatus.self) }
        if has("unread", "badge") { return roundTrip(SeenResponse.self) }
        if has("said", "thread") { return roundTrip(CommentResponse.self) }
        if has("said", "path", "sha") { return roundTrip(DocSaveResponse.self) }
        if has("said", "chat", "file") { return roundTrip(SendResponse.self) }
        if has("said", "id") { return roundTrip(FeedbackResponse.self) }
        if keys == ["said"] { return roundTrip(SaidResponse.self) }
        if keys == ["removed"] { return roundTrip(RemovedResponse.self) }
        if keys == ["ok"] { return roundTrip(OkResponse.self) }
        return nil
    }

    /// Decode as `T`, encode it back, and require every non-null key of the fixture in the result: a field the
    /// server sends that the model drops fails here.
    static func roundTrip<T: Codable>(_ type: T.Type) -> Decode {
        { object, name in
            let data = try JSONSerialization.data(withJSONObject: object)
            let value: T
            do {
                value = try SHAFTJSON.decoder().decode(T.self, from: data)
            } catch {
                throw Unmatched(description: "\(name) as \(T.self): \(error)")
            }
            if let feed = value as? Feed {
                for item in feed.content?.items ?? [] {
                    if case .unknown(let key, let type) = item {
                        throw Unmatched(description: "\(name): item \(key) has a type the Kit does not know: \(type)")
                    }
                }
            }
            let back = try JSONSerialization.jsonObject(with: SHAFTJSON.encoder().encode(value))
            try requireKeys(object, in: back, at: "\(name) (\(T.self))")
        }
    }

    static func requireKeys(_ fixture: Any, in decoded: Any, at path: String) throws {
        if let fixture = fixture as? [String: Any] {
            guard let decoded = decoded as? [String: Any] else {
                throw Unmatched(description: "\(path): the model has no object here")
            }
            for (key, value) in fixture where !(value is NSNull) {
                guard let kept = decoded[key] else {
                    throw Unmatched(description: "\(path).\(key): the fixture has it, the model drops it")
                }
                try requireKeys(value, in: kept, at: "\(path).\(key)")
            }
        } else if let fixture = fixture as? [Any] {
            guard let decoded = decoded as? [Any], decoded.count == fixture.count else {
                throw Unmatched(description: "\(path): the model's list differs in length")
            }
            for (index, element) in fixture.enumerated() {
                try requireKeys(element, in: decoded[index], at: "\(path)[\(index)]")
            }
        }
    }
}

struct DecodingTests {
    static func fixture(_ name: String) throws -> Data {
        let url = FixtureTests.fixtureDirectory.appending(path: "\(name).json")
        return try Data(contentsOf: url)
    }

    @Test func timestampsKeepTheirOffset() throws {
        // 14:02:11 at +01:00 is 13:02:11 UTC; a July stamp at +02:00 is two hours ahead of UTC (§1.6).
        let winter = try #require(SHAFTJSON.parseStamp("2026-11-03T14:02:11+01:00"))
        #expect(winter == Date(timeIntervalSince1970: 1_793_710_931))
        let summer = try #require(SHAFTJSON.parseStamp("2026-07-03T14:02:11+02:00"))
        #expect(summer == Date(timeIntervalSince1970: 1_783_080_131))
    }

    @Test func aTimestampWithoutOffsetIsRefused() {
        // A stored local time that reached the wire unstamped must not be read in the device's zone.
        #expect(SHAFTJSON.parseStamp("2026-11-03T14:02:11") == nil)
        let row = #"{"chats": [{"id": "web-1", "created": "2026-11-03T14:02:11", "ended": "2026-11-03T14:02:11+01:00", "excerpt": "", "messages": 0}]}"#
        #expect(throws: DecodingError.self) {
            try SHAFTJSON.decoder().decode(ChatsResponse.self, from: Data(row.utf8))
        }
    }

    @Test func theFailedFeedReadsAsTheServerSendsIt() throws {
        let feed = try SHAFTJSON.decoder().decode(Feed.self, from: Self.fixture("failed"))
        let content = try #require(feed.content)
        #expect(feed.partial == false)
        #expect(content.items.map(\.key) == ["u0", "a1", "s20261103-140230-118204-from-wren.md",
                                             "b20261103-140301-552019-from-operator.md", "u2", "a3", "u4", "a5", "u6", "a7"])
        guard case .assistant(let failed) = content.items[5] else { Issue.record("a3 is not an answer"); return }
        #expect(failed.failure?.retry?.retryOf == 3)
        #expect(failed.failure?.retry?.confirm == nil)
        guard case .user(let first) = content.items[0] else { Issue.record("u0 is not a user turn"); return }
        #expect(first.attachments.first?.url == "/app/v1/file/chat/web-3f9a1c0d7e2b4a65/0/0")
        #expect(content.chat?.ended == nil)
        #expect(content.canEnd)
    }

    @Test func aPartialFeedKeepsWhatItLeavesOut() throws {
        let full = try SHAFTJSON.decoder().decode(Feed.self, from: Self.fixture("queued"))
        let partial = try SHAFTJSON.decoder().decode(Feed.self, from: Self.fixture("live-partial"))
        #expect(partial.partial)
        #expect(partial.content == nil)
        let merged = full.merging(partial)
        #expect(merged.version == "b713d0c3dc0ad5b4")
        #expect(merged.live == "fb7597261efc6cb5")
        #expect(merged.reply?.answer == "October's report is running; so far 214 accounts reconciled.")
        #expect(merged.content == full.content)
        #expect(merged.content?.queued.first?.mid == "9d3e0f2a-6b1c-4a7d-8e5f-3c2b1a0d9e8f")
    }

    @Test func anUnknownItemTypeDoesNotBreakTheFeed() throws {
        let json = #"{"key": "x1", "type": "poll", "ts": "2026-11-03T14:02:11+01:00"}"#
        let item = try SHAFTJSON.decoder().decode(FeedItem.self, from: Data(json.utf8))
        #expect(item == .unknown(key: "x1", type: "poll"))
    }
}
