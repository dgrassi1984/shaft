/// Voice, meeting capture, and read-aloud take turns with the microphone: on iOS they share the process's
/// AVAudioSession; on the Mac voice mode and a meeting recording never run together (review 2026-11-27: SHAFT's
/// spoken replies went into the meeting's microphone).
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
