import PhotosUI
import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

/// The composer (web-and-api.md, "The web UI"): ↩ Reply cards above the field, each with its own answer; the files
/// attached as chips; the main field ("Anything else…" once a card waits); the paperclip; the arrow that sends now
/// (an interjection while a reply runs), and ⌃ beside it — or a long press on the arrow — for Queue after reply (only
/// while replying) and Next cycle with when that is. The draft (text, cards, files) is the instance's and survives a
/// quit.
struct ComposerView: View {
    /// The tallest the field grows before it scrolls: a third of the visible height.
    let maxHeight: CGFloat
    @Environment(ChatScreenModel.self) private var screen
    @Environment(InstancesStore.self) private var store
    @Environment(FilePreview.self) private var preview
    @State private var textHeight: CGFloat = 40
    @State private var fieldFocused = false
    @State private var picking = false
    @State private var photos: [PhotosPickerItem] = []
    @State private var importing = false
    @State private var sending = false
    @FocusState private var focusedAnswer: Int?

    private var chat: ChatStore { screen.chat }
    private var replying: Bool { chat.feed?.reply != nil }
    private var nextCycle: String { store.row(screen.instance)?.nextCycle ?? "" }

    var body: some View {
        @Bindable var chat = chat
        VStack(spacing: 6) {
            if !chat.draft.quotes.isEmpty || !chat.draft.attachments.isEmpty {
                ScrollView {
                    VStack(spacing: 6) {
                        ForEach(Array(chat.draft.quotes.enumerated()), id: \.offset) { index, _ in
                            // A binding that tolerates the card being gone: a send clears the draft while the
                            // cards are still being drawn (an index binding crashed there).
                            QuoteCard(quote: quoteBinding(index), index: index, focused: $focusedAnswer,
                                      onKey: { key in send(key == .send ? .now : .queue) }) {
                                chat.removeQuote(at: index)
                            }
                        }
                        if !chat.draft.attachments.isEmpty {
                            ScrollView(.horizontal) {
                                HStack(spacing: 6) {
                                    ForEach(chat.draft.attachments) { file in DraftChip(file: file) }
                                }
                            }
                            .scrollIndicators(.hidden)
                        }
                    }
                }
                .frame(maxHeight: maxHeight)
                .fixedSize(horizontal: false, vertical: true)
            }
            HStack(alignment: .bottom, spacing: 4) {
                attachMenu
                ComposerTextView(text: $chat.draft.text, height: $textHeight, focused: $fieldFocused,
                                 placeholder: chat.draft.quotes.isEmpty ? String(localized: "Message \(screen.instance)")
                                                                        : String(localized: "Anything else…"),
                                 maxHeight: maxHeight, focusRequest: screen.focusComposerRequest,
                                 onKey: { key in send(key == .send ? .now : .queue) },
                                 onPaste: { providers in
                                     Task { _ = await ItemImport.attach(providers, to: chat) }
                                 })
                    .frame(height: textHeight)
                    .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 18))
                    .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Color.secondary.opacity(0.45)))
                sendButton
                moreMenu
            }
            if HardwareKeyboard.shared.attached && fieldFocused {
                ViewThatFits {
                    Text("Return sends · Shift-Return new line · ⌘-Return queues after the reply")
                    Text("")
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.leading, 52)
                .accessibilityHidden(true)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .photosPicker(isPresented: $picking, selection: $photos, maxSelectionCount: 10,
                      matching: .any(of: [.images, .videos]))
        .onChange(of: photos) { _, items in
            guard !items.isEmpty else { return }
            Task {
                _ = await ItemImport.attach(items, to: chat)
                photos = []
            }
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
            if case .success(let urls) = result {
                for url in urls { chat.attach(fileAt: url) }
            }
        }
        .onChange(of: screen.sendRequest) { _, mode in
            guard let mode else { return }
            screen.sendRequest = nil
            send(mode)
        }
        .onChange(of: screen.focusQuoteRequest) { _, index in
            guard let index else { return }
            focusedAnswer = index
            screen.focusQuoteRequest = nil
        }
    }

    private var attachMenu: some View {
        Menu {
            Button { picking = true } label: { Label("Photos", systemImage: "photo.on.rectangle") }
            Button { importing = true } label: { Label("Files", systemImage: "folder") }
            PasteButton(supportedContentTypes: [.image, .data]) { providers in
                Task { _ = await ItemImport.attach(providers, to: chat) }
            }
        } label: {
            Image(systemName: "paperclip")
                .font(.title3)
                .foregroundStyle(.secondary)
                .frame(width: 44, height: 44)
        }
        .iconMenu()
        .accessibilityLabel("Attach files")
    }

    private var sendButton: some View {
        Menu {
            sendModes
        } label: {
            Image(systemName: replying ? "arrow.up.circle" : "arrow.up.circle.fill")
                .font(.system(size: 30))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(chat.draft.isEmpty ? Color.secondary : Color.accentColor)
                .frame(width: 44, height: 44)
        } primaryAction: {
            send(.now)
        }
        .iconMenu()
        .disabled(chat.draft.isEmpty || sending)
        .accessibilityLabel(replying ? "Send now, during its reply" : "Send")
        .accessibilityHint("Touch and hold for Queue after reply and Next cycle")
    }

    private var moreMenu: some View {
        Menu {
            sendModes
        } label: {
            Image(systemName: "chevron.up")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 30, height: 44)
        }
        .iconMenu()
        .disabled(chat.draft.isEmpty || sending)
        .accessibilityLabel("More ways to send")
    }

    @ViewBuilder
    private var sendModes: some View {
        if replying {
            Button { send(.queue) } label: {
                Label("Queue after reply", systemImage: "text.line.last.and.arrowtriangle.forward")
                Text("Sent when this reply ends")
            }
        }
        Button { send(.background) } label: {
            Label("Next cycle", systemImage: "moon")
            if !nextCycle.isEmpty { Text(nextCycle) }
        }
    }

    private func quoteBinding(_ index: Int) -> Binding<Quote> {
        Binding(get: { chat.draft.quotes.indices.contains(index) ? chat.draft.quotes[index] : Quote(quote: "") },
                set: { if chat.draft.quotes.indices.contains(index) { chat.draft.quotes[index] = $0 } })
    }

    private func send(_ mode: SendMode) {
        guard !chat.draft.isEmpty, !sending else { return }
        sending = true
        Task {
            await chat.send(mode: mode)
            sending = false
        }
    }
}

