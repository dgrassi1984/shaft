import OSLog
import SHAFTKit

/// Refresh the server's device inventory without interrupting pairing or settings.
@MainActor
enum VoiceReporter {
    private static var launched = false
    private static let logger = Logger(subsystem: "SHAFT", category: "VoiceReporter")

    static func atLaunch(client: APIClient) async {
        guard !launched else { return }
        launched = true
        await report(client: client)
    }

    static func report(client: APIClient) async {
        do {
            _ = try await client.reportVoices(voices: InstalledVoices.report())
        } catch {
            logger.error("Voice inventory report failed: \(error.localizedDescription, privacy: .public)")
        }
    }
}
