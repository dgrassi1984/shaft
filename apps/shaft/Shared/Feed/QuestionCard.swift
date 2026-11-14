import SHAFTKit
import SwiftUI

/// The structured part of a sent question, inside its existing Markdown card and Reply footer.
struct QuestionCard: View {
    let message: SentItem
    let choices: Choices
    @Environment(ChatScreenModel.self) private var screen
    @State private var picked: Set<Int> = []
    @State private var usesOther = false
    @State private var other = ""

    private var pending: QuestionAnswers.Pending? {
        message.file.flatMap { screen.chat.questionAnswers.pending[$0] }
    }

    private var otherText: String { usesOther ? other.trimmingCharacters(in: .whitespacesAndNewlines) : "" }

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            if let answer = message.answer ?? pending?.answer {
                Text("You answered: \(answer.summary(in: choices))")
                    .font(.callout).foregroundStyle(Color.textSecondary)
                if let error = pending?.error {
                    Text(error.message).font(.footnote).foregroundStyle(Color.textSecondary)
                }
            } else if let pending {
                if pending.sending {
                    Label("Sending…", systemImage: "arrow.up.circle")
                        .font(Design.Text.metadata).foregroundStyle(Color.textSecondary)
                        .accessibilityIdentifier("answer-sending-\(pending.request.mid)")
                } else if let error = pending.error {
                    Text(error.message).font(.callout).foregroundStyle(Color.textSecondary)
                    Button("Retry") {
                        guard let file = message.file else { return }
                        Task { await screen.chat.retryQuestion(file: file) }
                    }
                    .buttonStyle(CompactAccentButton())
                }
            } else if message.open, message.file != nil {
                ForEach(choices.options.indices, id: \.self) { index in
                    option(choices.options[index].label, detail: choices.options[index].detail,
                           selected: picked.contains(index)) {
                        if picked.contains(index) {
                            picked.remove(index)
                        } else if choices.multiple {
                            picked.insert(index)
                        } else {
                            picked = [index]
                            usesOther = false
                        }
                    }
                }
                option("Other…", detail: nil, selected: usesOther) {
                    usesOther.toggle()
                    if usesOther && !choices.multiple { picked.removeAll() }
                }
                if usesOther {
                    TextField("Your answer", text: $other, axis: .vertical)
                        .textFieldStyle(.plain)
                        .font(.body)
                        .padding(Design.Space.s8)
                        .background(Color.canvas, in: RoundedRectangle(cornerRadius: Design.Radius.card))
                        .accessibilityLabel("Other answer")
                }
                Button("Send") {
                    guard let file = message.file else { return }
                    let selection = picked.sorted()
                    let text = otherText.isEmpty ? nil : otherText
                    Task { await screen.chat.answerQuestion(file: file, picked: selection, other: text) }
                }
                .buttonStyle(CompactAccentButton())
                .disabled(!QuestionAnswers.valid(picked: picked.sorted(), other: otherText, choices: choices))
            } else {
                Text("No longer waiting for an answer.").font(.callout).foregroundStyle(Color.textSecondary)
            }
        }
    }

    private func option(_ label: String, detail: String?, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: Design.Space.s8) {
                Image(systemName: choices.multiple ? (selected ? "checkmark.square.fill" : "square")
                                                  : (selected ? "largecircle.fill.circle" : "circle"))
                    .foregroundStyle(selected ? Color.brand : Color.textSecondary)
                VStack(alignment: .leading, spacing: Design.Space.s4) {
                    Text(label).foregroundStyle(Color.textPrimary)
                    if let detail { Text(detail).font(.callout).foregroundStyle(Color.textSecondary) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .font(.body)
            .padding(.vertical, Design.Space.s4)
            .quietHitArea()
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? [.isSelected] : [])
        .accessibilityValue(selected ? "Selected" : "Not selected")
    }
}
