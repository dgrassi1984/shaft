import SHAFTKit
import SwiftUI

// The scaffold's stand-in UI (M3): pairing, the instance list and a plain-text feed, enough to drive the Kit end to end
// on a device. The UI lanes build the real one in Shared/, iOS/ and macOS/ and delete this folder when their root view
// replaces `PlaceholderRoot` in the entry points.

struct PlaceholderRoot: View {
    let model: AppModel

    var body: some View {
        if let session = model.session {
            if let store = session.store {
                PlaceholderInstances(model: model, store: store)
            } else {
                PlaceholderPairing(session: session)
            }
        } else {
            Text(model.startupError ?? "SHAFT could not start.")
                .padding()
        }
    }
}

struct PlaceholderPairing: View {
    let session: SHAFTSession
    @State private var link = ""
    @State private var pairing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Pair with SHAFT").font(.title2)
            Text("On the Mac, run `shaft web pair --name <device>` and paste the link it prints.")
                .foregroundStyle(.secondary)
            TextField("https://…/pair?t=…", text: $link)
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
            Button(pairing ? "Pairing…" : "Pair") {
                pairing = true
                Task {
                    await session.pair(pasted: link)
                    pairing = false
                }
            }
            .disabled(link.isEmpty || pairing)
            if let problem = session.problem {
                Text(problem).foregroundStyle(.red)
            }
        }
        .padding()
        .frame(maxWidth: 520)
    }
}

struct PlaceholderInstances: View {
    let model: AppModel
    let store: InstancesStore
    @State private var path: [String] = []

    var body: some View {
        NavigationStack(path: $path) {
            List(store.instances) { row in
                NavigationLink(value: row.name) {
                    VStack(alignment: .leading) {
                        Text(row.unread ? "● \(row.name)" : row.name).bold()
                        Text(row.line).lineLimit(1).foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("SHAFT")
            .navigationDestination(for: String.self) { name in
                PlaceholderFeed(chat: store.chat(name))
            }
            .overlay {
                if let error = store.error { Text(error.message).foregroundStyle(.red).padding() }
            }
        }
        .task { store.start() }
        .onDisappear { store.stop() }
        .onChange(of: store.badge) { _, badge in model.setBadge(badge) }
        .onChange(of: model.openChat) { _, name in
            guard let name else { return }
            path = [name]
            model.openChat = nil
        }
    }
}

struct PlaceholderFeed: View {
    let chat: ChatStore

    var body: some View {
        List {
            ForEach(chat.content?.items ?? []) { item in
                Text(Self.text(item)).textSelection(.enabled)
            }
            if let reply = chat.feed?.reply {
                Text("replying… \(reply.answer)").foregroundStyle(.secondary)
            }
            ForEach(chat.unsent) { entry in
                Text("not sent: \(entry.text)").foregroundStyle(.orange)
            }
        }
        .navigationTitle(chat.instance)
        .task { chat.start() }
        .onDisappear {
            chat.stop()
            Task { await chat.away() }
        }
        .onChange(of: chat.feed?.version) {
            Task { await chat.markSeen() }
        }
    }

    static func text(_ item: FeedItem) -> String {
        switch item {
        case .user(let turn): "you: \(turn.text)"
        case .assistant(let answer):
            answer.content.isEmpty ? (answer.failure?.why ?? "(no answer)") : answer.content
        case .retried: "The first try failed and was sent again."
        case .sent(let message): "\(message.kind): \(message.text)"
        case .later(let message): "next cycle: \(message.text)"
        case .ended: "The conversation ended."
        case .unknown(_, let type): "(\(type))"
        }
    }
}
