import SHAFTKit
import SwiftUI

struct IOSMeetingsScreen: View {
    let client: APIClient
    let recorder: IOSMeetingRecorder
    @Environment(\.dismiss) private var dismiss
    @State private var readingBlocksRecord = false

    var body: some View {
        MeetingsBrowser(client: client, refreshID: recorder.lastSent,
                        clipPlaybackAllowed: recorder.started == nil && !recorder.starting,
                        close: { dismiss() }) {
            VStack(alignment: .leading, spacing: Design.Space.s8) {
                if let started = recorder.started {
                    TimelineView(.periodic(from: started, by: 1)) { context in
                        Label(meetingClock(context.date.timeIntervalSince(started)), systemImage: "mic.fill")
                            .monospacedDigit()
                            .accessibilityLabel("Recording elapsed time")
                            .accessibilityValue(meetingClock(context.date.timeIntervalSince(started)))
                    }
                    if recorder.interrupted { Text("Recording paused by an audio interruption.") }
                    Button("Stop", systemImage: "stop.fill") { recorder.stop() }
                        .buttonStyle(.borderedProminent)
                } else {
                    Text("Tell the people in the meeting that you are recording.")
                        .font(.callout)
                        .foregroundStyle(Color.textSecondary)
                    Button("Record", systemImage: "mic.fill") {
                        guard !ReadAloudRequests.shared.isReading else {
                            readingBlocksRecord = true
                            return
                        }
                        Task { await recorder.start() }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(recorder.starting)
                }
                if let problem = recorder.problem {
                    Text(problem).font(.callout).foregroundStyle(Color.danger)
                }
                if let uploader = recorder.uploader {
                    if case .uploading(let progress) = uploader.state {
                        ProgressView(value: progress) { Text("Uploading recording…") }
                    } else {
                        ProgressView("Preparing or finishing upload…")
                    }
                } else if let problem = recorder.uploadProblem {
                    Text(problem).font(.callout).foregroundStyle(Color.danger)
                } else if recorder.lastSent != nil {
                    Text("Recording uploaded.").font(.callout).foregroundStyle(Color.textSecondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(Design.Space.s16)
        }
        .alert("Stop reading aloud before recording a meeting.", isPresented: $readingBlocksRecord) {
            Button("OK", role: .cancel) {}
        }
    }
}
