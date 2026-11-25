import AVFoundation
import SHAFTKit
import SwiftUI

struct MeetingsBrowser<RefreshID: Equatable, Controls: View>: View {
    let client: APIClient
    let refreshID: RefreshID
    let clipPlaybackAllowed: Bool
    private let close: (() -> Void)?
    private let controls: () -> Controls
    @State private var store: MeetingsStore
    @State private var selection: String?
    @State private var columns = NavigationSplitViewVisibility.all
    @State private var compactColumn = NavigationSplitViewColumn.sidebar
    @State private var aliases: [String]?
    @State private var accountError: String?

    init(client: APIClient, refreshID: RefreshID, clipPlaybackAllowed: Bool = true,
         close: (() -> Void)? = nil, @ViewBuilder controls: @escaping () -> Controls) {
        self.client = client
        self.refreshID = refreshID
        self.clipPlaybackAllowed = clipPlaybackAllowed
        self.close = close
        self.controls = controls
        _store = State(initialValue: MeetingsStore(client: client))
    }

    var body: some View {
        NavigationSplitView(columnVisibility: $columns, preferredCompactColumn: $compactColumn) {
            VStack(spacing: 0) {
                controls()
                if let error = store.listError { errorLine(error) }
                if let error = store.actionError { errorLine(error) }
                List(selection: $selection) {
                    ForEach(store.meetings) { meeting in
                        #if os(iOS)
                        NavigationLink(value: meeting.id) { meetingRow(meeting) }
                            .contextMenu { CopyMeetingReference(meeting: meeting) }
                        #else
                        meetingRow(meeting).tag(meeting.id)
                            .contextMenu { CopyMeetingReference(meeting: meeting) }
                        #endif
                    }
                }
                .listStyle(.sidebar)
                .overlay {
                    if store.meetings.isEmpty {
                        if store.loading {
                            ProgressView()
                        } else if store.listError == nil {
                            ContentUnavailableView("No meetings yet", systemImage: "waveform")
                        }
                    }
                }
            }
            #if os(iOS)
            .navigationTitle("Meetings")
            #endif
            .navigationSplitViewColumnWidth(min: 220, ideal: 260)
            .toolbar {
                if let close {
                    ToolbarItem(placement: .cancellationAction) { Button("Close", action: close) }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task {
                            await store.refresh()
                            if let selection, store.meetings.contains(where: { $0.id == selection }) {
                                await store.load(selection)
                            }
                            await loadAccounts()
                        }
                    } label: {
                        Label("Refresh Meetings", systemImage: "arrow.clockwise")
                    }
                    .disabled(store.loading || store.mutating)
                }
            }
        } detail: {
            if let meeting = store.meetings.first(where: { $0.id == selection }) {
                MeetingDetail(meeting: meeting, store: store, aliases: aliases, accountError: accountError,
                              clipPlaybackAllowed: clipPlaybackAllowed)
                    .id(meeting.id)
            } else {
                ContentUnavailableView("Select a meeting", systemImage: "waveform")
            }
        }
        #if os(iOS)
        .navigationSplitViewStyle(.balanced)
        #endif
        .background(Color.canvas)
        .tint(Color.brand)
        .task { await loadAccounts() }
        .task(id: refreshID) { await store.refresh() }
        .task(id: store.needsPolling) { await store.poll() }
        .onChange(of: store.meetings.map(\.id)) { _, ids in
            if let selection, !ids.contains(selection) {
                self.selection = nil
                compactColumn = .sidebar
            }
        }
    }

    private func loadAccounts() async {
        do {
            aliases = try await client.meetingAccountAliases()
            accountError = nil
        } catch {
            accountError = APIError.from(error)?.message
        }
    }

    private func errorLine(_ message: String) -> some View {
        Text(message)
            .font(.callout)
            .foregroundStyle(Color.danger)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(Design.Space.s12)
    }

    private func meetingRow(_ meeting: Meeting) -> some View {
        let color: Color = switch meeting.state {
        case "failed": .danger
        case "ready": .brand
        default: .textSecondary
        }
        return VStack(alignment: .leading, spacing: Design.Space.s4) {
            Text(meeting.displayTitle).font(.headline)
            Text(meeting.started.formatted(date: .abbreviated, time: .shortened))
                .font(.caption)
                .foregroundStyle(Color.textSecondary)
            HStack(spacing: Design.Space.s8) {
                Text(meeting.state.capitalized)
                    .font(Design.Text.pill)
                    .padding(.horizontal, Design.Space.s8)
                    .padding(.vertical, Design.Space.s4)
                    .foregroundStyle(color)
                    .background(color.opacity(0.12), in: .capsule)
                if let ended = meeting.ended {
                    Text(meetingClock(ended.timeIntervalSince(meeting.started)))
                        .font(Design.Text.table)
                        .foregroundStyle(Color.textSecondary)
                }
            }
            if let error = meeting.error {
                Text(error).font(.caption).foregroundStyle(Color.danger)
            }
        }
        .padding(.vertical, Design.Space.s4)
    }
}

