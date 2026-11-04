import SHAFTKit
import SwiftUI

/// What a platform shell needs to draw the chat's documents beside it: the chat screen on display. The shell puts
/// `documentsInspector` outside its navigation stack (an inspector inside one hides the chat's toolbar); a phone says
/// `sheetsDocuments`, and `ChatScreen` presents the documents as a sheet over the chat instead.
@MainActor
@Observable
final class ChatShell {
    /// Chat and inspector no longer fit beside each other under this window width (spec §5: 380 + 320).
    static let besideWidth: CGFloat = 700
    /// Sidebar, chat and inspector no longer fit together under this window width (spec §5).
    static let threeColumnWidth: CGFloat = 1000
    static let chatMinimum: CGFloat = 380
    static let inspectorWidth: CGFloat = 320
    /// The system opens an inspector as an overlay dimming the chat unless about 450 pt stay for the chat beside its
    /// minimum: this much holds at `besideWidth`. Under about 770 pt it is narrower than `inspectorWidth`.
    static let inspectorMinimum: CGFloat = 240
    #if os(macOS)
    /// The sidebar's minimum on the Mac.
    static let sidebarMinimum: CGFloat = 220

    /// Sidebar, chat and documents no longer fit together under this window width: the spec's 1000 (§5), or more for a
    /// sidebar dragged wider, so the chat keeps its minimum.
    static func threeColumnWidth(sidebar: CGFloat) -> CGFloat {
        max(threeColumnWidth, sidebar + chatMinimum + inspectorWidth)
    }
    #endif

    /// The chat screen showing now (set by the screen as it appears).
    var screen: ChatScreenModel?
    /// The chat's own window width, on a device whose windows resize.
    var width: CGFloat = .infinity
    #if os(macOS)
    /// The sidebar's width on the Mac, 0 while it is collapsed.
    var sidebarWidth: CGFloat = 0
    #endif
    /// A phone: no inspector, `ChatScreen` draws the documents as a sheet.
    let sheetsDocuments: Bool

    init(sheetsDocuments: Bool = false) {
        self.sheetsDocuments = sheetsDocuments
    }

    /// The documents cover the chat rather than sit beside it.
    var documentsCover: Bool { sheetsDocuments || width < Self.besideWidth }
    /// Opening the documents here leaves no room for the sidebar.
    var collapsesSidebar: Bool { width < Self.threeColumnWidth }
    /// The widest the inspector may be dragged and still leave the chat its minimum.
    var inspectorMaximum: CGFloat {
        guard width.isFinite else { return 560 }
        #if os(macOS)
        // The chat keeps its minimum, and the split view, which counts the inspector twice, stays inside the window
        // (MacRoot.splitChatMinimum). A dragged inspector never leaves it.
        let sidebarMinimum = sidebarWidth > 0 ? Self.sidebarMinimum : 0
        let counted = (width - sidebarMinimum - Self.chatMinimum + Self.inspectorWidth) / 2
        return max(Self.inspectorWidth, min(560, width - sidebarWidth - Self.chatMinimum, counted.rounded(.down)))
        #else
        return max(Self.inspectorWidth, min(560, width - Self.chatMinimum - (collapsesSidebar ? 0 : 300)))
        #endif
    }
}

extension EnvironmentValues {
    /// The window showing the chat is the one in front (the Mac's `MacPresence`): only then is the chat marked seen.
    /// A window behind another app's still shows the chat, so it keeps its long-poll (`scenePhase` says that). A
    /// phone or tablet has one window: always true there.
    @Entry var chatInFront = true
    @Entry var chatCovered = false
}

extension View {
    /// The documents of the chat on display, as an inspector beside it; the system draws it as a sheet where the
    /// window is compact, keeping what is open. `isPresented` is for a shell that sequences it: by default it follows
    /// `showDocuments`.
    func documentsInspector(_ shell: ChatShell, store: InstancesStore, isPresented: Binding<Bool>? = nil) -> some View {
        inspector(isPresented: isPresented ?? Binding(get: { shell.screen?.showDocuments ?? false },
                                                      set: { shell.screen?.showDocuments = $0 })) {
            if let screen = shell.screen, screen.showDocuments {
                DocumentsPanel(store: store, instance: screen.instance, cid: screen.chat.cid)
                    .id("\(screen.instance)-\(screen.chat.cid ?? "")")
                    .environment(screen)
                    .environment(screen.preview)
                    #if os(iOS)
                    .inspectorColumnWidth(min: ChatShell.inspectorMinimum, ideal: ChatShell.inspectorWidth,
                                          max: shell.inspectorMaximum)
                    #else
                    .inspectorColumnWidth(min: ChatShell.inspectorWidth, ideal: ChatShell.inspectorWidth,
                                          max: shell.inspectorMaximum)
                    #endif
            }
        }
    }
}
