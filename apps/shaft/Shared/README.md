# Shared: the chat both apps draw

The SwiftUI views and view models of the iPhone, iPad and Mac apps (PLAN-native-apps.md M4/M5), over SHAFTKit's
stores. Everything here compiles for iOS and macOS; what differs per platform sits behind small seams.

| Folder | What |
|---|---|
| `Root/` | `SessionRoot` (startup error, pairing, or the paired app with the instances store in the environment; the badge), `PairingView`, `SettingsView`, `InstancesList` and its rows |
| `Chat/` | `ChatScreen` (header, feed, composer, sheets; the long-poll only while visible and in front, `away`, seen, the new-reply announcement, drops), `ChatScreenModel` (sheets, focus requests, message actions), `ChatShell` (documents beside the chat), the sheets (Select text, a thumb's reason, earlier chats) |
| `Feed/` | `FeedView` (pinned to the bottom until scrolled up, New below, Show the N earlier messages), a view per item kind (§4.4) and per row after them (live reply, queued, pending feedback and edits, not sent, End) |
| `Composer/` | quote cards with their answers, attachment chips, the field, Send now / Queue after reply / Next cycle |
| `Documents/` | the panel (folders, sort, new document), a document (rendered, threads, overwritten lines, Select text with Reply and Comment, Download, Delete), the editor with the stale-save choice, comments |
| `Markdown/` | MarkdownUI with SHAFT's theme (tables that scroll, task lists, code with Copy), the operator's own text, Markdown as rendered text |
| `Attachments/` | thumbnails and file chips through Quick Look; files coming in from Photos, a paste or a drop |
| `Platform/` | the text seams: the composer's field and selectable text |
| `Support/` | times, the clipboard, announcements, small view helpers |

## Seams for the Mac

- **Text** (`Platform/TextViews.swift`): `ComposerTextView` (iOS: a `UITextView` with the PWA's key rules as key
  commands, paste of pictures and files; macOS: a `TextField` fallback with Return / Shift-Return / Cmd-Return) and
  `SelectableTextView` (iOS: a `UITextView` whose selection menu has Reply / + Quote and Comment; macOS: selectable
  text, the sheet's button quotes the whole). The Mac lane replaces the macOS halves with AppKit views of the same
  shape.
- **Documents beside the chat** (`Chat/ChatShell.swift`): a shell puts `ChatShell` in the environment of its chat
  column and `.documentsInspector(shell, store:)` on its split view, outside the chat's navigation stack. Without a
  shell (a phone) `ChatScreen` presents the documents as a sheet and stops watching while it covers the chat.
- **Size and keyboard**: `HardwareKeyboard` (the shortcuts line), `horizontalSizeClass` only under `#if os(iOS)`.
- **Clipboard**: `Clipboard.copy`.
