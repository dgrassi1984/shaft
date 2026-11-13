import SHAFTKit
import SwiftUI

/// The wizard's last step: the setup asks the instance for its welcome (`/setup/hello`), then the window shows its
/// chat, where the welcome arrives.
struct HelloStepView: View {
    let setup: MacSetup
    let steps: SetupSteps
    let skip: () -> Void
    let retry: () -> Void

    var body: some View {
        let name = setup.state.createdInstance ?? "your instance"
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            Text("Say hello").font(.largeTitle.bold()).foregroundStyle(Color.textPrimary)
            Text("SHAFT tells \(name) who you are, its drive and what it may reach, and asks it to introduce itself. "
                 + "Its welcome arrives in your chat with it, which opens now.")
                .foregroundStyle(Color.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            Text(AloneChoice(autonomy: setup.state.autonomy) == .own
                 ? "Answer it: once it has replied to your answer, \(name) works on its own."
                 : "Answer it whenever you like: \(name) stays paused, answering when you write.")
                .foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if let failure = steps.greeting.failure {
                SetupFailure(message: failure, retry: retry)
                Button("Open SHAFT without the welcome", action: skip)
                    .buttonStyle(.plain)
                    .foregroundStyle(Color.brand)
            }
        }
    }
}
