import SHAFTKit
import SwiftUI

#if os(macOS)
extension EnvironmentValues {
    /// The Mac's Update server for this session (MacSetup.updater): nil when this Mac's own install is not what the app
    /// is paired with, and on iOS, which has none.
    @Entry var serverUpdater: ServerUpdater? = nil
    /// "Use SHAFT from iPhone/iPad" for this session (MacSetup.phoneAccess): nil under the same conditions.
    @Entry var phoneAccess: PhoneAccess? = nil
}

/// Settings → Services → Update server (docs/app-api.md §19): the newer release, its page, then each step of
/// `shaft deploy` as it runs, "Reconnecting…" while the web service restarts under the app, and a Retry for a services
/// step that waited for a conversation.
struct UpdateServerRows: View {
    let updater: ServerUpdater

    var body: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            offer
            ForEach(updater.rows) { row in rowView(row) }
            outcome
        }
    }

    @ViewBuilder private var offer: some View {
        switch updater.check {
        case .idle:
            EmptyView()
        case .checking:
            HStack(spacing: Design.Space.s8) {
                ProgressView().controlSize(.small)
                Text("Looking for a newer release…").foregroundStyle(Color.textSecondary)
            }
        case .upToDate(let current):
            LabeledContent("Server", value: "Up to date (\(current))")
        case .available(let latest, let current):
            LabeledContent("Server", value: "\(current) · \(latest) is out")
            HStack(spacing: Design.Space.s16) {
                Button("Update server to \(latest)") { updater.update() }
                    .foregroundStyle(Color.brand)
                    .disabled(updater.isBusy)
                Link("What changes", destination: updater.releasePage(latest)).foregroundStyle(Color.brand)
            }
        case .incomplete(let tag, let step, _):
            LabeledContent("Server", value: "\(tag), not finished")
            Text("Updating to \(tag) stopped" + (step.isEmpty ? "." : " at its \(step) step.") + " Finishing it runs the "
                 + "update again; the steps already done are skipped.")
                .font(.footnote).foregroundStyle(Color.textSecondary)
            Button("Finish updating to \(tag)") { updater.update() }
                .foregroundStyle(Color.brand)
                .disabled(updater.isBusy)
        case .elsewhere(let project):
            Text("The server runs from \(project), not from \(updater.checkout.path): update it there "
                 + "(`bin/shaft deploy`, docs/deploy.md).")
                .font(.footnote).foregroundStyle(Color.textSecondary).textSelection(.enabled)
        case .unverified:
            Text("The server does not say which checkout it runs from, so it is not updated from here: update it with "
                 + "`bin/shaft deploy` in its checkout (docs/deploy.md).")
                .font(.footnote).foregroundStyle(Color.textSecondary)
        case .failed(let message):
            Text(message).foregroundStyle(Color.warning)
            Button("Try again") { updater.checkForUpdate() }.foregroundStyle(Color.brand)
        }
    }

    private func rowView(_ row: DeployRow) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Design.Space.s8) {
            Image(systemName: row.waiting ? "clock" : row.ok ? "checkmark.circle" : "xmark.circle")
                .foregroundStyle(row.ok ? Color.textSecondary : Color.warning)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(row.step.capitalized).font(.footnote.weight(.semibold))
                Text(row.ok ? row.detail : (row.error ?? row.detail))
                    .font(.footnote)
                    .foregroundStyle(row.ok ? Color.textSecondary : Color.warning)
                    .textSelection(.enabled)
            }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var outcome: some View {
        if updater.reconnecting {
            HStack(spacing: Design.Space.s8) {
                ProgressView().controlSize(.small)
                Text("Reconnecting…").foregroundStyle(Color.textSecondary)
            }
        } else {
            switch updater.phase {
            case .idle:
                EmptyView()
            case .running:
                HStack(spacing: Design.Space.s8) {
                    ProgressView().controlSize(.small)
                    Text("Updating \(updater.tag ?? "the server")…").foregroundStyle(Color.textSecondary)
                }
            case .waiting(let why):
                Text("The server is on \(updater.tag ?? "the new release"), but its API and web services restart only "
                     + "once no conversation is open (\(why.replacingOccurrences(of: "waiting: ", with: ""))).")
                    .foregroundStyle(Color.textSecondary)
                Button("Retry") { updater.retryServices() }.foregroundStyle(Color.brand)
            case .done:
                Text("Updated to \(updater.tag ?? "the new release").").foregroundStyle(Color.textSecondary)
            case .failed(let message):
                Text(message).foregroundStyle(Color.warning).textSelection(.enabled)
                switch updater.check {
                case .available, .incomplete:
                    Button("Try again") { updater.update() }.foregroundStyle(Color.brand)
                default:
                    EmptyView()
                }
            }
        }
    }
}
#endif

/// In place of the session when the server is older than this app needs (§19): what it runs, on the Mac the update, and
/// Check again (also when the app comes back to the front: the server may have been updated meanwhile).
struct NeedsNewerServer: View {
    let server: String?
    let store: InstancesStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var checking = false
    #if os(macOS)
    @Environment(\.serverUpdater) private var updater
    #endif

    var body: some View {
        ScrollView {
            VStack(spacing: Design.Space.s16) {
                MarkMessage("This app needs a newer SHAFT server", message: Text(explanation)) {
                    Button(checking ? "Checking…" : "Check again") { recheck() }
                        .buttonStyle(ProminentAction())
                        .disabled(checking)
                }
                .frame(minHeight: 260)
                #if os(macOS)
                if let updater {
                    UpdateServerRows(updater: updater)
                        .frame(maxWidth: Design.measure, alignment: .leading)
                        .task { if updater.check == .idle { updater.checkForUpdate() } }
                }
                #endif
            }
            .padding(Design.Space.s24)
            .frame(maxWidth: .infinity)
        }
        .background(Color.canvas.ignoresSafeArea())
        .onChange(of: scenePhase) { _, phase in if phase == .active { recheck() } }
    }

    private func recheck() {
        guard !checking else { return }
        checking = true
        Task {
            await store.checkCompatibility()
            checking = false
        }
    }

    private var explanation: String {
        let runs = server.map { "The server runs \($0); " } ?? "The server predates the app's version check; "
        #if os(macOS)
        if updater != nil { return runs + "update it below, then this window opens as usual." }
        #endif
        return runs + "update it on its Mac: the Mac app's Settings → Services → Update server, or docs/deploy.md."
    }
}
