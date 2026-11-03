import SHAFTKit
import SwiftUI

/// Select text: the message's text, selectable. A selection's menu (or the bar's button) gives ↩ Reply — + Quote once a
/// card waits — which adds a card for the selected passage; the sheet stays for another passage until Done.
struct SelectTextSheet: View {
    let selecting: ChatScreenModel.Selecting
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.dismiss) private var dismiss
    @State private var selection = NSRange(location: 0, length: 0)
    @State private var added = 0

    private var quoteTitle: String {
        screen.chat.draft.quotes.isEmpty ? String(localized: "\u{21A9}\u{FE0E} Reply") : String(localized: "+ Quote")
    }

    var body: some View {
        NavigationStack {
            SelectableTextView(text: selecting.text, selection: $selection, quoteTitle: quoteTitle,
                               onQuote: quote, onComment: nil)
                .navigationTitle("Select text")
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                #endif
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done") { dismiss() }
                    }
                    if !screen.archived {
                        ToolbarItem(placement: .primaryAction) {
                            Button(quoteTitle) { quote(selected ?? selecting.text) }
                                .accessibilityHint(selected == nil ? "Quotes the whole message" : "Quotes the selection")
                        }
                    }
                }
                .safeAreaInset(edge: .bottom) {
                    if added > 0 {
                        Text(added == 1 ? "1 quote added to your message" : "\(added) quotes added to your message")
                            .font(.footnote)
                            .padding(10)
                            .frame(maxWidth: .infinity)
                            .background(.regularMaterial)
                    }
                }
        }
    }

    private var selected: String? {
        let text = selecting.text as NSString
        guard selection.length > 0, NSMaxRange(selection) <= text.length else { return nil }
        return text.substring(with: selection)
    }

    private func quote(_ text: String) {
        screen.quote(text, doc: selecting.doc)
        added += 1
        Announce.say(String(localized: "Quoted"))
    }
}

/// The reason for a thumb (§9): "What was wrong? (optional)" for thumbs down, "What was good? (optional)" for Send… on
/// thumbs up; Send to <n>, Just keep it, Cancel.
struct RatingSheet: View {
    let target: ChatScreenModel.RatingTarget
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.dismiss) private var dismiss
    @State private var reason = ""
    @State private var busy = false
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField(target.thumb == "down" ? "What was wrong? (optional)" : "What was good? (optional)",
                              text: $reason, axis: .vertical)
                        .lineLimit(3...8)
                        .focused($focused)
                } footer: {
                    Text(target.thumb == "down"
                         ? "Sent, it goes with your next message in this chat (or to its inbox); kept, it stays with you."
                         : "Sent, it goes to its inbox for its next cycle.")
                }
                Section {
                    Button("Send to \(screen.instance)") { finish(send: true) }.bold()
                    if target.ratingID == nil {
                        Button("Just keep it") { finish(send: false) }
                    }
                }
                .disabled(busy)
            }
            .navigationTitle(target.thumb == "down" ? "Thumbs down" : "Thumbs up")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
            .onAppear {
                reason = target.reason
                focused = true
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func finish(send: Bool) {
        busy = true
        Task {
            await screen.finishRating(target, reason: reason, send: send)
            busy = false
            dismiss()
        }
    }
}

/// Earlier chats (§5): every ended chat, newest first, each with its date, first line and how many messages. One opens
/// read-only.
struct EarlierChatsSheet: View {
    let instance: String
    var open: (ChatSummary) -> Void
    @Environment(InstancesStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var chats: [ChatSummary]?
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Group {
                if let chats {
                    if chats.isEmpty {
                        ContentUnavailableView("No earlier chats", systemImage: "clock",
                                               description: Text("Ended chats with \(instance) show here."))
                    } else {
                        List(chats) { chat in
                            Button {
                                dismiss()
                                open(chat)
                            } label: {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(chat.excerpt.isEmpty ? String(localized: "(no message)") : chat.excerpt)
                                        .lineLimit(2)
                                        .foregroundStyle(.primary)
                                    Text("\(Stamp.short(chat.created)) · \(chat.messages) messages")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                .frame(minHeight: 44)
                            }
                        }
                    }
                } else if let error {
                    ContentUnavailableView("Could not load the earlier chats", systemImage: "exclamationmark.triangle",
                                           description: Text(error))
                } else {
                    ProgressView()
                }
            }
            .navigationTitle("Earlier chats")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
            }
            .task {
                do {
                    chats = try await store.client.chats(instance).chats
                } catch {
                    self.error = APIError.from(error).message
                }
            }
        }
    }
}
