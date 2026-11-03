import AppKit
import SHAFTKit
import SwiftUI
import UserNotifications

/// The Mac app's entry point (native SwiftUI, not Catalyst): the app delegate takes the push token and notification
/// taps, and hands them to `AppModel`. `MacRoot` is a plain split view over the shared views until the Mac lane (M5)
/// builds the Mac's own.
@main
struct SHAFTApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            MacRoot(model: delegate.model)
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

/// The shared views in a sidebar and a detail column: the Mac lane's starting point, not its finished UI.
struct MacRoot: View {
    let model: AppModel

    var body: some View {
        SessionRoot(model: model) { session, store in
            MacChats(session: session, store: store)
        }
    }
}

struct MacChats: View {
    let session: SHAFTSession
    let store: InstancesStore
    @Environment(AppModel.self) private var model
    @State private var selection: String?
    @State private var settings = false
    @State private var shell = ChatShell()

    var body: some View {
        NavigationSplitView {
            InstancesList(selection: $selection, settings: { settings = true })
        } detail: {
            NavigationStack {
                if let selection {
                    ChatScreen(chat: store.chat(selection)).id(selection)
                } else {
                    Text("Choose an instance to chat with.").foregroundStyle(.secondary)
                }
            }
            .environment(shell)
        }
        .documentsInspector(shell, store: store)
        .sheet(isPresented: $settings) { SettingsView(session: session) }
        .onChange(of: model.openChat) { _, name in
            guard let name else { return }
            selection = name
            model.openChat = nil
        }
    }
}