private struct MeetingDetail: View {
    let meeting: Meeting
    let store: MeetingsStore
    let aliases: [String]?
    let accountError: String?
    let clipPlaybackAllowed: Bool
    @State private var draftTitle: String?
    @State private var draftAccount: String?
    @State private var confirmDelete = false
    @State private var forgetVoices = false
    @State private var clipLabel: String?
    @State private var player: AVAudioPlayer?
    @State private var playbackError: String?

    private var transcript: MeetingTranscript? {
        guard meeting.state == "ready", store.detail?.id == meeting.id else { return nil }
        return store.detail?.transcript
    }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: Design.Space.s20) {
                header
                #if os(iOS)
                if let error = store.actionError { Text(error).foregroundStyle(Color.danger) }
                #endif
                if let error = store.detailError {
                    Text(error).foregroundStyle(Color.danger)
                }
                if let transcript {
                    speakers(transcript)
                    Divider()
                    transcriptRows(transcript)
                } else if store.loadingDetail {
                    ProgressView()
                }
            }
            .frame(maxWidth: Design.measure, alignment: .leading)
            .padding(Design.Space.gutter)
            .frame(maxWidth: .infinity)
        }
        .background(Color.canvas)
        .task(id: meeting.state) { await store.load(meeting.id) }
        .task(id: clipLabel) {
            if let clipLabel { await play(clipLabel) }
        }
        .onDisappear { stopClip() }
        .onChange(of: clipPlaybackAllowed) { if !clipPlaybackAllowed { stopClip() } }
        .onChange(of: meeting.state) { stopClip() }
        .onChange(of: meeting.title) { if draftTitle == meeting.title { draftTitle = nil } }
        .onChange(of: meeting.account) { if draftAccount == (meeting.account ?? "") { draftAccount = nil } }
        .sheet(isPresented: $confirmDelete) { deleteSheet }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            Text(meeting.displayTitle).font(.title.bold())
            Text(meeting.started.formatted(date: .long, time: .shortened))
                .foregroundStyle(Color.textSecondary)
            filingFields
            if let transcript {
                let names = Set(transcript.speakers.values.compactMap(\.name)).sorted()
                Label(names.isEmpty ? "No named speakers" : names.formatted(.list(type: .and)),
                      systemImage: "person.2")
                    .foregroundStyle(Color.textSecondary)
            }
            ViewThatFits(in: .horizontal) {
                HStack { meetingActions }
                VStack(alignment: .leading) { meetingActions }
            }
        }
    }

    @ViewBuilder
    private var meetingActions: some View {
        CopyMeetingReference(meeting: meeting)
        Button("Reprocess") { Task { await store.reprocess(meeting.id) } }
            .disabled(store.mutating || !["ready", "failed"].contains(meeting.state))
        Button("Delete…", role: .destructive) {
            forgetVoices = false
            confirmDelete = true
        }
        .disabled(store.mutating)
    }

    private var filingFields: some View {
        let title = draftTitle ?? meeting.title
        let savedAccount = meeting.account ?? ""
        let account = draftAccount ?? savedAccount
        return VStack(alignment: .leading, spacing: Design.Space.s8) {
            LabeledContent("Title") {
                TextField("Title", text: Binding(get: { title }, set: { draftTitle = $0 }))
                    .labelsHidden()
            }
            Picker("Account", selection: Binding(get: { account }, set: { draftAccount = $0 })) {
                Text("Unfiled").tag("")
                if let current = meeting.account, aliases?.contains(current) != true {
                    Text(current).tag(current)
                }
                ForEach(aliases ?? [], id: \.self) { alias in Text(alias).tag(alias) }
            }
            .disabled(aliases == nil)
            if let accountError {
                Text(accountError).font(.callout).foregroundStyle(Color.danger)
            }
            if meeting.account == nil {
                Text("Unfiled: choose an account so instances can read it.")
                    .font(.callout)
                    .foregroundStyle(Color.textSecondary)
            }
            Button("Save") {
                Task {
                    await store.file(meeting.id, account: account.isEmpty ? nil : account,
                                     title: title == meeting.title ? nil : title)
                }
            }
            .disabled(title == meeting.title && account == savedAccount)
        }
        .disabled(store.mutating)
    }

    private func speakers(_ transcript: MeetingTranscript) -> some View {
        let labels = transcript.speakers.keys.sorted()
        return VStack(alignment: .leading, spacing: Design.Space.s12) {
            Text("Speakers").font(.title3.bold())
            if let error = store.peopleError {
                HStack {
                    Text(error).foregroundStyle(Color.danger)
                    Button("Retry") { Task { await store.loadPeople() } }
                }
            }
            if let playbackError { Text(playbackError).foregroundStyle(Color.danger) }
            ForEach(labels, id: \.self) { label in
                if let speaker = transcript.speakers[label] {
                    MeetingSpeakerRow(label: label, speaker: speaker, transcript: transcript,
                                      otherLabels: labels.filter { $0 != label }, people: store.people,
                                      playbackAllowed: clipPlaybackAllowed, play: { clipLabel = label },
                                      save: { name in Task { await store.name(meeting.id, label: label, name: name) } },
                                      merge: { target in Task { await store.merge(meeting.id, label: label, into: target) } })
                    .disabled(store.mutating || store.loadingDetail || clipLabel != nil)
                }
            }
        }
        .task { await store.loadPeople() }
    }

    private func transcriptRows(_ transcript: MeetingTranscript) -> some View {
        let segments = store.segments.map { segment in
            (start: segment.start, text: "\(meetingClock(segment.start)) \(transcript.displaySpeaker(segment.speaker)): \(segment.text)",
             isGap: false)
        }
        let gaps = transcript.recordingGaps.map { gap in
            (start: gap.start, text: "[gap \(meetingClock(gap.start))–\(meetingClock(gap.end)): \(gap.reason)]",
             isGap: true)
        }
        let rows = (segments + gaps).sorted { $0.start < $1.start }
        return Section {
            ForEach(rows.indices, id: \.self) { index in
                let row = rows[index]
                Text(row.text)
                    .foregroundStyle(row.isGap ? Color.textSecondary : Color.textPrimary)
            }
        } header: {
            Text("Transcript").font(.title3.bold())
        }
        .textSelection(.enabled)
    }

    private var deleteSheet: some View {
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            Text("Delete this meeting?").font(.headline)
            Text("This deletes the recording and transcript. Text already sent to instances cannot be recalled.")
            Toggle("Also forget voices learned in this meeting", isOn: $forgetVoices)
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { confirmDelete = false }
                    .keyboardShortcut(.cancelAction)
                Button("Delete", role: .destructive) {
                    let forget = forgetVoices
                    confirmDelete = false
                    Task { await store.delete(meeting.id, forgetVoices: forget) }
                }
            }
        }
        .padding(Design.Space.gutter)
        #if os(macOS)
        .frame(width: 420)
        #else
        .frame(maxWidth: 420)
        .presentationDetents([.medium, .large])
        #endif
    }

    private func play(_ label: String) async {
        player?.stop()
        playbackError = nil
        defer { if !Task.isCancelled { clipLabel = nil } }
        do {
            let data = try await store.clip(meeting.id, label: label)
            try Task.checkCancellation()
            let audio = try AVAudioPlayer(data: data, fileTypeHint: "com.microsoft.waveform-audio")
            guard audio.play() else {
                playbackError = "Could not play the clip for Speaker \(label)."
                return
            }
            player = audio
        } catch {
            if !Task.isCancelled, let error = APIError.from(error) { playbackError = error.message }
        }
    }

    private func stopClip() {
        clipLabel = nil
        player?.stop()
        player = nil
    }
}

