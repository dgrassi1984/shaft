import SHAFTKit
import SwiftUI

/// SHAFT is installed on this Mac and the app is not paired with it: the setup's pair step alone, no link to paste
/// (`shaft web pair --local`). It runs by itself (SetupGate.pairsAutomatically); a failure shows the real error with
/// Try again, and Run setup opens the wizard (whose Welcome also offers a pasted link).
struct LocalPairingView: View {
    let setup: MacSetup
    let session: SHAFTSession

    var body: some View {
        MarkMessage("SHAFT on this Mac", message: message) {
            VStack(spacing: Design.Space.s12) {
                switch setup.pairing {
                case .pairing:
                    ProgressView().controlSize(.small)
                case .failed(let error):
                    Text(error)
                        .foregroundStyle(Color.warning)
                        .textSelection(.enabled)
                        .multilineTextAlignment(.center)
                        .accessibilityAddTraits(.isStaticText)
                    Button("Try again") { setup.pairLocally(session) }.buttonStyle(ProminentAction())
                case .idle(let problem):
                    if let problem {
                        Text(problem)
                            .foregroundStyle(Color.warning)
                            .multilineTextAlignment(.center)
                    }
                    Button("Pair with this Mac") { setup.pairLocally(session) }.buttonStyle(ProminentAction())
                }
                if setup.pairing != .pairing {
                    Button("Run setup") { setup.openWizard(session) }
                        .buttonStyle(.plain)
                        .foregroundStyle(Color.brand)
                }
            }
            .frame(maxWidth: Design.measure)
        }
        .onChange(of: setup.pairing) { _, now in
            if case .failed(let error) = now { Announce.say(error) }
        }
    }

    private var message: Text {
        switch setup.pairing {
        case .pairing: Text("Pairing this app with the SHAFT server on this Mac…")
        case .failed: Text("This app could not pair with the SHAFT server on this Mac.")
        case .idle: Text("This app is not paired with the SHAFT server on this Mac.")
        }
    }
}
