import SHAFTKit
import SwiftUI

struct IOSMeetingsScreen: View {
    let client: APIClient?
    let recorder: IOSMeetingRecorder
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        Group {
            if let client {
                MeetingsBrowser(client: client, refreshID: recorder.lastSent,
                                clipPlaybackAllowed: recorder.started == nil && !recorder.starting,
                                close: { dismiss() }) {
                    // In the sidebar column, above the list: as an inset over the whole split view it covered the
                    // list's first rows and the navigation title (2026-11-26, iPhone).
                    controls
                }
            } else {
                NavigationStack {
                    ContentUnavailableView("Pair with SHAFT to browse meetings", systemImage: "waveform")
                        .navigationTitle("Meetings")
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
                        }
                        .safeAreaInset(edge: .top, spacing: 0) { controls }
                }
            }
        }
    }

    private var controls: some View {
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
            } else if client != nil {
                Text("Tell the people in the meeting that you are recording.")
                    .font(.callout)
                    .foregroundStyle(Color.textSecondary)
                Button("Record", systemImage: "mic.fill") {
                    ReadAloudRequests.shared.stop()
                    Task { await recorder.start() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(recorder.starting)
            }
            if let problem = recorder.problem {
                Text(problem).font(.callout).foregroundStyle(Color.danger)
            }
            ForEach(recorder.damagedRecordings, id: \.self) { file in
                Text("Recording damaged. File kept: \(file.lastPathComponent)")
                    .font(.callout).foregroundStyle(Color.danger)
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
}