/// A ↩ Reply card: the quoted text (tap: expand or collapse), ×, and its own answer field.
struct QuoteCard: View {
    @Binding var quote: Quote
    let index: Int
    var focused: FocusState<Int?>.Binding
    /// Return in the answer (a computer's keyboard): sends, as in the main field.
    var onKey: (ComposerKey) -> Void = { _ in }
    var remove: () -> Void
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 6) {
                Button { expanded.toggle() } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        if !quote.doc.isEmpty {
                            Text("from \(quote.doc)").font(.caption2).foregroundStyle(.secondary)
                        }
                        Text(quote.quote)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .lineLimit(expanded ? nil : 2)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.leading, 8)
                            .overlay(alignment: .leading) {
                                Rectangle().fill(Color.accentColor.opacity(0.6)).frame(width: 3)
                            }
                    }
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Quote: \(quote.quote)")
                .accessibilityHint(expanded ? "Collapses it" : "Expands it")
                Button(action: remove) {
                    Image(systemName: "xmark").font(.footnote).foregroundStyle(.secondary).touchTarget()
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Remove this quote")
            }
            TextField("Your answer to this", text: $quote.answer, axis: .vertical)
                .lineLimit(1...6)
                .focused(focused, equals: index)
                #if os(macOS)
                // The main field's keys (web-and-api.md): Return sends, Shift- or Option-Return is a new line,
                // Cmd-Return queues after the reply.
                .onKeyPress(.return, phases: .down) { press in
                    if press.modifiers.contains(.command) {
                        onKey(.queue)
                    } else if press.modifiers.contains(.shift) || press.modifiers.contains(.option) {
                        quote.answer += "\n"
                    } else {
                        onKey(.send)
                    }
                    return .handled
                }
                #endif
                .padding(8)
                .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
                .accessibilityLabel("Answer to quote \(index + 1)")
        }
        .padding(8)
        .background(Color.secondary.opacity(0.06), in: RoundedRectangle(cornerRadius: 12))
    }
}

/// A file attached to the draft, with ×.
struct DraftChip: View {
    let file: DraftAttachment
    @Environment(ChatScreenModel.self) private var screen
    @Environment(FilePreview.self) private var preview

    var body: some View {
        HStack(spacing: 0) {
            Button {
                if let url = screen.chat.fileURL(of: file) { preview.open(local: url) }
            } label: {
                FileChip(name: file.name, bytes: file.bytes)
            }
            .buttonStyle(.plain)
            Button { screen.chat.removeAttachment(file.id) } label: {
                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary).touchTarget()
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Remove \(file.name)")
        }
    }
}
