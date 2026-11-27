import SHAFTKit
import SwiftUI

struct MeetingsWindow: View {
    static let windowID = "meetings"

    let client: APIClient
    let recorder: MeetingRecorder
    let model: AppModel

    var body: some View {
        MeetingsBrowser(client: client, refreshID: recorder.phase, requested: model.openMeeting,
                        opened: { id in if model.openMeeting == id { model.openMeeting = nil } }) {
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
                if let from = recorder.recordingFrom { Text("From \(from)").font(.callout).foregroundStyle(.secondary) }
                if let notice = recorder.inputNotice { Text(notice).font(.callout).foregroundStyle(.orange) }
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
                switch recorder.phase {
                case .uploading:
                    if let uploader = recorder.uploader, uploader.totalBytes > 0 {
                        ProgressView("Sending to SHAFT…", value: Double(uploader.bytesSent), total: Double(uploader.totalBytes))
                    } else {
                        ProgressView("Sending to SHAFT…")
                    }
                case .failed(let message):
                    Text(message).font(.callout).foregroundStyle(.red)
                    Button("Send Again") { Task { await recorder.sendPending() } }
                default:
                    EmptyView()
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
    }
}
