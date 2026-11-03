import Foundation
import UserNotifications

/// The notification service extensions' work (§11): the APNs payload carries only the title, the badge and the
/// instance; the body is fetched from `/app/v1/push/latest` over the tailnet with the app's shared credential. Any
/// failure (not paired, offline, too slow, nothing for this instance) leaves the notification as it came: title only.
public enum NotificationContent {
    /// The instance a notification is about: the payload's `instance` (PLAN §2.2), for the tap to open its chat.
    public static func instance(from userInfo: [AnyHashable: Any]) -> String? {
        userInfo["instance"] as? String
    }

    /// What to show for a push about `instance`: the newest queued event for it. Nil on any failure. `session` is for
    /// tests; the extension's own is ephemeral, with `timeout` on the whole fetch.
    public static func fetch(instance: String?, store: any CredentialStore, timeout: TimeInterval = 20,
                             session: URLSession? = nil) async -> Filled? {
        guard let credential = try? store.load() else { return nil }
        let own: URLSession? = session == nil ? {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = timeout
            configuration.timeoutIntervalForResource = timeout
            return URLSession(configuration: configuration)
        }() : nil
        defer { own?.finishTasksAndInvalidate() }
        let client = APIClient(credential: credential, session: session ?? own!)
        guard let latest = try? await client.pushLatest() else { return nil }
        return choose(latest, instance: instance)
    }

    /// The event to show: the newest for `instance` (or the newest of all when the payload named none). Nil when no
    /// event matches: a body from another chat would be wrong, so the title stays alone.
    public static func choose(_ latest: PushLatest, instance: String?) -> Filled? {
        let event = instance.map { name in latest.events.last { $0.instance == name } } ?? latest.events.last
        guard let event else { return nil }
        return Filled(title: event.title, body: event.body, badge: latest.badge, instance: event.instance)
    }

    /// What `fetch` found, to lay onto the notification.
    public struct Filled: Sendable, Equatable {
        public var title: String
        public var body: String
        public var badge: Int
        public var instance: String?

        public func apply(to content: UNMutableNotificationContent) {
            if !title.isEmpty { content.title = title }
            content.body = body
            content.badge = NSNumber(value: badge)
            if let instance {
                content.threadIdentifier = instance
                content.userInfo["instance"] = instance
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
        let instance = lock.withLock { content.flatMap { NotificationContent.instance(from: $0.userInfo) } }
        Task {
            let filled = await NotificationContent.fetch(instance: instance, store: store, timeout: timeout)
            self.deliver(filled)
        }
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
