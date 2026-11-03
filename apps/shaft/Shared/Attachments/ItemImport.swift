import ImageIO
import PhotosUI
import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

/// Files coming into the composer: from Photos, a paste or a drop. Each is copied into the draft (`ChatStore.attach`),
/// so nothing depends on a loan from the system once it is attached. A HEIC photo goes as JPEG: the server shows
/// `.jpg .png .gif .webp` as pictures (§4.4).
enum ItemImport {
    /// Attach what item providers hold (a paste, a drop): files by their file representation, pictures as data.
    @MainActor
    static func attach(_ providers: [NSItemProvider], to chat: ChatStore) async -> Int {
        var count = 0
        for (index, provider) in providers.enumerated() {
            if let (data, name) = await load(provider, index: index) {
                if chat.attach(data: data, name: name) { count += 1 }
            }
        }
        return count
    }

    /// Attach picked photos and videos.
    @MainActor
    static func attach(_ items: [PhotosPickerItem], to chat: ChatStore) async -> Int {
        var count = 0
        for (index, item) in items.enumerated() {
            guard let data = try? await item.loadTransferable(type: Data.self) else { continue }
            let type = item.supportedContentTypes.first ?? .jpeg
            let (bytes, ext) = convert(data, type: type)
            if chat.attach(data: bytes, name: "photo-\(stamp())-\(index + 1).\(ext)") { count += 1 }
        }
        return count
    }

    @MainActor
    static func load(_ provider: NSItemProvider, index: Int) async -> (Data, String)? {
        let types = provider.registeredContentTypes
        // A file (a drop from Files): its bytes and its own name.
        if let type = types.first(where: { !$0.conforms(to: .image) && $0.conforms(to: .data) })
            ?? types.first(where: { $0.conforms(to: .image) }) {
            if let (data, name) = await fileRepresentation(provider, type: type) {
                let (bytes, ext) = convert(data, type: type)
                let base = (name as NSString).deletingPathExtension
                let named = type.conforms(to: .image) && ext != (name as NSString).pathExtension.lowercased()
                    ? "\(base.isEmpty ? "picture-\(stamp())-\(index + 1)" : base).\(ext)" : name
                return (bytes, named)
            }
        }
        return nil
    }

    @MainActor
    private static func fileRepresentation(_ provider: NSItemProvider, type: UTType) async -> (Data, String)? {
        let suggested = provider.suggestedName
        return await withCheckedContinuation { continuation in
            _ = provider.loadFileRepresentation(for: type, openInPlace: false) { url, _, _ in
                // The URL lives only inside this handler: read it now.
                guard let url, let data = try? Data(contentsOf: url) else {
                    continuation.resume(returning: nil)
                    return
                }
                let name = suggested.map { name -> String in
                    (name as NSString).pathExtension.isEmpty ? name + "." + (type.preferredFilenameExtension ?? "bin") : name
                } ?? url.lastPathComponent
                continuation.resume(returning: (data, name))
            }
        }
    }

    /// The bytes as the server shows them: a HEIC/HEIF or other non-web picture re-encoded as JPEG.
    static func convert(_ data: Data, type: UTType) -> (Data, String) {
        let web: [UTType] = [.jpeg, .png, .gif, .webP]
        if type.conforms(to: .image), !web.contains(where: { type.conforms(to: $0) }),
           let source = CGImageSourceCreateWithData(data as CFData, nil),
           let image = CGImageSourceCreateImageAtIndex(source, 0, nil) {
            let out = NSMutableData()
            if let destination = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) {
                CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary)
                if CGImageDestinationFinalize(destination) { return (out as Data, "jpg") }
            }
        }
        return (data, type.preferredFilenameExtension ?? "bin")
    }

    private static func stamp() -> String {
        Date().formatted(.verbatim("\(year: .defaultDigits)\(month: .twoDigits)\(day: .twoDigits)-\(hour: .twoDigits(clock: .twentyFourHour, hourCycle: .zeroBased))\(minute: .twoDigits)\(second: .twoDigits)",
                                   timeZone: .current, calendar: .current))
    }
}
