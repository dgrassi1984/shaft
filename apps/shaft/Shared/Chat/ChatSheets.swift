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
                #if os(iOS)
                .navigationTitle("Select text")
                .navigationBarTitleDisplayMode(.inline)
                #else
                .sheetTitle("Select text")
                #endif
                #if os(iOS)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done") { dismiss() }
                    }
                    if !screen.archived {
                        ToolbarItem(placement: .primaryAction) { quoteButton }
                    }
                }
                #endif
                .safeAreaInset(edge: .bottom) {
                    if added > 0 {
                        Text(added == 1 ? "1 quote added to your message" : "\(added) quotes added to your message")
                            .font(.footnote)
                            .foregroundStyle(Color.textPrimary)
                            .padding(Design.Space.s12)
                            .frame(maxWidth: .infinity)
                            .glassSurface(in: Capsule())
                            .padding(.horizontal, Design.Space.gutter)
                            .padding(.bottom, Design.Space.s8)
                    }
                }
                #if os(macOS)
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    SheetFooter {
                        Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
                        if !screen.archived {
                            quoteButton.compactPrimary().keyboardShortcut(.defaultAction)
                        }
                    }
                }
                #endif
        }
        .sheetFrame()
    }

    private var quoteButton: some View {
        Button(quoteTitle) { quote(selected ?? selecting.text) }
            .accessibilityHint(selected == nil ? "Quotes the whole message" : "Quotes the selection")
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
    /// A compact sheet (spec §4): the field, its note and the two actions, with no dead space under them.
    private static let compactHeight: CGFloat = 340

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: Design.Space.s12) {
                    TextField(target.thumb == "down" ? "What was wrong? (optional)" : "What was good? (optional)",
                              text: $reason, axis: .vertical)
                        .lineLimit(3...8)
                        .textFieldStyle(.plain)
                        .foregroundStyle(Color.textPrimary)
                        .focused($focused)
                        .padding(Design.Space.s12)
                        .elevatedBlock()
                    Text(target.thumb == "down"
                         ? "Sent, it goes with your next message in this chat (or to its inbox); kept, it stays with you."
                         : "Sent, it goes to its inbox for its next cycle.")
                        .font(.footnote)
                        .foregroundStyle(Color.textSecondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button { finish(send: true) } label: {
                        Text("Send to \(screen.instance)").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(ProminentAction())
                    if target.ratingID == nil {
                        Button("Just keep it") { finish(send: false) }
                            .frame(minHeight: 44)
                    }
                }
                .disabled(busy)
                .padding(.horizontal, Design.Space.gutter)
                .padding(.vertical, Design.Space.s8)
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
        .sheetFrame(minWidth: 420, minHeight: 300)
        .presentationDetents([.height(Self.compactHeight), .large])
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
    /// Opens the chosen chat by its id.
    var open: (String) -> Void
    @Environment(InstancesStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var chats: [ChatSummary]?
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Group {
                if let chats {
                    if chats.isEmpty {
                        MarkMessage("No earlier chats", message: Text("Ended chats with \(instance) show here."))
                    } else {
                        List(chats) { chat in
                            Button {
                                dismiss()
                                open(chat.id)
                            } label: {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(chat.excerpt.isEmpty ? String(localized: "(no message)") : chat.excerpt)
                                        .lineLimit(2)
                                        .foregroundStyle(Color.textPrimary)
                                    Text("\(Stamp.short(chat.created)) · \(chat.messages == 1 ? String(localized: "1 message") : String(localized: "\(chat.messages) messages"))")
                                        .font(Design.Text.metadata)
                                        .foregroundStyle(Color.textSecondary)
                                }
                                .frame(minHeight: 44)
                            }
                            .tint(Color.textPrimary)
                            .listRowBackground(Color.clear)
                            .listRowSeparatorTint(Color.hairline)
                        }
                        .listStyle(.plain)
                        .scrollContentBackground(.hidden)
                    }
                } else if let error {
                    MarkMessage("Could not load the earlier chats", message: Text(error))
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
            .background(Color.canvas.ignoresSafeArea())
            .task {
                do {
                    chats = try await store.client.chats(instance).chats
                } catch {
                    if let error = APIError.from(error) { self.error = error.message }
                }
            }
        }
        .sheetFrame()
        .presentationDetents([.medium, .large])
        .presentationBackground(Color.canvas)       // nothing of the chat behind it shows through
    }
}
