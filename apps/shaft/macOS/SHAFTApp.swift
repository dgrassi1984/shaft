import AppIntents
import AppKit
import SHAFTKit
import SwiftUI
import UserNotifications

/// The Mac app's entry point: the main, Meetings and Settings windows, and the menu commands.
/// The app delegate takes the push token and
/// notification clicks, hands them to `AppModel`, and keeps the app running with its window closed, so a push still
/// arrives and a click brings the window back on that chat.
@main
struct SHAFTApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        Window("SHAFT", id: MacRoot.windowID) {
            MacRoot(model: delegate.model, state: delegate.state, setup: delegate.setup)
                .environment(\.appUpdates, delegate.updates)
                .modifier(MacAppearance())
        }
        .defaultSize(width: 1100, height: 760)
        .commands {
            MacCommands(state: delegate.state, menu: delegate.state.menu)
            CommandGroup(after: .appInfo) { CheckForUpdatesCommand(updates: delegate.updates) }
            CommandMenu("Meeting") { MeetingMenuItems(recorder: delegate.meetings) }
        }

        Window("Meetings", id: MeetingsWindow.windowID) {
            if let client = delegate.model.session?.store?.client {
                MeetingsWindow(client: client, recorder: delegate.meetings, model: delegate.model)
                    .id(ObjectIdentifier(client))
                    .modifier(MacAppearance())
            } else {
                MarkMessage("Meetings unavailable", message: Text("Pair this Mac in the SHAFT window first."))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .defaultSize(width: 960, height: 700)

        // Meeting recording from the menu bar, also with the window closed (docs/meetings.md).
        MenuBarExtra {
            MeetingMenuItems(recorder: delegate.meetings)
        } label: {
            MeetingMenuLabel(recorder: delegate.meetings)
        }

        Settings {
            MacSettings(model: delegate.model, setup: delegate.setup, updates: delegate.updates)
                .modifier(MacAppearance())
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let model = MacDevSession.model() ?? AppModel()
    let state = MacState()
    /// The setup gate: the wizard, or pairing with this Mac's SHAFT without a link.
    let setup = MacSetup()
    /// Self-update, in the Developer ID download only (`AppUpdates`, `UpdateFeed`).
    let updates = AppUpdates()
    /// Meeting recording: the capture, and sending each recording to the paired server.
    /// A dev run (`SHAFT_DEV_DIR`) keeps its recordings in that folder: at launch the recorder sends every pending one to
    /// the paired server, and a dev run's server is a fixture, never the place for the operator's real recordings.
    lazy var meetings = MeetingRecorder(client: { [weak self] in self?.model.session?.store?.client },
                                        folder: MacDevSession.directory?.appending(path: "Meetings", directoryHint: .isDirectory)
                                            ?? MeetingRecorder.defaultFolder)
    private var keyMonitor: Any?
    @AppStorage(AppTheme.storageKey) private var rawTheme = AppTheme.system.rawValue

    override init() {
        super.init()
        let openChat: OpenInstanceChatIntent.ChatOpener = { [weak self] instance in
            guard let self else { return }
            model.notificationTapped(instance: instance)
            state.bringForward()
        }
        AppDependencyManager.shared.add(dependency: openChat)
        let meetingActions = MeetingIntentActions(
            start: { [weak self] online in
                guard let recorder = self?.meetings else { return "Open SHAFT to record a meeting." }
                guard !recorder.isRecording else { return "A meeting is already recording." }
                guard !recorder.starting else { return "A recording is already starting." }
                await recorder.start(online: online)
                if recorder.isRecording { return "Recording started" }
                if case .failed(let problem) = recorder.phase { return "\(problem)" }
                return "Could not start recording."
            },
            stop: { [weak self] in
                guard let self, meetings.isRecording else { return "No meeting is recording." }
                await meetings.stop()
                if model.session?.store?.client == nil { return "Recording stopped. Pair with SHAFT to send it." }
                if let warning = meetings.warning { return "\(warning) Upload queued." }
                return "Recording stopped. Upload queued."
            })
        AppDependencyManager.shared.add(dependency: meetingActions)
    }

    func applicationWillFinishLaunching(_ notification: Notification) {
        AppTheme.stored(rawTheme).apply()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // A recording whose upload did not finish (the app quit, no server) goes out once the session has opened.
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(15))
            await self?.meetings.sendPending()
        }
        HitchMonitor.start()
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
        model.registerForRemoteNotifications = { NSApplication.shared.registerForRemoteNotifications() }
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

    /// Quitting while recording finishes the meeting's file first (review 2026-11-27: a quit left an .m4a without its
    /// index, which no decoder opens); it is sent at the next launch. The window and the menu bar say a recording is
    /// running; a Sparkle update relaunch also comes here.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        MainActor.assumeIsolated { meetings.finishForQuit() }
        return .terminateNow
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
        let userInfo = notification.request.content.userInfo
        let instance = NotificationContent.instance(from: userInfo)
        let cid = NotificationContent.cid(from: userInfo)
        let model = model
        return await MainActor.run { AppModel.presentation(for: instance, cid: cid, visibleChat: model.visibleChat) }
    }

    /// A notification clicked: the window comes forward (opened again if it was closed) on that chat.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse) async {
        let userInfo = response.notification.request.content.userInfo
        let instance = NotificationContent.instance(from: userInfo)
        let cid = NotificationContent.cid(from: userInfo)
        let meeting = NotificationContent.meeting(from: userInfo)
        if response.actionIdentifier != UNNotificationDefaultActionIdentifier {
            guard response.actionIdentifier != UNNotificationDismissActionIdentifier else { return }
            let request = NotificationQuestion.answer(actionIdentifier: response.actionIdentifier, userInfo: userInfo,
                typedText: (response as? UNTextInputNotificationResponse)?.userText,
                requestIdentifier: response.notification.request.identifier)
            await model.answerNotification(request, instance: instance)
            return
        }
        await MainActor.run {
            model.notificationTapped(instance: instance, cid: cid, meeting: meeting)
            if meeting != nil { state.bringMeetingsForward() } else { state.bringForward() }
        }
    }
}

