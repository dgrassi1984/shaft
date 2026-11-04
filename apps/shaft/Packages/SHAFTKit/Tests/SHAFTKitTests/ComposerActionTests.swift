import SHAFTKit
import Testing

/// The composer's one button (PLAN-native-design §3): Stop only while a reply runs and nothing waits to be sent.
struct ComposerActionTests {
    @Test func emptyWhileReplyingIsStop() {
        #expect(ComposerAction.primary(hasContent: false, replying: true) == .stop)
    }

    @Test func typingWhileReplyingIsSend() {
        #expect(ComposerAction.primary(hasContent: true, replying: true) == .send)
    }

    @Test func idleIsSend() {
        #expect(ComposerAction.primary(hasContent: false, replying: false) == .send)
        #expect(ComposerAction.primary(hasContent: true, replying: false) == .send)
    }
}
