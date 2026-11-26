import AppKit
import SHAFTKit
import SwiftUI

/// The menu bar extra's menu, and the Meeting menu's items: record online or in person, stop, and what happened to the
/// last recording. Recording always starts from the operator's own click; the line about telling the people in the
/// meeting is shown before every start.
struct MeetingMenuItems: View {
    let recorder: MeetingRecorder
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Button("Show Meetings") {
            NSApp.activate()
            openWindow(id: MeetingsWindow.windowID)
        }
        .keyboardShortcut("m", modifiers: [.option, .command])
        Divider()

        switch recorder.phase {
        case .recording(let started, let online):
            Text(online ? "Recording the call" : "Recording in person")
            Text("Since \(started.formatted(date: .omitted, time: .shortened))")
            if let from = recorder.recordingFrom { Text("From \(from)") }
            if let warning = recorder.warning { Text(warning) }
            Divider()
            Button("Stop Recording") { Task { await recorder.stop() } }
                .keyboardShortcut("r", modifiers: [.command, .shift])
        default:
            Text("Tell the people in the meeting that you are recording.")
            Button("Record Online Meeting") { Task { await recorder.start(online: true) } }
                .keyboardShortcut("r", modifiers: [.command, .shift])
                .disabled(recorder.phase == .uploading)
            Button("Record In-Person Meeting") { Task { await recorder.start(online: false) } }
                .disabled(recorder.phase == .uploading)
            MeetingMicrophoneMenu(recorder: recorder)
            status
        }
    }

    @ViewBuilder private var status: some View {
        switch recorder.phase {
        case .uploading:
            Divider()
            if let uploader = recorder.uploader, uploader.totalBytes > 0 {
                Text("Sending to SHAFT… \(Int(100 * Double(uploader.bytesSent) / Double(uploader.totalBytes)))%")
            } else {
                Text("Sending to SHAFT…")
            }
        case .sent:
            Divider()
            Text("Sent to SHAFT: the transcript follows.")
        case .failed(let message):
            Divider()
            Text(message)
            Button("Send Again") { Task { await recorder.sendPending() } }
        default:
            EmptyView()
        }
    }
}

/// The menu bar icon: a filled record dot while recording.
struct MeetingMenuLabel: View {
    let recorder: MeetingRecorder

    var body: some View {
        Image(systemName: recorder.isRecording ? "record.circle.fill" : "waveform.circle")
            .accessibilityLabel(recorder.isRecording ? "SHAFT is recording a meeting" : "SHAFT meetings")
    }
}

/// The Meeting menu's Microphone submenu: System Default or one connected input, kept for the next recordings
/// (online and in person). Shown only while not recording: a recording keeps the input it started with.
struct MeetingMicrophoneMenu: View {
    let recorder: MeetingRecorder

    var body: some View {
        Picker("Microphone", selection: Binding(get: { recorder.microphoneUID ?? "" },
                                                set: { recorder.chooseMicrophone($0.isEmpty ? nil : $0) })) {
            Text(recorder.defaultMicrophoneName.map { "System Default (\($0))" } ?? "System Default").tag("")
            ForEach(recorder.microphoneEntries) { mic in Text(mic.name).tag(mic.uid) }
        }
        .pickerStyle(.menu)
    }
}
