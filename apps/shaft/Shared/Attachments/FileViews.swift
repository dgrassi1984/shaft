import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

/// A message's files: pictures as thumbnails (space kept while they load), the rest as chips. A tap opens Quick Look.
struct AttachmentsView: View {
    let attachments: [Attachment]
    @Environment(InstancesStore.self) private var store
    @Environment(FilePreview.self) private var preview

    var body: some View {
        if !attachments.isEmpty {
            VStack(alignment: .leading, spacing: Design.Space.s8) {
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

    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous) }

    var body: some View {
        Group {
            if let image {
                Image(decorative: image, scale: scale)
                    .resizable()
                    .scaledToFit()
                    .frame(maxWidth: side, maxHeight: side)
                    .clipShape(shape)
                    .overlay { shape.strokeBorder(Color.hairline) }
            } else if let failed {
                FileChip(name: attachment.name, bytes: attachment.bytes, busy: false, note: failed)
            } else {
                shape.fill(Color.elevated)
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
        HStack(spacing: Design.Space.s8) {
            if busy {
                ProgressView().controlSize(.small)
            } else {
                Image(systemName: FileChip.symbol(for: name)).foregroundStyle(Color.brand).accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: Design.Space.s4) {
                Text(name)
                    .font(.subheadline)
                    .foregroundStyle(Color.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(note ?? DocsTree.size(bytes))
                    .font(Design.Text.metadata)
                    .foregroundStyle(Color.textSecondary)
                    .lineLimit(2)
            }
        }
        .padding(.horizontal, Design.Space.s12)
        .padding(.vertical, Design.Space.s8)
        .frame(minHeight: 44)
        .elevatedBlock()
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
