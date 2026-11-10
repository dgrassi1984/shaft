import SHAFTKit
import SwiftUI

// M3 contract stub (lane S owns this file and replaces the body; the name and its parameters stay).

/// Choose the instance's voice for one language from this device's installed voices, each with a Preview, and save
/// it on the server (`setVoice`), so the iPhone and the Mac use the same one. `onChosen` gets the server's answer.
struct VoicePicker: View {
    let client: APIClient
    let instance: String
    let language: VoiceLanguage
    var onChosen: (VoicesResponse) -> Void = { _ in }

    var body: some View {
        Text("Choose a voice")
    }
}
