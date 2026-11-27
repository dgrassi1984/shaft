import Foundation
import Testing
import UserNotifications
@testable import SHAFTKit

struct PushTests {
    @Test func theTokenGoesAsLowercaseHex() {
        #expect(PushRegistrar.hex(Data([0x00, 0xAB, 0x7F, 0xFF])) == "00ab7fff")
    }

    @Test func registrationSendsTokenEnvTopicAndPlatform() async throws {
        let server = StubServer { _ in .json(200, #"{"push": {"on": true, "env": "sandbox", "reason": "registered"}}"#) }
        let registrar = try PushRegistrar(client: server.client, environment: .sandbox,
                                          topic: "io.github.dgrassi1984.shaft", platform: "ios")
        let answer = try await registrar.register(token: Data([0xDE, 0xAD, 0xBE, 0xEF]))
        #expect(answer.push.on)
        let request = try #require(server.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/app/v1/push/register")
        #expect(request.json as? [String: String] == ["token": "deadbeef", "env": "sandbox",
                                                      "topic": "io.github.dgrassi1984.shaft", "platform": "ios"])
    }

    @Test func pushOffComesBackWithWhatIsMissing() async throws {
        let server = StubServer { _ in
            .json(503, #"{"error": {"code": "push_off", "message": "native push is off: SHAFT_APNS_KEY_ID is not set"}}"#)
        }
        let registrar = try PushRegistrar(client: server.client, environment: .production, topic: "t", platform: "macos")
        let error = await #expect(throws: APIError.self) { _ = try await registrar.register(token: Data([1])) }
        #expect(error?.code == "push_off")
        #expect(error?.message == "native push is off: SHAFT_APNS_KEY_ID is not set")
    }

    /// Bug: Settings said "Could not connect to the server." twice under Notifications, and "on (sandbox)" twice when on.
    @Test func settingsSaysEachThingAboutPushOnce() {
        let down = "Could not connect to the server."
        #expect(PushStatus.notes(nil, loadProblem: down, registered: down) == [down])
        #expect(PushStatus.notes(nil, loadProblem: nil, registered: nil).isEmpty)
        let registration = "native push is off: SHAFT_APNS_KEY_ID is not set"
        #expect(PushStatus.notes(nil, loadProblem: down, registered: registration) == [down, registration])
        #expect(PushStatus.notes(nil, loadProblem: "", registered: "").isEmpty)
        let on = PushStatus(on: true, env: "sandbox", reason: "registered", problem: nil)
        #expect(PushStatus.notes(on, loadProblem: nil, registered: "Notifications on (sandbox)").isEmpty)
        let off = PushStatus(on: false, env: nil, reason: "no token", problem: nil)
        #expect(PushStatus.notes(off, loadProblem: nil, registered: "no token") == ["no token"])
        #expect(PushStatus.notes(off, loadProblem: nil, registered: "refused") == ["no token", "refused"])
    }

    @Test func theEnvironmentComesFromTheProfile() {
        func profile(_ entitlements: String) -> Data {
            // A provisioning profile is a CMS envelope around a plist; the bytes around it are not text.
            var data = Data([0x30, 0x80, 0x06, 0x09])
            data.append(Data("""
                <?xml version="1.0" encoding="UTF-8"?>
                <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
                <plist version="1.0"><dict><key>Entitlements</key><dict>\(entitlements)</dict></dict></plist>
                """.utf8))
            data.append(Data([0x00, 0xA0, 0x82]))
            return data
        }
        #expect(PushEnvironment.fromProfile(profile("<key>aps-environment</key><string>development</string>")) == .sandbox)
        #expect(PushEnvironment.fromProfile(profile("<key>com.apple.developer.aps-environment</key><string>production</string>")) == .production)
        #expect(PushEnvironment.fromProfile(profile("")) == nil)
        #expect(PushEnvironment.fromProfile(Data("not a profile".utf8)) == nil)
    }
}

struct NotificationContentTests {
    static let latest = PushLatest(events: [
        PushEvent(title: "tundra replied", body: "October's report is ready.", url: "/i/tundra/chat", tag: "tundra", instance: "tundra"),
        PushEvent(title: "wisp asks", body: "Keep the per-account lines?", url: "/i/wisp/chat", tag: "wisp", instance: "wisp"),
        PushEvent(title: "tundra says", body: "Totals sent.", url: "/i/tundra/chat", tag: "tundra", instance: "tundra"),
    ], badge: 2)

