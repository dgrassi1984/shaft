import SHAFTKit
import SwiftUI

#if os(macOS)
import AppKit

/// Settings → Services → "Use SHAFT from iPhone/iPad" (Mac, an install on this Mac): adds Tailscale later, then shows
/// the link the phone pairs with. Every state says what to do next, with the server's own words when it refused.
struct PhoneAccessRows: View {
    let access: PhoneAccess

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            Text("Use SHAFT from iPhone/iPad").font(.headline)
            switch access.state {
            case .idle:
                Text("Reach this Mac's SHAFT from your phone or tablet over your private Tailscale network. "
                     + "It keeps working on this Mac as it does now.")
                    .font(.footnote).foregroundStyle(Color.textSecondary)
                Button("Set up") { access.start() }.foregroundStyle(Color.brand)
            case .missingApp:
                Text("Tailscale is not installed on this Mac. Install it, open it and sign in, then come back here.")
                    .foregroundStyle(Color.textSecondary)
                HStack(spacing: Design.Space.s16) {
                    Link("Get Tailscale", destination: PhoneAccess.tailscaleDownload).foregroundStyle(Color.brand)
                    Button("Check again") { access.start() }.foregroundStyle(Color.brand)
                }
            case .working(let what):
                HStack(spacing: Design.Space.s8) {
                    ProgressView().controlSize(.small)
                    Text(what).foregroundStyle(Color.textSecondary)
                }
            case .failed(let message):
                Text(message).foregroundStyle(Color.warning).textSelection(.enabled)
                Button("Try again") { access.start() }.foregroundStyle(Color.brand)
            case .link(let link, let minutes):
                Text("On your phone, open the SHAFT app and paste this link. It works once, for \(minutes) minutes.")
                    .font(.footnote).foregroundStyle(Color.textSecondary)
                Text(link).font(.footnote.monospaced()).textSelection(.enabled).lineLimit(3)
                HStack(spacing: Design.Space.s16) {
                    Button("Copy link") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(link, forType: .string)
                    }.foregroundStyle(Color.brand)
                    Button("New link") { access.newLink() }.foregroundStyle(Color.brand)
                }
            }
        }
        .onDisappear { access.stop() }      // leaving Settings or closing its window ends what runs
    }
}
#endif
