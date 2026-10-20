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
- No limit on tool rounds in a conversation (Ctrl-C is the brake). Shift-Enter in iTerm2 needs
  Settings / Profiles / Keys / General / "Report modifiers using CSI u" (Option-Enter always works).

## The API (`shaft api on`)

Each instance is a "model" of an OpenAI-compatible API (`GET /v1/models`, `POST /v1/chat/completions`,
streaming or not), served on `127.0.0.1:8781` and exposed by `tailscale serve` on port 8443 of this Mac's
tailnet name; tailnet only, never Funnel. Every request needs the key (`shaft api key`) and a
Tailscale login listed in `SHAFT_API_LOGINS`. The instance answers as itself, with its tools; tool
activity arrives as `reasoning_content`. A conversation keeps one id while its first message stays the
same (or, with an `X-Shaft-Conversation: <name>` header, while it keeps that name), keeps the channel its client names
with `X-Shaft-Channel: web|telegram` (the web UI and the Telegram service send it; `api` without it), and is written
up into the journal
after `SHAFT_IDLE_MINUTES` without a message. `POST /v1/chat/end` requests that write-up now;
its 202 response confirms the request, not a completed journal entry. `POST /v1/chat/interject`
(same body as a chat request) adds the last message to the reply still running in that conversation: the instance
reads it at its next step and its answer covers it; the reply's JSON says how many it heard
(`interjections_heard`), and 409 means no reply is running there ([PLAN-interject.md](plans/PLAN-interject.md)).

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
## The web UI (`shaft web on`)

`shaft top` in a browser, with a chat-first interface on the tailnet. See [web UI design](plans/PLAN-webui.md)
and [chat design](plans/PLAN-webui-chat.md).

- `/` opens the chat with the instance you last talked to. The left rail lists every instance's chat (its state, its
  last line, a mark when it answered while you were elsewhere; the tab title counts them), then **Manage**: Overview
  (`/overview`, with the alert count; its alerts show five at a time, each with **Dismiss**, plus **Dismiss these
  alerts**, and when more are open **Show all N alerts** — it stays open across refreshes — and **Dismiss all N**.
  With no instances it says **No instances yet**, that they are created from the terminal with `shaft new <name>`,
  and links the README quickstart), Messages, Trials (the list and a trial's report refresh on their own, so a
  queued trial shows up within a few seconds), Costs, Services (the state and the log both refresh), and the
  supervisor's **Settings** (see
  [configuration reference](configuration.md): the judge pool as ticked models, every knob with its source and default, "saved, not
  live yet" with the guarded restarts; ports and paths shown, set in a terminal). A service action that would kill
  or cut off runs in progress (a scheduler restart, autonomy off, api/model/embed while runs use them) shows as
  **Force …**, asks first naming those runs, and is logged as `web: operator forced … past the guard`.
- Unread marks clear when the current chat's reply is rendered in a visible tab. Config, Journal, earlier chats,
  and a failed feed refresh do not consume a new reply. Marks are shared across tabs in the same browser.
- The chat: files and images both ways (drop them anywhere on the page, paste or pick; each is a chip you can take
  out), the reply's activity and clock live, interject, Stop, End. Select text in a message and **↩ Reply** quotes
  it in your message. Each message with text has Copy and Reply icons for its whole Markdown source, including
  your own messages. Code blocks and documents have Copy icons too. Your draft survives a reload; `/` writes,
  Alt-↑/↓ changes chat; opt-in desktop notifications (⋯ menu) for a reply that lands while the tab is hidden.
  Quotes are saved as drafts too. Picking files again adds them to the current attachments. Files added during
  a send stay in the composer. Leaving with attachments asks for confirmation because files cannot survive a reload.
  An unchanged retry keeps its message ID, including after reload. A reload drops the saved draft if that ID is
  already in the rendered chat. Editing text or changing attachments starts a new message ID.
  Chat days are separated by date, using the supervisor’s local time. Today’s messages show only HH:MM;
  hover over a time for its full date and time.
  Markdown task lists use full-width items with a checkbox before the text. On phones, stacked Markdown
  table labels use the heading’s plain text.
- Each panel stays marked stale until its own refresh succeeds. The connection warning stays until every failed
  panel recovers. **↓ New below** appears only for a new turn below the visible area.
  Refreshes keep Manage open or closed, link focus, and activity scroll. Chat thumbnails reserve space;
  images loading keep the feed at the bottom if you have not scrolled up. After Save, focus returns to Edit
  and survives the document refresh.
- The chat header refreshes every 1.5 seconds: state, model, today's spend, actions, busy warning, Clear and
  Earlier chats. The open history list and ⋯ menu and keyboard focus survive refreshes; your draft and documents stay in place.
  A failed refresh marks the header stale. Clear sends the conversation id shown by the header; a missing or
  outdated id is refused with a request to reload. Cleared conversations stay in Earlier web chats and Transcripts.
