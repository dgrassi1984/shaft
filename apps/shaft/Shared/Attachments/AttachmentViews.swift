import ImageIO
import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

/// Files the chat shows (§4.4 attachments, §8): fetched once with the bearer, kept for the session, pictures downsized
/// here (the server sends no thumbnails), and opened in Quick Look, whose share button saves or sends them on.
@MainActor
final class AttachmentLoader {
    static let shared = AttachmentLoader()
    private let data = NSCache<NSString, NSData>()
    private let files = NSCache<NSString, NSURL>()
    private let thumbnails = NSCache<NSString, CGImage>()
    private var generation = UUID()
    private var running: [String: Task<Data, any Error>] = [:]

    private init() {
        data.totalCostLimit = 32 * 1024 * 1024
        thumbnails.totalCostLimit = 16 * 1024 * 1024
        files.countLimit = 128
    }

    func clear() {
        generation = UUID()
        running.values.forEach { $0.cancel() }
        running.removeAll()
        data.removeAllObjects()
        files.removeAllObjects()
        thumbnails.removeAllObjects()
        try? FileManager.default.removeItem(at: FileManager.default.temporaryDirectory.appending(path: "SHAFT-files"))
    }

    private func key(_ url: String, client: APIClient) -> String {
        let base = client.credential.baseURL
        let scheme = base.scheme?.lowercased() ?? ""
        return "\(scheme)://\(base.host?.lowercased() ?? ""):\(base.port ?? (scheme == "https" ? 443 : 80))|\(url)"
    }

    /// The bytes of a server-relative URL, from memory after the first fetch.
    func data(_ url: String, client: APIClient) async throws -> Data {
        let key = key(url, client: client)
        let generation = generation
        if let cached = data.object(forKey: key as NSString) { return cached as Data }
        if let task = running[key] { return try await task.value }
        let task = Task { try await client.download(url).data }
        running[key] = task
        defer { if self.generation == generation { running[key] = nil } }
        let fetched = try await task.value
        guard self.generation == generation else { throw CancellationError() }
        data.setObject(fetched as NSData, forKey: key as NSString, cost: fetched.count)
        return fetched
    }

    /// A picture at most `side` points wide or tall, decoded off the main thread.
    func thumbnail(_ url: String, client: APIClient, side: CGFloat, scale: CGFloat) async throws -> CGImage {
        let generation = generation
        let key = "\(key(url, client: client))#\(Int(side * scale))"
        if let cached = thumbnails.object(forKey: key as NSString) { return cached }
        let bytes = try await data(url, client: client)
        let pixels = side * scale
        let image = try await Task.detached(priority: .userInitiated) { () throws -> CGImage in
            guard let source = CGImageSourceCreateWithData(bytes as CFData, nil),
                  let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                      kCGImageSourceCreateThumbnailFromImageAlways: true,
                      kCGImageSourceCreateThumbnailWithTransform: true,
                      kCGImageSourceThumbnailMaxPixelSize: pixels,
                  ] as CFDictionary) else {
                throw APIError.decoding(path: url, message: "not a picture")
            }
            return image
        }.value
        guard self.generation == generation else { throw CancellationError() }
        thumbnails.setObject(image, forKey: key as NSString, cost: image.bytesPerRow * image.height)
        return image
    }

    /// A local copy under its own name, for Quick Look and Share.
    func file(_ url: String, name: String, client: APIClient) async throws -> URL {
        let generation = generation
        let key = key(url, client: client)
        if let cached = files.object(forKey: key as NSString) {
            let local = cached as URL
            if FileManager.default.fileExists(atPath: local.path) { return local }
        }
        let bytes = try await data(url, client: client)
        guard self.generation == generation else { throw CancellationError() }
        let local = try Self.write(bytes, name: name, key: key)
        files.setObject(local as NSURL, forKey: key as NSString)
        return local
    }

    /// Bytes the app holds (a document's download) as a file named `name`.
    static func write(_ bytes: Data, name: String, key: String) throws -> URL {
        let folder = FileManager.default.temporaryDirectory
            .appending(path: "SHAFT-files", directoryHint: .isDirectory)
            .appending(path: String(UInt64(bitPattern: Int64(key.hashValue))), directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let safe = name.replacingOccurrences(of: "/", with: "_")
        let local = folder.appending(path: safe.isEmpty ? "file" : safe)
        try bytes.write(to: local, options: .atomic)
        return local
    }
}

