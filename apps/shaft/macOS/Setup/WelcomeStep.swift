import SwiftUI

/// The wizard's first step: what SHAFT does, how long setup takes, the risks in short (SECURITY.md), and a tick that
/// they are understood before Continue. "Connect to an existing SHAFT" pairs with a server elsewhere by a pasted link
/// instead.
struct WelcomeStepView: View {
    @Binding var understood: Bool
    let connect: () -> Void

    static let security = URL(string: "https://github.com/dgrassi1984/shaft/blob/main/SECURITY.md")!

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            ShaftMark()
            Text("Welcome to SHAFT").font(.largeTitle.bold()).foregroundStyle(Color.textPrimary)
            Text("SHAFT runs AI agents, called instances, on this Mac. Each one keeps its own memory, sets its own goals, "
                 + "writes and tests its own tools, and talks with you. A supervisor they cannot change runs them in macOS "
                 + "sandboxes, holds every key, and tests each change they make to themselves.")
                .foregroundStyle(Color.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            Label("Setup takes about 10 minutes, and about 10 more if you connect Google.", systemImage: "clock")
                .foregroundStyle(Color.textSecondary)

            VStack(alignment: .leading, spacing: Design.Space.s12) {
                Text("Before you start").font(.headline).foregroundStyle(Color.textPrimary)
                risk("terminal", "Instances run code they wrote themselves, and can reach the public internet.")
                risk("arrow.up.forward.app", "Anything you let an instance read can leave this Mac, including through "
                     + "instructions hidden in what it reads.")
                risk("dollarsign.circle", "Spending caps are checks made before work starts, not billing limits: cloud "
                     + "calls cost money, and one task can go past a cap.")
                Link("Read SECURITY.md", destination: Self.security)
                    .foregroundStyle(Color.brand)
            }
            .padding(Design.Space.s16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .elevatedBlock()

            Toggle("I understand", isOn: $understood)
                .toggleStyle(.checkbox)
                .foregroundStyle(Color.textPrimary)

            Divider().overlay(Color.hairline).padding(.vertical, Design.Space.s8)
            Text("Already running SHAFT on another Mac?").foregroundStyle(Color.textSecondary)
            Button("Connect to an existing SHAFT", action: connect)
                .buttonStyle(.plain)
                .foregroundStyle(Color.brand)
        }
    }

    private func risk(_ symbol: String, _ text: String) -> some View {
        Label {
            Text(text).foregroundStyle(Color.textPrimary).fixedSize(horizontal: false, vertical: true)
        } icon: {
            Image(systemName: symbol).foregroundStyle(Color.warning)
        }
    }
}
