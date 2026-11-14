import Foundation
import SHAFTKit
import UIKit
import UniformTypeIdentifiers

/// Copy file representations inside their callbacks: the provider removes its temporary URL when they return.
@MainActor
enum ShareItemLoader {
    static func load(_ provider: NSItemProvider, index: Int, directory: URL) async -> SharedItem {
        let types = provider.registeredContentTypes
        // A file URL takes precedence over a textual representation offered by Files.
        if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            return await item(provider, type: .fileURL, index: index, directory: directory)
        }
        if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier) {
            return await item(provider, type: .url, index: index, directory: directory)
        }
        let fileType = types.first { $0.conforms(to: .image) || $0.conforms(to: .movie) }
            ?? types.first { $0.conforms(to: .data) && !$0.conforms(to: .text) }
        if let type = fileType {
            let name = provider.suggestedName
            let copied: SharedItem? = await withCheckedContinuation { continuation in
                provider.loadFileRepresentation(forTypeIdentifier: type.identifier) { url, _ in
                    guard let url else { continuation.resume(returning: nil); return }
                    do {
                        continuation.resume(returning: .file(try copy(url, name: name, type: type, directory: directory)))
                    } catch {
                        continuation.resume(returning: .unsupported(error.localizedDescription))
                    }
                }
            }
            if let copied { return copied }
            return await item(provider, type: type, index: index, directory: directory)
        }
        if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier) {
            return await item(provider, type: .plainText, index: index, directory: directory)
        }
        return .unsupported(provider.suggestedName ?? "item \(index + 1) (unsupported type)")
    }

    private static func item(_ provider: NSItemProvider, type: UTType, index: Int,
                             directory: URL) async -> SharedItem {
        let suggested = provider.suggestedName
        return await withCheckedContinuation { continuation in
            provider.loadItem(forTypeIdentifier: type.identifier, options: nil) { value, error in
                do {
                    let result: SharedItem
                    if let url = value as? URL {
                        result = url.isFileURL
                            ? .file(try copy(url, name: suggested, type: type, directory: directory)) : .url(url)
                    } else if type.conforms(to: .url),
                              let text = (value as? String) ?? (value as? Data).flatMap({ String(data: $0, encoding: .utf8) }),
                              let url = URL(string: text), url.scheme != nil {
                        result = url.isFileURL
                            ? .file(try copy(url, name: suggested, type: type, directory: directory)) : .url(url)
                    } else if type.conforms(to: .image), let image = value as? UIImage,
                              let bytes = image.jpegData(compressionQuality: 0.85) {
                        result = .file(try write(bytes, name: "image-\(index + 1).jpg", directory: directory))
                    } else if type.conforms(to: .text), let text = value as? String {
                        result = .text(text)
                    } else if let bytes = value as? Data {
                        if type.conforms(to: .text), let text = String(data: bytes, encoding: .utf8) {
                            result = .text(text)
                        } else {
                            let name = filename(suggested ?? "image-\(index + 1)", type: type)
                            result = .file(try write(bytes, name: name, directory: directory))
                        }
                    } else {
                        result = .unsupported(error?.localizedDescription ?? suggested ?? "item \(index + 1)")
                    }
                    continuation.resume(returning: result)
                } catch {
                    continuation.resume(returning: .unsupported(error.localizedDescription))
                }
            }
        }
    }

    nonisolated private static func filename(_ name: String, type: UTType) -> String {
        let name = (name as NSString).lastPathComponent
        return (name as NSString).pathExtension.isEmpty
            ? name + "." + (type.preferredFilenameExtension ?? "bin") : name
    }

    nonisolated private static func destination(_ name: String, directory: URL) throws -> URL {
        let folder = directory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let safe = (name as NSString).lastPathComponent
        return folder.appending(path: safe.isEmpty || safe == "." || safe == ".." ? "attachment" : safe)
    }

    nonisolated private static func copy(_ url: URL, name: String?, type: UTType,
                                        directory: URL) throws -> OutgoingFile {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let target = try destination(filename(name ?? url.lastPathComponent, type: type), directory: directory)
        try FileManager.default.copyItem(at: url, to: target)
        return OutgoingFile(name: target.lastPathComponent, mimeType: MultipartForm.mimeType(for: target.lastPathComponent),
                            fileURL: target)
    }

    nonisolated private static func write(_ bytes: Data, name: String, directory: URL) throws -> OutgoingFile {
        let target = try destination(name, directory: directory)
        try bytes.write(to: target)
        return OutgoingFile(name: target.lastPathComponent, mimeType: MultipartForm.mimeType(for: target.lastPathComponent),
                            fileURL: target)
    }
}
