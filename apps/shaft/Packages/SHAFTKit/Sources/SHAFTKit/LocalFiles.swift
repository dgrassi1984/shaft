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
        // Earlier Mac builds ran sandboxed: their drafts and outbox live in the app's container. A move that fails
        // throws, so the app reports it at startup instead of running on empty storage beside unreadable data.
        if let id = Bundle.main.bundleIdentifier {
            let home = FileManager.default.homeDirectoryForCurrentUser
            let container = home.appending(path: "Library/Containers/\(id)/Data/Library/Application Support/SHAFT",
                                           directoryHint: .isDirectory)
            if container.standardizedFileURL != root.standardizedFileURL {
                try adoptSandboxContainer(container, into: root)
            }
        }
        #endif
        return root
    }

    /// Move what a sandboxed build left in its container into `root`, file by file, so a partial earlier move is
    /// finished by the next one. A file absent from `root` moves; one in both with the same bytes drops the container
    /// copy; one in both with other bytes is kept beside it as `<name>.from-sandbox<ext>` (no store reads that name:
    /// drafts and the outbox are opened by key). Throws, naming the container, when anything cannot be moved.
    static func adoptSandboxContainer(_ container: URL, into root: URL) throws {
        let manager = FileManager.default
        do {
            guard try exists(container) else { return }
            try manager.createDirectory(at: root, withIntermediateDirectories: true)
            try adopt(container, into: root)
        } catch {
            throw CocoaError(.fileWriteUnknown, userInfo: [
                NSLocalizedDescriptionKey: "Could not move the drafts and outbox from \(container.path) to \(root.path): "
                    + error.localizedDescription])
        }
    }

    /// Whether `url` exists. Only a genuine "no such file" is absence: a denied ancestor or any other failure throws.
    private static func exists(_ url: URL) throws -> Bool {
        var info = stat()
        if lstat(url.path, &info) == 0 { return true }
        let code = errno
        if code == ENOENT { return false }
        throw POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO)
    }

    private static func adopt(_ source: URL, into target: URL) throws {
        let manager = FileManager.default
        for item in try manager.contentsOfDirectory(at: source, includingPropertiesForKeys: [.isDirectoryKey]) {
            let name = item.lastPathComponent
            let destination = target.appending(path: name)
            let isDirectory = (try item.resourceValues(forKeys: [.isDirectoryKey])).isDirectory == true
            if !manager.fileExists(atPath: destination.path) {
                try manager.moveItem(at: item, to: destination)
            } else if isDirectory {
                try adopt(item, into: destination)
                if try manager.contentsOfDirectory(atPath: item.path).isEmpty { try manager.removeItem(at: item) }
            } else if manager.contentsEqual(atPath: item.path, andPath: destination.path) {
                try manager.removeItem(at: item)
            } else {
                // Never delete a preserved copy: an identical one absorbs the incoming file, a different one
                // sends it to the next unused name (`.from-sandbox`, `.from-sandbox-2`, …).
                let base = destination.deletingPathExtension().lastPathComponent
                let ext = destination.pathExtension.isEmpty ? "" : "." + destination.pathExtension
                var number = 1
                while true {
                    let marker = number == 1 ? ".from-sandbox" : ".from-sandbox-\(number)"
                    let kept = target.appending(path: base + marker + ext)
                    if !manager.fileExists(atPath: kept.path) {
                        try manager.moveItem(at: item, to: kept)
                        break
                    }
                    if manager.contentsEqual(atPath: item.path, andPath: kept.path) {
                        try manager.removeItem(at: item)
                        break
                    }
                    number += 1
                }
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