private struct MeetingSpeakerRow: View {
    let label: String
    let speaker: MeetingTranscript.Speaker
    let transcript: MeetingTranscript
    let otherLabels: [String]
    let people: [SpeakerPerson]
    let playbackAllowed: Bool
    let play: () -> Void
    let save: (String) -> Void
    let merge: (String) -> Void
    @State private var name = ""

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            HStack {
                Text(transcript.displaySpeaker(label)).fontWeight(.medium)
                Text("\(speaker.seconds.formatted(.number.precision(.fractionLength(0...1)))) s")
                    .font(Design.Text.table)
                    .foregroundStyle(Color.textSecondary)
                Spacer()
                if !otherLabels.isEmpty {
                    Menu("Merge into…") {
                        ForEach(otherLabels, id: \.self) { target in
                            Button("\(target) · \(transcript.displaySpeaker(target))") { merge(target) }
                        }
                    }
                    .fixedSize()
                    .accessibilityLabel("Merge Speaker \(label) into another speaker")
                }
            }
            if speaker.name == nil {
                let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
                let matches = people.filter { trimmed.isEmpty || $0.name.localizedCaseInsensitiveContains(trimmed) }
                    .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
                HStack {
                    Button(action: play) { Image(systemName: "play.fill") }
                        .accessibilityLabel("Play clip for Speaker \(label)")
                        .help(speaker.clips?.isEmpty == false ? "Play speaker clip" : "No clip available")
                        .disabled(!playbackAllowed || speaker.clips?.isEmpty != false)
                    TextField("Name Speaker \(label)", text: $name)
                        .onSubmit { if !trimmed.isEmpty { save(trimmed) } }
                    if !people.isEmpty {
                        Menu("Suggestions") {
                            ForEach(matches) { person in
                                Button(person.name) { name = person.name }
                            }
                        }
                        .fixedSize()
                        .disabled(matches.isEmpty)
                    }
                    Button("Save") { save(trimmed) }.disabled(trimmed.isEmpty)
                }
            }
        }
    }
}

private struct CopyMeetingReference: View {
    let meeting: Meeting

    var body: some View {
        let reference = meeting.chatReference()
        Button("Copy Reference") {
            guard let reference else { return }
            Clipboard.copy(reference)
        }
        .disabled(reference == nil)
        .help(reference == nil ? "File the meeting under an account first." : "Copy a reference to paste into chat.")
    }
}

private extension Meeting {
    var displayTitle: String { title.isEmpty ? "Untitled meeting" : title }
}

private extension MeetingTranscript {
    func displaySpeaker(_ label: String) -> String {
        speakers[label]?.name ?? "Speaker \(label) (unknown)"
    }
}

func meetingClock(_ seconds: Double) -> String {
    let value = max(0, Int(exactly: seconds.rounded(.towardZero)) ?? 0)
    return String(format: "%02ld:%02ld:%02ld", value / 3600, value / 60 % 60, value % 60)
}