/// Where the feed's file taps go: the Quick Look preview of the chat screen.
@MainActor
@Observable
final class FilePreview {
    var url: URL?
    var loading: String?
    var error: String?

    func open(_ attachment: Attachment, client: APIClient) {
        loading = attachment.name
        Task {
            do {
                url = try await AttachmentLoader.shared.file(attachment.url, name: attachment.name, client: client)
                error = nil
            } catch {
                self.error = "Could not open \(attachment.name): \(APIError.from(error).message)"
            }
            loading = nil
        }
    }

    func open(local: URL) {
        url = local
    }
}

/// A message's files: pictures as thumbnails (space kept while they load), the rest as chips. A tap opens Quick Look.
struct AttachmentsView: View {
    let attachments: [Attachment]
    @Environment(InstancesStore.self) private var store
    @Environment(FilePreview.self) private var preview

    var body: some View {
        if !attachments.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(attachments.enumerated()), id: \.offset) { _, attachment in
                    if attachment.image {
                        Button { preview.open(attachment, client: store.client) } label: {
                            Thumbnail(attachment: attachment)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Picture \(attachment.name)")
                        .accessibilityHint("Opens it")
                    } else {
                        Button { preview.open(attachment, client: store.client) } label: {
                            FileChip(name: attachment.name, bytes: attachment.bytes,
                                     busy: preview.loading == attachment.name)
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint("Opens it")
                    }
                }
            }
        }
    }
}

struct Thumbnail: View {
    let attachment: Attachment
    @Environment(InstancesStore.self) private var store
    @Environment(\.displayScale) private var scale
    @State private var image: CGImage?
    @State private var failed: String?
    private let side: CGFloat = 220

    var body: some View {
        Group {
            if let image {
                Image(decorative: image, scale: scale)
                    .resizable()
                    .scaledToFit()
                    .frame(maxWidth: side, maxHeight: side)
                    .clipShape(RoundedRectangle(cornerRadius: 10))
            } else if let failed {
                FileChip(name: attachment.name, bytes: attachment.bytes, busy: false, note: failed)
            } else {
                RoundedRectangle(cornerRadius: 10)
                    .fill(Color.secondary.opacity(0.15))
                    .frame(width: side, height: side * 0.62)
                    .overlay { ProgressView() }
            }
        }
        .task(id: attachment.url) {
            do {
                image = try await AttachmentLoader.shared.thumbnail(attachment.url, client: store.client, side: side,
                                                                    scale: scale)
            } catch is CancellationError {
            } catch {
                failed = APIError.from(error).message
            }
        }
    }
}

/// A file as a chip: an icon by its kind, its name and size.
struct FileChip: View {
    let name: String
    let bytes: Int
    var busy = false
    var note: String?

    var body: some View {
        HStack(spacing: 8) {
            if busy {
                ProgressView().controlSize(.small)
            } else {
                Image(systemName: FileChip.symbol(for: name)).foregroundStyle(.secondary).accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(name).lineLimit(1).truncationMode(.middle)
                Text(note ?? DocsTree.size(bytes)).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .frame(minHeight: 44)
        .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
        .accessibilityElement(children: .combine)
    }

    static func symbol(for name: String) -> String {
        let type = UTType(filenameExtension: (name as NSString).pathExtension)
        if type?.conforms(to: .image) == true { return "photo" }
        if type?.conforms(to: .pdf) == true { return "doc.richtext" }
        if type?.conforms(to: .text) == true || type?.conforms(to: .sourceCode) == true { return "doc.text" }
        if type?.conforms(to: .archive) == true { return "doc.zipper" }
        return "doc"
    }
}
