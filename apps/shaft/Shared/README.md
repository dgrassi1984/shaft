# Shared: the chat both apps draw

The SwiftUI views and view models of the iPhone, iPad and Mac apps, over SHAFTKit's
stores. The session forwards badge confirmations to `AppModel.setBadge`, independently of these views.
Everything here compiles for iOS and macOS; what differs per platform sits behind small seams.

| Folder | What |
|---|---|
| `Design/` | tokens and `Appearance.swift`: per-device theme and reading text size, the Appearance settings section |
| `Root/` | `SessionRoot` (startup error, pairing, or the paired app with the instances store in the environment), `PairingView`, `SettingsView`, `InstancesList` and its rows (an instance, its open threads, From *instance*, New thread), `ChatSelection` |
| `Settings/` | `InstanceSettingsView` (server settings for one instance) and `ServerSection` (API restart and scheduler drain, shared with app settings through `session.store.services`) |
| `Chat/` | `ChatScreen` (header, feed, composer, sheets; the long-poll while visible and active; watching only while uncovered; seen marks while shown, `away`, seen, the new-reply announcement, drops), `ChatScreenModel` (sheets, focus requests, message actions, a send asked for by a menu), `ChatShell` (Documents and Cycles beside the chat), the sheets (Select text, a thumb's reason, earlier chats), `ArchivedChatView` (an earlier chat pushed over the current one) |
| `Feed/` | `FeedView` (pinned to the bottom until scrolled up, Latest and New below in the bar above the composer, Show the N earlier messages), a view per item kind (§4.4) and per row after them (live reply, queued, pending feedback and edits, not sent) |
| `Composer/` | quote cards with their answers, attachment chips, the field, Send or Stop, and a "More ways to send" chevron beside Send once there is text or other draft content (Queue after reply / Next cycle); these choices also stay in + and, on iOS, a long press on Send |
| `Documents/` | the panel (folders, sort, new document, Download… on a document's row: right-click or long-press, a leading swipe on a phone), a document (rendered, threads, overwritten lines, Select text with Reply and Comment, Download…, Delete), `DocumentSaver.swift` (Download saves the document's bytes where the operator picks: the save panel, Files), the editor with the stale-save choice, comments, `CommentHighlight.swift` and `DocumentMarkdown.swift` |
| `Cycles/` | `SidePanel` selects Documents or Cycles with a system segmented picker; `CyclesPanel` shows Next, filtered server days and earlier pages; `CycleDetailView` separates plans, actions, claims, judge verdicts and what remains |
| `Markdown/` | MarkdownUI with SHAFT's theme (tables that scroll, task lists, code with language, Copy and syntax colours), the operator's own text, Markdown as rendered text |
| `Attachments/` | thumbnails and file chips (`FileViews.swift`); `FilePreviewView.swift` presents a Markdown file as a read-only page drawn as a document is, any other in a sheet with `QLPreviewView` on macOS and Quick Look on iOS; files coming in from Photos, a paste or a drop |
| `Platform/` | the text seams: the composer's field and selectable text (`TextViews.swift`, iOS), a message's text (`MessageText.swift`) |
| `Support/` | times, the clipboard, announcements, small view helpers |
| `Voice/` | voice mode: `VoiceScreen` (its model, the iPhone's full-screen cover and the Mac's panel where the composer was) and `VoiceControls`; `VoiceSetup` (this device's voice preferences, permissions, the speech model, the session from the engine's parts, the Debug hooks); `VoicePicker` (a voice per language, with Preview); `VoiceReporter` (this device's voices, for the instance's `propose_voice`). The engine itself is in SHAFTKit's `Voice/` |

## Threads

On a server with topic threads an instance has several open chats, each its own conversation, plus a From *instance*
feed of what it sent from its cycles. The list nests them under the instance (title, replying or waiting, unread), with
New thread; a thread's row has Rename… in its context menu, and the chat's ⋯ has Rename thread… and End. The selection
is a `ChatSelection` (instance, and a `ChatRef` or nil for its default: the most recently active thread), resolved by
`InstancesStore.resolve`; a thread that ended gives way to the default. Each conversation has its own `ChatStore`, draft
and unsent messages (SHAFTKit keys them by thread), so nothing typed in one thread shows in another. The header shows
the thread's title after the name, and New thread (it ends nothing) replaces New chat. The From feed has its own
conversation with Send now, Queue after reply, and Next cycle. Next cycle written in a thread carries the thread's id
(`cid`): the note shows in that thread,
and the answer its cycle writes later comes there too, marked **⏾ from its cycle** beside "sent you a message". An
answer for a thread that had ended by then comes to From with **re: «title»** beside its heading, which opens that
earlier chat. A notification opens the thread it names. A server before threads answers `404` for
`/app/v1/threads/<n>`: that instance keeps its one chat and New chat, as before, and is not asked again this session.

If the initial chat list cannot load, the chat area shows the error, Retry and Settings on all three devices.
The store keeps trying every few seconds while active; Retry restarts that loop immediately. Settings offers Appearance before pairing, plus Unpair
and "Forget the pairing here only" when the server cannot be reached.

In a current chat, "Settings for <name>" is first in ⋯ and also in the title's model/spend popover. It opens a
sheet on every platform; while it is open the chat is covered for read tracking. Archived chats omit both entries.
The form reads values and choices from `InstanceSettingsStore`. Text edits save on submit, focus loss and disappearance.
Successful saves show the server's value; refused saves preserve the draft. Pausing asks first; empty cloud cap
removes the instance override, so the default cap still applies. Loading, load errors and older servers each have an explicit state.

Both settings screens show the Services section (`ServerSection`) using the paired session's shared `ServicesStore`, including in the Mac
Settings scene, which does not inherit the main window's store environment. API restarts ask first and retain the
server's busy refusal; scheduler drains show their waiting list and Cancel. After `drained`, polling waits up to 90 seconds
for a new PID; an unconfirmed result retains tracking for a later refresh. A screen leaving calls `stop()` only
for a scheduler watch and only when no API restart is running, because `stop()` cancels all watchers. If both are
running, both watches continue until completion or another settings screen closes without an API restart running.

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
  `sheetsDocuments`, so documents open in a system sheet instead of the inspector. The chat stops watching while the sheet covers it, but keeps polling while visible and active.
- **Seen** (`ChatScreen.markSeen`): the chat is marked seen while it is watched (its window shows it and nothing covers
  it) and the end of the newest item is on screen, however tall the item is. On the Mac a window shown beside another
  app's window in front counts: what is on screen has been seen. Failed seen requests retry after 2, 4, 8, 16, then 30
  seconds, capped at 30 seconds, using the current feed version; retries cancel when that end leaves the screen, or the
  chat is covered or no longer shown. The Mac gives the chat a `scenePhase` that is active while the window shows it
  (`MacPresence`: visible, not hidden, minimized or fully covered).
- **Menus**: `ChatScreenModel.sendRequest` (a send in a mode, through the composer's own send) and
  `confirmingNewChat` and `confirmingEnd`.
- **Pointer and keyboard on a computer** (macOS only): `HoverActions` (`Feed/FeedItemViews.swift`), Copy and Reply
  after a message's thumbs while the pointer is over it; Return in a card's answer sends, and the keyboard then moves to the
  main field (`Composer/ComposerView.swift`).
- **Size**: the shell's width rules (`horizontalSizeClass` is gone from `ChatScreen`),
  `sheetFrame()` (a sheet's size on the Mac), `iconMenu()` (a composer menu drawn as its icon on the Mac).
- **Clipboard**: `Clipboard.copy`.

## Cycles

The header opens Documents or Cycles in the same inspector on iPad and Mac, or sheet on iPhone. Tapping the open
panel's header button closes it; the other button switches panels. Document links select Documents, including when
Cycles is open. The Mac Documents command uses the same selection behavior.

Cycles creates its store per instance. It refreshes when opened, when the app returns to the foreground, and on
pull-to-refresh, without a timer. Days retain the server's date strings; timestamps use `Stamp`. Queue due text
stays as written, and UTC-midnight due dates display as dates in UTC. Claims are plain text; verdicts use the design
system's pill. Missing, unreadable and loading states have separate lines. Goals, improvement plans, and sleep or
reflection content use `MarkdownText`, the renderer in `Markdown/MarkdownView.swift`.
