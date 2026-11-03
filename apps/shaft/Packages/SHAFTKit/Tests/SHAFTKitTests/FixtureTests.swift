import Foundation
import Testing
@testable import SHAFTKit

/// Every JSON fixture decodes into the Kit's models, and the models keep every field the fixture has.
///
/// The fixtures come from every `*.json` in:
/// - `Tests/SHAFTKitTests/Fixtures/` — the examples of docs/app-api.md, copied;
/// - the server's fixtures, written by its own serialiser (docs/app-api.md §13.5): `supervisor/tests/app_api_fixtures/`
///   and `supervisor/tests/fixtures/app_api/` in this repository, when present;
/// - `$SHAFT_APP_API_FIXTURES`, when set.
///
/// A fixture is matched to its model by its shape (its top-level keys), so a new one needs no code here. A fixture
/// that matches no model fails: either the contract grew or the fixture is not an /app/v1 answer.
struct FixtureTests {
    static func fixtureFiles() -> [URL] {
        var folders = [URL]()
        if let bundled = Bundle.module.url(forResource: "Fixtures", withExtension: nil) { folders.append(bundled) }
        let repo = URL(filePath: #filePath)
            .deletingLastPathComponent()  // SHAFTKitTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // SHAFTKit
            .deletingLastPathComponent()  // Packages
            .deletingLastPathComponent()  // shaft
            .deletingLastPathComponent()  // apps
        folders.append(repo.appending(path: "supervisor/tests/app_api_fixtures"))
        folders.append(repo.appending(path: "supervisor/tests/fixtures/app_api"))
        if let extra = ProcessInfo.processInfo.environment["SHAFT_APP_API_FIXTURES"] {
            folders.append(URL(filePath: extra))
        }
        return folders.flatMap { folder in
            ((try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? [])
                .filter { $0.pathExtension == "json" }
                .sorted { $0.lastPathComponent < $1.lastPathComponent }
        }
    }

    @Test func theContractExamplesAreThere() {
        let names = Set(Self.fixtureFiles().map(\.lastPathComponent))
        for name in ["contract-idle.json", "contract-live.json", "contract-live-partial.json", "contract-instances.json",
                     "contract-doc-comments.json", "contract-push-latest.json", "contract-errors.json"] {
            #expect(names.contains(name), "missing fixture \(name)")
        }
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
        // A map of named answers (`{"unpaired": {"error": …}, …}`).
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
        if has("chats") { return roundTrip(ChatsResponse.self) }
        if has("dir", "max_bytes", "docs") { return roundTrip(DocsList.self) }
        if has("path", "text", "sha", "threads") { return roundTrip(DocumentDetail.self) }
        if has("events", "badge") { return roundTrip(PushLatest.self) }
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
        let url = try #require(Bundle.module.url(forResource: "Fixtures/\(name)", withExtension: "json"))
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

    @Test func theIdleFeedReadsAsTheContractSays() throws {
        let feed = try SHAFTJSON.decoder().decode(Feed.self, from: Self.fixture("contract-idle"))
        let content = try #require(feed.content)
        #expect(feed.partial == false)
        #expect(content.items.map(\.key) == ["u0", "a1", "s20261103-140230-118204-from-thelos.md",
                                             "b20261103-140301-552019-from-operator.md", "u2", "a3"])
        guard case .assistant(let failed) = content.items[5] else { Issue.record("a3 is not an answer"); return }
        #expect(failed.failure?.retry?.retryOf == 3)
        #expect(failed.failure?.retry?.confirm == nil)
        guard case .user(let first) = content.items[0] else { Issue.record("u0 is not a user turn"); return }
        #expect(first.attachments.first?.url == "/app/v1/file/chat/web-3f9a1c0d7e2b4a65/0/0")
        #expect(content.chat?.ended == nil)
        #expect(content.canEnd)
    }

    @Test func aPartialFeedKeepsWhatItLeavesOut() throws {
        let full = try SHAFTJSON.decoder().decode(Feed.self, from: Self.fixture("contract-live"))
        let partial = try SHAFTJSON.decoder().decode(Feed.self, from: Self.fixture("contract-live-partial"))
        #expect(partial.partial)
        #expect(partial.content == nil)
        let merged = full.merging(partial)
        #expect(merged.version == "41d07be2c95a8f13")
        #expect(merged.live == "7b19e4c0d2a65f38")
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
