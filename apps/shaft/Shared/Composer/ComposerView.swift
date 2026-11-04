import PhotosUI
import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

/// The composer (web-and-api.md, "The web UI"): one glass surface holding the ↩ Reply cards, each with its own answer,
/// the files attached as chips, and the capsule of + (files, and the ways to send), the main field ("Anything else…"
/// once a card waits) and the one button: Send, or Stop while a reply runs and nothing waits to be sent
/// (`ComposerAction`). A long press on Send gives Queue after reply (only while replying) and Next cycle with when
/// that is. The draft (text, cards, files) is the instance's and survives a quit.
struct ComposerView: View {
    /// The tallest the field grows before it scrolls: a third of the visible height.
    let maxHeight: CGFloat
    @Environment(ChatScreenModel.self) private var screen
    @Environment(InstancesStore.self) private var store
    @Environment(FilePreview.self) private var preview
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .body) private var scaledDisc: CGFloat = 32
    @State private var textHeight: CGFloat = 40
    @State private var fieldFocused = false
    @State private var picking = false
    @State private var photos: [PhotosPickerItem] = []
    @State private var importing = false
    @State private var sending = false
    @State private var stopping = false
    @FocusState private var focusedAnswer: Int?

    /// The button's disc grows with the text, up to what the capsule can hold beside the field at the largest sizes.
    private var disc: CGFloat { min(scaledDisc, 48) }
    private var control: CGFloat { max(44, disc + 12) }
    private var chat: ChatStore { screen.chat }
    private var replying: Bool { chat.feed?.reply != nil }
    private var nextCycle: String { store.row(screen.instance)?.nextCycle ?? "" }
    /// A reply the server will not stop leaves the button as an idle Send.
    private var action: ComposerAction {
        ComposerAction.primary(hasContent: !chat.draft.isEmpty, replying: replying && chat.content?.canStop == true)
    }

    var body: some View {
        @Bindable var chat = chat
        VStack(spacing: Design.Space.s4) {
            VStack(spacing: 0) {
                if !chat.draft.quotes.isEmpty || !chat.draft.attachments.isEmpty {
                    ScrollView {
                        VStack(spacing: Design.Space.s8) {
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
                                    HStack(spacing: Design.Space.s8) {
                                        ForEach(chat.draft.attachments) { file in DraftChip(file: file) }
                                    }
                                }
                                .scrollIndicators(.hidden)
                            }
                        }
                        .padding([.horizontal, .top], Design.Space.s8)
                    }
                    .frame(maxHeight: maxHeight)
                    .fixedSize(horizontal: false, vertical: true)
                }
                HStack(alignment: .bottom, spacing: 0) {
                    attachMenu
                    field($chat.draft.text)
                    primaryButton
                }
            }
            .motion(Design.Motion.spring, value: chat.draft.quotes.count, reduce: reduceMotion)
            .glassSurface(in: RoundedRectangle(cornerRadius: Design.Radius.composer, style: .continuous))
            if HardwareKeyboard.shared.attached && fieldFocused {
                ViewThatFits {
                    Text("Return sends · Shift-Return new line · ⌘-Return queues after the reply")
                    Text("")
                }
                .font(Design.Text.metadata)
                .foregroundStyle(Color.textSecondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.leading, Design.Space.s16)
                .accessibilityHidden(true)
            }
        }
        .frame(maxWidth: Design.measure)
        .padding(.horizontal, Design.Space.gutter)
        .padding(.vertical, Design.Space.s8)
        .frame(maxWidth: .infinity)
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
        .onChange(of: picking || importing) { _, covered in screen.composerCovered = covered }
        .onDisappear { screen.composerCovered = false }
        .onChange(of: screen.dismissRequest) {
            picking = false
            importing = false
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

    private func field(_ text: Binding<String>) -> some View {
        ComposerTextView(text: text, height: $textHeight, focused: $fieldFocused,
                         placeholder: chat.draft.quotes.isEmpty ? String(localized: "Message \(screen.instance)")
                                                                : String(localized: "Anything else…"),
                         maxHeight: maxHeight, focusRequest: screen.focusComposerRequest,
                         onKey: { key in send(key == .send ? .now : .queue) },
                         onPaste: { providers in
                             Task { _ = await ItemImport.attach(providers, to: chat) }
                         })
            .frame(height: textHeight)
            .clipped()                           // a placeholder too long at the largest text stops at the button
    }

    private var attachMenu: some View {
        Menu {
            Button { picking = true } label: { Label("Photos", systemImage: "photo.on.rectangle") }
            Button { importing = true } label: { Label("Files", systemImage: "folder") }
            PasteButton(supportedContentTypes: [.image, .data]) { providers in
                Task { _ = await ItemImport.attach(providers, to: chat) }
            }
            Divider()
            sendModes.disabled(chat.draft.isEmpty || sending)
        } label: {
            Image(systemName: "plus")
                .font(.body.weight(.medium))
                .dynamicTypeSize(...DynamicTypeSize.accessibility1)
                .foregroundStyle(Color.textSecondary)
                .frame(width: control, height: control)
                .contentShape(Rectangle())
        }
        .iconMenu()
        .accessibilityLabel("Attach files")
        .accessibilityHint("Also Queue after reply and Next cycle")
    }

    @ViewBuilder
    private var primaryButton: some View {
        Group {
            switch action {
            case .send: sendButton
            case .stop: stopButton
            }
        }
        .frame(width: control, height: control)
        .motion(Design.Motion.spring, value: action, reduce: reduceMotion)
    }

    private var sendButton: some View {
        Menu {
            sendModes
        } label: {
            buttonFace("arrow.up", on: !chat.draft.isEmpty)
        } primaryAction: {
            send(.now)
        }
        .iconMenu()
        .disabled(chat.draft.isEmpty || sending)
        .accessibilityLabel(replying ? "Send now, during its reply" : "Send")
        .accessibilityHint("Touch and hold for Queue after reply and Next cycle")
    }

    private var stopButton: some View {
        Button {
            stopping = true
            Task {
                await chat.stopReply()
                stopping = false
            }
        } label: {
            buttonFace("stop.fill", on: true)
        }
        .buttonStyle(.plain)
        .disabled(stopping)
        .accessibilityLabel("Stop the reply")
    }

    /// The accent's disc when the button has something to do; a quiet one with nothing to send.
    private func buttonFace(_ symbol: String, on: Bool) -> some View {
        Image(systemName: symbol)
            .font(.footnote.weight(.bold))
            .dynamicTypeSize(...DynamicTypeSize.accessibility1)
            .foregroundStyle(on ? Color.onAccent : Color.textTertiary)
            .frame(width: disc, height: disc)
            .background(on ? Color.brand : Color.hairline, in: Circle())
            .frame(width: control, height: control)
            .contentShape(Rectangle())
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
        #if os(macOS)
        // Sent from a card's answer, whose card goes with the send: the keyboard stays in the composer.
        if focusedAnswer != nil { screen.focusComposerRequest += 1 }
        #endif
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    #if os(iOS)
    @Environment(ChatScreenModel.self) private var screen
    @State private var answerHeight: CGFloat = 40
    @State private var answerFocused = false
    @State private var focusRequest = 0
    #endif

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            HStack(alignment: .top, spacing: Design.Space.s4) {
                Button {
                    withAnimation(reduceMotion ? nil : Design.Motion.spring) { expanded.toggle() }
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        if !quote.doc.isEmpty {
                            Text("from \(quote.doc)").font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                        }
                        Text(quote.quote)
                            .font(.callout)
                            .foregroundStyle(Color.textSecondary)
                            .lineLimit(expanded ? nil : 2)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.leading, Design.Space.s8)
                            .overlay(alignment: .leading) { AccentBar() }
                    }
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Quote: \(quote.quote)")
                .accessibilityHint(expanded ? "Collapses it" : "Expands it")
                Button(action: remove) {
                    Image(systemName: "xmark").font(.footnote).foregroundStyle(Color.textSecondary).touchTarget()
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Remove this quote")
            }
            #if os(iOS)
            ComposerTextView(text: $quote.answer, height: $answerHeight, focused: $answerFocused,
                             placeholder: String(localized: "Your answer to this"), maxHeight: 140,
                             focusRequest: focusRequest, onKey: onKey,
                             onPaste: { providers in Task { _ = await ItemImport.attach(providers, to: screen.chat) } })
                .frame(height: answerHeight)
                .onChange(of: focused.wrappedValue, initial: true) { _, value in
                    if value == index { focusRequest += 1 }
                }
                .onChange(of: answerFocused) { _, value in
                    if value { focused.wrappedValue = index }
                    else if focused.wrappedValue == index { focused.wrappedValue = nil }
                }
                .padding(Design.Space.s4)
                .background(Color.canvas, in: RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous))
                .accessibilityLabel("Answer to quote \(index + 1)")
            #else
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
                .padding(Design.Space.s4)
                .background(Color.canvas, in: RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous))
                .accessibilityLabel("Answer to quote \(index + 1)")
            #endif
        }
        .padding(Design.Space.s8)
        .elevatedBlock()
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
                Image(systemName: "xmark.circle.fill").foregroundStyle(Color.textSecondary).touchTarget()
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Remove \(file.name)")
        }
    }
}
