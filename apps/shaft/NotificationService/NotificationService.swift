import SHAFTKit
import UserNotifications

/// The notification service extension of both apps (iOS and macOS build this same file). The APNs payload carries the
/// title only; this fetches the body from `/app/v1/push/latest` with the app's shared Keychain credential, and leaves
/// the title alone on any failure or when the system's time runs out (SHAFTKit `NotificationDelivery`).
final class NotificationService: UNNotificationServiceExtension {
    private var delivery: NotificationDelivery?

    override func didReceive(_ request: UNNotificationRequest,
                             withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        guard let content = request.content.mutableCopy() as? UNMutableNotificationContent else {
            contentHandler(request.content)
            return
        }
        let delivery = NotificationDelivery(content: content, handler: contentHandler)
        self.delivery = delivery
        guard let store = try? KeychainStore.forMainBundle() else {
            delivery.expire()
            return
        }
        delivery.run(store: store)
    }

    override func serviceExtensionTimeWillExpire() {
        delivery?.expire()
    }
}
