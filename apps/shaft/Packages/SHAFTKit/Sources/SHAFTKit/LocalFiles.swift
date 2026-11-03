import Foundation

/// The app's own files: drafts and the outbox, under Application Support (the app's container on iOS and in the macOS
/// sandbox). Stored records name their files relative to these folders, never by absolute path: iOS moves the
/// container when the app is updated.
public enum LocalFiles {
    public static func root() throws -> URL {
        let support = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                  appropriateFor: nil, create: true)
        return support.appending(path: "SHAFT", directoryHint: .isDirectory)
    }

    public static func drafts() throws -> URL { try root().appending(path: "drafts", directoryHint: .isDirectory) }
    public static func outbox() throws -> URL { try root().appending(path: "outbox", directoryHint: .isDirectory) }

    /// Write `data` atomically, creating the folder.
    static func write(_ data: Data, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: [.atomic])
    }

    /// Copy a file into `folder` under its own name (made safe for a path), creating the folder.
    static func copy(_ source: URL, into folder: URL, name: String) throws -> URL {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let target = folder.appending(path: safeName(name), directoryHint: .notDirectory)
        if FileManager.default.fileExists(atPath: target.path) { try FileManager.default.removeItem(at: target) }
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        try FileManager.default.copyItem(at: source, to: target)
        return target
    }

    /// A file name with no folder in it: `/` and `:` become `_`, and an empty or dot name becomes `file`.
    static func safeName(_ name: String) -> String {
        let cleaned = name.replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: ":", with: "_")
        return cleaned.isEmpty || cleaned == "." || cleaned == ".." ? "file" : cleaned
    }

    /// An instance name as a folder or file name: the server's `[A-Za-z0-9_-]+` (§1.3).
    static func checkInstance(_ instance: String) throws {
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        guard !instance.isEmpty, instance.unicodeScalars.allSatisfy(allowed.contains) else {
            throw CocoaError(.fileWriteInvalidFileName, userInfo: [NSLocalizedDescriptionKey: "not an instance name: \(instance)"])
        }
    }
}