    @Test func theNewestEventForTheInstanceIsShown() {
        let filled = NotificationContent.choose(Self.latest, instance: "tundra")
        #expect(filled == NotificationContent.Filled(title: "tundra says", body: "Totals sent.", badge: 2, instance: "tundra"))
    }

    /// A push about a finished meeting carried no instance (`""`) and no meeting: the body came from whichever event was
    /// newest and the tap opened nothing (review 2026-11-27). It shows its own event and the tap opens the meeting.
    @Test func aMeetingPushShowsItsOwnEventAndOpensTheMeeting() throws {
        let userInfo: [AnyHashable: Any] = ["instance": "", "meeting": "m-20261127-0855-8291"]
        #expect(NotificationContent.instance(from: userInfo) == nil)
        #expect(NotificationContent.meeting(from: userInfo) == "m-20261127-0855-8291")
        #expect(NotificationContent.meeting(from: ["instance": "wisp"]) == nil)
        var events = Self.latest.events
        events.insert(PushEvent(title: "Meeting ready: Planning", body: "Ada · 1 voice to name", url: "/meetings/m-20261127-0855-8291",
                                tag: "meeting-m-20261127-0855-8291", instance: nil, meeting: "m-20261127-0855-8291"), at: 0)
        let latest = PushLatest(events: events, badge: 2)
        let filled = try #require(NotificationContent.choose(latest, instance: nil, meeting: "m-20261127-0855-8291"))
        #expect(filled.title == "Meeting ready: Planning")
        #expect(filled.body == "Ada · 1 voice to name")
        #expect(NotificationContent.choose(latest, instance: nil, meeting: "m-20261127-0855-0000") == nil)
        let content = UNMutableNotificationContent()
        filled.apply(to: content)
        #expect(content.userInfo["meeting"] as? String == "m-20261127-0855-8291")
        #expect(content.userInfo["instance"] == nil)
    }

    @Test func noEventForTheInstanceLeavesTheTitleAlone() {
        #expect(NotificationContent.choose(Self.latest, instance: "orrin") == nil)
        #expect(NotificationContent.choose(PushLatest(events: [], badge: 0), instance: nil) == nil)
    }

    @Test func theBodyIsFetchedWithTheSharedCredential() async throws {
        let server = StubServer { _ in
            .json(200, String(decoding: try DecodingTests.fixture("push-latest"), as: UTF8.self))
        }
        let store = MemoryCredentialStore(server.credential)
        let instance = NotificationContent.instance(from: ["instance": "wisp"])
        let filled = try #require(await NotificationContent.fetch(instance: instance, store: store, session: server.session))
        let content = UNMutableNotificationContent()
        content.title = "wisp replied"
        filled.apply(to: content)
        #expect(content.body == "Should the nightly report keep the per-account lines?")
        #expect(content.badge == 1)
        #expect(content.threadIdentifier == "wisp")
        #expect(server.requests.first?.path == "/app/v1/push/latest")
        #expect(server.requests.first?.query("instance") == "wisp")
        #expect(server.requests.first?.header("Authorization") == "Bearer 8c1f4e2a9b07d3e5.s3cret")
    }

    @Test func notPairedOrOfflineGivesNothingAndDeliversTheTitleOnce() async throws {
        #expect(await NotificationContent.fetch(instance: "tundra", store: MemoryCredentialStore()) == nil)
        let unreachable = ServerCredential(baseURL: URL(string: "https://127.0.0.1:9")!, device: "d", secret: "s",
                                           name: "n", login: "l")
        #expect(await NotificationContent.fetch(instance: "tundra", store: MemoryCredentialStore(unreachable),
                                                timeout: 2) == nil)

        let content = UNMutableNotificationContent()
        content.title = "tundra replied"
        let delivered = Delivered()
        let delivery = NotificationDelivery(content: content) { delivered.add($0) }
        delivery.expire()
        delivery.deliver(NotificationContent.Filled(title: "late", body: "too late", badge: 1, instance: "tundra"))
        #expect(delivered.titles == ["tundra replied"])
        #expect(delivered.bodies == [""])
    }
}

final class Delivered: @unchecked Sendable {
    private let lock = NSLock()
    private var contents: [UNNotificationContent] = []
    func add(_ content: UNNotificationContent) { lock.withLock { contents.append(content) } }
    var titles: [String] { lock.withLock { contents.map(\.title) } }
    var bodies: [String] { lock.withLock { contents.map(\.body) } }
}
