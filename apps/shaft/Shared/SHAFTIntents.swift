import AppIntents
import Foundation
import SHAFTKit

struct InstanceEntity: AppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "SHAFT instance"
    static let defaultQuery = InstanceQuery()

    let id: String
    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(id)") }
}

struct InstanceQuery: EntityQuery {
    func entities(for identifiers: [String]) async throws -> [InstanceEntity] {
        let available = Set(try await suggestedEntities().map(\.id))
        return identifiers.filter { available.contains($0) }.map(InstanceEntity.init(id:))
    }

    func suggestedEntities() async throws -> [InstanceEntity] {
        do {
            let client = try ShortcutServer.client()
            guard case .value(let response, _) = try await client.instances() else {
                throw ShortcutError("SHAFT did not return an instance list.")
            }
            return response.instances.map { InstanceEntity(id: $0.name) }
        } catch let error as APIError {
            throw ShortcutServer.error(error)
        }
    }
}

struct SendToInstanceIntent: AppIntent {
    static let title: LocalizedStringResource = "Send to SHAFT instance"
    static let supportedModes: IntentModes = .background

    @Parameter(title: "Instance") var instance: InstanceEntity
    @Parameter(title: "Text", inputOptions: .init(multiline: true)) var text: String?
    @Parameter(title: "Files") var files: [IntentFile]?

    static var parameterSummary: some ParameterSummary {
        Summary("Send \(\.$text) to \(\.$instance)") { \.$files }
    }

    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        let directory = FileManager.default.temporaryDirectory.appending(path: "shaft-shortcut-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let items = try (files ?? []).enumerated().map { index, file in
            try Task.checkCancellation()
            // Keep the supplied name in the message, with a separate disk path for each file.
            let url = directory.appending(path: String(index))
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            if let source = file.fileURL {
                let scoped = source.startAccessingSecurityScopedResource()
                defer { if scoped { source.stopAccessingSecurityScopedResource() } }
                try FileManager.default.copyItem(at: source, to: url)
            } else {
                try file.data.write(to: url)
            }
            let mimeType = file.type?.preferredMIMEType ?? MultipartForm.mimeType(for: file.filename)
            return SharedItem.file(OutgoingFile(name: file.filename, mimeType: mimeType, fileURL: url))
        }
        let content = SharedItems(items, note: text ?? "")
        guard content.isSendable else { throw ShortcutError("Add text or a file to send.") }

        do {
            let client = try ShortcutServer.client()
            _ = try await client.send(instance.id, OutgoingMessage(text: content.text, mode: .now, files: content.files))
        } catch let error as APIError {
            throw ShortcutServer.error(error)
        }
        let confirmation = "Sent to \(instance.id)"
        return .result(value: confirmation, dialog: "Sent to \(instance.id)")
    }
}

struct OpenInstanceChatIntent: OpenIntent {
    typealias ChatOpener = @MainActor @Sendable (String) -> Void

    static let title: LocalizedStringResource = "Open SHAFT instance chat"
    static let supportedModes: IntentModes = .foreground

    @Parameter(title: "Instance") var target: InstanceEntity
    @Dependency var openChat: ChatOpener

    static var parameterSummary: some ParameterSummary { Summary("Open chat with \(\.$target)") }

    @MainActor
    func perform() async throws -> some IntentResult {
        openChat(target.id)
        return .result()
    }
}

struct SHAFTShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: SendToInstanceIntent(), phrases: ["Send to \(.applicationName)"],
                    shortTitle: "Send to instance", systemImageName: "paperplane")
        AppShortcut(intent: OpenInstanceChatIntent(), phrases: ["Open a chat in \(.applicationName)"],
                    shortTitle: "Open instance chat", systemImageName: "bubble.left")
        AppShortcut(intent: RecordMeetingIntent(), phrases: ["Record a meeting with \(.applicationName)"],
                    shortTitle: "Record Meeting", systemImageName: "record.circle")
        AppShortcut(intent: StopMeetingRecordingIntent(), phrases: ["Stop recording my meeting with \(.applicationName)"],
                    shortTitle: "Stop Meeting Recording", systemImageName: "stop.circle")
    }
}

private enum ShortcutServer {
    static func client() throws -> APIClient {
        guard let credential = try KeychainStore.forMainBundle().load() else { throw APIError.notPaired }
        return APIClient(credential: credential)
    }

    static func error(_ error: APIError) -> ShortcutError {
        if error.isUnpaired { return ShortcutError("Open SHAFT and pair it first") }
        return ShortcutError("\(error.message)")
    }
}

private struct ShortcutError: LocalizedError, CustomLocalizedStringResourceConvertible {
    let localizedStringResource: LocalizedStringResource
    var errorDescription: String? { String(localized: localizedStringResource) }

    init(_ message: LocalizedStringResource) { localizedStringResource = message }
}
