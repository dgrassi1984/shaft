# Shared: the chat both apps draw

The SwiftUI views and view models of the iPhone, iPad and Mac apps (PLAN-native-apps.md M4/M5), over SHAFTKit's
stores. The session forwards badge confirmations to `AppModel.setBadge`, independently of these views.
Everything here compiles for iOS and macOS; what differs per platform sits behind small seams.

| Folder | What |
|---|---|
| `Root/` | `SessionRoot` (startup error, pairing, or the paired app with the instances store in the environment), `PairingView`, `SettingsView`, `InstancesList` and its rows |
| `Chat/` | `ChatScreen` (header, feed, composer, sheets; the long-poll only while visible and in front, `away`, seen, the new-reply announcement, drops), `ChatScreenModel` (sheets, focus requests, message actions, a send asked for by a menu), `ChatShell` (documents beside the chat), the sheets (Select text, a thumb's reason, earlier chats), `ArchivedChatView` (an earlier chat pushed over the current one) |
| `Feed/` | `FeedView` (pinned to the bottom until scrolled up, Latest and New below in the bar above the composer, Show the N earlier messages), a view per item kind (§4.4) and per row after them (live reply, queued, pending feedback and edits, not sent) |
| `Composer/` | quote cards with their answers, attachment chips, the field, Send or Stop, and a "More ways to send" chevron beside Send once there is text or other draft content (Queue after reply / Next cycle); these choices also stay in + and, on iOS, a long press on Send |
| `Documents/` | the panel (folders, sort, new document), a document (rendered, threads, overwritten lines, Select text with Reply and Comment, Download, Delete), the editor with the stale-save choice, comments, `CommentHighlight.swift` and `DocumentMarkdown.swift` |
| `Markdown/` | MarkdownUI with SHAFT's theme (tables that scroll, task lists, code with language, Copy and syntax colours), the operator's own text, Markdown as rendered text |
| `Attachments/` | thumbnails and file chips through Quick Look (`FileViews.swift`); files coming in from Photos, a paste or a drop |
| `Platform/` | the text seams: the composer's field and selectable text (`TextViews.swift`, iOS), a message's text (`MessageText.swift`) |
| `Support/` | times, the clipboard, announcements, small view helpers |

If the initial chat list cannot load, the chat area shows the error, Retry and Settings on all three devices.
The store keeps trying every few seconds while active; Retry restarts that loop immediately. Settings offers Unpair
and "Forget the pairing here only" when the server cannot be reached.

## Platform seams

- **Text** (`Platform/TextViews.swift` for iOS, `../macOS/MacTextViews.swift` for the Mac): `ComposerTextView` (the
  composer's field with the PWA's key rules and paste of pictures and files: a `UITextView` with key commands; an
  `NSTextView`) and `SelectableTextView` (Select text, whose selection's menu has ↩ Reply / + Quote and Comment).
- **A message's text** (`Platform/MessageText.swift`): `MessageMarkdown` and `MessageOperatorText`. iOS draws them with
  MarkdownUI and selects through the long-press menu's Select text; the Mac draws them in an `NSTextView` that selects
  in place (`../macOS/MessageTextView.swift`), with its right-click menu drawn from the `messageMenu` the
  `messageMenu(_:)` modifier puts in the environment. On the Mac a quote's new card takes the keyboard
  (`ChatScreenModel.quote`), as on a computer in the web.
- **Documents beside the chat** (`Chat/ChatShell.swift`): a shell puts `ChatShell` in the environment of its chat
  column and `.documentsInspector(shell, store:)` on its split view, outside the chat's navigation stack. A phone sets
  `sheetsDocuments`, so documents open in a system sheet instead of the inspector. The chat stops watching while it covers it.
- **In front** (`chatInFront`, `Chat/ChatShell.swift`): the chat is marked seen only while its window is the one in
  front and the end of the newest item is on screen, however tall the item is. Failed seen requests retry after 2, 4,
  8, 16, then 30 seconds, capped at 30 seconds, using the current feed version; retries cancel when that end leaves the
  screen, or the chat is covered or no longer in front. The Mac sets it from its window, and gives the chat a `scenePhase` that is active
  while the window shows it.
- **Menus**: `ChatScreenModel.sendRequest` (a send in a mode, through the composer's own send) and
  `confirmingNewChat` and `confirmingEnd`.
- **Pointer and keyboard on a computer** (macOS only): `HoverActions` (`Feed/FeedItemViews.swift`), Copy and Reply
  after a message's thumbs while the pointer is over it; Return in a card's answer sends, and the keyboard then moves to the
  main field (`Composer/ComposerView.swift`).
- **Size**: the shell's width rules (`horizontalSizeClass` is gone from `ChatScreen`),
  `sheetFrame()` (a sheet's size on the Mac), `iconMenu()` (a composer menu drawn as its icon on the Mac).
- **Clipboard**: `Clipboard.copy`.
