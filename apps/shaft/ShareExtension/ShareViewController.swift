import Observation
import SHAFTKit
import SwiftUI
import UIKit

/// A share card over the host app. Files stay in this extension's container until it finishes or is cancelled.
@MainActor
final class ShareViewController: UIViewController {
    private let model = ShareModel()

    override func viewDidLoad() {
        super.viewDidLoad()
        let host = UIHostingController(rootView: ShareCard(model: model, cancel: { [weak self] in
            self?.model.cancel()
            self?.extensionContext?.cancelRequest(withError: NSError(domain: NSCocoaErrorDomain,
                                                                      code: NSUserCancelledError))
        }, complete: { [weak self] in
            self?.model.cleanUp()
            self?.extensionContext?.completeRequest(returningItems: nil)
        }))
        addChild(host)
        host.view.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(host.view)
        NSLayoutConstraint.activate([
            host.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            host.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            host.view.topAnchor.constraint(equalTo: view.topAnchor),
            host.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        host.didMove(toParent: self)
        preferredContentSize = CGSize(width: 420, height: 440)
        let items = extensionContext?.inputItems as? [NSExtensionItem] ?? []
        model.start(items)
    }
}

@MainActor
@Observable
private final class ShareModel {
    var instances: [InstanceRow] = []
    var selected = ""
    var note = ""
    var items: [SharedItem] = []
    var loading = true
    var sending = false
    var sent = false
    var error: String?
    private var client: APIClient?
    private var task: Task<Void, Never>?
    private var pending: (instance: String, message: OutgoingMessage)?
    private let directory = FileManager.default.temporaryDirectory.appending(path: "shaft-share-\(UUID().uuidString)")

    var content: SharedItems { SharedItems(items, note: note) }
    var canSend: Bool { !loading && !sending && !sent && client != nil && !selected.isEmpty && content.isSendable }
    /// Loading the pairing or the instances failed (no connection, Tailscale off): Retry loads them again, keeping the
    /// shared items already read, instead of a Send that can't be enabled.
    var needsReload: Bool { !loading && !sent && error != nil && (client == nil || instances.isEmpty) }
    var locked: Bool { pending != nil || sending || sent }

    func start(_ input: [NSExtensionItem]) {
        task = Task {
            defer { loading = false }
            do {
                // The shared items first and once: they come from the host app, not the network, and a Retry of the
                // instances below must not read them twice.
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                var index = 0
                for inputItem in input {
                    if let text = inputItem.attributedContentText?.string, !text.isEmpty { items.append(.text(text)) }
                    for provider in inputItem.attachments ?? [] {
                        try Task.checkCancellation()
                        items.append(await ShareItemLoader.load(provider, index: index, directory: directory))
                        index += 1
                    }
                }
                try Task.checkCancellation()
            } catch {
                self.error = APIError.from(error)?.message
                if Task.isCancelled { cleanUp() }
                return
            }
            await loadInstances()
        }
    }

    /// Retry after the pairing or the instances could not be loaded.
    func reload() {
        guard needsReload else { return }
        loading = true
        error = nil
        task = Task {
            defer { loading = false }
            await loadInstances()
        }
    }

    private func loadInstances() async {
        do {
            guard let credential = try KeychainStore.forMainBundle().load() else {
                error = "Open SHAFT and pair it first"
                return
            }
            let client = APIClient(credential: credential)
            self.client = client
            if case .value(let response, _) = try await client.instances() {
                instances = response.instances
            }
            let previous = UserDefaults.standard.string(forKey: "lastSharedInstance")
            selected = instances.first(where: { $0.name == previous })?.name ?? instances.first?.name ?? ""
            if instances.isEmpty { error = "No instances available. Open SHAFT to create one." }
        } catch {
            self.error = APIError.from(error).map { $0.isUnpaired ? "Open SHAFT and pair it first" : $0.message }
        }
    }

    func send(complete: @escaping @MainActor () -> Void) {
        guard canSend, let client else { return }
        if pending == nil {
            pending = (selected, OutgoingMessage(text: content.text, mode: .now, files: content.files))
        }
        guard let pending else { return }
        sending = true
        error = nil
        task = Task {
            defer { sending = false }
            do {
                _ = try await client.send(pending.instance, pending.message)
                try Task.checkCancellation()
                sent = true
                try await Task.sleep(for: .milliseconds(700))
                complete()
            } catch {
                self.error = APIError.from(error)?.message
            }
        }
    }

    func cancel() {
        task?.cancel()
        cleanUp()
    }

    func cleanUp() { try? FileManager.default.removeItem(at: directory) }
}

@MainActor
private struct ShareCard: View {
    @Bindable var model: ShareModel
    let cancel: () -> Void
    let complete: @MainActor () -> Void

    var body: some View {
        NavigationStack {
            Form {
                if model.loading {
                    ProgressView("Loading shared content…")
                } else {
                    Picker("To:", selection: $model.selected) {
                        ForEach(model.instances) { instance in Text(instance.name).tag(instance.name) }
                    }
                    .disabled(model.locked)
                    .onChange(of: model.selected) { _, value in
                        if !value.isEmpty { UserDefaults.standard.set(value, forKey: "lastSharedInstance") }
                    }
                    TextField("Add a line (optional)", text: $model.note, axis: .vertical)
                        .disabled(model.locked)
                    if !model.content.text.isEmpty { Text(model.content.text).lineLimit(6) }
                    ForEach(Array(model.content.files.enumerated()), id: \.offset) { _, file in
                        Label(file.name, systemImage: "paperclip")
                    }
                    ForEach(Array(model.content.unsupportedMessages.enumerated()), id: \.offset) { _, message in
                        Text(message).foregroundStyle(.red)
                    }
                    if !model.content.isSendable { Text("Nothing to send. Share text, a URL, or a file.") }
                }
                if let error = model.error { Text(error).foregroundStyle(.red) }
                if model.sending { ProgressView("Sending…") }
                if model.sent { Label("Sent", systemImage: "checkmark.circle.fill") }
            }
            .navigationTitle("SHAFT")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel", action: cancel) }
                ToolbarItem(placement: .confirmationAction) {
                    if model.needsReload {
                        Button("Retry") { model.reload() }
                    } else {
                        Button(model.error == nil ? "Send" : "Retry") { model.send(complete: complete) }
                            .disabled(!model.canSend)
                    }
                }
            }
        }
    }
}
