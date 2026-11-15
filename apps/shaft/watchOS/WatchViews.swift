import SwiftUI

/// The instances, as the iPhone lists them. Tap one to talk to it.
struct InstanceListView: View {
    @Environment(WatchModel.self) private var model
    @State private var path: [String] = []

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                if model.instances.isEmpty {
                    VStack(spacing: 8) {
                        if model.loadingList {
                            ProgressView()
                        } else {
                            Text(model.listProblem ?? "Asking your iPhone…")
                                .font(.footnote)
                                .multilineTextAlignment(.center)
                            Button("Try again") { model.refreshInstances() }
                        }
                    }
                } else {
                    List(model.instances, id: \.self) { name in
                        NavigationLink(name, value: name)
                    }
                }
            }
            .navigationTitle("SHAFT")
            .navigationDestination(for: String.self) { name in TalkView(instance: name) }
        }
        #if DEBUG
        .onChange(of: model.devOpen) { _, name in
            if let name { path = [name] }
        }
        #endif
    }
}

/// One instance: 🎙 opens the watch's dictation; the reply shows below and is read aloud.
struct TalkView: View {
    let instance: String
    @Environment(WatchModel.self) private var model

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                TextFieldLink(prompt: Text("Speak to \(instance)")) {
                    Label("Talk", systemImage: "mic.fill")
                        .frame(maxWidth: .infinity)
                } onSubmit: { text in
                    model.send(text, to: instance)
                }
                .buttonStyle(.borderedProminent)
                status
            }
        }
        .navigationTitle(instance)
        .onAppear { model.loadLatest(instance) }
        .onDisappear { model.speaker.stop() }
    }

    @ViewBuilder private var status: some View {
        switch model.phase(of: instance) {
        case .idle:
            Text("Tap Talk and speak.")
                .font(.footnote)
                .foregroundStyle(.secondary)
        case .sending(let text):
            Label("Sending…", systemImage: "arrow.up.circle")
                .font(.footnote)
            Text(text).font(.footnote).foregroundStyle(.secondary)
        case .waiting(let said):
            HStack {
                ProgressView().frame(width: 20)
                Text("\(instance) is replying…").font(.footnote)
            }
            Text(said).font(.caption2).foregroundStyle(.secondary)
            Button("Check again", systemImage: "arrow.clockwise") { model.loadLatest(instance) }
        case .reply(let reply):
            HStack {
                if model.speaker.speaking {
                    Button("Stop", systemImage: "stop.fill") { model.speaker.stop() }
                } else {
                    Button("Play", systemImage: "play.fill") { model.speaker.speak(reply) }
                }
            }
            .labelStyle(.iconOnly)
            Text(reply.text.isEmpty ? AttributedString("(no words in the reply)") : reply.screenText)
                .font(.footnote)
            if let failure = reply.failure {
                Text(failure).font(.caption2).foregroundStyle(.orange)
            }
        case .failed(let message, let retry):
            Text(message)
                .font(.footnote)
                .foregroundStyle(.orange)
            if retry {
                Button("Retry", systemImage: "arrow.clockwise") { model.retry(instance) }
            }
        }
    }
}