- **Earlier chats** sits in the chat header. Its quick list shows the latest 20 and the total count;
  **All earlier chats** opens `/i/<name>/chats`, with every saved ended web chat, newest first.
  Each entry shows its date, the first line of the first user message, trimmed to 80 characters, and message count.
  Archive links use `?id=` and support bookmarks and normal browser Back. An archive has an **Earlier chat**
  banner with its date and **Back to the current chat**. Its footer says when it ended, with no invitation to type.
  Beside an archive, Documents says **Current files — not as they were in this chat**. The files are current,
  not snapshots. Editing, commenting, deleting, restoring and creating documents are unavailable there.
  **Open in the current chat to edit or comment** opens the selected file in the current chat. Reply document
  cards in an archive also open the file in the current chat.
- **Clear chat** ends an open conversation and clears the page. The conversation stays in Earlier chats and
  Transcripts. **End the conversation** leaves messages visible; the next message starts a new conversation.
  The current chat's ended footer says **journal write-up requested**. The web UI has no completion marker.
  **Stop the reply** stops only the running reply and keeps the conversation open.
- Failed replies explain the next action and keep the raw error under **Technical details**. Partial answers remain
  visible and are marked **incomplete**. **Retry** fills the composer with the original message; you must press Send.
  It asks before replacing a different draft. Retry is offered only for a refused connection before sending, or a
  known model HTTP 5xx failure with API evidence that no tools ran. Older records and uncertain outcomes link to
  Activity and Services. Interjected requests need inspection rather than a one-message retry.
- **Documents**: a Markdown file an instance writes under its `notes/docs/` shows beside the chat, live, as it writes
  it; a reply's documents are cards under it. The drawer shows a vertical list with sizes, changed marks and comment
  counts, or one open document (by default the one this chat last changed). **← Documents** returns to the list
  and asks before dropping unsaved edits. When the chat area is too narrow for both columns, Documents opens as
  a sheet over the chat. Close it with ✕ or Escape.
  Switching documents offers Save, Discard, or Stay before changing the address. If you type during Save,
  the editor keeps the newer text, based on the revision just saved.
  Unsaved document drafts stay in this browser per instance and document across reloads, chat navigation,
  and closing the panel. Reopening restores the editor and marks the draft. An older draft says
  "Unsaved draft restored — started from an older version"; Save still checks its original revision and
  shows the existing refusal and diff before you can choose to replace newer content.
  Cancel or explicitly discarding edits removes the draft. Saving clears the saved draft and keeps any
  newer typing. Reloading or leaving warns while the editor has unsaved changes.
  Edit one in the panel (or start one: Documents, then New document):
  Save is refused if it wrote the file meanwhile (you see what changed and keep your text), and it hears of your
  change as a diff; with your next message, or at once if it is replying. If a later write of its drops lines you
  wrote, the panel says so and offers your version back. The first message of each web chat tells the instance
  this, in a line you do not see.
  The trash icon asks before deleting a document and refuses if its sha changed or the file is gone. The instance
  hears a short deletion notice with your next message, or immediately while replying. A pending edit followed by
  deletion becomes one deletion notice. Comment threads and their copies are kept; old chat links show that the
  document is gone. Its next run commits the deletion with its notes; earlier committed versions remain in git.
- **Comments on a document** ([PLAN-comments](plans/PLAN-comments.md)), as on a Claude artifact: select text in it
  and press **💬 Comment** (or 💬 in its title bar for the whole document). **Send comment to <instance>** is ticked
  by default: it gets the comment at once (a message of its own, or an interjection while it replies) and answers
  under `### c7`, which lands in the thread. Unticked, it is a note only you see, to edit, delete or send later. It
  opens threads of its own too. The button says **Send comment** when ticked, **Save private note** when unticked,
  and **Save note** when editing a private note. Feedback says whether delivery succeeded or the note was saved
  but not sent, with the reason. A saved, unsent note still has **Send** available.
  Comment drafts and their send choice stay in this browser after Escape, Cancel, opening another comment, or
  reload. Reopen the same document comment, selection, reply thread, or note edit to recover its draft. Recovery
  never sends it. **Discard** removes the draft; a successful submission clears it, including when saved but not
  sent. The commented text stays highlighted (click it to open its thread); a thread whose
  text left the document says so. **Resolve** folds a thread away, and the instance hears of it with your next
  message. The threads live in the supervisor (`.supervisor/web/comments/`), never in a file it could overwrite; a
  read-only copy of what it was sent is kept in its `notes/comments/`.
