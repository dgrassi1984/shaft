import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

/// Download on a document, from its row in the list or from its page: the document's bytes (§10.3, whatever its size)
/// saved where the operator picks, under its own name — the save panel on a Mac, Files on an iPhone or iPad.
@MainActor
@Observable
final class DocumentSaver {
    /// The bytes fetched, waiting for the operator to pick where they go.
    var file: SavedDocument?
    /// Why the last download did not reach the save step.
    var problem: String?
    /// The document being fetched: a second Download waits for it.
    private(set) var fetching: String?

    func download(_ path: String, from docs: DocumentsStore) {
        guard fetching == nil else { return }
        fetching = path
        Task {
            defer { fetching = nil }
            let name = (path as NSString).lastPathComponent
            guard let fetched = await docs.download(path) else {
                problem = "Could not download \(name): \(docs.error?.message ?? "no answer from the server.")"
                return
            }
            file = SavedDocument(data: fetched.data, name: SavedDocument.name(fetched.filename, path: path))
        }
    }
}

/// A downloaded document as the system's exporter writes it: its bytes as they are, under the document's name.
struct SavedDocument: FileDocument {
    // Any bytes: the name carries the extension, so a `.md` stays a `.md` (a narrower type would make the panel
    // rename it, and the exporter would refuse a type the document does not list).
    static let readableContentTypes: [UTType] = [.data]

    let data: Data
    let name: String

    init(data: Data, name: String) {
        self.data = data
        self.name = name
    }

    init(configuration: ReadConfiguration) throws {
        data = configuration.file.regularFileContents ?? Data()
        name = configuration.file.filename ?? "document"
    }

    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
        FileWrapper(regularFileWithContents: data)
    }

    /// The server's name for it when it gives one, else the last part of its path; never a path of its own.
    static func name(_ served: String?, path: String) -> String {
        let candidate = (served?.isEmpty == false ? served! : (path as NSString).lastPathComponent)
        let safe = candidate.replacingOccurrences(of: "/", with: "_")
        return safe.isEmpty ? "document" : safe
    }
}

extension View {
    /// Where `saver`'s downloads go: the save panel (or Files), and an alert when one could not be fetched.
    func documentSaver(_ saver: DocumentSaver) -> some View {
        // A cancelled panel closes through the binding, which lets the bytes go.
        fileExporter(isPresented: Binding(get: { saver.file != nil }, set: { if !$0 { saver.file = nil } }),
                     document: saver.file, contentType: .data, defaultFilename: saver.file?.name) { result in
            if case .failure(let error) = result {
                saver.problem = "Could not save it: \(error.localizedDescription)"
            }
            saver.file = nil
        }
        .alert(saver.problem ?? "", isPresented: Binding(get: { saver.problem != nil },
                                                         set: { if !$0 { saver.problem = nil } })) {
            Button("OK", role: .cancel) { saver.problem = nil }
        }
    }
}
