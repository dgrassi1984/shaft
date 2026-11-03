import SHAFTKit
import SwiftUI

/// The iPhone and iPad layouts over the shared views. A phone (compact width) shows the chat full screen with a chat
/// switcher; an iPad shows the instances in a sidebar beside the chat. The chat last open comes back at launch, and a
/// notification tap opens its chat.
struct IOSRoot: View {
    let model: AppModel

    var body: some View {
        SessionRoot(model: model) { session, store in
            ChatsLayout(session: session, store: store)
        }
    }
}

struct ChatsLayout: View {
    let session: SHAFTSession
    let store: InstancesStore
    @Environment(AppModel.self) private var model
    @Environment(\.horizontalSizeClass) private var sizeClass
    @AppStorage("chat.last") private var last = ""
    @State private var selection: String?
    @State private var path: [ArchivedRoute] = []
    @State private var switching = false
    @State private var settings = false
    @State private var columns = NavigationSplitViewVisibility.automatic
    @State private var shell = ChatShell()

    var body: some View {
        Group {
            if sizeClass == .compact {
                NavigationStack(path: $path) {
                    chat
                        .toolbar {
                            ToolbarItem(placement: .topBarLeading) {
                                Button { switching = true } label: {
                                    Image(systemName: store.badge > 0 ? "list.bullet.circle.fill" : "list.bullet")
                                }
                                .accessibilityLabel("Chats")
                                .accessibilityValue(store.badge > 0 ? "\(store.badge) unread" : "")
                            }
                        }
                        .navigationDestination(for: ArchivedRoute.self) { route in
                            ArchivedChatView(route: route) { path.removeAll() }
                        }
                }
                .sheet(isPresented: $switching) {
                    NavigationStack {
                        InstancesList(selection: Binding(get: { selection }, set: { name in
                            if let name { open(name) }
                            switching = false
                        }), settings: {
                            switching = false
                            settings = true
                        })
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) { Button("Close") { switching = false } }
                        }
                    }
                    .environment(store)
                    .presentationDetents([.medium, .large])
                }
            } else {
                NavigationSplitView(columnVisibility: $columns) {
                    InstancesList(selection: Binding(get: { selection }, set: { name in if let name { open(name) } }),
                                  settings: { settings = true })
                        .navigationSplitViewColumnWidth(min: 260, ideal: 300, max: 380)
                } detail: {
                    NavigationStack(path: $path) {
                        chat
                            .navigationDestination(for: ArchivedRoute.self) { route in
                                ArchivedChatView(route: route) { path.removeAll() }
                            }
                    }
                    .environment(shell)
                }
                // The documents beside the chat, outside its navigation stack (ChatShell): inside it, their own pages
                // were pushed onto the chat's stack.
                .documentsInspector(shell, store: store)
            }
        }
        .sheet(isPresented: $settings) { SettingsView(session: session) }
        .onAppear(perform: restore)
        .onChange(of: store.instances.map(\.name)) { restore() }
        .onChange(of: model.openChat) { _, name in
            guard let name else { return }
            open(name)
            switching = false
            model.openChat = nil
        }
    }

    @ViewBuilder
    private var chat: some View {
        if let selection {
            ChatScreen(chat: store.chat(selection), openArchived: { summary in
                path.append(ArchivedRoute(instance: selection, cid: summary.id))
            })
            .id(selection)
        } else if store.loaded {
            ContentUnavailableView("No chat open", systemImage: "bubble.left.and.bubble.right",
                                   description: Text("Choose an instance to chat with."))
        } else {
            ProgressView()
        }
    }

    private func open(_ name: String) {
        if selection != name { path.removeAll() }
        selection = name
        last = name
    }

    /// The chat open last time, else the first instance; a tap's chat wins.
    private func restore() {
        if let name = model.openChat {
            open(name)
            model.openChat = nil
            return
        }
        guard selection == nil, !store.instances.isEmpty else { return }
        let names = store.instances.map(\.name)
        open(names.contains(last) ? last : names[0])
    }
}
