/// What the composer's one button does. While a reply runs it stops that reply, until there is
/// something to send: then it sends, so interjecting keeps working.
public enum ComposerAction: Sendable, Equatable {
    case send
    case stop

    public static func primary(hasContent: Bool, replying: Bool) -> ComposerAction {
        replying && !hasContent ? .stop : .send
    }
}