- Each instance's other pages sit behind the chat's ⋯ menu: **Summary**, **Config** (`config.env`, a field per key),
  **Drive** (`drive.md`: the diff is shown in full, then Apply saves it and Back keeps your text), **Access** (tools, wake floor and cloud cap, jobs, shared folder,
  mail and calendar grants, battery, key names), **Judge**, **Changes**, **Benchmarks**, **Transcripts**,
  **Journal**, **Activity** (connections, plugins, jobs), with its actions and earlier chats.
- **Kill** (with the other actions: overview cards, the ⋯ menu, Summary) pauses the instance and stops what it runs
  now, as `shaft kill`. **Delete…** is on its Summary page only, enabled once it runs nothing: you type its name to
  confirm (the server checks it again), it runs `shaft delete <name> --yes` with the archive, and the page goes back
  to the overview ([kill and delete](plans/PLAN-kill-delete.md)).
- Settings and Config saves update only the saved row, including its next save's comparison value. Use the default,
  Unset, Drive saves and removal, and Access switches update their control in place too. Other rows keep unsaved
  edits. Adding a Config key adds its editable row. A save still refuses a value changed by another writer.
  Settings saves do not reload the page; the saved-but-not-live notice refreshes separately.

1. Run `shaft api on` first to configure the API key and tailnet login.
   `shaft web on` installs the service (`com.shaft.web`, `127.0.0.1:8785`) and exposes it with `tailscale serve` on
   port 8444 of this Mac's tailnet name. It needs `SHAFT_API_LOGINS` (`shaft api on` records your login).
2. `shaft web pair --name phone` prints a one-time link, valid for 10 minutes. Open it in the browser you want to use
   and press Pair. `shaft web status` lists the paired browsers; `shaft web unpair <id|name>|--all` revokes them.

**On your iPhone** ([PLAN-pwa.md](plans/PLAN-pwa.md)): open the URL in Safari, then **Share**, then **Add to Home Screen**. The app keeps its own
cookies, separate from Safari's, so pair it from inside: run `shaft web pair --name phone-app`, copy the link, and
paste it into the app's "not paired" page. Expired links show the same paste box for a new link. Pairing and notice
pages are centred without a sidebar and link back to the app. If pairing is revoked while you use the app, the
next refresh or action opens the pairing page. Your chat draft stays in this browser.
- **Notifications:** in the app, ⋯ then **Notify me on this device** (iOS 16.4+). A reply that ends while SHAFT is closed
  or locked then arrives as "example replied", with the badge, through Web Push; so does a message an instance sends
  you when it is routed to the web ("example asks …", see
  [Where an instance reaches you](integrations.md#where-an-instance-reaches-you-shaft-channel)). The push carries
  nothing: the app fetches the text over the tailnet, each push its own (queued per device in
  `.supervisor/web/push-queue.json`). Pushes are signed with a key made by `/usr/bin/openssl`, kept in
  `.supervisor/web/`. The button says **Notifications on** only when SHAFT holds this device's subscription (it asks
  `/push/status` on each visit): a refused registration leaves it off and says why, and a device SHAFT dropped shows
  **Notifications incomplete — retry**.
- **Typing:** on a phone, Return makes a new line and Send sends.
- **Layout:** the chat fills the screen. ☰ lists the chats and pages, 📄 the documents, ⋯ the instance's pages and
  actions. Chats and Documents sheets move focus inside and keep Tab there. The covered page is inactive.
  Close with ✕ or Escape to return focus to the opener. Choosing a Chats link also closes that sheet.
  Resizing restores normal interaction when a sheet becomes a side panel. Messages and network notices
  appear above the sheets. Sheets and the ⋯ menu leave room for the notch in landscape.
- **Offline:** when SHAFT cannot be reached, the app says so and retries. Opening a page also shows the cached
  offline page if the proxy returns 502, 503 or 504; **Try again** returns to the app.

Every request needs your tailnet login **and** a paired browser. Every POST needs the page's own token, and every
response forbids inline script. Instances cannot reach the port. Every action runs the matching `shaft`
subcommand, logged as `web: operator …`; config, drive, document and settings edits go through `shaft config` /
`shaft drive` / `shaft docs` / `shaft settings --from-web`. A chat message carries an id, so sending it again after
no answer does not send it twice. Any other action that gets no answer says the outcome is unknown and which page
to check; the page waits at least as long as the server allows that action. Chat
goes through the API (`X-Shaft-Conversation`, `/v1/chat/end`), so restart the API on the same code
(`shaft api on`). A reply keeps going if the browser closes; only Stop stops it. Secrets, `new`, `breed`,
`finetune`, the Google logins and `mind cutover` stay in the terminal.

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
