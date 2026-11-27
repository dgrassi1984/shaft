import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

@main
struct MeetingWidgets: WidgetBundle {
    var body: some Widget {
        MeetingLiveActivity()
        RecordMeetingControl()
        StopMeetingRecordingControl()
    }
}

struct MeetingLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: MeetingActivity.self) { context in
            HStack {
                VStack(alignment: .leading) {
                    Text(status(context.state)).font(.headline)
                    elapsed(context)
                }
                Spacer()
                stopButton(context.state)
            }
            .padding()
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) { elapsed(context) }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack {
                        Text(status(context.state))
                        Spacer()
                        stopButton(context.state)
                    }
                }
            } compactLeading: {
                Image(systemName: "mic.fill")
                    .accessibilityLabel("Meeting recording")
            } compactTrailing: {
                elapsed(context)
                    .font(.caption2)
                    .frame(maxWidth: 54)
            } minimal: {
                Image(systemName: "mic.fill")
                    .accessibilityLabel("Meeting recording")
            }
        }
    }

    private func status(_ state: MeetingActivity.ContentState) -> String {
        switch state {
        case .recording: "Recording meeting"
        case .interrupted: "Recording paused"
        case .ended(_, let problem): problem ?? "Recording stopped"
        }
    }

    @ViewBuilder
    private func elapsed(_ context: ActivityViewContext<MeetingActivity>) -> some View {
        Group {
            if case .ended(let end, _) = context.state {
                Text(Duration.seconds(max(0, end.timeIntervalSince(context.attributes.started))),
                     format: .time(pattern: .minuteSecond))
            } else {
                Text(context.attributes.started, style: .timer)
            }
        }
        .monospacedDigit()
        .accessibilityLabel("Recording elapsed time")
    }

    @ViewBuilder
    private func stopButton(_ state: MeetingActivity.ContentState) -> some View {
        switch state {
        case .recording, .interrupted:
            Button(intent: StopMeetingRecordingIntent()) {
                Label("Stop", systemImage: "stop.fill")
            }
            .buttonStyle(.borderedProminent)
        case .ended:
            EmptyView()
        }
    }
}

struct RecordMeetingControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "RecordMeeting") {
            ControlWidgetButton(action: RecordMeetingIntent()) {
                Label("Record Meeting", systemImage: "record.circle")
            }
        }
        .displayName("Record Meeting")
    }
}

struct StopMeetingRecordingControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "StopMeetingRecording") {
            ControlWidgetButton(action: StopMeetingRecordingIntent()) {
                Label("Stop Meeting Recording", systemImage: "stop.circle")
            }
        }
        .displayName("Stop Meeting Recording")
    }
}