/// The Settings window (⌘,): the server, this device, notifications, Update server and Unpair (Shared's SettingsView),
/// the app's own updates among them. Before pairing it still offers the app's updates.
struct MacSettings: View {
    let model: AppModel
    let setup: MacSetup
    let updates: AppUpdates

    var body: some View {
        Group {
            if let session = model.session {
                SettingsView(session: session)
                    .environment(\.serverUpdater, setup.updater(for: session))
                    .environment(\.phoneAccess, setup.phoneAccess(for: session))
                    .environment(\.serverKeys, setup.serverKeys(for: session))
            } else {
                VStack(spacing: 0) {
                    Form {
                        Section("Updates") { AppUpdatesRows(updates: updates) }
                            .listRowBackground(Color.elevated)
                    }
                    .formStyle(.grouped)
                    .frame(height: 150)
                    MarkMessage("Not paired", message: Text("Pair this Mac in the SHAFT window first."))
                }
            }
        }
        .environment(model)
        .environment(\.appUpdates, updates)
        .frame(width: 480, height: 556)
        .scrollContentBackground(.hidden)
        .containerBackground(Color.canvas, for: .window)
    }
}

private struct MacAppearance: ViewModifier {
    @AppStorage(AppTheme.storageKey) private var rawTheme = AppTheme.system.rawValue

    func body(content: Content) -> some View {
        content.onChange(of: rawTheme, initial: true) { _, raw in AppTheme.stored(raw).apply() }
    }
}

private extension AppTheme {
    @MainActor func apply() {
        switch self {
        case .system: NSApp.appearance = nil
        case .light: NSApp.appearance = NSAppearance(named: .aqua)
        case .dark: NSApp.appearance = NSAppearance(named: .darkAqua)
        }
    }
}
