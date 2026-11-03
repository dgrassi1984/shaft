# The Mac app

The native macOS app (SwiftUI, not Catalyst; PLAN-native-apps.md M5) over the shared views in `../Shared/`.

| File | What |
|---|---|
| `SHAFTApp.swift` | the entry point: one window, the menu commands, the Settings window (⌘,: Shared's `SettingsView`); the app delegate (the push token, notification clicks that bring the window forward on that chat, no banner for the chat in the window in front, `/` to the composer, the app kept running with its window closed) |
| `MacRoot.swift` | the window: the instances in the sidebar, the chat (earlier chats pushed over it), the documents as an inspector (`ChatShell`); `MacState`, which the menus share; `MacPresence` (the chat long-polls while the window shows it and is marked seen only while the window is in front); the Dock badge; `shaft://chat/<instance>` |
| `MacCommands.swift` | the menu bar: File → New Message (⌘N), New Chat (⇧⌘N); Chat → Stop (⌘.), End (⇧⌘E), Queue after reply (⌘↩; Send when idle), Send for the Next Cycle, Documents (⌥⌘D), Earlier Chats (⇧⌘Y), Next/Previous Chat (⌥↓/⌥↑), Reload (⌘R); the sidebar and inspector toggles. `MacMenu` publishes what they may do |
| `MacTextViews.swift` | the Mac halves of Shared's text seams: the composer's field (an `NSTextView`: Return sends, Shift/Option-Return a new line, Cmd-Return queues, files pasted or dropped are attached) and the selectable text of Select text (↩ Reply and Comment in its right-click menu) |
| `MessageTextView.swift` | a message's text in an `NSTextView` that selects in place, with ↩ Reply / + Quote beside a selection and a right-click menu (↩ Reply on the selection, the system's Copy and Look Up, Copy message, Reply to message, Copy code, the thumbs) |
| `MenuAction.swift` | the text views' own menu items (a closure each) and the read-only text's right-click menu without Cut, Paste, Font and Spelling. Tested in `../macOSTests/` |
| `MarkdownAttributed.swift` | Markdown (and the operator's own text) as AppKit text: headings, lists, task boxes, code blocks, quotes, tables (`NSTextTable`), links. Tested in `../macOSTests/` |
| `MacDevSession.swift`, `MacDevDriver.swift` | Debug builds only: a run from a terminal against the fixture server (`SHAFT_DEV_DIR`, `SHAFT_DEV_PAIR`, `SHAFT_DEV_PRESENCE`) and a script that drives it (`SHAFT_DEV_DRIVE`); `../README.md` says how |
