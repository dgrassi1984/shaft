import Foundation
import UserNotifications

/// The notification service extensions' work (§11): the APNs payload carries only the title, the badge and the
/// instance; the body is fetched from `/app/v1/push/latest` over the tailnet with the app's shared credential. Any
/// failure (not paired, offline, too slow, nothing for this instance) leaves the notification as it came: title only.
public enum NotificationContent {
    /// The instance a notification is about: the payload's `instance` (PLAN §2.2), for the tap to open its chat.
    /// Nil for an empty one: a push about no instance (a meeting) carries `""`.
    public static func instance(from userInfo: [AnyHashable: Any]) -> String? {
        guard let instance = userInfo["instance"] as? String, !instance.isEmpty else { return nil }
        return instance
    }

    /// The meeting a notification is about (ready or failed): the payload's `meeting`, for the tap to open it.
    public static func meeting(from userInfo: [AnyHashable: Any]) -> String? {
        guard let meeting = userInfo["meeting"] as? String, !meeting.isEmpty else { return nil }
        return meeting
    }

    /// The thread a notification is about: the payload's `cid` (a chat id, or `"from"` for the From feed), or the one
    /// the extension found in the event it showed. Nil from a server before threads: the tap opens the instance's
    /// default conversation.
    public static func cid(from userInfo: [AnyHashable: Any]) -> String? {
        guard let cid = userInfo["cid"] as? String, !cid.isEmpty else { return nil }
        return cid
    }

    /// What to show for a push about `instance`: the newest queued event for it. Nil on any failure. `session` is for
    /// tests; the extension's own is ephemeral, with `timeout` on the whole fetch.
    public static func fetch(instance: String?, cid: String? = nil, meeting: String? = nil, store: any CredentialStore,
                             timeout: TimeInterval = 20, session: URLSession? = nil) async -> Filled? {
        guard let credential = try? store.load() else { return nil }
        let own: URLSession? = session == nil ? {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = timeout
            configuration.timeoutIntervalForResource = timeout
            return URLSession(configuration: configuration)
        }() : nil
        defer { own?.finishTasksAndInvalidate() }
        let client = APIClient(credential: credential, session: session ?? own!)
        guard let latest = try? await client.pushLatest(instance: instance, meeting: meeting) else { return nil }
        return choose(latest, instance: instance, cid: cid, meeting: meeting)
    }

    /// The event to show: the newest for `instance` (or the newest of all when the payload named none), of the thread
    /// `cid` when the payload named one and an event of it is there. Nil when no event of the instance matches: a body
    /// from another instance's chat would be wrong, so the title stays alone. A push about a meeting shows only that
    /// meeting's event.
    public static func choose(_ latest: PushLatest, instance: String?, cid: String? = nil,
                              meeting: String? = nil) -> Filled? {
        if let meeting {
            guard let event = latest.events.last(where: { $0.meeting == meeting }) else { return nil }
            var filled = Filled(title: event.title, body: event.body, badge: latest.badge, instance: nil)
            filled.meeting = meeting
            return filled
        }
        let mine = instance.map { name in latest.events.filter { $0.instance == name } } ?? latest.events
        let event = cid.flatMap { cid in mine.last { $0.cid == cid } } ?? mine.last
        guard let event else { return nil }
        var filled = Filled(title: event.title, body: event.body, badge: latest.badge, instance: event.instance,
                            cid: event.cid)
        filled.question = event.question
        return filled
    }

    /// What `fetch` found, to lay onto the notification.
    public struct Filled: Sendable, Equatable {
        public var title: String
        public var body: String
        public var badge: Int
        public var instance: String?
        /// The thread the event is about (`PushEvent.cid`): kept in `userInfo["cid"]`, so the tap opens it.
        public var cid: String?
        public var question: NotificationQuestion? = nil
        /// The meeting the event is about: kept in `userInfo["meeting"]`, so the tap opens it.
        public var meeting: String? = nil

        public init(title: String, body: String, badge: Int, instance: String?, cid: String? = nil) {
            self.title = title
            self.body = body
            self.badge = badge
            self.instance = instance
            self.cid = cid
        }

