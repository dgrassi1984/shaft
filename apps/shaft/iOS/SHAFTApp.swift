import SHAFTKit
import SwiftUI
import UIKit
import UserNotifications

/// The iPhone and iPad app's entry point: the app delegate takes the push token and notification taps, and hands them
/// to `AppModel`; `IOSRoot` draws the chats.
@main
struct SHAFTApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            IOSRoot(model: delegate.model)
                // shaft://chat/<instance>: the chat a notification tap opens, from a link or a shortcut.
                .onOpenURL { url in delegate.model.notificationTapped(instance: AppLinks.instance(in: url)) }
        }
    }
}

@MainActor
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    let model = AppModel()

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        Task {
            // APNs may change the token: register at every launch (§11.1).
            if await model.requestNotifications() { application.registerForRemoteNotifications() }
        }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        model.didRegister(token: deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: any Error) {
        model.didFailToRegister(error)
    }

    // The center calls these off the main actor: read what is needed there, then hop. The chat on screen shows its
    // reply itself: no banner for it.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification)
        async -> UNNotificationPresentationOptions {
        let instance = NotificationContent.instance(from: notification.request.content.userInfo)
        let model = model
        return await MainActor.run { AppModel.presentation(for: instance, visibleChat: model.visibleChat) }
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse) async {
        let instance = NotificationContent.instance(from: response.notification.request.content.userInfo)
        await MainActor.run { model.notificationTapped(instance: instance) }
    }
}
