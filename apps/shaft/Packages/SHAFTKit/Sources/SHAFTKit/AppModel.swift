import Foundation
import Observation
import UserNotifications

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
    /// The instance whose chat a notification tap asked for: the UI opens it, then sets this to nil.
    public var openChat: String?

    public init() {
        do {
            session = try SHAFTSession.standard()
        } catch {
            startupError = "SHAFT could not start: \(error.localizedDescription)"
        }
    }

    public init(session: SHAFTSession) {
        self.session = session
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

    /// A notification was tapped: open the chat it is about.
    public func notificationTapped(instance: String?) {
        if let instance { openChat = instance }
    }

    /// Show the server's badge (§12) on the app icon.
    public func setBadge(_ count: Int) {
        UNUserNotificationCenter.current().setBadgeCount(count) { _ in }
    }
}
