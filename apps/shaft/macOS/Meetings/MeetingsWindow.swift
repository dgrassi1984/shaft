import SHAFTKit
import SwiftUI

struct MeetingsWindow: View {
    static let windowID = "meetings"

    let client: APIClient
    let recorder: MeetingRecorder

    var body: some View {
        MeetingsBrowser(client: client, refreshID: recorder.phase) {
            MeetingRecordControls(recorder: recorder)
        }
        .frame(minWidth: 700, minHeight: 460)
    }
}

/// Record and stop from the Meetings window, the same actions as the Meeting menu. Recording starts only from the
/// operator's own click, after the line about telling the people in the meeting.
struct MeetingRecordControls: View {
    let recorder: MeetingRecorder

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            switch recorder.phase {
            case .recording(let started, let online):
                TimelineView(.periodic(from: started, by: 1)) { context in
                    Label("\(online ? "Recording the call" : "Recording in person") · "
                          + Duration.seconds(context.date.timeIntervalSince(started))
                            .formatted(.time(pattern: .hourMinuteSecond)),
                          systemImage: "record.circle")
                        .monospacedDigit()
                }
                if let warning = recorder.warning { Text(warning).font(.callout).foregroundStyle(.red) }
                Button("Stop Recording", systemImage: "stop.fill") { Task { await recorder.stop() } }
                    .buttonStyle(.borderedProminent)
            default:
                Text("Tell the people in the meeting that you are recording.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                HStack {
                    Button("Record Online", systemImage: "record.circle") { Task { await recorder.start(online: true) } }
                        .buttonStyle(.borderedProminent)
                    Button("Record In Person", systemImage: "person.2") { Task { await recorder.start(online: false) } }
                }
                .disabled(recorder.phase == .uploading)
                if case .failed(let message) = recorder.phase {
                    Text(message).font(.callout).foregroundStyle(.red)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
    }
}
