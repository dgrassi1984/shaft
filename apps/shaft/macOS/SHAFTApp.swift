import AppKit
import SHAFTKit
import SwiftUI
import UserNotifications

/// The Mac app's entry point (native SwiftUI, not Catalyst): one window (`MacRoot`: the instances, the chat, its
/// documents), the menu commands (`MacCommands`) and a Settings window. The app delegate takes the push token and
/// notification clicks, hands them to `AppModel`, and keeps the app running with its window closed, so a push still
/// arrives and a click brings the window back on that chat.
@main
struct SHAFTApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        Window("SHAFT", id: MacRoot.windowID) {
            MacRoot(model: delegate.model, state: delegate.state)
        }
        .defaultSize(width: 1100, height: 760)
        .commands { MacCommands(state: delegate.state, menu: delegate.state.menu) }

        Settings {
            MacSettings(model: delegate.model)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let model = MacDevSession.model() ?? AppModel()
    let state = MacState()
    private var keyMonitor: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        #if DEBUG
        FrameMeter.startIfAsked()
        #endif
        UNUserNotificationCenter.current().delegate = self
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, self.slashFocusesComposer(event) else { return event }
            return nil
        }
        #if DEBUG
        MacDevDriver.delegateMonitor = { [weak self] event in
            guard let self else { return .some(event) }
            return self.slashFocusesComposer(event) ? .some(nil) : .some(event)
        }
        #endif
        if MacDevSession.asksForNotifications {
            Task {
                // APNs may change the token: register at every launch (§11.1).
                if await model.requestNotifications() { NSApplication.shared.registerForRemoteNotifications() }
            }
        }
        MacDevSession.pairIfAsked(model)
        #if DEBUG
        MacDevDriver.start(state: state, model: model, delegate: self)
        #endif
    }

    /// A push still arrives, and the Dock icon still shows the badge, with the window closed.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    /// The Dock icon clicked with no window open brings the window back.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { state.bringForward() }
        return true
    }

    func application(_ application: NSApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        model.didRegister(token: deviceToken)
    }

    func application(_ application: NSApplication, didFailToRegisterForRemoteNotificationsWithError error: any Error) {
        model.didFailToRegister(error)
    }

    /// "/" writes, as on the web: it focuses the composer unless the keyboard is already in a text field.
    private func slashFocusesComposer(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection([.command, .control, .option])
        guard event.characters == "/", flags.isEmpty, let window = event.window,
              window.identifier?.rawValue.hasPrefix(MacRoot.windowID) == true, state.currentScreen != nil else {
            return false
        }
        if let field = window.firstResponder as? NSTextView, field.isEditable { return false }
        state.focusComposer()
        return true
    }

    // The center calls these off the main actor: read what is needed there, then hop. The chat on screen in the
    // window in front shows its reply itself: no banner for it.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification)
        async -> UNNotificationPresentationOptions {
        let instance = NotificationContent.instance(from: notification.request.content.userInfo)
        let model = model
        return await MainActor.run { AppModel.presentation(for: instance, visibleChat: model.visibleChat) }
    }

    /// A notification clicked: the window comes forward (opened again if it was closed) on that chat.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse) async {
        let instance = NotificationContent.instance(from: response.notification.request.content.userInfo)
        await MainActor.run {
            model.notificationTapped(instance: instance)
            state.bringForward()
        }
    }
}

/// The Settings window (⌘,): the server, this device, notifications and Unpair (Shared's SettingsView).
struct MacSettings: View {
    let model: AppModel

    var body: some View {
        Group {
            if let session = model.session, session.isPaired {
                SettingsView(session: session)
            } else {
                ContentUnavailableView("Not paired", systemImage: "link",
                                       description: Text("Pair this Mac in the SHAFT window first."))
            }
        }
        .environment(model)
        .frame(width: 480, height: 520)
        .scrollContentBackground(.hidden)
        .containerBackground(Color.canvas, for: .window)
    }
}
