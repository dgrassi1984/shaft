/// Voice setup and meeting capture both take over the process's AVAudioSession.
@MainActor
final class IOSAudioUse {
    enum Owner: Equatable { case voice, meeting }

    static let meetingInProgress = "Stop the meeting recording before starting voice mode."
    static let shared = IOSAudioUse()
    private(set) var owner: Owner?

    func claim(_ owner: Owner) -> Bool {
        guard self.owner == nil else { return false }
        self.owner = owner
        return true
    }

    func release(_ owner: Owner) {
        if self.owner == owner { self.owner = nil }
    }
}
