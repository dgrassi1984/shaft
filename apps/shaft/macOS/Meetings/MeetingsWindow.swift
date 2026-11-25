import SHAFTKit
import SwiftUI

struct MeetingsWindow: View {
    static let windowID = "meetings"

    let client: APIClient
    let recorder: MeetingRecorder

    var body: some View {
        MeetingsBrowser(client: client, refreshID: recorder.phase) { EmptyView() }
            .frame(minWidth: 700, minHeight: 460)
    }
}
