# Terminal, API, and web clients

## The terminal client (`shaft talk`)

On a terminal, `shaft talk` opens a client (in `.venv-ui/`: `prompt_toolkit` + `rich`) that talks to the
sandboxed agent over JSON lines (`talk --json`); pipes and `--plain` get line mode.

- **Enter** sends · **Shift-Enter**, **Option-Enter** or **Ctrl-J** new line · multi-line paste · ↑/↓ history
  (kept per instance in `.supervisor/`)
- Markdown rendered live while the answer streams; tool calls as compact lines; a status line with phase,
  elapsed time and speed (waiting for the model then reading my memory % then thinking then answer)
- **Ctrl-C** stops a reply; at the prompt it clears your text, or ends the conversation if empty; Ctrl-D ends
- `/help` · `/thinking` · `/status` · `/version` · `/inbox` · `/clear` · `/exit`
- The banner's second line says which code is running: the supervisor's commit, the instance's code (and
  template changes not merged yet), and the running model server's commit and settings; flagged when it
  runs older code than is checked out (`shaft model on` restarts it). `shaft status` shows the same.
- The instance is held only while it replies: an open chat waiting for you does not block the API or a
  journal write-up. A chat left with nothing typed for `SHAFT_IDLE_MINUTES` (30) closes itself.
- Terminal and API replies get about `TALK_REPLY_CALLS` (6) tool calls or `TALK_REPLY_S` (90) seconds
  before the next model call must answer, offered only `queue_for_next_cycle`. Each knob accepts 0 to disable its budget.
  `plan_reply(calls, seconds, why)` changes this reply's budget from its start and announces the plan.
  Plans above `TALK_REPLY_MAX_CALLS` (20) or `TALK_REPLY_MAX_S` (300) are refused; 0 disables a ceiling.
  Both conversation controls are excluded from the calls count. Work expected to exceed a ceiling is queued up front.
  Requests to take your time mean a brief reply and the full task queued for the next cycle.
  Running calls finish normally. `queue_for_next_cycle(task, why)` writes unfinished work to the
  instance's own inbox, marked `queued from conversation` with the conversation date. The next live
  cycle sees it and is asked to report back. Queuing itself sends nothing to the operator.
- Shift-Enter in iTerm2 needs
  Settings / Profiles / Keys / General / "Report modifiers using CSI u" (Option-Enter always works).

## The API (`shaft api on`)

Each instance is a "model" of an OpenAI-compatible API (`GET /v1/models`, `POST /v1/chat/completions`,
streaming or not), served on `127.0.0.1:8781` and exposed by `tailscale serve` on port 8443 of this Mac's
tailnet name; tailnet only, never Funnel. Chat and model-list requests need the key (`shaft api key`) and a
Tailscale login listed in `SHAFT_API_LOGINS`. The instance answers as itself, with its tools; tool
activity arrives as `reasoning_content`. A conversation keeps one id while its first message stays the
same (or, with an `X-Shaft-Conversation: <name>` header, while it keeps that name), keeps the channel its client names
with `X-Shaft-Channel: web|telegram` (the web UI and the Telegram service send it; `api` without it), and is written
up into the journal
after `SHAFT_IDLE_MINUTES` without a message, counted from when its last reply ended (never while a reply is
still running). `POST /v1/chat/end` requests that write-up now;
its 202 response confirms the request, not a completed journal entry. `POST /v1/chat/interject`
(same body as a chat request) adds the last message to the reply still running in that conversation: the instance
reads it at its next step and its answer covers it; the reply's JSON says how many it heard
(`interjections_heard`), and 409 means no reply is running there ([docs/plans/PLAN-interject.md](../docs/plans/PLAN-interject.md)).
A non-streamed chat request may carry `X-Shaft-Request-Id: <id>` (1-64 letters, digits, - or _): asked again with the
same id, the API gives the answer it gave, without running the reply again (`.supervisor/api-answered.json`, the
newest 500 kept). Telegram sends each message's update id, so a message answered just before the service restarted is
not answered twice.

**Inbox doorbell**: `POST /v1/inbox/<instance>` drops a message in the instance's
inbox, read at its next wake; for a script on this Mac, a phone shortcut, CI on the tailnet. It takes its own
key (`shaft api inbox-key`), which can only deliver messages, never chat with or drive an instance, and needs no
tailnet login. Body: JSON `{"text": "…", "source": "phone", "wake": true}`, or any body (a webhook's JSON is kept
whole); the source can also come from `X-Shaft-Source`, or `X-GitHub-Event` (as `github:<event>`), and
`X-Shaft-Wake: 1` asks for a wake. The message is marked as outside content (information, not the operator's
instructions), logged in `.supervisor/messages.jsonl` (shown in `shaft top`) and `.supervisor/doorbell.jsonl`.
`wake` starts a cycle only if the instance is idle and has had fewer than `SHAFT_INBOX_WAKES_PER_HOUR` (4)
doorbell wakes in the last hour; at most `SHAFT_INBOX_PER_HOUR` (30) messages per instance per hour (429
after). Still tailnet only: GitHub webhooks would need Funnel, which this API never uses; an instance can
poll GitHub itself, or a GitHub Action can join the tailnet.

```bash
curl -s https://<mac>.<tailnet>.ts.net:8443/v1/inbox/example -H "Authorization: Bearer $(shaft api inbox-key 2>/dev/null)" \
  -H "Content-Type: application/json" -d '{"text": "CI on main is red", "source": "ci", "wake": true}'
```
### `/v1` wire contract

Examples use illustrative instance names and values. This API accepts a subset of
OpenAI chat fields. It does not promise the full OpenAI API contract.

| Route | Request | Success |
|---|---|---|
| `GET /v1/models` | Chat bearer key and allowed tailnet login | `200`, `{"object":"list","data":[{"id":"example","object":"model","created":0,"owned_by":"shaft"}]}` |
| `POST /v1/chat/completions` | JSON `model` equal to an instance name; `messages` ending in a user message; optional `stream`, default false | `200` JSON completion, or SSE when streaming |
| `POST /v1/chat/interject` | Same model and conversation identity; `messages` ending in a nonempty user message | `202`, `{"interjected":true,"talk_id":"…"}`; `409 conflict` if no reply is outstanding |
| `POST /v1/chat/end` | JSON `model`; identify the conversation with `X-Shaft-Conversation` or `messages` | `202`, `{"ended":"…"}`; write-up requested in the background |
| `POST /v1/inbox/<instance>` | Inbox bearer key alone, or chat key and allowed login; text or JSON body | `202`, `{"delivered":"…","instance":"example","source":"ci","wake":"…"}` |

