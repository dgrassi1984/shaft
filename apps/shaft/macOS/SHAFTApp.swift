import AppKit
import SHAFTKit
import SwiftUI
import UserNotifications

/// The Mac app's entry point (native SwiftUI, not Catalyst): the app delegate takes the push token and notification
/// taps, and hands them to `AppModel`. The UI lane replaces `PlaceholderRoot` with the real root view.
@main
struct SHAFTApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            PlaceholderRoot(model: delegate.model)
                .frame(minWidth: 480, minHeight: 360)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let model = AppModel()

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        Task {
            // APNs may change the token: register at every launch (§11.1).
            if await model.requestNotifications() { NSApplication.shared.registerForRemoteNotifications() }
        }
    }

    func application(_ application: NSApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        model.didRegister(token: deviceToken)
    }

    func application(_ application: NSApplication, didFailToRegisterForRemoteNotificationsWithError error: any Error) {
        model.didFailToRegister(error)
    }

    // The center calls these off the main actor: read what is needed there, then hop.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification)
        async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound, .badge]
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse) async {
        let instance = NotificationContent.instance(from: response.notification.request.content.userInfo)
        await MainActor.run { model.notificationTapped(instance: instance) }
    }
}
