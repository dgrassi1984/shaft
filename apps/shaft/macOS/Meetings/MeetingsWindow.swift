import AppKit
import SHAFTKit
import SwiftUI

struct MeetingsWindow: View {
    static let windowID = "meetings"

    let client: APIClient
    let recorder: MeetingRecorder
    let model: AppModel

    var body: some View {
        MeetingsBrowser(client: client, refreshID: recorder.phase, requested: model.openMeeting,
                        opened: { id in if model.openMeeting == id { model.openMeeting = nil } },
                        stopRecording: recorder.isRecording ? { Task { await recorder.stop() } } : nil) {
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
                TimelineView(.periodic(from: .now, by: 0.2)) { _ in
                    ProgressView(value: Double(recorder.level))
                        .accessibilityLabel("Microphone level")
                }
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
                switch recorder.phase {
                case .uploading:
                    HStack {
                        if let uploader = recorder.uploader, uploader.totalBytes > 0 {
                            ProgressView("Sending to SHAFT…", value: Double(uploader.bytesSent), total: Double(uploader.totalBytes))
                        } else {
                            ProgressView("Sending to SHAFT…")
                        }
                        Button("Stop Sending") { recorder.cancelUpload() }
                    }
                case .failed(let message):
                    Text(message).font(.callout).foregroundStyle(.red)
                    Button("Send Again") { Task { await recorder.sendPending() } }
                default:
                    EmptyView()
                }
                // What the last recording could not keep stays said after it was sent (review 2026-11-27).
                if let warning = recorder.warning { Text(warning).font(.callout).foregroundStyle(.orange) }
                if !recorder.damaged.isEmpty {
                    HStack {
                        Text("\(recorder.damaged.count) recording\(recorder.damaged.count == 1 ? " holds" : "s hold") no readable audio and \(recorder.damaged.count == 1 ? "was" : "were") not sent.")
                            .font(.callout).foregroundStyle(.orange)
                        Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting(recorder.damaged) }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
    }
}