Chat `messages` are forwarded to the instance. A message's `content` may be text
or OpenAI content parts. Interject extracts only text parts from its last message.
Client system messages precede SHAFT's delivery instruction; the instance's own
identity and memory still apply. Fields such as `temperature`, `max_tokens`, and
client tool definitions are not forwarded from the top-level request.

| Header | Meaning |
|---|---|
| `Authorization: Bearer <key>` | Chat key, or the narrower inbox key on inbox routes |
| `X-Shaft-Conversation` | Optional conversation name, 1–64 letters, digits, `_` or `-`; otherwise identity comes from the instance and first user message |
| `X-Shaft-Channel` | `api`, `web`, or `telegram`; absent means `api` |
| `X-Shaft-Request-Id` | Optional request id with the same character limits; non-streamed successful responses can be replayed with `replayed:true` |

A non-streamed completion has `id`, `object:"chat.completion"`, `created` in Unix
seconds, `model`, and `choices:[{index:0,finish_reason:"stop",message:{role:"assistant",
content: str,reasoning_content: str}}]`. `usage` contains `prompt_tokens`,
`completion_tokens`, and `total_tokens`, all currently 0. Top-level
`interjections_heard` counts accepted interjections actually heard by the agent.
If the run errors without answer content, the response is `502` with
`{"error":{"message":"…","type":"server_error"}}`. With partial content, the
non-streamed response is still `200`; it does not expose `reply_error`.

A stream uses `Content-Type: text/event-stream`. Each `data:` JSON object has
`id`, `object:"chat.completion.chunk"`, `created`, `model`, and
`choices:[{index:0,delta:{…},finish_reason:null}]`. The first delta sets
`role:"assistant"`; subsequent deltas carry `content` or `reasoning_content`.
The final chunk has an empty delta, `finish_reason:"stop"`, and
`interjections_heard`. On failure it also has
`reply_error:{message: str,tools_ran: bool}`. If no answer content exists, a
preceding content delta contains `[error: …]`. The stream ends with
`data: [DONE]`. After streaming starts, errors stay in the HTTP 200 stream.

End returns `400` without a conversation identity, `404 not_found` when none is
open, and `409 conflict` while a reply is outstanding. Interject returns `400`
for empty text and `500 server_error` if it cannot pass the message to the agent.
Common errors use `{"error":{"message":str,"type":str}}`: `401 authentication_error`,
`403 permission_error`, and `400` or `404 invalid_request_error` for invalid JSON,
headers, message shape, route, or model, as applicable. Request validation is
limited; this is not a complete OpenAI request-schema validator.

Inbox JSON with `text` uses its `source` and `wake` fields; other JSON is stored
whole. The request's `wake` uses Python truthiness, so a nonempty string, even
`"false"`, asks for a wake. Prefer a JSON boolean. `X-Shaft-Wake` accepts `1`,
`true`, or `yes`, case-insensitively. Source precedence is JSON `source`, then
`X-Shaft-Source`, then `github:<X-GitHub-Event>`, then `outside`; it is stripped
and lowercased. The response's `wake` is always a descriptive string, not a boolean,
and delivery can succeed even if waking is refused. Unknown instances return
`404`; invalid JSON or refused delivery returns `400`; exceeding the message
rate limit returns `429 rate_limit_error`.

Source: `supervisor/api.py`, `Handler`, `_chunk`, and `Conversations`.

## The web UI (`shaft web on`)

`shaft top` in a browser, with a chat-first interface on the tailnet. See [web UI design](../docs/plans/PLAN-webui.md)
and [chat design](../docs/plans/PLAN-webui-chat.md). The native chat apps talk to the same service through a JSON API,
`/app/v1` ([below](#the-native-chat-apps-api-appv1)).

- `/` opens the chat with the instance you last talked to. The left rail lists every instance's chat (its state, its
  last line, as text rather than its Markdown, a mark when it answered while you were elsewhere; the tab title counts them), then **Manage**: Overview
  (`/overview`, with the alert count; its alerts show five at a time, each with **Dismiss**, plus **Dismiss these
  alerts**, and when more are open **Show all N alerts** — it stays open across refreshes — and **Dismiss all N**.
  With no instances it says **No instances yet**, that they are created from the terminal with `shaft new <name>`,
  and links the README quickstart), Messages, Trials (the list and a trial's report refresh on their own, so a
  queued trial shows up within a few seconds), Costs, Services (the state and the log both refresh), and the
  supervisor's **Settings** (see
  [configuration reference](configuration.md): the judge pool as ticked models, every knob with its source and default, "saved, not
  live yet" with the guarded restarts; ports and paths shown, set in a terminal). A service action that would kill
  or cut off runs in progress (a scheduler restart, autonomy off, api/model/embed while runs use them) shows as
  **Force …**, asks first naming those runs, and is logged as `web: operator forced … past the guard`. Beside the
  scheduler's *Force restart* is **Restart when idle** (`shaft autonomy restart`: nothing new starts, the runs end,
  then it restarts, cutting none); while that drain is pending the row says what it waits for and offers **Cancel
  restart**.
- **Experiments** (`/experiments`, next to Trials) shows saved results for each instance: Cards (both controls),
  Attention, Commitments, Forecast, Workspace, Monitors, Choice blindness, Resource shock, Privileged prediction,
  Recognition, and Battery perturbation. It defaults to the first instance with saved experiment records; the
  instance links change the selection. Each card shows a description, status, last recorded run, recorded spend,
  escaped report text and a terminal report command. Knob experiments link to their latest trial for each arm.
  There are no paid-run buttons. The page and `/fragment/experiments` require a paired browser like Trials.
  Reports refresh automatically and are cached per instance for five seconds. The page and fragment wait up to
  three seconds total for readers. Unfinished cards show **loading** until polling picks up their results;
  broken readers show an error in their own card. Reads never invoke git or a model; the navigation uses the saved web service version.
  Trial records supply queue/progress/final status and saved failure reasons. Direct-run ledgers often record
  individual results without a run completion marker or worker identity: the page says so rather than guessing
  liveness or an unsaved failure. No records means **not run**; spend without a result means **no result**.
  Spend is cumulative across recorded runs, limited to matching operator trial-ledger rows where available,
  otherwise saved result dollars. Missing timestamps and costs say **not recorded**. Battery perturbation uses
  the latest saved item's probes and recorded protocol/temperature, without mixing eras or reading instance config.
