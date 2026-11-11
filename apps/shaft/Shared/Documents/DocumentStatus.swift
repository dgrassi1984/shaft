import SwiftUI

/// What a write answered, how it failed, or a download under way, floating over the bottom of the page or the list.
struct DocumentStatus: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(color)
            .multilineTextAlignment(.center)
            .padding(.horizontal, Design.Space.s12)
            .padding(.vertical, Design.Space.s8)
            .glassEffect(.regular, in: .rect(cornerRadius: Design.Radius.chip, style: .continuous))
            .padding(.horizontal, Design.Space.s16)
            .padding(.bottom, Design.Space.s8)
            .frame(maxWidth: .infinity)
            .accessibilityAddTraits(.updatesFrequently)
    }
}
