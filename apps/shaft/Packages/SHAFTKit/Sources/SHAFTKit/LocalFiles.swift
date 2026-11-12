import Foundation

/// The app's own files: drafts and the outbox, under Application Support (the app's container on iOS; the real
/// `~/Library/Application Support/SHAFT` on the Mac, which left the App Sandbox: see `adoptSandboxContainer`). Stored records name their files relative to these folders, never by absolute path: iOS moves the
/// container when the app is updated.
public enum LocalFiles {
    public static func root() throws -> URL {
        let support = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                  appropriateFor: nil, create: true)
        let root = support.appending(path: "SHAFT", directoryHint: .isDirectory)
        #if os(macOS)
        // Earlier Mac builds ran sandboxed: their drafts and outbox live in the app's container. Best effort: a
        // folder that cannot be moved stays where it is and the app starts with what it finds.
        if let id = Bundle.main.bundleIdentifier {
            let home = FileManager.default.homeDirectoryForCurrentUser
            let container = home.appending(path: "Library/Containers/\(id)/Data/Library/Application Support/SHAFT",
                                           directoryHint: .isDirectory)
            if container.standardizedFileURL != root.standardizedFileURL {
                try? adoptSandboxContainer(container, into: root)
            }
        }
        #endif
        return root
    }

    /// Move what a sandboxed build left in its container (`drafts`, `outbox`, `servers`) into `root`, once: a folder
    /// moves only when `root` has none of that name, so the move never overwrites and a second call does nothing.
    static func adoptSandboxContainer(_ container: URL, into root: URL) throws {
        let manager = FileManager.default
        guard manager.fileExists(atPath: container.path) else { return }
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        for name in ["drafts", "outbox", "servers"] {
            let source = container.appending(path: name, directoryHint: .isDirectory)
            let target = root.appending(path: name, directoryHint: .isDirectory)
            if manager.fileExists(atPath: source.path), !manager.fileExists(atPath: target.path) {
                try manager.moveItem(at: source, to: target)
            }
        }
    }

    public static func drafts() throws -> URL { try root().appending(path: "drafts", directoryHint: .isDirectory) }
    public static func outbox() throws -> URL { try root().appending(path: "outbox", directoryHint: .isDirectory) }

    /// Move legacy storage once; every origin keeps its own drafts and outbox.
    static func namespaced(_ directory: URL, origin: URL) throws -> URL {
        let key = Data(serverOrigin(origin).utf8).base64EncodedString()
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "+", with: "-")
        let target = directory.deletingLastPathComponent().appending(path: "servers")
            .appending(path: key).appending(path: directory.lastPathComponent)
        let manager = FileManager.default
        if manager.fileExists(atPath: directory.path), !manager.fileExists(atPath: target.path) {
            try manager.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
            try manager.moveItem(at: directory, to: target)
        }
        return target
    }

    /// Canonical scheme, host and effective port, shared by storage and request checks.
    static func serverOrigin(_ url: URL) -> String {
        let scheme = url.scheme?.lowercased() ?? ""
        let host = url.host?.lowercased() ?? ""
        let port = url.port ?? (scheme == "https" ? 443 : 80)
        return "\(scheme)://\(host):\(port)"
    }

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