- The navigation footer shows the web service's running version, including `+changes` for edits present at
  start-up. It is also visible in the phone's chat list sheet. After HEAD moves, the next page load shows
  `checked out: …` linking to Services. Services shows the checkout above the live table and a version for
  every service, including the scheduler, alongside the existing older-code warning. Older records use their
  saved commit; a service without a record says `unknown — restart to record`.
- Click the running version in the navigation footer to open **What's new**. The popup shows the 20 newest
  release tag messages, their dates, and which release the web service is running. Above them, up to 15
  first-parent commit subjects since the newest release show merged pull request numbers and branches.
  Release data is cached for at most five seconds. Each opening fetches fresh HTML without browser caching.
  The popup scrolls on a phone and closes with Escape, **Close**, or a tap on the backdrop.
- **Reload / update** in the navigation footer and the chat's ⋯ menu asks the service worker to update,
  deletes only `shaft-*` app caches, then reloads the current page. The update notice uses the same action.
  It keeps saved drafts, the pairing cookie and notifications (the worker is not unregistered: that would drop
  this device's push subscription).
  If cleanup fails or a browser API is missing, it still reloads and shows which step failed.
  Only that notice is stored in sessionStorage, then removed after it is shown. If the browser blocks
  sessionStorage, the notice cannot survive reload.
- Unread marks clear when the current chat's reply, or a message the instance sent you, is on screen in the feed of a
  visible tab (scrolled into view, not only rendered: one below the fold stays unread until you scroll to it). A
  message it sends you marks its chat unread in the rail too. Config, Journal, earlier chats, and a failed feed refresh
  do not consume a new reply. Marks are shared across tabs in the same browser.
  A long chat shows its last 80 turns; **Show the N earlier messages** shows them all (`?all=1`, its refreshes too).
  Opening an instance's chat lands on its last message, earlier chats too, and stays there while what loads after
  the jump (the draft in the composer, the documents sheet, an image, the keyboard closing after Send) grows the feed
  or moves it — until you scroll up yourself (a wheel, a finger, the scrollbar, a key). The page
  carries the version of the chat and documents it drew, so their first refresh is a 304, not the whole chat again.
- The chat: files and images both ways (drop them anywhere on the page, paste or pick; each is a chip you can take
  out), the reply's activity and clock live, interject, Stop, End. Select text in a message or a document and
  **↩ Reply** quotes it: the quote waits in a card above the field (× takes it out), with its own answer field.
  Tap the quoted text to expand or collapse it. Select more and **+ Quote** adds another card to the same message.
  Send puts each Markdown quote before its answer, in order; unanswered quotes still go. The main field becomes
  **Anything else…**: once any quote has an answer it is sent first, before the quotes, so it never reads as the last
  answer; with no answers it goes after them and answers them all. On a computer, a new card focuses its answer; on a phone or tablet the
  keyboard stays down meanwhile, until you tap an answer field (a message's own Reply brings it, to that answer). From the
  documents sheet or full screen, the sheet closes first so the composer shows. Text left
  selected in the chat does not stop it refreshing: the selected message waits until you let go. Each message with
  text has Copy and Reply for its whole Markdown source, your own messages too, under the message: on a computer they
  show on hover or when Tab reaches them, and right-click or the context-menu key opens them as a menu; on a phone or
  tablet a long press opens the menu (Copy, Reply, Select text — a long press no longer selects text by itself). The
  last answer keeps its Copy and Reply in view. Code blocks and documents have Copy icons too (a code block's stays
  at its corner while the code scrolls sideways). Your draft survives a reload; `/` writes,
  Alt-↑/↓ changes chat; opt-in desktop notifications (⋯ menu) for a reply that lands while the tab is hidden.
  Below the quote cards, the composer keeps its main field: the paperclip attaches files, the arrow sends now
  (during a reply: an interjection), and ⌃ beside it — or a long press on the arrow — offers **Queue after reply**
  (only while a reply runs) and **Next cycle**. A tap on the arrow keeps the keyboard up and the text focused, as in Messages.
  Enter sends. Alt-Enter and Shift-Enter insert a new line. Cmd-Enter, or Ctrl-Enter off macOS, queues a message
  until the current reply ends, including a failed or stopped reply. While idle it sends now. Queued messages
  appear below the live reply with Cancel and survive a reload or closed tab. They become one next message,
  joined in order with a blank line between texts and all attachments combined. Quotes stay with their text.
  On a phone or tablet Return is a new line while the on-screen keyboard is up (use Send to send); with a hardware
  keyboard (an iPad's, once it has proved itself: a key with a physical code, or a modifier or arrow — a floating
  on-screen keyboard leaves the page its height and is not taken for one), Return sends as on a computer, after its
  autocorrection, and Shift/Alt-Return is a new line;
  the shortcuts line under the field then shows (on a screen at least 600 px wide).
  **Next cycle** ([docs/plans/PLAN-background-messages](../docs/plans/PLAN-background-messages.md)) is not answered now: the message,
  with its files, goes to the instance's `inbox/` and it takes it up in its next scheduled cycle, as it reads any
  inbox message. Nothing opens or holds a conversation. Its line in the menu, its tooltip and the confirmation say when that is
  (the next wake's time, a cycle running now, no scheduled cycles, or paused). It shows in the chat as yours, marked
  **⏾ next cycle · waiting**, then **read** once a cycle has read it. Its answer, if it sends one, is a message
  like any other (a card with Reply, and a push). Quoting a question answers it, as Send now does. Only the
  button does this: Enter and Cmd-Enter never do.
  `POST /chat/<name>/send` accepts multipart `queue=1` or `background=1`; `POST /chat/<name>/unqueue` cancels a
  queued `mid`.
  Both require the same CSRF token as other web posts.
  Quotes and their individual answers are saved as drafts too. Picking files again adds them to the current
  attachments. Files added during a send stay in the composer. Leaving with attachments asks for confirmation
  because files cannot survive a reload.
  An unchanged retry keeps its message ID, including after reload. A reload, or the next chat refresh, drops the saved
  draft if that ID is already in the chat. A restored attempt that had files says to attach them again (files cannot
  survive a reload). Editing text or changing attachments starts a new message ID. The "sending…" copy of a message
  goes as soon as the chat shows the message itself.
  Chat days are separated by date, using the supervisor’s local time. Today’s messages show only HH:MM;
  hover over a time for its full date and time.
  Markdown task lists use full-width items with a checkbox before the text. On phones, stacked Markdown
  table labels use the heading’s plain text. Markdown headings step down by level and stay at least as large
  as the paragraph under them, with more space above a heading than below it.
- Each panel stays marked stale until its own refresh succeeds. The connection warning stays until every failed
  panel recovers. **↓ New below** appears only for a new turn below the visible area.
  Refreshes keep Manage open or closed, link focus, and activity scroll. Chat thumbnails reserve space;
  images loading keep the feed at the bottom if you have not scrolled up. After Save, focus returns to Edit
  and survives the document refresh.
- While a reply runs, its activity and the answer so far are refreshed on their own (`/fragment/chat/<name>/live`):
  one line says what it is doing now (tap it for every step), dots show before the first word, then the answer
  streams in as it is written. The rest of the chat is answered "not modified" until something else changes, rather
  than sent again whole every 1.5 seconds. The streaming answer is not a live region (VoiceOver would read it again at
  every refresh); the finished reply is announced once. The activity log keeps your place if you scrolled up in it
  and follows its end if you are there; when the reply ends it stays until the answer replaces it, which is fetched
  at once. Rendered
  Markdown and the documents' hashes are kept until their text or file changes.
- The chat header refreshes every 1.5 seconds. It is the name with its state and model under it, then three controls:
  **Documents**, **New chat** and **⋯**. ⋯ holds the instance's pages, Earlier chats, its actions, notifications and
  Reload / update, and last, apart and in red, **Kill**. The icons are one drawn set in the text's colour (no emoji)
  and stay their size when the text size grows. When the instance is busy and not writing a reply, its state under
  the name (with an icon: working, asleep, paused by you, waiting for you) opens a notice on hover, tap or keyboard
  saying what a message does now. The open notice, history list, ⋯ menu and keyboard focus survive refreshes; your
  draft and documents stay in place.
  A failed refresh marks the header stale. New chat (what Clear did) sends the conversation id shown by the header; a
  missing or outdated id is refused with a request to reload. Its conversation stays in Earlier web chats and
  Transcripts.
- **Earlier chats** is in the ⋯ menu. Its quick list shows the latest 20 and the total count;
  **All earlier chats** opens `/i/<name>/chats`, with every saved ended web chat, newest first.
  Each entry shows its date, the first line of the first user message, trimmed to 80 characters, and message count.
  Archive links use `?id=` and support bookmarks and normal browser Back. An archive has an **Earlier chat**
  banner with its date and **Back to the current chat**. Its footer says when it ended, with no invitation to type.
  Beside an archive, Documents says **Current files — not as they were in this chat**. The files are current,
  not snapshots. Editing, commenting, deleting, restoring and creating documents are unavailable there.
  **Open in the current chat to edit or comment** opens the selected file in the current chat. Reply document
  cards in an archive also open the file in the current chat.
- **New chat** ends an open conversation and clears the page. The conversation stays in Earlier chats and
  Transcripts. **End the conversation** leaves messages visible; the next message starts a new conversation.
  The current chat's ended footer says **journal write-up requested**. The web UI has no completion marker.
  **Stop the reply** stops only the running reply and keeps the conversation open.
- Failed replies explain the next action and keep the raw error under **Technical details**. Partial answers remain
  visible and are marked **incomplete**. **Retry** sends the original message again, in one tap, in place: the failed
  try and its question fold into one line ("The first try failed and was sent again") and the instance no longer sees
  them (a repeated tap carries the same message id, so it is not sent twice; a quote waiting in the composer is asked
  about, never sent along). It asks before replacing a different draft. That one-tap Retry is for a refused connection
  before sending, or a known model HTTP 5xx failure with API evidence that no tools ran. Any other failure, older
  records included, links to Activity and Services and still offers **Retry**, which first asks "Actions may already
  have run. Send it again anyway?" and then sends a new message: the failed try stays in view and in the conversation,
  since what it ran may matter. Interjected and attachment-only requests have no one message to send again, so they
  get only the links.
- **Documents**: a Markdown file an instance writes under its `notes/docs/` shows beside the chat, live, as it writes
  it; a reply's documents are cards under it. The drawer shows a vertical list with each entry's size (1.5 KB, exact
  bytes on hover), modified time (14:32 today, 4 Mar 14:32 this year, a date before), changed marks and comment
  counts, or one open document (by default the one this chat last changed). The **Name**, **Size** and **Modified**
  heads sort the list (newest first by default; a head's first click is A→Z, largest or newest, the next reverses
  it); the order is this browser's, kept in a cookie, so a reload, another chat or another tab lists them the same
  way. A document over 256 KB says **too big to show**; opening it offers its download. Subfolders of `notes/docs/`
  are entries of their own (📁, with how many documents they hold, their total size, their latest change, and their
  marks and comments added up), listed first in the same order, then the documents at that level: a folder opens its
  list (`?dir=`, so a reload stays there), the path at
  the top (Documents / ucg / sub) and **←** go back up. **← <folder>** (or **← Documents**) returns from a document
  to its folder's list and asks before dropping unsaved edits; deleting one returns there too. When the chat area is too narrow for both columns, Documents opens as
  a sheet over the chat. Close it with ✕ or Escape; loading a `?doc=` address or clicking a document card, list
  entry or chip opens the panel again.
  **Download** saves the open document's current file from disk, including in earlier chats. Unsaved edits are
  not included.
  Switching documents offers Save, Discard, or Stay before changing the address. If you type during Save,
  the editor keeps the newer text, based on the revision just saved.
  A text file an instance sends you (`send_message` with an attachment: `.md`, `.markdown`, `.txt`, `.csv`, `.json` and
  the other types Files opens as text) opens read-only in the same panel when you click it in the chat, Markdown rendered
  (`?att=<message file>/<index>`): **Download** saves it, **← Documents** returns to the list, ⌘/Ctrl-click downloads
  straight away. Only that instance's own messages preview there, up to 200 KB of UTF-8; anything else says why
  and offers the download. Images keep their thumbnails, and the message page renders a Markdown attachment too.
  Unsaved document drafts stay in this browser per instance and document across reloads, chat navigation,
  and closing the panel. Reopening restores the editor and marks the draft. An older draft says
  "Unsaved draft restored — started from an older version"; Save still checks its original revision and
  shows the existing refusal and diff before you can choose to replace newer content.
  Cancel or explicitly discarding edits removes the draft. Saving clears the saved draft and keeps any
  newer typing. Reloading or leaving warns while the editor has unsaved changes.
  Edit one in the panel, in the field labeled Markdown of that file, or start one: Documents, then New document.
  The name field is labeled Document name (Document name in ucg/ inside a folder): the document goes in the folder
  shown, and a name like `q4/plan` makes the subfolder too (four levels at most). Once created it opens.
  Save is refused if it wrote the file meanwhile (you see what changed and keep your text), and it hears of your
  change as a diff; with your next message, or at once if it is replying. If a later write of its drops lines you
  wrote, the panel says so and offers your version back. The first message of each web chat tells the instance
  this, in a line you do not see.
  The trash icon asks before deleting a document and refuses if its sha changed or the file is gone. The instance
  hears a short deletion notice with your next message, or immediately while replying. A pending edit followed by
  deletion becomes one deletion notice. Comment threads and their copies are kept; old chat links show that the
  document is gone. Its next run commits the deletion with its notes; earlier committed versions remain in git.
- **Comments on a document** ([docs/plans/PLAN-comments](../docs/plans/PLAN-comments.md)), as on a Claude artifact: select text in it
  and press **💬 Comment** (or 💬 in its title bar for the whole document). **Send to <instance>** has three
  choices. **now** (the default): it gets the comment at once (a message of its own, or an interjection while it
  replies) and answers under `### c7`, which lands in the thread. **in its next cycle**: the comment goes to its
  inbox and it answers with a message to you under `### c7`, which lands in the thread too (marked **⏾ for its next
  cycle**). **don't send**: it starts no reply but the instance can read it: leave several, then ask it to review
  them. Each message you send names the open ones waiting, and an answer under `### c7` lands in the thread as well.
  You can edit, delete or send a comment only while it is unsent and unanswered. It opens threads of its own too. The button says
  **Send comment**, **Send for its next cycle** or **Save without sending**, and **Save note** when editing one not
  sent. Feedback says whether delivery succeeded or the comment was saved but not sent, with the reason. A saved,
  unsent comment still has **Send** and **Next cycle** available.
  Comment drafts and their choice stay in this browser after Escape, Cancel, opening another comment, or
  reload. Reopen the same document comment, selection, reply thread, or note edit to recover its draft. Recovery
  never sends it. **Discard** removes the draft; a successful submission clears it, including when saved but not
  sent. The commented text stays highlighted (click it to open its thread); a thread whose
  text left the document says so. **Resolve** folds a thread away, and the instance hears of it with your next
  message. The threads live in the supervisor (`.supervisor/web/comments/`), never in a file it could overwrite; a
  read-only copy in its `notes/comments/` holds every comment of an open thread, sent or not, and of a resolved one
  what was sent.
- Each instance's other pages sit behind the chat's ⋯ menu: **Summary**, **Config** (its **standing orders** first — each in force
  with its Drop, the corrections its messages are still checked against, a field to add one, all through `shaft orders`
  ([architecture](architecture.md)) — then `config.env`, a field per key; each value is named for its key),
  **Drive** (`drive.md` for that instance: the diff is shown in full, then Apply saves it and Back keeps your text; an
  unsaved edit is kept in this browser and restored after a reload, and leaving asks first, as it does for an
  unsaved Config or Settings value),
  **Files** (`/i/<name>/files?path=notes/` lists one folder, folders first, with sizes (1.5 KB; a folder shows —) and modified times.
  Breadcrumbs and Up return to parent folders. The count includes all visible entries; pages show at most 200.
  Folder and page addresses support reload, bookmarks and Back. Every file has Download; text files also have
  Open, served as plain text with the same security policy. `/i/<name>/file?path=notes/example.md` downloads
  the current bytes; `&open=1` opens recognized text types. Empty files download normally; unreadable paths
  show the reason. `secrets/`, `config.env`, `.git/`, `mail/` and `.browser/` are hidden and refused at every
  depth. Links, absolute paths and `..` are refused. Both routes require the tailnet login and paired browser.
  Folder zip downloads are not available),
  **Access** (tools, wake floor and cloud cap, jobs, shared folder,
  mail and calendar read/write grants, Google accounts and re-login, battery, key names),
  **Judge**, **Changes**, **Benchmarks**, **Cycles**,
  **Journal**, **Activity** (connections, plugins, jobs), with its actions and earlier chats.
- **Cycles** keeps `/i/<name>/transcripts`. Next shows the current state, next check, the instance's next action
  and unfinished work, commitments, task queue, and collapsed goals and improvement plan. Next refreshes through
  `/fragment/i/<name>/cycles-next`. Done groups cycles by server date, newest first, three days at a time,
  with Earlier days paging.
  Filters are All, For you, Fell short, Delivered, and Conversations. The instance's claim is plain text;
  the judge's verdict is a separate badge. Day counts count claims separately from judged cycles.
  `?ts=<timestamp>` opens Planned, Did, Result, and Left, with separate second judgments and a raw transcript link.
  Missing duration or cost says "unknown"; an absent judgment says "not judged". Conversations retain the raw
  `/i/<name>/transcript?id=<id>` view.
- **Access Google accounts** lists each alias, address, OAuth client, granted scopes, login date, and missing operator scopes.
  Missing scopes show the exact CLI login command and the client's Google Cloud Data Access setup steps.
  Calendar rows show whether they are writable. **Allow writing** and **Read only** change the stored grant by calendar ID.
  The grant form's **writable** checkbox grants reading, then writing. Writing requires `calendar.events` consent;
  the CLI refuses the write grant with setup and re-login instructions if that scope is missing.
  Writes that tell nobody else happen at once; the rest wait for approval on `/calendar/proposals`.
- **Log in again** on Access starts Google consent in the same paired browser, including a phone on the tailnet.
  Configure a separate OAuth client of type **Web application** in the account client's same Google Cloud project.
  Register exactly `<web.url()>/google/callback`, using the actual HTTPS tailnet URL shown by Access, and set
  `GMAIL_<CLIENT>_WEB_CLIENT_ID` and `GMAIL_<CLIENT>_WEB_CLIENT_SECRET` in `.env`.
  Without those credentials, Access shows the setup steps instead of the button. There is no desktop-client fallback.
  The callback requires the same paired browser and a one-use state that expires after ten minutes; PKCE binds the code exchange.
  It checks the signed-in address before saving and returns to Access with a result. Restarting the web server cancels pending logins.
- **Calendar approvals** (`/calendar/proposals`) lists pending proposals and up to 30 decided proposals, newest proposals first.
  Each shows the instance, change summary, who would be told, and expiry time. Pending proposals have **Approve** and **Decline**
  buttons; decided proposals show their state and outcome. Approval submits the saved request; decline sends no write.
  Each proposal sends a Telegram message through the instance's bot to the paired operator when both are configured,
  linking to this page when an HTTPS web URL is configured. Notification failure leaves the proposal available here.
- **Kill** (last and apart from the other actions: in the ⋯ menu and on Summary; not on the overview's cards) pauses the instance and stops what it runs
  now, as `shaft kill`. **Delete…** is on its Summary page only, enabled once it runs nothing: you type its name to
  confirm (the server checks it again), it runs `shaft delete <name> --yes` with the archive, and the page goes back
  to the overview ([kill and delete](../docs/plans/PLAN-kill-delete.md)).
- Settings and Config saves update only the saved row, including its next save's comparison value. Use the default,
  Unset, Drive saves and removal, and Access switches update their control in place too. Other rows keep unsaved
  edits. Adding a Config key adds its editable row. A save still refuses a value changed by another writer.
  Settings saves do not reload the page; the saved-but-not-live notice refreshes separately.

1. Run `shaft api on` first to configure the API key and tailnet login.
   `shaft web on` installs the service (`com.shaft.web`, `127.0.0.1:8785`) and exposes it with `tailscale serve` on
   port 8444 of this Mac's tailnet name. It needs `SHAFT_API_LOGINS` (`shaft api on` records your login).
2. `shaft web pair --name phone` prints a one-time link, valid for 10 minutes. Open it in the browser you want to use
   and press Pair. `shaft web status` lists the paired browsers; `shaft web unpair <id|name>|--all` revokes them.

**On your iPhone** ([docs/plans/PLAN-pwa.md](../docs/plans/PLAN-pwa.md)): open the URL in Safari, then **Share**, then **Add to Home Screen**. The app keeps its own
cookies, separate from Safari's, so pair it from inside: run `shaft web pair --name phone-app`, copy the link, and
paste it into the app's "not paired" page. Expired links show the same paste box for a new link. Pairing and notice
pages are centred without a sidebar and link back to the app. If pairing is revoked while you use the app, the
next refresh or action opens the pairing page. Your chat draft stays in this browser.
- **Notifications:** in the app, ⋯ then **Notify me on this device** (iOS 16.4+). A reply that ends while SHAFT is closed
  or locked then arrives as "example replied", with the badge, through Web Push; so does a message an instance sends
  you when it is routed to the web ("example asks …", see
  [Where an instance reaches you](integrations.md#where-an-instance-reaches-you-shaft-channel-)). The push carries
  nothing: the app fetches the text over the tailnet, each push its own (queued per device in
  `.supervisor/web/push-queue.json`). Pushes are signed with a key made by `/usr/bin/openssl`, kept in
  `.supervisor/web/`. The button says **Notifications on** only when SHAFT holds this device's subscription (it asks
  `/push/status` on each visit): a refused registration leaves it off and says why. When SHAFT dropped it (a push
  service refused it: 401, 403, 404, 410), holds another endpoint (the browser renewed it) or it was made for another
  key, the visit hands SHAFT the browser's subscription again, made anew for another key; if that is refused the
  button shows **Notifications incomplete — retry**, and a renewal cut short is finished on the next visit. A push
  service's refusal of an endpoint the device has since renewed drops nothing. The service worker also hands SHAFT a
  subscription the browser renews by itself (`pushsubscriptionchange`; Safari does not send it, so on an iPhone the
  visit's check is the one that counts). Unpairing a browser drops its subscription, and only paired browsers
  are pushed. A device that has the chat on screen gets no push for it; the others still do, and a page going to the
  background (the phone locked) or closing says so at once; a refresh sent before that cannot undo it.
  Native iOS and macOS apps use APNs through the same per-device queue and watcher/pairing filters. APNs receives
  only the event title, instance name and badge count; the notification extension fetches the message over the
  tailnet from `/app/v1/push/latest?instance=<n>`, using the payload's instance. The request takes only that chat's
  events and falls back only to that chat's last event, leaving other chats queued. An invalid instance name
  returns `400 bad_request`; omitting the parameter keeps the all-events behaviour. The badge counts instances
  unseen by that device. Tokens record `sandbox`
  or `production`, which selects the APNs host. HTTP 410, `BadDeviceToken`, `Unregistered`, and `DeviceTokenNotForTopic` remove
  the matching registration. `ExpiredProviderToken` and `InvalidProviderToken` keep
  it and clear the cached provider JWT so the next attempt signs a new one. Other
  configuration errors keep the registration and log the configuration. Native push needs the three
  [APNs settings and key file](configuration.md#supervisor-secrets-and-identity), with missing configuration
  reported by `apns.configured()`; the server imports the APNs module directly.
- **Typing:** on a phone, Return makes a new line and Send sends; with a hardware keyboard Return sends. Fields, including the Drive editor, use at least 16 px text.
  The reply's activity, a document's overwritten-lines disclosure, and each judge model have 44 px touch targets.
  Field borders have at least 3:1 contrast against the page and panel backgrounds in both themes.
- **Home Screen:** iPhone and iPad touch icons (152, 167, 180 px) and a launch screen per device, orientation and
  light/dark (`/launch/<w>x<h>-<scheme>.png`, drawn on demand from an allowlist of sizes, served before pairing:
  WebKit fetches them without cookies). **Unpair this browser** is in Settings, and in the phone's ☰ sheet.
- **Layout:** the chat fills the screen. The sidebar icon lists the chats and pages, Documents the documents, ⋯ the
  instance's pages and actions. Every other page has a bar on a phone: **‹ Chat** (back to the chat — the installed
  app has no browser Back), its title, and the sidebar icon; an instance's pages are one strip that scrolls sideways.
  Markdown tables in answers and documents keep their columns and scroll sideways; a page's own tables become
  label/value rows. Messages and notices show at the top, clear of the notch and the composer. Choosing another
  document marks the panel loading until it is there. Chats and Documents sheets move focus inside and keep Tab there. The covered page is inactive.
  Close with ✕ or Escape to return focus to the opener. Choosing a Chats link also closes that sheet.
  Resizing restores normal interaction when a sheet becomes a side panel. Messages and network notices
  appear above the sheets. The documents sheet and the comment box stay above the on-screen keyboard. Sheets and the ⋯ menu leave room for the notch in landscape.
- **Offline:** when SHAFT cannot be reached, the app says so and retries. Opening a page also shows the cached
  offline page if the proxy returns 502, 503 or 504; **Try again** returns to the app.

Every web-service request needs an allowed tailnet login and a host accepted by
`web.allowed_hosts()`. That is the recorded tailnet host, or `SHAFT_WEB_HOSTS` when
set. Protected web pages also require a paired-browser cookie; their POSTs require
same-origin validation and the page's CSRF token. Pairing uses a one-time token and
its POST requires same origin, without an existing pairing or CSRF token. Static
and app-shell assets are available before pairing. Native `/app/v1` routes use
their own bearer rules, with a one-time-token pairing exception and no cookie,
origin, or CSRF check. Responses forbid inline script. Instances cannot reach the port.

Most management actions run matching CLI subcommands, logged as `web: operator …`.
Chat, feedback, and calendar approval decisions call in-process functions.
Config, drive, document and settings edits go through `shaft config` /
`shaft drive` / `shaft docs` / `shaft settings --from-web`. A chat message carries an id, so sending it again after
no answer does not send it twice. Any other action that gets no answer says the outcome is unknown and which page
to check; the page waits at least as long as the server allows that action. Chat
goes through the API (`X-Shaft-Conversation`, `/v1/chat/end`), so restart the API on the same code
(`shaft api on`). A reply keeps going if the browser closes; only Stop stops it. Secrets, `new`, `breed`,
`finetune`, first-time Google logins, instance Google key logins, and `mind cutover` stay in the terminal.
Existing operator Google accounts can log in again from Access.

Sources: [terminal selection](../supervisor/__main__.py),
[terminal client](../supervisor/talk_ui.py),
[API authentication and routes](../supervisor/api.py),
[API installation](../supervisor/api.py),
[web authentication](../supervisor/web.py),
[chat](../supervisor/web_chat.py),
[document editing](../supervisor/docs.py),
[Web Push](../supervisor/webpush.py).
Browser and phone instructions describe the intended client workflow; device behavior
has not been exercised as part of this documentation check.

### Reply feedback (docs/plans/PLAN-feedback.md)

Thumbs up and down beside a reply's Copy/Reply actions, and in its long-press or
right-click menu, keep a rating in the supervisor. They also appear on messages an instance sent you and their
`/messages/<file>` pages, including replies in ended chats. Thumbs up saves immediately. Thumbs down opens
"What was wrong? (optional)" with **Send to <name>**, **Just keep it**, and **Cancel**. Just keep it stores the rating
without sending it. A lit, unsent thumb has a **Send…** link that opens the sheet with its reason filled in;
thumbs up asks "What was good? (optional)". Sending is always an explicit action.

A sent thumbs up goes straight to the instance's inbox for its next cycle, never into a chat. A sent thumbs down
on the instance's currently open web chat waits for your next message in that chat. A removable item in the feed
shows which reply it concerns. Removing it keeps the rating without sending it. Feedback waits through
interjections while the instance is replying; it joins the next ordinary or queued user turn, which shows a
feedback chip. Pending feedback alone cannot send an empty message. Ending or clearing that chat delivers its
pending feedback to the inbox. Thumbs down on ended or other chats, and on message cards or `/messages` pages,
also goes straight to the inbox. Inbox deliveries are logged with `kind: "feedback"`.

The chosen thumb stays lit and its tooltip shows the reason. Once delivered it shows "sent HH:MM". Tap a lit thumb
to clear the rating. Clearing a sent rating records the clear and explains that the message was already delivered.
The instance's note quotes the first 300 characters of its recorded reply and includes the optional reason.
Thumbs down asks it to record a correction if appropriate; thumbs up states that this is information about what
was useful, not a score, and changes no grade.

`POST /feedback/<name>` uses the same pairing, origin and CSRF checks as chat posts. Form fields are `op=rate`,
`clear`, `send`, or `unpend`. Rating takes `kind=chat`, `cid`, `k` (the zero-based assistant turn index), or
`kind=message`, `file`, plus `thumb=up|down` and optional `reason` (at most 2000 characters). Clear, send and unpend
take the current rating's `id`. Send can supply an updated reason, which creates a replacement rating before
sending. Unpend removes feedback from the next chat message while keeping the rating. A successful response's
`X-Feedback-Id` header identifies the current rating, including a replacement. Sending an already pending or
delivered rating returns 409 with an explanation.

The server reads the reply text and timestamp from its own records. Unknown instances return 404, invalid
targets return 409. Ratings, deliveries and clears append to `.supervisor/feedback.jsonl`; the latest rating per
target wins. Feedback changes no grades. Fine-tuning changes belong to milestone F3.

## The native chat apps' API (`/app/v1`)

The iPhone, iPad and Mac apps ([docs/plans/PLAN-native-apps.md](../docs/plans/PLAN-native-apps.md)) use a JSON API on the web service
itself, same port, tailnet only. Its contract, every route, field, status code and example, is
[app-api.md](app-api.md); the JSON the server gives for representative chats is checked in under
`supervisor/tests/app_api_fixtures/`, the only fixture source. It includes pairing, chats, send and feedback
responses and retried turns as well as the chat scenarios. The apps' own tests decode those files, or the folder
set by `SHAFT_APP_API_FIXTURES`.

- **Who may use it.** As for the web UI, every request needs your tailnet login (`SHAFT_API_LOGINS`) and this Mac's
  host accepted by `web.allowed_hosts()`, including a configured `SHAFT_WEB_HOSTS` override. Instead of the cookie it needs `Authorization: Bearer <device>.<secret>`, the same pair a
  paired browser's cookie holds, checked the same way (`devices.json`, the secret's hash). `/app/v1` reads no cookie
  and the web pages accept no bearer, so a web page can never reach the API with a credential the browser adds on
  its own; there is no CSRF token and no CORS.
- **Pairing.** `shaft web pair --name iphone-app` prints the link, as for a browser. Paste it into the app, which posts
  it to `POST /app/v1/pair` and keeps the device's secret in its Keychain. The link still works once, for 10 minutes.
  A paired app is a paired device: `shaft web status` lists it under **native apps**, with its platform and whether
  native push reaches it (and the server's `native push: ready` or what is missing). `shaft web unpair` revokes it,
  as does **Unpair** in the app (`POST /app/v1/unpair`). Unpairing also drops its push token and its seen marks.
- **One chat model.** The JSON feed and the web page's feed are drawn from one function (`web_chat.build`), so the app
  shows what the PWA shows: the same items with the same keys, the 80-message window, the reply running now, what is
  queued, feedback waiting for your next message.
- **Live updates.** `GET /app/v1/chat/<name>?since=…&live=…&wait=25` holds the request until the chat changes (a
  long-poll, rather than the PWA's 1.5 s polling); while only the running reply moves it sends the reply alone. A held
  request for the current chat marks it on screen by default; only `watch=0` suppresses watching. Held polls refresh that mark
  only when `at` is supplied; without it the initial mark expires. No push goes to a watching device for that chat.
- **Sending** goes through the same paths as the web composer: **Send now**, **Queue after reply**, **Next cycle**,
  Retry, quotes (made into the message text on the server, exactly as the web composer writes them), files. Each
  message carries the app's id for it (`mid`): sent again while that chat is open, or after it went for the next
  cycle, it is not sent twice.
- **Unread and the badge.** The PWA keeps its unread marks in the browser. The apps need the server to know, for the
  icon's badge: `.supervisor/web/seen.json` holds, per app and instance, what the app last showed. An instance is
  unread when its own newest answer or message is newer than that; your own messages never make a chat unread.
- **Settings and restarts.** `GET` and `POST /app/v1/settings/<name>` read or save one setting with `expect`;
  `/pause` and `/resume` share the web action. `GET /app/v1/services` shows only API and scheduler status.
  `POST /app/v1/services/api/restart` is guarded against active runs and replies. Scheduler `/restart` drains,
  and `/cancel` withdraws that request. Other services cannot be restarted here. See [the contract](app-api.md#15-instance-settings-and-service-restarts).
- Documents and comments, feedback, attachments and earlier chats are there too, each through the web UI's own
  logic. Push to the apps (APNs) is set up separately ([docs/plans/PLAN-native-apps.md](../docs/plans/PLAN-native-apps.md) §2.2).
- **The iPhone and iPad app** ([apps/shaft/README.md](../apps/shaft/README.md)) is the PWA chat natively: the chat
  full screen with a chat switcher on a phone, the instances beside it on an iPad with the documents as a third
  column; the same feed, composer (quote cards with answers, Send now, Queue after reply, Next cycle, the keyboard
  rules), not-sent messages with Retry, documents with comments, and ratings. A notification tap, or
  `shaft://chat/<instance>`, opens that chat; the chat on screen shows no banner. To try it without the live service,
  `apps/shaft/DevServer/fixture_server.py` serves the real `/app/v1` over throwaway data.
- **The Mac app** (the same codebase, a native macOS app) has one window: the instances in the sidebar (state, last
  line, unread mark, replying), the chat, and its documents as an inspector. A message's text selects in place, across
  paragraphs, tables and code, with **↩ Reply** (**+ Quote** once a card waits) beside the selection and in its
  right-click menu, which also has Copy and Reply for the whole message, Copy code and the thumbs. Enter sends,
  Shift-Enter and Option-Enter insert a new line, Cmd-Enter queues (sends now when idle); `/` focuses the composer;
  files pasted or dropped are attached. The menu bar has New Message (⌘N), New Chat (⇧⌘N), Stop (⌘.), End (⇧⌘E),
  Queue after reply (⌘↩), Next cycle, Documents (⌥⌘D), Earlier chats (⇧⌘Y), Next/Previous chat (⌥↓/⌥↑) and Reload
  (⌘R); Settings (⌘,) shows the server and this device, with Unpair. The chat long-polls while the window shows it and
  is marked seen only while the window is in front; the Dock icon carries the server's badge; a notification click
  brings the window forward (opened again if it was closed: the app keeps running) on that chat.

Sources: [the API](../supervisor/app_api.py), [the chat model](../supervisor/web_chat.py),
[seen marks](../supervisor/web_seen.py), [authentication and pairing](../supervisor/web.py).

## Web route reference

These routes return HTML or files unless stated otherwise. `<n>` is an instance
name. Protected routes use the browser checks above. This table groups route
families from `supervisor/web.py`; `/app/v1` is specified separately.

| Method | Route | Purpose |
|---|---|---|
| GET, POST | `/pair` | One-time browser pairing; available before pairing |
| GET | `/static/<path>`, `/launch/<size>-<scheme>.png`, `/favicon.ico`, `/manifest.webmanifest`, `/sw.js`, `/offline` | Static and app-shell resources before pairing |
| GET | `/` | Redirect to the last chat, first instance, or overview |
| GET | `/overview`, `/i/<n>[/<tab>]`, `/costs`, `/messages`, `/messages/<file>` | Operator pages and instance tabs |
| GET | `/i/<n>/files`, `/i/<n>/file`, `/i/<n>/transcript` | File browser, file download, and transcript detail |
| GET | `/calendar/proposals` | Pending approval queue |
| GET | `/experiments`, `/trials`, `/trials/<n>/<id>` | Experiment and trial pages |
| GET | `/settings`, `/services` | Settings and service status |
| GET | `/fragment/overview`, `/fragment/changelog`, `/fragment/rail`, `/fragment/calendar-proposals` | Incremental page fragments |
| GET | `/fragment/i/<n>/cycles-next`, `/fragment/i/<n>/live` | Instance cycle plan and live state |
| GET | `/fragment/chat/<n>`, `/fragment/chat/<n>/header`, `/fragment/chat/<n>/live`, `/fragment/docs/<n>` | Chat, activity, and document fragments; `/fragment/chat/<n>?away=1&at=<Unix-ms>` clears watching and returns 204 |
| GET | `/fragment/experiments`, `/fragment/trials`, `/fragment/trials/<n>/<id>`, `/fragment/settings`, `/fragment/services`, `/fragment/services/log` | Refreshable status and report fragments |
| GET | `/chatfiles/<cid>/<k>/<j>`, `/files/<file>/<j>` | Chat and message attachments |
| GET | `/google/login`, `/google/callback` | Re-login for existing Google accounts |
| GET | `/push/key`, `/push/status`, `/push/latest`, `/push/token` | Web Push JSON; latest/token additionally require `Sec-Fetch-Site: same-origin` |
| POST | `/push/subscribe`, `/push/unsubscribe`, `/unpair` | Browser notification registration and revocation |
| POST | `/chat/<n>/send`, `/chat/<n>/unqueue`, `/chat/<n>/clear` | Send, remove queued message, or clear chat |
| POST | `/action/chat-stop`, `/action/chat-end`, `/feedback/<n>` | Chat controls and feedback |
| POST | `/docs/<n>/save`, `/docs/<n>/delete`, `/docs/<n>/comment[/edit\|/delete\|/send\|/state]` | Document and comment changes |
| POST | `/preview/drive/<n>`, `/action/<verb>` | Drive diff preview and management actions through `web_actions.run` |
