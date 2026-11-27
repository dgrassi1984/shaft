import AppIntents
import SHAFTKit
import SwiftUI
import UIKit
import UserNotifications

/// The iPhone and iPad app's entry point: the app delegate takes the push token and notification taps, and hands them
/// to `AppModel`; `IOSRoot` draws the chats.
@main
struct SHAFTApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage(AppTheme.storageKey) private var rawTheme = AppTheme.system.rawValue

    private var meetingClient: APIClient? {
        guard let session = delegate.model.session, !session.isDemo else { return nil }
        return session.store?.client
    }

    var body: some Scene {
        WindowGroup {
            IOSRoot(model: delegate.model, recorder: delegate.meetings)
                .onChange(of: scenePhase, initial: true) { updateMeetingUploads() }
                .onChange(of: meetingClient.map(ObjectIdentifier.init)) { updateMeetingUploads() }
                .preferredColorScheme(AppTheme.stored(rawTheme).colorScheme)
                // shaft://chat/<instance>: the chat a notification tap opens, from a link or a shortcut.
                .onOpenURL { url in
                    let target = AppLinks.target(in: url)
                    delegate.model.notificationTapped(instance: target?.instance, cid: target?.cid)
                }
        }
    }

    private func updateMeetingUploads() {
        delegate.meetings.updateUploadContext(foreground: scenePhase == .active, client: meetingClient)
    }
}

@MainActor
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    let model = AppModel()

    override init() {
        super.init()
        let openChat: OpenInstanceChatIntent.ChatOpener = { [weak self] instance in
            self?.model.notificationTapped(instance: instance)
        }
        AppDependencyManager.shared.add(dependency: openChat)
        let meetingActions = MeetingIntentActions(
            start: { [weak self] _ in
                guard UIApplication.shared.applicationState != .background else {
                    return "Open SHAFT to record a meeting."
                }
                guard let recorder = self?.meetings else { return "Open SHAFT to record a meeting." }
                guard recorder.started == nil else { return "A meeting is already recording." }
                guard !recorder.starting else { return "A recording is already starting." }
                ReadAloudRequests.shared.stop()
                await recorder.start()
                if recorder.started != nil { return "Recording started" }
                if let problem = recorder.problem { return "\(problem)" }
                return "Could not start recording."
            },
            stop: { [weak self] in
                guard let self, meetings.started != nil else { return "No meeting is recording." }
                meetings.stop()
                if let problem = meetings.problem { return "\(problem)" }
                guard let session = model.session, !session.isDemo, session.store?.client != nil else {
                    return "Recording stopped. Pair with SHAFT to send it."
                }
                if !meetings.uploadsInForeground { return "Recording stopped. Open SHAFT to send it." }
                return "Recording stopped. Upload queued."
            })
        AppDependencyManager.shared.add(dependency: meetingActions)
    }

    lazy var meetings = IOSMeetingRecorder(audio: SystemMeetingAudio(), audioUse: .shared,
                                           device: { UIDevice.current.name }, startActivity: SystemMeetingActivity.start)
    /// The Apple Watch app's way to the server (it has none of its own).
    private lazy var watch = WatchRelay(model: model)

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        HitchMonitor.start()
        #if DEBUG
        FrameMeter.startIfAsked()
        // SHAFT_START_DEMO=1 (SIMCTL_CHILD_SHAFT_START_DEMO through simctl): open the demo at launch, so the watch app
        // has the iPhone's sample instances for its App Store screenshot (AppStore/take-screenshots.sh).
        if ProcessInfo.processInfo.environment["SHAFT_START_DEMO"] == "1" { model.session?.startDemo() }
        #endif
        UNUserNotificationCenter.current().delegate = self
        // At launch, before anything waits: a watch message may be what woke the app.
        watch.start()
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
        let userInfo = notification.request.content.userInfo
        let instance = NotificationContent.instance(from: userInfo)
        let cid = NotificationContent.cid(from: userInfo)
        let model = model
        return await MainActor.run { AppModel.presentation(for: instance, cid: cid, visibleChat: model.visibleChat) }
    }

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
            await sendAnswer(request, instance: instance)
            return
        }
        await MainActor.run { model.notificationTapped(instance: instance, cid: cid, meeting: meeting) }
    }

    private func sendAnswer(_ request: ChoiceAnswerRequest?, instance: String?) async {
        let background = BackgroundTask(name: "Notification answer")
        let send = Task { await model.answerNotification(request, instance: instance) }
        background.onExpiration = { send.cancel() }
        defer { background.end() }
        await send.value
    }
}