        public func apply(to content: UNMutableNotificationContent) {
            if !title.isEmpty { content.title = title }
            content.body = body
            content.badge = NSNumber(value: badge)
            if let instance {
                content.threadIdentifier = instance
                content.userInfo["instance"] = instance
            }
            if let cid { content.userInfo["cid"] = cid }
            if let meeting { content.userInfo["meeting"] = meeting }
            content.categoryIdentifier = ""
            content.userInfo.removeValue(forKey: "question_file")
            content.userInfo.removeValue(forKey: "question_option_count")
            if let question {
                content.userInfo["question_file"] = question.file
                content.userInfo["cid"] = question.cid
                if let category = question.category {
                    content.categoryIdentifier = category.identifier
                    content.userInfo["question_option_count"] = question.choices.options.count
                }
            }
        }
    }
}

/// One notification in a service extension: delivers exactly once, whichever comes first — the body fetched, or the
/// system's "time is up" (`serviceExtensionTimeWillExpire`), which delivers the title alone.
public final class NotificationDelivery: @unchecked Sendable {
    private let lock = NSLock()
    private var content: UNMutableNotificationContent?
    private var handler: ((UNNotificationContent) -> Void)?

    public init(content: UNMutableNotificationContent, handler: @escaping (UNNotificationContent) -> Void) {
        self.content = content
        self.handler = handler
    }

    /// Fetch the body and deliver. Runs `NotificationContent.fetch` with the shared Keychain credential.
    public func run(store: any CredentialStore, timeout: TimeInterval = 20) {
        let (instance, cid, meeting) = lock.withLock {
            (content.flatMap { NotificationContent.instance(from: $0.userInfo) },
             content.flatMap { NotificationContent.cid(from: $0.userInfo) },
             content.flatMap { NotificationContent.meeting(from: $0.userInfo) })
        }
        Task {
            let filled = await NotificationContent.fetch(instance: instance, cid: cid, meeting: meeting, store: store,
                                                         timeout: timeout)
            await self.deliverAfterRegistration(filled) { category in
                await QuestionCategoryRegistry.shared.register(category)
            }
        }
    }

    /// Expiry may deliver the original content while registration waits. A late completion cannot deliver again.
    func deliverAfterRegistration(_ filled: NotificationContent.Filled?,
                                  register: @Sendable (NotificationQuestion.Category) async -> Bool) async {
        guard lock.withLock({ handler != nil }) else { return }
        if let category = filled?.question?.category, !(await register(category)) {
            deliver(nil)
            return
        }
        deliver(filled)
    }

    /// Deliver with the body when there is one, else as it came. A second call does nothing.
    public func deliver(_ filled: NotificationContent.Filled?) {
        let (content, handler): (UNMutableNotificationContent?, ((UNNotificationContent) -> Void)?) = lock.withLock {
            defer {
                self.content = nil
                self.handler = nil
            }
            return (self.content, self.handler)
        }
        guard let content, let handler else { return }
        filled?.apply(to: content)
        handler(content)
    }

    /// The system's time is up: deliver the title alone.
    public func expire() {
        deliver(nil)
    }
}

/// The app's own links: `shaft://chat/<instance>` opens that instance's chat, as a notification tap does;
/// `shaft://chat/<instance>/<cid>` one thread of it (`cid` `from`: its From feed).
public enum AppLinks {
    public static func chat(_ instance: String, cid: String? = nil) -> URL? {
        URL(string: "shaft://chat/\(instance)" + (cid.map { "/\($0)" } ?? ""))
    }

    /// The instance a link names, nil for any other link (or a name the server could not have: §1.3).
    public static func instance(in url: URL) -> String? {
        target(in: url)?.instance
    }

    /// The instance a link names and the thread, when it names one.
    public static func target(in url: URL) -> ChatTarget? {
        guard url.scheme == "shaft", url.host() == "chat" else { return nil }
        let parts = url.pathComponents.dropFirst()
        let name = parts.first ?? ""
        guard Self.isName(name) else { return nil }
        let cid = parts.dropFirst().first
        guard let cid else { return ChatTarget(instance: name, cid: nil) }
        return Self.isName(cid) ? ChatTarget(instance: name, cid: cid) : nil
    }

    private static func isName(_ text: String) -> Bool {
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        return !text.isEmpty && text.unicodeScalars.allSatisfy(allowed.contains)
    }
}

/// A conversation a notification or a link asks for: the instance, and the thread (a chat id, `"from"`) when it names
/// one. `ref` is what the app opens; nil leaves it to the instance's default.
public struct ChatTarget: Hashable, Sendable {
    public var instance: String
    public var cid: String?

    public init(instance: String, cid: String? = nil) {
        self.instance = instance
        self.cid = cid
    }

    public var ref: ChatRef? { ChatRef(cid: cid) ?? .from }
}
