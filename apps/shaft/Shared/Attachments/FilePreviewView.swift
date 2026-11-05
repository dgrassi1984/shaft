import SwiftUI
#if os(macOS)
import Quartz
#else
import QuickLook
#endif

extension View {
    func filePreview(_ url: Binding<URL?>) -> some View {
        #if os(macOS)
        sheet(item: Binding(
            get: { url.wrappedValue.map { PreviewFile(url: $0) } },
            set: { url.wrappedValue = $0?.url }
        )) { file in
            MacFilePreview(url: file.url)
        }
        #else
        self.quickLookPreview(url)
        #endif
    }
}

#if os(macOS)
private struct PreviewFile: Identifiable {
    let url: URL
    var id: URL { url }
}

private struct MacFilePreview: View {
    let url: URL
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: Design.Space.s12) {
                Text(verbatim: url.lastPathComponent)
                    .font(.headline)
                    .foregroundStyle(Color.textPrimary)
                    .lineLimit(1)
                Spacer()
                Button("Done") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(Design.Space.s16)
            MacPreviewContent(url: url)
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
