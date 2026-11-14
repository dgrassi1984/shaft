import CryptoKit
import Foundation
import UserNotifications

/// The question carried by a push event. The question's cid also covers the From feed.
public struct NotificationQuestion: Codable, Sendable, Equatable {
    public var file: String
    public var cid: String
    public var choices: Choices

    public struct Category: Sendable, Equatable {
        public struct Action: Sendable, Equatable {
            public let identifier: String
            public let title: String
            public let textInputButtonTitle: String?
            public let textInputPlaceholder: String?
        }
        public let identifier: String
        public let actions: [Action]

        var notificationCategory: UNNotificationCategory {
            let actions: [UNNotificationAction] = actions.map { action in
                if let button = action.textInputButtonTitle {
                    return UNTextInputNotificationAction(identifier: action.identifier, title: action.title,
                        options: [], textInputButtonTitle: button,
                        textInputPlaceholder: action.textInputPlaceholder ?? "")
                }
                return UNNotificationAction(identifier: action.identifier, title: action.title, options: [])
            }
            return UNNotificationCategory(identifier: identifier, actions: actions, intentIdentifiers: [], options: [])
        }
    }

    /// A deterministic description, independent of the notification center and Swift's randomized Hasher. At most
    /// three options: with Other they fill the four actions a notification shows (review 2026-11-14: a fourth option
    /// made a fifth action, and Other could not show).
    public var category: Category? {
        guard !choices.multiple, (2...3).contains(choices.options.count) else { return nil }
        let hash = SHA256.hash(data: Data(file.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
        var actions = choices.options.enumerated().map { index, option in
            Category.Action(identifier: "choice.\(index)", title: option.label,
                            textInputButtonTitle: nil, textInputPlaceholder: nil)
        }
        actions.append(Category.Action(identifier: "other", title: "Other…",
                                       textInputButtonTitle: "Send", textInputPlaceholder: "Your answer"))
        return Category(identifier: "shaft.question.\(hash)", actions: actions)
    }

    /// Nil for a tap or unusable metadata. The option count comes from the extension's fetched choices.
    public static func answer(actionIdentifier: String, userInfo: [AnyHashable: Any], typedText: String?,
                              requestIdentifier: String) -> ChoiceAnswerRequest? {
        guard let file = userInfo["question_file"] as? String, !file.isEmpty,
              let cid = NotificationContent.cid(from: userInfo),
              let count = userInfo["question_option_count"] as? Int, (2...3).contains(count) else { return nil }
        let picked: [Int]
        let other: String?
        if actionIdentifier == "other" {
            guard let text = typedText, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  text.count <= 2000 else { return nil }
            picked = []
            other = text
        } else {
            guard actionIdentifier.hasPrefix("choice."), let index = Int(actionIdentifier.dropFirst(7)),
                  actionIdentifier == "choice.\(index)", (0..<count).contains(index) else { return nil }
            picked = [index]
            other = nil
        }
        return ChoiceAnswerRequest(cid: cid, file: file, picked: picked, other: other,
                                   mid: "notif-" + requestIdentifier)
    }

    /// Newest first, retaining non-question categories separately at registration time.
    static func recentCategories(adding identifier: String, to recent: [String]) -> [String] {
        Array(([identifier] + recent.filter { $0 != identifier }).prefix(20))
    }
}

/// Serialize the extension's read/modify/register operations. Persist order across extension launches because the
/// notification center returns an unordered set. The defaults contain category identifiers only.
actor QuestionCategoryRegistry {
    static let shared = QuestionCategoryRegistry()
    private var pending: Task<Bool, Never>?

    func register(_ category: NotificationQuestion.Category) async -> Bool {
        let previous = pending
        let task = Task {
            _ = await previous?.value
            return await Self.install(category)
        }
        pending = task
        return await task.value
    }

    private static func install(_ category: NotificationQuestion.Category) async -> Bool {
        let key = "shaft.question.categories"
        let center = UNUserNotificationCenter.current()
        let existing = await center.notificationCategories()
        let recent = NotificationQuestion.recentCategories(adding: category.identifier,
            to: UserDefaults.standard.stringArray(forKey: key) ?? [])
        var kept = existing.filter {
            !$0.identifier.hasPrefix("shaft.question.") || recent.contains($0.identifier)
        }
        kept = kept.filter { $0.identifier != category.identifier }
        kept.insert(category.notificationCategory)
        center.setNotificationCategories(kept)
        UserDefaults.standard.set(recent, forKey: key)
        // Read back before delivering content that names the category. Expiry can still deliver while this waits.
        return await center.notificationCategories().contains { $0.identifier == category.identifier }
    }
}
