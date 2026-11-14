import Foundation
import Testing
import UserNotifications
@testable import SHAFTKit

struct NotificationQuestionTests {
    private let question = NotificationQuestion(file: "tundra-question.md", cid: "from",
                                                choices: QuestionAnswerTests.choices)
    private var info: [AnyHashable: Any] {
        ["instance": "tundra", "cid": "from", "question_file": "tundra-question.md", "question_option_count": 2]
    }

    @Test func latestDecodesQuestionAndUsesItsCID() async throws {
        let server = StubServer { _ in
            .json(200, #"{"events":[{"title":"tundra asks","body":"When?","url":"/i/tundra/chat","tag":"tundra","instance":"tundra","question":{"file":"tundra-question.md","cid":"from","choices":{"options":[{"label":"Now"},{"label":"Wait"}],"multiple":false}}}],"badge":1}"#)
        }
        let latest = try await server.client.pushLatest(instance: "tundra")
        #expect(latest.events.first?.question?.file == "tundra-question.md")
        #expect(latest.events.first?.question?.choices.options.map(\.label) == ["Now", "Wait"])
        let filled = try #require(NotificationContent.choose(latest, instance: "tundra"))
        #expect(filled.question?.cid == "from")
        let content = UNMutableNotificationContent()
        filled.apply(to: content)
        #expect(content.userInfo["question_file"] as? String == "tundra-question.md")
        #expect(content.userInfo["cid"] as? String == "from")
        #expect(content.userInfo["question_option_count"] as? Int == 2)
        #expect(content.categoryIdentifier == question.category?.identifier)
    }

    @Test func absentAndNullQuestionsLeaveNoCategory() throws {
        for field in ["", ",\"question\":null"] {
            let data = Data("""
                {"events":[{"title":"wisp replied","body":"Ready","url":"/i/wisp/chat","tag":"wisp","instance":"wisp"\(field)}],"badge":1}
                """.utf8)
            let latest = try SHAFTJSON.decoder().decode(PushLatest.self, from: data)
            #expect(latest.events.first?.question == nil)
            let content = UNMutableNotificationContent()
            content.categoryIdentifier = "stale"
            content.userInfo["question_file"] = "stale.md"
            NotificationContent.choose(latest, instance: "wisp")?.apply(to: content)
            #expect(content.categoryIdentifier.isEmpty)
            #expect(content.userInfo["question_file"] == nil)
        }
    }

    @Test func categoryHasStableFileHashAndBackgroundActions() throws {
        let category = try #require(question.category)
        // SHA256("tundra-question.md"), first eight bytes. Literal catches a changed hash algorithm.
        #expect(category.identifier == "shaft.question.930a0270040a222a")
        #expect(category.actions.map(\.identifier) == ["choice.0", "choice.1", "other"])
        #expect(category.actions.map(\.title) == ["Now", "Wait", "Other…"])
        #expect(category.actions.last?.textInputButtonTitle == "Send")
        #expect(category.actions.last?.textInputPlaceholder == "Your answer")
        let native = category.notificationCategory
        #expect(native.actions.allSatisfy { !$0.options.contains(.foreground) })
        let other = try #require(native.actions.last as? UNTextInputNotificationAction)
        #expect(other.textInputButtonTitle == "Send")
        #expect(other.textInputPlaceholder == "Your answer")
        var sameFile = question
        sameFile.cid = "web-ada"
        #expect(sameFile.category?.identifier == category.identifier)
        sameFile.file = "wisp-question.md"
        #expect(sameFile.category?.identifier != category.identifier)
    }

    @Test func fourOptionsWorkButMoreOrMultipleOpenTheChat() {
        var value = question
        value.choices.options = (0..<4).map { ChoiceOption(label: "Option \($0)", detail: nil) }
        #expect(value.category?.actions.map(\.identifier) == ["choice.0", "choice.1", "choice.2", "choice.3", "other"])
        value.choices.options.append(ChoiceOption(label: "Fifth", detail: nil))
        #expect(value.category == nil)
        value.choices = question.choices
        value.choices.multiple = true
        #expect(value.category == nil)
        var filled = NotificationContent.Filled(title: "tundra asks", body: "When?", badge: 1, instance: "tundra")
        filled.question = value
        let content = UNMutableNotificationContent()
        filled.apply(to: content)
        #expect(content.categoryIdentifier.isEmpty)
        #expect(content.userInfo["question_file"] as? String == value.file)
        #expect(content.userInfo["cid"] as? String == "from")
    }

    @Test func choiceAndOtherMapToFrozenRequests() throws {
        let picked = try #require(NotificationQuestion.answer(actionIdentifier: "choice.1", userInfo: info,
            typedText: nil, requestIdentifier: "push-ada"))
        #expect(picked == ChoiceAnswerRequest(cid: "from", file: "tundra-question.md", picked: [1],
                                             other: nil, mid: "notif-push-ada"))
        #expect(NotificationQuestion.answer(actionIdentifier: "choice.1", userInfo: info,
            typedText: nil, requestIdentifier: "push-ada") == picked)
        let other = NotificationQuestion.answer(actionIdentifier: "other", userInfo: info,
            typedText: "Tomorrow", requestIdentifier: "push-wisp")
        #expect(other == ChoiceAnswerRequest(cid: "from", file: "tundra-question.md", picked: [],
                                            other: "Tomorrow", mid: "notif-push-wisp"))
        var thread = info
        thread["cid"] = "web-ada"
        #expect(NotificationQuestion.answer(actionIdentifier: "choice.0", userInfo: thread,
            typedText: nil, requestIdentifier: "push-ada")?.cid == "web-ada")
    }

    @Test func tapsMissingMetadataAndInvalidAnswersDoNotMakeRequests() {
        for action in [UNNotificationDefaultActionIdentifier, UNNotificationDismissActionIdentifier,
                       "choice.2", "choice.-1", "choice.x", "choice.01", "unknown"] {
            #expect(NotificationQuestion.answer(actionIdentifier: action, userInfo: info,
                typedText: nil, requestIdentifier: "push-ada") == nil)
        }
        for key in ["question_file", "cid", "question_option_count"] {
            var missing = info
            missing.removeValue(forKey: key)
            #expect(NotificationQuestion.answer(actionIdentifier: "choice.0", userInfo: missing,
                typedText: nil, requestIdentifier: "push-ada") == nil)
        }
        for text in [nil, " \n", String(repeating: "a", count: 2001)] as [String?] {
            #expect(NotificationQuestion.answer(actionIdentifier: "other", userInfo: info,
                typedText: text, requestIdentifier: "push-ada") == nil)
        }
    }

    @Test func categoryRetentionRefreshesExistingAndCapsAtTwenty() {
        let ids = (0..<20).map { "shaft.question.\($0)" }
        #expect(NotificationQuestion.recentCategories(adding: ids[10], to: ids)
                == [ids[10]] + ids.filter { $0 != ids[10] })
        #expect(NotificationQuestion.recentCategories(adding: "shaft.question.new", to: ids)
                == ["shaft.question.new"] + Array(ids.prefix(19)))
    }

    @Test func deliveryWaitsForRegistrationAndExpiryWinsOnce() async throws {
        for expires in [false, true] {
            let content = UNMutableNotificationContent()
            content.title = "Original"
            let delivered = Delivered()
            let delivery = NotificationDelivery(content: content) { delivered.add($0) }
            var filled = NotificationContent.Filled(title: "Filled", body: "When?", badge: 1, instance: "tundra")
            filled.question = question
            let expected = try #require(question.category)
            await delivery.deliverAfterRegistration(filled) { category in
                #expect(category == expected)
                #expect(delivered.titles.isEmpty)
                if expires { delivery.expire() }
                await Task.yield()
                return true
            }
            delivery.expire()
            #expect(delivered.titles == [expires ? "Original" : "Filled"])
            #expect(delivered.bodies == [expires ? "" : "When?"])
        }
    }

    @Test func failedRegistrationLeavesTheOriginalNotification() async {
        let content = UNMutableNotificationContent()
        content.title = "Original"
        let delivered = Delivered()
        let delivery = NotificationDelivery(content: content) { delivered.add($0) }
        var filled = NotificationContent.Filled(title: "Filled", body: "When?", badge: 1, instance: "tundra")
        filled.question = question
        await delivery.deliverAfterRegistration(filled) { _ in false }
        delivery.expire()
        #expect(delivered.titles == ["Original"])
        #expect(delivered.bodies == [""])
    }
}
