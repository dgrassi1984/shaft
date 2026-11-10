import AppKit
import SHAFTKit
import SwiftUI

/// The menu bar's commands: File → New Message and New Chat; a Chat menu with Stop, End,
/// Queue after reply, Next cycle, Talk (voice mode, ⇧⌘T), Documents, Earlier chats, the next and previous chat
/// (Option-↓/↑) and Reload. Each acts on the chat on display and is off when it cannot (an earlier chat, nothing to
/// send, no reply to stop, voice mode off for the instance).
struct MacCommands: Commands {
    let state: MacState
    /// What the menus may do now. Menu commands are not redrawn when observable state they read changes, so the
    /// window publishes this snapshot (`MacMenu.publish`) and the commands observe it.
    @ObservedObject var menu: MacMenu

    private var screen: ChatScreenModel? { state.currentScreen }
    private var now: MacMenu.Snapshot { menu.snapshot }

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("New Message") { state.focusComposer() }
                .keyboardShortcut("n")
                .disabled(!now.current)
            Button("New Chat…") { screen?.confirmingNewChat = true }
                .keyboardShortcut("n", modifiers: [.command, .shift])
                .disabled(!now.canClear)
        }
        CommandMenu("Chat") {
            Button("Stop the Reply") { if let chat = screen?.chat { Task { await chat.stopReply() } } }
                .keyboardShortcut(".")
                .disabled(!now.canStop)
            Button("End the Conversation") { screen?.confirmingEnd = true }
                .keyboardShortcut("e", modifiers: [.command, .shift])
                .disabled(!now.canEnd)
            Divider()
            // ⌘↩ queues while a reply runs and sends now when idle (the server's `queue`), as the field's ⌘-Return.
            Button(now.replying ? "Queue After Reply" : "Send") {
                screen?.sendRequest = .queue
            }
            .keyboardShortcut(.return)
            .disabled(!now.canSend)
            // Next cycle is only ever a deliberate choice: no shortcut, as Enter and Cmd-Enter never do it.
            Button("Send for the Next Cycle") { screen?.sendRequest = .background }
                .disabled(!now.canSend)
            Divider()
            // The voice panel in the chat window. "Talk" is what the scripted driver's `menu Talk` picks.
            Button("Talk") { screen?.showVoice = true }
                .keyboardShortcut("t", modifiers: [.command, .shift])
                .disabled(!now.canTalk)
            Button("Documents") { state.screen?.togglePanel(.documents) }
                .keyboardShortcut("d", modifiers: [.command, .option])
                .disabled(!now.chat)
            Button("Earlier Chats…") { state.screen?.showEarlierChats = true }
                .keyboardShortcut("y", modifiers: [.command, .shift])
                .disabled(!now.chat)
            Divider()
            Button("Next Chat") { state.step(1) }
                .keyboardShortcut(.downArrow, modifiers: .option)
                .disabled(!now.instances)
            Button("Previous Chat") { state.step(-1) }
                .keyboardShortcut(.upArrow, modifiers: .option)
                .disabled(!now.instances)
            Divider()
            Button("Reload") { state.reload() }
                .keyboardShortcut("r")
                .disabled(!now.paired)
        }
        SidebarCommands()
        InspectorCommands()
    }
}

/// What the menu commands may do, published by the window as it changes.
@MainActor
final class MacMenu: ObservableObject {
    struct Snapshot: Equatable {
        /// A chat is shown (current or earlier) / the current one.
        var chat = false
        var current = false
        var canStop = false
        var canEnd = false
        var canClear = false
        /// The composer holds something to send (and shows: the voice panel takes its place).
        var canSend = false
        /// Voice mode is on for the current chat's instance.
        var canTalk = false
        var replying = false
        var instances = false
        var paired = false
    }

    @Published private(set) var snapshot = Snapshot()

    /// The snapshot of the window's state now (read in a view's body, so the view follows each part of it).
    static func snapshot(_ state: MacState) -> Snapshot {
        let screen = state.currentScreen
        let content = screen?.chat.content
        return Snapshot(chat: state.screen != nil, current: screen != nil, canStop: content?.canStop == true,
                        canEnd: content?.canEnd == true, canClear: content?.canClear == true,
                        canSend: screen.map { !$0.chat.draft.isEmpty && !$0.showVoice } ?? false,
                        canTalk: screen.map { state.store?.row($0.instance)?.voiceMode == "on" } ?? false,
                        replying: screen?.chat.feed?.reply != nil,
                        instances: !(state.store?.instances.isEmpty ?? true), paired: state.store != nil)
    }

    func publish(_ snapshot: Snapshot) {
        guard self.snapshot != snapshot else { return }
        self.snapshot = snapshot
        // SwiftUI redraws the commands at once but brings the menu bar's items up to date only as a menu opens:
        // until then a key equivalent (⌘., ⌥↓) found its item as it was, off. Do now what opening them does.
        DispatchQueue.main.async {
            for item in NSApp.mainMenu?.items ?? [] {
                guard let menu = item.submenu else { continue }
                menu.delegate?.menuNeedsUpdate?(menu)
                menu.update()
            }
        }
    }
}
