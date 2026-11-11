import SwiftUI
#if os(macOS)
import Quartz
#else
import QuickLook
#endif

extension View {
    /// The file `preview` holds: a Markdown page, or the system's preview.
    func filePreview(_ preview: FilePreview) -> some View {
        systemPreview(Binding(get: { preview.url }, set: { preview.url = $0 }))
            .sheet(item: Binding(get: { preview.page }, set: { preview.page = $0 })) { page in
                MarkdownFileSheet(page: page)
            }
    }

    private func systemPreview(_ url: Binding<URL?>) -> some View {
        #if os(macOS)
        sheet(item: Binding(
            get: { url.wrappedValue.map { PreviewFile(url: $0) } },
            set: { url.wrappedValue = $0?.url }
        )) { file in
            MacFileSheet(url: file.url) { MacPreviewContent(url: file.url) }
        }
        #else
        quickLookPreview(url)
        #endif
    }
}

/// A Markdown file as a document reads, read-only: it is the message's, so nothing here edits or comments on it. Share
/// keeps the way out that Quick Look gave.
private struct MarkdownFileSheet: View {
    let page: MarkdownPage
    #if os(iOS)
    @Environment(\.dismiss) private var dismiss
    #endif

    var body: some View {
        #if os(macOS)
        MacFileSheet(url: page.file) { content }
        #else
        NavigationStack {
            content
                .navigationTitle(page.file.lastPathComponent)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                    ToolbarItem(placement: .primaryAction) { ShareFile(url: page.file) }
                }
        }
        #endif
    }

    @ViewBuilder
    private var content: some View {
        if page.text.allSatisfy(\.isWhitespace) {
            ContentUnavailableView("Empty file", systemImage: "doc")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.canvas)
        } else {
            ScrollView {
                MarkdownText(source: page.text)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .readingColumn()
                    .padding(.vertical, Design.Space.s16)
            }
            .readingTextSize()
            .background(Color.canvas)
        }
    }
}

private struct ShareFile: View {
    let url: URL

    var body: some View {
        ShareLink(item: url) { Image(systemName: "square.and.arrow.up") }
            .accessibilityLabel("Share \(url.lastPathComponent)")
    }
}

#if os(macOS)
private struct PreviewFile: Identifiable {
    let url: URL
    var id: URL { url }
}

private struct MacFileSheet<Content: View>: View {
    let url: URL
    @ViewBuilder var content: () -> Content
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: Design.Space.s12) {
                Text(verbatim: url.lastPathComponent)
                    .font(.headline)
                    .foregroundStyle(Color.textPrimary)
                    .lineLimit(1)
                Spacer()
                ShareFile(url: url)
                Button("Done") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(Design.Space.s16)
            content()
        }
        .frame(minWidth: 640, idealWidth: 760, minHeight: 480, idealHeight: 620)
        .background(Color.canvas)
    }
}

private struct MacPreviewContent: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> NSView {
        guard let preview = QLPreviewView(frame: .zero, style: .normal) else {
            return NSHostingView(rootView: PreviewUnavailable(url: url))
        }
        preview.previewItem = url as NSURL
        preview.autostarts = true
        return preview
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        if let preview = nsView as? QLPreviewView {
            if preview.previewItem as? NSURL != url as NSURL {
                preview.previewItem = url as NSURL
            }
        } else if let fallback = nsView as? NSHostingView<PreviewUnavailable> {
            fallback.rootView = PreviewUnavailable(url: url)
        }
    }

    static func dismantleNSView(_ nsView: NSView, coordinator: ()) {
        (nsView as? QLPreviewView)?.close()
    }
}

private struct PreviewUnavailable: View {
    let url: URL

    var body: some View {
        VStack(spacing: Design.Space.s8) {
            Text(verbatim: url.lastPathComponent)
                .foregroundStyle(Color.textPrimary)
            Text("This file cannot be previewed.")
                .foregroundStyle(Color.textSecondary)
        }
        .padding(Design.Space.s24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.canvas)
    }
}
#endif
