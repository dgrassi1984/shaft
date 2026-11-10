import SHAFTKit
import SwiftUI

struct ServerSection: View {
    let store: ServicesStore
    @State private var confirming: String?

    var body: some View {
        Section {
            if store.unsupported {
                Text("This needs a newer SHAFT server.").foregroundStyle(Color.textSecondary)
            } else {
                if let error = store.loadError {
                    Text(error.message).foregroundStyle(Color.warning)
                    Button("Try again") { Task { await store.load() } }.foregroundStyle(Color.brand)
                }
                if store.services.isEmpty && store.loadError == nil { ProgressView() }
                ForEach(store.services.filter { ["api", "scheduler"].contains($0.name) }, id: \.name) { service in
                    serviceRow(service)
                }
            }
        } header: { Text("Services") } footer: {
            Text("These restart a service for all instances.").foregroundStyle(Color.textSecondary)
        }
        .listRowBackground(Color.elevated)
        .task { await store.load() }
        .confirmationDialog(confirmation, isPresented: Binding(
            get: { confirming != nil }, set: { if !$0 { confirming = nil } }
        ), titleVisibility: .visible) {
            if let name = confirming {
                Button("Restart") {
                    confirming = nil
                    Task { await store.restart(name) }
                }
            }
        }
    }

    private var confirmation: String {
        confirming == "api" ? "Restart the API? Running replies would be cut, so it refuses while one runs." :
            "Restart the scheduler once its runs end? It starts nothing new until then, and cuts nothing."
    }

    private func serviceRow(_ service: ServiceStatus) -> some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            LabeledContent(service.name == "api" ? "API" : "Scheduler", value: service.running ? "Running" : "Not running")
            if let started = service.started {
                Text("Started \(Stamp.full(started))").font(.footnote).foregroundStyle(Color.textSecondary)
            }
            if let version = service.version {
                Text("Version \(version)").font(.footnote).foregroundStyle(Color.textSecondary)
            }
            if service.name == "api", !service.busy.isEmpty {
                Text(service.busy).font(.footnote).foregroundStyle(Color.textSecondary)
            }
            if service.name == "scheduler", let drain = service.drain, drain.pending {
                Text("Waiting for: \(drain.waiting.isEmpty ? "runs to end" : drain.waiting.joined(separator: ", "))")
                    .foregroundStyle(Color.textSecondary)
                Button("Cancel") { Task { await store.cancel() } }.foregroundStyle(Color.brand)
            } else if store.restarting.contains(service.name) {
                HStack(spacing: Design.Space.s8) {
                    ProgressView().controlSize(.small)
                    Text("Restarting…").foregroundStyle(Color.textSecondary)
                }
            } else {
                Button("Restart") { confirming = service.name }.foregroundStyle(Color.brand)
            }
            if let outcome = store.outcome[service.name] {
                switch outcome {
                case .restarted:
                    if let pid = service.pid { Text("Restarted, new process \(String(pid))").foregroundStyle(Color.textSecondary) }
                case .failed(let text): Text(text).foregroundStyle(Color.warning)
                case .unconfirmed:
                    Text("Asked, but no new process has appeared yet.").foregroundStyle(Color.textSecondary)
                    Button("Refresh") { Task { await store.load() } }.foregroundStyle(Color.brand)
                case .waiting: EmptyView()
                }
            }
        }
    }
}
