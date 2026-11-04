import SwiftUI

/// SHAFT's mark, as in the app icon (supervisor/web_icons.py): a shaft of light with its dot, on a tile. The 64-unit
/// drawing, in the design's tokens so it holds in light and dark.
struct ShaftMark: View {
    @ScaledMetric(relativeTo: .largeTitle) private var size: CGFloat = 56

    var body: some View {
        let unit = size / 64
        ZStack(alignment: .top) {
            RoundedRectangle(cornerRadius: 14 * unit, style: .continuous)
                .fill(Color.elevated)
                .frame(width: 60 * unit, height: 60 * unit)
                .frame(width: size, height: size)
            RoundedRectangle(cornerRadius: 6 * unit, style: .continuous)
                .fill(Color.brand)
                .frame(width: 12 * unit, height: 44 * unit)
                .padding(.top, 10 * unit)
            Circle()
                .fill(Color.textPrimary)
                .frame(width: 8 * unit, height: 8 * unit)
                .padding(.top, 14 * unit)
        }
        .accessibilityHidden(true)
    }
}

/// A screen with nothing to show yet (spec §4): the mark at the top, what happened, and what to do about it.
struct MarkMessage<Actions: View>: View {
    let title: LocalizedStringKey
    var message: Text?
    @ViewBuilder var actions: () -> Actions

    init(_ title: LocalizedStringKey, message: Text? = nil, @ViewBuilder actions: @escaping () -> Actions = { EmptyView() }) {
        self.title = title
        self.message = message
        self.actions = actions
    }

    var body: some View {
        ContentUnavailableView {
            VStack(spacing: Design.Space.s12) {
                ShaftMark()
                Text(title).font(.title3.weight(.semibold)).foregroundStyle(Color.textPrimary)
            }
        } description: {
            if let message { message.foregroundStyle(Color.textSecondary) }
        } actions: {
            actions()
        }
    }
}

/// An action on a screen of its own: filled in the accent, its text `onAccent` so it reads on both accents.
struct ProminentAction: ButtonStyle {
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.semibold))
            .foregroundStyle(enabled ? Color.onAccent : Color.textTertiary)
            .padding(.horizontal, Design.Space.s20)
            .frame(minHeight: 44)
            .background(enabled ? Color.brand : Color.hairline, in: Capsule())
            .contentShape(Capsule())
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}
