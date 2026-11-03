import SHAFTKit
import SwiftUI

/// An earlier chat pushed over the current one (§5).
struct ArchivedRoute: Hashable {
    let instance: String
    let cid: String
}

/// An earlier chat, read-only, holding its own store while it is on screen.
struct ArchivedChatView: View {
    let route: ArchivedRoute
    var back: () -> Void
    @Environment(InstancesStore.self) private var store
    @State private var chat: ChatStore?

    var body: some View {
        Group {
            if let chat {
                ChatScreen(chat: chat, backToCurrent: back)
            } else {
                ProgressView()
            }
        }
        .onAppear {
            if chat == nil { chat = store.archivedChat(route.instance, cid: route.cid) }
        }
    }
}
