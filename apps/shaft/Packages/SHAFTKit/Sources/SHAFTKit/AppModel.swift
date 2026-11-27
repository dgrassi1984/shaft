import Foundation
import Observation
import UserNotifications
#if os(macOS)
import AppKit
#endif

/// What both app entry points hold: the session (or why it could not open), the push token's way to the server, and
/// the chat a notification tap asked for. The platform delegates (UIKit, AppKit) feed it; the UI reads it.
@MainActor
@Observable
public final class AppModel {
    public private(set) var session: SHAFTSession?
    /// Why the session could not open (the Keychain group missing from the build, unreadable storage).
    public private(set) var startupError: String?
    /// Why the system gave no push token, when it did not.
    public private(set) var pushError: String?
    /// The chat a notification tap (or a link) asked for, its thread when it named one: the UI opens it, then sets this
    /// to nil.
    public var openChat: ChatTarget?
    /// The meeting a notification tap asked for (ready or failed): the Meetings screen opens on it, then sets this to
    /// nil.
    public var openMeeting: String?
    /// The chat on screen while the app is in front (its thread's `cid`, `"from"`, or nil for the current chat), nil
    /// otherwise: a notification about it shows no banner.
    public var visibleChat: ChatTarget?

    private let start: () throws -> SHAFTSession

    public convenience init() {
        self.init(start: {
            #if os(iOS)
            // The app's defaults disappear on uninstall; the shared Keychain item does not.
            try PairingStore(store: KeychainStore.forMainBundle()).forgetOnFirstLaunch(defaults: .standard)
            #endif
            return try SHAFTSession.standard()
        })
    }

    /// `start` opens the session; a failure is `startupError`, and `retryStart()` runs it again.
    init(start: @escaping () throws -> SHAFTSession) {
        self.start = start
        retryStart()
    }

    /// Run the startup again (the Try again of the "could not start" screen), e.g. after the cause was removed.
    public func retryStart() {
        do {
            session = try start()
            startupError = nil
            connectBadge()
        } catch {
            session = nil
            startupError = "SHAFT could not start: \(error.localizedDescription)"
        }
    }

    public init(session: SHAFTSession) {
        self.start = { session }
        self.session = session
        connectBadge()
    }

    private func connectBadge() {
        session?.onBadge = { [weak self] count in self?.setBadge(count) }
    }

    /// Ask for alerts, badges and sounds. True when allowed; the caller then registers for remote notifications.
    public func requestNotifications() async -> Bool {
        do {
            return try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound])
        } catch {
            pushError = error.localizedDescription
            return false
        }
    }

    /// The system's APNs token: sent to the server now, and again after a pairing.
    public func didRegister(token: Data) {
        pushError = nil
        Task { await session?.registerPush(token: token) }
    }

    public func didFailToRegister(_ error: any Error) {
        pushError = "No push token: \(error.localizedDescription)"
    }

    /// How a notification that arrives while the app is open shows: nothing but the badge for the chat already on
    /// screen (its feed shows the reply), a banner for another conversation. A push without cid names Main.
    nonisolated public static func presentation(for instance: String?, cid: String? = nil, visibleChat: ChatTarget?)
        -> UNNotificationPresentationOptions {
        if let instance, let visibleChat, instance == visibleChat.instance, (cid ?? ChatRef.fromID) == (visibleChat.cid ?? ChatRef.fromID) {
            return [.badge]
        }
        return [.banner, .list, .sound, .badge]
    }

    /// A notification was tapped: open the meeting it is about, else the chat it is about (its thread when it names
    /// one).
    public func notificationTapped(instance: String?, cid: String? = nil, meeting: String? = nil) {
        if let meeting { openMeeting = meeting }
        else if let instance { openChat = ChatTarget(instance: instance, cid: cid) }
    }

    /// Notification actions run without opening a chat. Every refusal, including an answer made elsewhere, is shown.
    public func answerNotification(_ request: ChoiceAnswerRequest?, instance: String?) async {
        let reason: String
        do {
            guard let request, let instance else {
                throw APIError.badLink("The notification has no valid question or answer. Open the chat to answer it.")
            }
            guard let client = session?.store?.client else { throw APIError.notPaired }
            _ = try await client.answer(instance, request)
            return
        } catch {
            reason = APIError.from(error)?.message ?? "Sending was interrupted. Open the chat to check the answer."
        }
        let content = UNMutableNotificationContent()
        content.title = "Answer not sent"
        content.body = "Your answer was not sent. \(reason)"
        content.sound = .default
        // No instance metadata: this failure must show a banner even when its chat is already visible.
        let notification = UNNotificationRequest(identifier: "answer-failed-" + (request?.mid ?? UUID().uuidString),
                                                  content: content, trigger: nil)
        do {
            try await UNUserNotificationCenter.current().add(notification)
        } catch {
            pushError = "\(content.body) The failure notification could not be posted: \(error.localizedDescription)"
        }
    }

    /// Show the server's badge (§12) on the app icon.
    public func setBadge(_ count: Int) {
        UNUserNotificationCenter.current().setBadgeCount(count) { _ in }
        #if os(macOS)
        NSApp.dockTile.badgeLabel = count > 0 ? "\(count)" : nil
        #endif
    }
}
