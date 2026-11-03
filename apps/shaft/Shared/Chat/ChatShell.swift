import SHAFTKit
import SwiftUI

/// What a platform shell needs to draw the chat's documents beside it: the chat screen on display. The shell puts
/// `documentsInspector` outside its navigation stack (an inspector inside one hides the chat's toolbar); a phone, which
/// has no shell in its environment, gets the documents as a sheet over the chat instead.
@MainActor
@Observable
final class ChatShell {
    /// The chat screen showing now (set by the screen as it appears).
    var screen: ChatScreenModel?
}

extension View {
    /// The documents of the chat on display, as an inspector beside it.
    func documentsInspector(_ shell: ChatShell, store: InstancesStore) -> some View {
        inspector(isPresented: Binding(get: { shell.screen?.showDocuments ?? false },
                                       set: { shell.screen?.showDocuments = $0 })) {
            if let screen = shell.screen, screen.showDocuments {
                DocumentsPanel(store: store, instance: screen.instance, cid: screen.chat.cid)
                    .id("\(screen.instance)-\(screen.chat.cid ?? "")")
                    .environment(screen)
                    .environment(screen.preview)
                    .inspectorColumnWidth(min: 320, ideal: 400, max: 560)
            }
        }
    }
}
