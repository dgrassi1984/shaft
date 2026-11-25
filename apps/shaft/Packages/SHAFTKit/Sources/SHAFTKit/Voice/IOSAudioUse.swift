#if os(iOS)
/// Voice, meeting capture, and read-aloud share the process's AVAudioSession.
@MainActor
public final class IOSAudioUse {
    public enum Owner: Equatable { case voice, meeting, readAloud }

    public static let meetingInProgress = "Stop the meeting recording before starting voice mode."
    public static let shared = IOSAudioUse()
    public private(set) var owner: Owner?

    public init() {}

    public func claim(_ owner: Owner) -> Bool {
        guard self.owner == nil else { return false }
        self.owner = owner
        return true
    }

    public func release(_ owner: Owner) {
        if self.owner == owner { self.owner = nil }
    }
}
#endif
