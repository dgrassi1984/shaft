# The native apps' API: `/app/v1`

The contract between the SHAFT web service and the native chat apps for iPhone, iPad and Mac
([PLAN-native-apps.md](plans/PLAN-native-apps.md) M0). The server side is `supervisor/app_api.py` (M1). Every field
below is what the server sends; the Swift side (`SHAFTKit`) decodes the fixtures the server's own serialiser writes
(§13.5), never a hand-written copy.

The API shows a chat exactly as the PWA's chat page shows it. One function says what a chat shows
(`web_chat.feed`, §13.1); the HTML feed renders it and this API serialises it, so a fix to one cannot miss the other.
§14 maps every piece of the HTML feed to its JSON field.

## 1. Basics

### 1.1 Where

- The web service (`shaft web on`), on the same port as the web UI: `https://<mac>.<tailnet>.ts.net:8444/app/v1/…`
  through `tailscale serve`. Tailnet only, never Funnel.
- The app learns the base URL from the pairing link (§2): everything before `/pair`.

### 1.2 Auth

Every request passes three checks, in this order:

1. **The tailnet login.** `tailscale serve` adds `Tailscale-User-Login`; it must be in `SHAFT_API_LOGINS`, as for
   the web UI (`web._route`). Refused: `403 login`.
2. **The host.** `Host` must be the recorded tailnet name (`web.allowed_hosts`). Refused: `403 host`.
3. **The device.** `Authorization: Bearer <device>.<secret>`: the same pair a paired browser's cookie holds, checked
   against `.supervisor/web/devices.json` (the secret's SHA-256, compared in constant time). Refused when the header is
   missing or malformed, the device is unknown (never paired, or unpaired since) or the secret is wrong: `401 unpaired`.
   The app then shows its pairing screen. `POST /app/v1/pair` (§2) is the one route without a bearer.

- `/app/v1` reads no cookie, and the web UI's routes accept no bearer. A browser page can never reach the API with an
  ambient credential, so there is no CSRF token and no origin check. No route sends CORS headers.
- `last_seen` in `devices.json` is refreshed as the cookie refreshes it (every 5 minutes at most), so
  `shaft web status` shows the app in use. `shaft web unpair <device>` revokes it; its next request gets `401
  unpaired`.

### 1.3 Requests

- **Bodies:** `POST` bodies are JSON (`Content-Type: application/json`), except `POST /app/v1/chat/<n>/send`, which is
  `multipart/form-data` (files go with it).
- **Size:** a send's body is at most `ATTACH_MAX_TOTAL` + 1 MB (51 MB); every other body at most 1 MB. A larger one is
  refused before it is read: `413 too_large`.
- **Names:** `<n>` is an instance name (`[A-Za-z0-9_-]+`, one of `instance_names()`), `<cid>` a web chat id
  (`web-` + 16 hex). An unknown one, or a chat of another instance: `404 not_found`.
- **Ids made by the app:**
  - `mid`, a message's id: a lowercase UUID, 1–64 of `[A-Za-z0-9_-]`. Sent again (a retry), it is not delivered twice
    while the chat it went into is still open (its last 50 ids), or once it went for the next cycle (the message log).
    A retry after that chat ended is a new message in a new chat, as the web's send is.
  - `pid`, a comment post's id: `p-` + 16 lowercase hex. Sent again, it is not stored twice.

### 1.4 Responses

- JSON, `Content-Type: application/json; charset=utf-8`, `Cache-Control: no-store`, with the web UI's security
  headers. Files (§8) and a document's raw download (§10.3) are the exceptions.
- **Presence:** every field listed is always present. A field with no value is `null` (or `[]`, `""`, `false` where
  the type says so). The only absent fields are those of a partial feed (§4.2), marked there.
- **Status codes:** `200` success; `304` a long-poll or a conditional `GET` with nothing new (empty body); the errors
  below.

### 1.5 Errors

Every error answers `{"error": {"code": str, "message": str}}`. `message` is plain English for the screen, and says
what to do where it can; it is never empty. A few codes carry one or two more fields inside `error`, listed here.

| Status | `code` | When | Extra fields |
|---|---|---|---|
| 400 | `bad_request` | Malformed JSON or multipart, a field missing or of the wrong type, `wait` out of range, a bad `mid` or `pid`, a bad push token, env, topic or platform | — |
| 401 | `unpaired` | No bearer, a malformed one, an unknown device, a wrong secret | — |
| 403 | `login` | The tailnet login is missing or not in `SHAFT_API_LOGINS` | — |
| 403 | `host` | `Host` is not this Mac's tailnet name | — |
| 403 | `pair_expired` | The pairing token is unknown, used, or older than 10 minutes | — |
| 404 | `not_found` | No such route, instance, chat, turn, attachment, document or rating | — |
| 405 | `method` | The route exists, the method does not | — |
| 409 | `refused` | The action was refused: `web_chat.Refused`, `doc_comments.Refused`, `docs.Refused`, a feedback `ValueError`. The message says why ("it is still replying: stop the reply first") | — |
| 409 | `stale` | A document save or delete with a `sha` that is not the document's any more | `sha`: str or null, `diff`: str or null (§10.4) |
| 409 | `too_big` | A document over `docs.MAX_BYTES` (256 KB) asked for as text | — |
| 409 | `stale_version` | `seen` with a version that is no longer the feed's | `version`: str, the current one (§7.5) |
| 413 | `too_large` | The body is over the limit (§1.3) | — |
| 500 | `internal` | Anything else that broke: `"<path> failed: <Type>: <what>"`, also in `supervisor.log` | — |
| 503 | `push_off` | Native push is not configured on the server (`apns.configured()` names what is missing) | — |

```json
{"error": {"code": "refused", "message": "it is still replying: stop the reply first"}}
```

### 1.6 Timestamps

- The supervisor stores local times without an offset (`2026-11-03T14:02:11`). The API sends each with the server's
  UTC offset in effect at that moment, at second precision: `2026-11-03T14:02:11+01:00` (RFC 3339; a July stamp gets
  `+02:00`). `app_api.stamp` does it: `datetime.fromisoformat(ts).astimezone().isoformat(timespec="seconds")`.
- Times stored as epoch seconds (a document's `mtime`) are sent the same way.
- A missing time is `null`, never `""`.
- Chat day separators ("Today", `2026-11-02`) are not sent: the app draws them from the items' times in the device's
  own time zone. Cycles (§15) is an exception: `days[].date` and `next` are server dates in `YYYY-MM-DD` form.
- Cycles keeps raw timestamp ids in `id`, and sends stored `ts` fields as offset timestamps named `at`.
  `plan.now.since` is offset only when it contains a full date and time. A clock string such as `"12:34"` stays as given.
  Commitment `due` and queue `due_at` keep plain dates as `YYYY-MM-DD`; values with a time receive an offset.
  Unparseable queue text goes in `due_at_text`, with `due_at: null`.

### 1.7 Keys

- Every feed item has a `key`, equal to the `data-k` the HTML feed gives the same item (§14). A key never changes
  while its item exists, so a list diffs on it.
- `k` is a turn's index in the chat's `turns` (from 0). It never shifts: the window of §4 drops turns from the front
  without renumbering.

## 2. Pairing

QR pairing is dropped: the app takes the pairing link, pasted. On the Mac the operator copies it from the terminal and
Universal Clipboard carries it to the phone or iPad.

1. On the Mac: `shaft web pair --name iphone-app` prints the link, as for a browser:
   `https://<mac>.<tailnet>.ts.net:8444/pair?t=<token>`. It works once, for 10 minutes.
2. The app takes the pasted link: the base URL is everything before `/pair`.
3. It posts the link (or the bare token) to the API:

### `POST /app/v1/pair`

- **Auth:** the tailnet login and the host (§1.2), no bearer.
- **Request (JSON):**

  | Field | Type | |
  |---|---|---|
  | `link` | str | the whole pasted link; or |
  | `token` | str | the token alone (one of the two is required) |
  | `platform` | str | `"ios"` (iPhone and iPad) or `"macos"` |

- **Response 200:**

  | Field | Type | |
  |---|---|---|
  | `device` | str | the device id (16 hex) |
  | `secret` | str | the secret: kept in the Keychain, sent as `Bearer <device>.<secret>`, never shown |
  | `name` | str | the name given to `shaft web pair --name` |
  | `login` | str | the tailnet login it was paired by |

- **Errors:** `400 bad_request` (neither `link` nor `token`, or a bad `platform`); `403 pair_expired`.
- **Effect:** the device is a paired device in `devices.json` (with `"platform"`), as a browser is: `shaft web status`
  lists it, `shaft web unpair` revokes it.

```json
// POST /app/v1/pair  {"link": "https://mac.tail1234.ts.net:8444/pair?t=Zq3…", "platform": "ios"}
{"device": "8c1f4e2a9b07d3e5", "secret": "kP0v…", "name": "iphone-app", "login": "operator@example.com"}
```

### `POST /app/v1/unpair`

This device unpairs itself (the PWA's "Unpair this browser"). Its APNs token, web subscription and seen marks go with
it. Request `{}`. Response 200 `{"said": "This device is unpaired."}`. The bearer stops working at once.

## 3. The chat list

### `GET /app/v1/instances`

The rail: one row per instance, in the web rail's order (`instance_names()`).

- **Auth:** §1.2.
- **Conditional:** the response carries an `ETag`; with a matching `If-None-Match` the answer is `304`. The app polls
  it every few seconds while on screen.
- **Response 200:**

  | Field | Type | |
  |---|---|---|
  | `instances` | list | the rows below |
  | `badge` | int | instances with something this device has not seen (`web_seen.badge`, §12) |

  Each row:

  | Field | Type | |
  |---|---|---|
  | `name` | str | |
  | `state` | str | what it is doing, as the web header says it: `"idle"`, `"in a cycle"`, `"sleeping"`, `"paused by you"`, `"in a conversation (waiting for you)"`, … `"?"` when unknown |
  | `since` | str | display text for how long (`dashboard.instance_state`), `""` when none |
  | `busy_note` | str or null | what a message does now, when it is busy and not replying ("Your message pauses its cycle; the cycle resumes after the reply."); null when idle or replying |
  | `model` | str | its model, as the header's facts show it |
  | `usd_today` | number | cloud spend today, 0 when none |
  | `line` | str | the rail's last line: `"you: …"` for the operator's own, the instance's first line otherwise; at most 80 characters plus "…" when cut; `""` when none (`web_chat.summary`) |
  | `last` | timestamp or null | when it last answered or sent the operator a message |
  | `replying` | bool | a reply is running in its web chat |
  | `unread` | bool | it has something this device has not seen (§12) |
  | `next_cycle` | str | what Next cycle does now (`autonomy.next_cycle`): the composer's Next cycle hint; `""` when it cannot be read |

```json
{
  "instances": [
    {"name": "thelos", "state": "in a cycle", "since": "14:01", "model": "cloud claude-sonnet-5",
     "busy_note": "Your message pauses its cycle; the cycle resumes after the reply.", "usd_today": 0.42,
     "line": "Merged the ledger fix; the nightly report is in notes/docs/ledger.md.",
     "last": "2026-11-03T14:06:40+01:00", "replying": false, "unread": true,
     "next_cycle": "a cycle is running and read its inbox when it began: this starts the next one once it ends"},
    {"name": "wren", "state": "idle", "since": "", "model": "qwen3.6-35b", "busy_note": null, "usd_today": 0,
     "line": "you: thanks", "last": "2026-11-02T22:10:05+01:00", "replying": false, "unread": false,
     "next_cycle": "for wren's next cycle, which this starts now"}
  ],
  "badge": 1
}
```

## 4. The feed

### `GET /app/v1/chat/<n>` — the current chat

### `GET /app/v1/chat/<n>/<cid>` — an earlier chat

The current chat is the one the web chat page shows (`web_chat.shown`): the open one, else the last one, unless the
operator cleared it (then none: `chat` is null). `<cid>` names any chat of `<n>`; an ended one is read-only
(`archived: true`), the open one reads as the current chat does.

- **Auth:** §1.2.
- **Query:**

  | Param | Type | |
  |---|---|---|
  | `since` | str | the `version` the app has (§4.3) |
  | `live` | str | the `live` revision the app has (`live=` when it is `""`). Send both, from the same response, or neither (`400`) |
  | `wait` | int | 0–25: how long to hold the request for a change (long-poll). Default 0. Out of range: `400` |
  | `all` | `1` | every turn instead of the last 80 (the HTML's "Show the N earlier messages") |
  | `at` | int | the device's clock in Unix ms when it sent the request (watching, below) |
  | `watch` | `0` or `1` | default 1: the chat is on screen. 0 while a sheet covers it (documents on iPhone) |

- **The long-poll:**
  - With no `since`, or `since`/`live` differing from the feed's, it answers at once.
  - Else it holds the request up to `wait` seconds, looking every 0.5 s, and answers within 0.5 s of either revision
    moving. Nothing moved: `304`, empty body. The app asks again at once.
  - The app's request timeout must exceed `wait` (URLSession's default 60 s does).
- **Watching:** a request for the current chat (`/chat/<n>`, not `/<cid>`) with `watch=1` marks this device as having
  that chat on screen (`web_chat.watching(name, device, at)`), and keeps it marked every 2 s while it is held. A watching
  device gets no push for that chat (`web_chat.watchers`, `WATCH_S` 6 s). `watch=0` unmarks it, as the web's covering
  sheet does. `at` orders it against `away` (§7.6): a poll sent before the app went away does not mark it again.
- **Response 200:** a full feed (§4.2), or a partial one when only `live` moved.
- **Errors:** `400 bad_request`, `404 not_found`.

### 4.1 Window

- Without `all=1` the feed holds the chat's last 80 turns (`CHAT_WINDOW`), as the HTML does. `earlier` says how many
  turns are left out; the app offers "Show the N earlier messages", which asks again with `all=1` (and keeps `all=1` on
  its next polls).
- Messages the instance sent (§4.4 `sent`) and Next-cycle messages (`later`) older than the first turn shown are left
  out with them.
- The window changes `items` only: `version`, `live`, `mids` and `last` are computed over the whole chat.

### 4.2 Response

| Field | Type | |
|---|---|---|
| `instance` | str | `<n>` |
| `partial` | bool | true: only `live` moved since the app's `since`/`live`. The fields marked † are then **absent**: keep the ones you have |
| `version` | str | 16 hex: the revision of everything but the running reply's activity and answer (§4.3) |
| `live` | str | 16 hex, or `""` with no reply running: the revision of that activity and answer |
| `reply` | object or null | the reply running now (§4.6); null when none |
| `chat` † | object or null | `{id, state, created, ended}` below; null when no chat is shown |
| `archived` † | bool | a past chat (`<cid>` of an ended chat): read-only, nothing in it acts on the current chat |
| `earlier` † | int | turns left out by the window (§4.1); 0 with `all=1` or a short chat |
| `items` † | list | the conversation in time order (§4.4) |
| `queued` † | list | messages queued for after the reply (§4.7) |
| `pending_feedback` † | list | thumbs down waiting for the operator's next message (§4.8) |
| `pending_edits` † | list of str | documents the operator changed that the instance hears of with the next message (`notes/docs/`-relative paths) |
| `mids` † | list of str | every `mid` this chat holds (sent turns, queued ones, Next-cycle messages over its span): an outbox copy whose `mid` is here has arrived |
| `last` † | timestamp or null | the newest item from the instance (an answer or a message it sent): what unread compares (§12) |
| `can_stop` † | bool | a reply runs and this is not an archived chat |
| `can_end` † | bool | the chat is open, idle and has turns (the web's End button) |
| `can_clear` † | bool | a chat is shown and not archived (New chat; refused while replying) |

`chat`:

| Field | Type | |
|---|---|---|
| `id` | str | `web-…` |
| `state` | str | `"idle"`, `"replying"` or `"ended"` |
| `created` | timestamp | |
| `ended` | timestamp or null | |

With `chat` null (no chat, or cleared), `items` holds only what the instance sent since (as the HTML's empty chat
does), and the app shows "Chat with <n>".

### 4.3 Revisions

- `version` hashes (SHA-256, first 16 hex) the canonical JSON (sorted keys) of the whole feed model without
  `reply.activity` and `reply.answer`, over every turn whatever the window. It moves with anything the feed shows: a
  turn, a sent or Next-cycle message (and its `read`), a rating, a queued message, the reply starting or ending.
- `live` hashes `reply.activity` and `reply.answer`; `""` when no reply runs.
- The HTML's ETags cannot stand in for either: they hash rendered bytes and leave the activity to
  `/fragment/chat/<n>/live`.

### 4.4 Items

Each item has `key` and `type`; items are in time order (by `ts`, ties in the order turns, sent messages, the ended
line, as the HTML sorts them).

**`user`**: a message from the operator (key `u<k>`).

| Field | Type | |
|---|---|---|
| `k` | int | the turn's index |
| `ts` | timestamp | |
| `text` | str | what the operator wrote (`shown`), `""` for a turn that is only a comment or a document edit. Not Markdown: lines starting `> ` are a quote block, a fenced code block (```) is code, the rest is plain text with its line breaks (`web_views._my_text`) |
| `mid` | str or null | its message id |
| `interjected` | bool | sent while a reply ran ("· during its reply") |
| `attachments` | list | files sent with it: `{name, bytes, image, text, url}` (below) |
| `edits` | list of str | documents edited with it ("✎ edited <path>": opens the document) |
| `deleted` | list of str | documents deleted with it ("🗑 deleted <path>": nothing to open) |
| `comments` | list | document comments it carried, `{id, doc}` ("💬 c3": opens that thread) |
| `feedback` | list of str | rating ids of thumbs down that went with it ("👎 feedback") |

**`assistant`**: the instance's answer (key `a<k>`).

Rendering accepts `•` bullets and expands a line of at least three points, `Lead: a · b · c`,
into its lead and a bullet list. A numbered line interrupts a paragraph when it starts at 1
or follows the last numbered item outside code. Written starting numbers are preserved.
Native apps normalize this only for drawing; Copy and Reply keep the source.

| Field | Type | |
|---|---|---|
| `k` | int | |
| `ts` | timestamp | |
| `content` | str | the answer, Markdown (GFM). `""` with no answer: with `failure` null the app shows "(no answer)" |
| `activity` | list of str | what it did during the reply ("what it did (n)"), oldest first, at most 200 |
| `incomplete` | bool | a partial answer that then failed (content and a failure both) |
| `failure` | object or null | why it failed and how to recover (below); null for a reply that ended normally |
| `docs` | list | documents it created or changed: `{path, created}` (`created` false: changed). In an archived chat a card opens the current document ("Open in the current chat to edit or comment") |
| `answered` | list | comment threads it answered: `{id, doc}` ("💬 answered c3") |
| `opened` | list | comment threads it opened: `{id, doc}` ("💬 opened c4") |
| `rating` | object or null | the operator's thumb on it (§4.5) |
| `last` | bool | the chat's last answer (the HTML keeps its Copy in view) |

`failure`:

| Field | Type | |
|---|---|---|
| `kind` | str | `"stopped"`, `"connection"`, `"service"`, `"model"` or `"unknown"` |
| `why` | str | "You stopped this reply." / "The connection to the instance failed." / "The service could not finish the reply." / "The model could not finish the reply." / "The reply failed." |
| `action` | str or null | "No external actions ran: Retry sends it again." or "Actions may already have run. Check Activity and Services before you retry." (or "… before sending again." with no retry); null when stopped |
| `detail` | str | the raw note, for "Technical details" |
| `retry` | object or null | null: no Retry (stopped, nothing to send again, or an archived chat) |
| `retry.text` | str | the message to send again |
| `retry.retry_of` | int or null | `k`: send it with `retry_of` (§6) and the failed try folds into one line. Null: it is sent as a new message, the failed try kept |
| `retry.confirm` | str or null | ask this first ("Actions may already have run. Send it again anyway?"); null: one tap |
| `links` | list | `{label, url}` to check before retrying: `Activity` (`/i/<n>/activity`) and `Services` (`/services`), web UI paths (open them in the browser at the base URL); `[]` when stopped or when the retry is safe |

**`retried`**: a try that failed and was sent again by Retry (key `a<k>`): "The first try failed and was sent again
(14:03)." Fields: `k`, `ts`. The question it answered is not an item.

**`sent`**: a message or question the instance sent the operator (`send_message`) over the chat's span (key
`s<file>`).

| Field | Type | |
|---|---|---|
| `file` | str | its file name in the message log: rating target, `/messages/<file>` in the web UI |
| `ts` | timestamp | |
| `kind` | str | `"message"` or `"question"` |
| `read` | bool | it has left the operator's inbox. A question that is read is answered ("a question (answered)") |
| `text` | str | Markdown |
| `attachments` | list | `{name, bytes, image, text, url}` |
| `rating` | object or null | §4.5 |

The span (`web_chat.sent_during`): from the chat's start, and after it ended too until the operator's next message
starts a new chat; a past chat keeps its own span; with no chat, since the last one was cleared or ended, or, if it
never had one, those still unread.

**`later`**: a message the operator left for the instance's next cycle (Next cycle), shown in the chat though not a
turn of it (key `b<file>`).

| Field | Type | |
|---|---|---|
| `file` | str | |
| `ts` | timestamp | |
| `text` | str | what the operator wrote (as `user.text`) |
| `mid` | str or null | |
| `read` | bool | false: waiting in its inbox ("⏾ next cycle · waiting"); true: a cycle has read it |
| `attachments` | list | `{name, bytes, image, text, url}` |
| `comments` | list | `{id, doc}`: comments it carried |

**`ended`**: the chat ended (key `ended`). Field: `ts`. The current chat says "Ended <time>: journal write-up
requested. Your next message starts a new conversation."; an archived one "This conversation ended on <time>."

**Attachments** (`user`, `sent`, `later`):

| Field | Type | |
|---|---|---|
| `name` | str | its file name |
| `bytes` | int | its size |
| `image` | bool | `.jpg .jpeg .png .gif .webp`: shown as a picture |
| `text` | bool | `.md .txt .csv .json .log .py .yaml .yml .toml .html .xml`: the app can preview it as text (the web previews up to 200 KB) |
| `url` | str | `/app/v1/file/chat/<cid>/<k>/<j>` or `/app/v1/file/message/<file>/<j>` (§8), relative to the base URL |

### 4.5 Ratings

`rating` on `assistant` and `sent` items:

| Field | Type | |
|---|---|---|
| `id` | str | `f-…`: what `clear`, `send` and `unpend` name (§9) |
| `thumb` | str | `"up"` or `"down"` |
| `reason` | str | `""` when none |
| `sent` | object or null | delivered: `{ts, via, message}` (`via` `"chat"` or `"inbox"`; shows "sent HH:MM"). Null: kept, not sent (offer Send…) |
| `pending` | bool | waiting for the next message in this chat (§4.8) |

### 4.6 The running reply

`reply` (null when none):

| Field | Type | Revision | |
|---|---|---|---|
| `started` | timestamp | version | "replying since 14:05", and the clock |
| `activity` | list of str | live | what it has done so far, oldest first; the last line is what it is doing now |
| `answer` | str | live | the answer so far, Markdown, as it streams; `""` before the first word (show the typing dots) |

The reply's Stop is `POST /app/v1/chat/<n>/stop` (§7.2), offered when `can_stop`. When the reply ends, the answer
becomes an `assistant` item and `version` moves.

### 4.7 Queued messages

`queued`, in order, each shown after the running reply ("queued — sent when this reply ends") with Cancel
(`unqueue`, §7.1) unless archived:

| Field | Type | |
|---|---|---|
| `key` | str | `queue-<mid>` |
| `mid` | str | |
| `text` | str | as `user.text` |
| `attachments` | list | `{name, bytes}`: not stored yet, so no URL |

### 4.8 Pending feedback

`pending_feedback`: thumbs down on a reply of this chat that go with the operator's next message ("👎 on its reply of
14:06 goes with your next message", ×: `unpend`, §9). Empty in an archived chat.

| Field | Type | |
|---|---|---|
| `key` | str | `queue-feedback-<id>` |
| `id` | str | the rating id |
| `reply_ts` | timestamp | when the rated reply came |

### 4.9 Examples

A full feed: an idle chat with a quoted answer and a picture, an answer that made a document and was rated, a question
the instance sent, a Next-cycle message, and a failed reply with a safe Retry.

```json
{
  "instance": "thelos",
  "partial": false,
  "version": "9f2c4e1ab07d3c55",
  "live": "",
  "reply": null,
  "chat": {"id": "web-3f9a1c0d7e2b4a65", "state": "idle", "created": "2026-11-03T13:58:40+01:00", "ended": null},
  "archived": false,
  "earlier": 0,
  "items": [
    {"key": "u0", "type": "user", "k": 0, "ts": "2026-11-03T13:58:40+01:00",
     "text": "> Ship the ledger fix today?\n\nYes, once the tests pass. This is the failing run:",
     "mid": "0b8e6c3a-5d1f-4c2e-9a7b-2f4d6e8a1c3b", "interjected": false,
     "attachments": [{"name": "ci-run.png", "bytes": 48213, "image": true, "text": false,
                      "url": "/app/v1/file/chat/web-3f9a1c0d7e2b4a65/0/0"}],
     "edits": [], "deleted": [], "comments": [], "feedback": []},
    {"key": "a1", "type": "assistant", "k": 1, "ts": "2026-11-03T14:01:12+01:00",
     "content": "The failure is the rounding in `net_daily`. Fixed and pushed; the summary is in **ledger.md**.",
     "activity": ["read_file notes/docs/ledger.md", "run tests: 412 passed"], "incomplete": false, "failure": null,
     "docs": [{"path": "ledger.md", "created": false}], "answered": [{"id": "c3", "doc": "ledger.md"}], "opened": [],
     "rating": {"id": "f-6a0c2e4b8d1f3a5c7e9b0d2f", "thumb": "up", "reason": "", "sent": null, "pending": false},
     "last": false},
    {"key": "s20261103-140230-118204-from-thelos.md", "type": "sent", "file": "20261103-140230-118204-from-thelos.md",
     "ts": "2026-11-03T14:02:30+01:00", "kind": "question", "read": false,
     "text": "Should the nightly report keep the per-account lines?", "attachments": [], "rating": null},
    {"key": "b20261103-140301-552019-from-operator.md", "type": "later",
     "file": "20261103-140301-552019-from-operator.md", "ts": "2026-11-03T14:03:01+01:00",
     "text": "Look at the March reconciliation when you have time.", "mid": "5e2d9c1b-7a3f-4e60-8b2d-1c9f0a7e3d54",
     "read": false, "attachments": [], "comments": []},
    {"key": "u2", "type": "user", "k": 2, "ts": "2026-11-03T14:05:50+01:00", "text": "Rerun the report for October.",
     "mid": "a41c7e09-3b2d-4f8e-9c61-0d5b7a2e4f13", "interjected": false, "attachments": [], "edits": [],
     "deleted": [], "comments": [], "feedback": []},
    {"key": "a3", "type": "assistant", "k": 3, "ts": "2026-11-03T14:06:40+01:00", "content": "", "activity": [],
     "incomplete": false,
     "failure": {"kind": "model", "why": "The model could not finish the reply.",
                 "action": "No external actions ran: Retry sends it again.",
                 "detail": "error: CloudError: OpenRouter HTTP 502 (tools_ran=false)",
                 "retry": {"text": "Rerun the report for October.", "retry_of": 3, "confirm": null}, "links": []},
     "docs": [], "answered": [], "opened": [], "rating": null, "last": true}
  ],
  "queued": [],
  "pending_feedback": [],
  "pending_edits": [],
  "mids": ["0b8e6c3a-5d1f-4c2e-9a7b-2f4d6e8a1c3b", "a41c7e09-3b2d-4f8e-9c61-0d5b7a2e4f13",
           "5e2d9c1b-7a3f-4e60-8b2d-1c9f0a7e3d54"],
  "last": "2026-11-03T14:06:40+01:00",
  "can_stop": false,
  "can_end": true,
  "can_clear": true
}
```

The same chat while a reply runs, with a message queued (full):

```json
{
  "instance": "thelos", "partial": false, "version": "41d07be2c95a8f13", "live": "c08e57f2a1b9d364",
  "reply": {"started": "2026-11-03T14:07:02+01:00",
            "activity": ["read_file notes/docs/ledger.md", "run report --month 2026-10"],
            "answer": "October's report is running; so far"},
  "chat": {"id": "web-3f9a1c0d7e2b4a65", "state": "replying", "created": "2026-11-03T13:58:40+01:00", "ended": null},
  "archived": false, "earlier": 0, "items": [/* the items above, but the Retry folded u2 and a3 into one `retried` line (key a3), then its u4 */],
  "queued": [{"key": "queue-9d3e0f2a-6b1c-4a7d-8e5f-3c2b1a0d9e8f", "mid": "9d3e0f2a-6b1c-4a7d-8e5f-3c2b1a0d9e8f",
              "text": "And send me the totals.", "attachments": []}],
  "pending_feedback": [], "pending_edits": [], "mids": [/* the three above, u4's and the queued one's */],
  "last": "2026-11-03T14:06:40+01:00",
  "can_stop": true, "can_end": false, "can_clear": true
}
```

A second later, only the streamed answer moved (`?since=41d07be2c95a8f13&live=c08e57f2a1b9d364&wait=25`):

```json
{"instance": "thelos", "partial": true, "version": "41d07be2c95a8f13", "live": "7b19e4c0d2a65f38",
 "reply": {"started": "2026-11-03T14:07:02+01:00",
           "activity": ["read_file notes/docs/ledger.md", "run report --month 2026-10", "write notes/docs/october.md"],
           "answer": "October's report is running; so far 214 accounts reconciled."}}
```

## 5. Earlier chats

### `GET /app/v1/chats/<n>`

The instance's ended chats, newest first (the web's "Earlier chats", all of them). Open one with
`GET /app/v1/chat/<n>/<cid>`.

| Field | Type | |
|---|---|---|
| `chats` | list | `{id, created, ended, excerpt, messages}` |
| `id` | str | |
| `created` | timestamp | |
| `ended` | timestamp | |
| `excerpt` | str | the first line of the operator's first message, at most 80 characters (and "…"); `""` when none |
| `messages` | int | how many messages the operator sent in it |

```json
{"chats": [{"id": "web-77ab02c4e9d15f30", "created": "2026-11-02T21:40:03+01:00", "ended": "2026-11-02T22:12:44+01:00",
            "excerpt": "What did the October close look like?", "messages": 6}]}
```

## 6. Sending

### `POST /app/v1/chat/<n>/send`

The web's `/chat/<n>/send` path (`web_chat.send`, `queue`, `background`), multipart.

- **Request (multipart/form-data):**

  | Field | Type | |
  |---|---|---|
  | `text` | str | what the operator wrote below the quotes; may be `""` |
  | `mid` | str | required: the message's id (§1.3). The same `mid` again answers as the first send did, without sending twice — also across `now`, `queue` and `background` |
  | `mode` | str | `now` (default): a new reply, or an interjection while one runs ("Send now"). `queue`: after the running reply (sent now when idle). `background`: to the inbox for its next cycle, no reply now |
  | `quotes` | str | JSON list of `{quote, answer, doc}` (↩ Reply cards), `[]` or absent for none. `quote`: the selected text as shown; `answer`: its own answer, may be `""`; `doc`: the `notes/docs/`-relative path when it was quoted from a document, else `""` |
  | `retry_of` | str | `retry.retry_of` of a failed reply (§4.4), with `mode=now` only; the text must be `retry.text` |
  | files | file parts | any part with a filename. At most 10 files, 20 MB each, 50 MB in all |

- **The quotes become the message's text** on the server, exactly as the web's composer writes them (`web.js`
  `quoteText`, `takeQuote`), so both clients send the same text:
  - each quote: `"> (from notes/docs/<doc>)\n"` when it has a `doc`, then each line of `quote.strip()` (split on
    `\n`) as `"> " + line`, trailing spaces trimmed;
  - with its answer: `quote + "\n\n" + answer.strip()`;
  - the message: when any quote has an answer, `text` first, then the quotes; else the quotes first, then `text`; the
    non-empty pieces joined with `"\n\n"`.
- **Response 200:**

  | Field | Type | |
  |---|---|---|
  | `said` | str | what happened, for a status line: "sent to thelos", "thelos reads it at its next step", "queued — sent when this reply ends", "for thelos's next cycle, which this starts now", "already sent to thelos", " · answers its question" appended when it answered a quoted question |
  | `chat` | str or null | the chat it went into (null for `background`) |
  | `file` | str or null | `background`: the inbox file (a `later` item's `file`); else null |

- **Errors:** `400 bad_request` (no `mid`, a bad `mode`, `quotes` not a JSON list of those objects); `409 refused`
  ("an empty message is not sent", "a message is at most 20000 characters (…)", a file over the limits, "the API is
  not reachable …"); `413 too_large`.
- If the chat ended meanwhile, the message starts a new chat, as the web's send does.

```json
// mode=now, text="Rerun it", quotes=[{"quote":"Should the nightly report keep the per-account lines?","answer":"Yes","doc":""}]
{"said": "sent to thelos · answers its question", "chat": "web-3f9a1c0d7e2b4a65", "file": null}
```

## 7. Chat actions

All take JSON and answer `{"said": str}` unless said otherwise; a refused one answers `409 refused` with the reason.
`cid` must be a chat of `<n>` (`404 not_found` otherwise).

### 7.1 `POST /app/v1/chat/<n>/unqueue`

`{"mid": str}`: cancel a queued message. `{"said": "queued message cancelled"}` (also when it had already gone).

### 7.2 `POST /app/v1/chat/<n>/stop`

`{"cid": str}`: stop the running reply; the conversation stays open. `409 refused` "no reply is running".

### 7.3 `POST /app/v1/chat/<n>/end`

`{"cid": str}`: end the conversation now (journal write-up requested). `{"said": "ended: journal write-up requested"}`.
Refused while it replies, or when it is already over.

### 7.4 `POST /app/v1/chat/<n>/clear`

`{"cid": str}`: New chat. An open conversation is ended first; the chat is kept (Earlier chats, Transcripts).
`cid` is the chat the app shows: refused when that is no longer the current one ("that is not the chat on this page any
more: reload it"), or while it replies.

### 7.5 `POST /app/v1/chat/<n>/seen`

`{"version": str}`: this device has shown the current chat up to that version (the app sends it when the newest item's end is
on screen and the app is in the foreground).

- **Response 200:** `{"unread": false, "badge": int}`: the badge to set now.
- **`409 stale_version`**, with the current `version` in `error`, when `version` is not the feed's current one: the
  feed moved since the app drew it, so the app must not mark what it has not shown. Not an error to show: send `seen`
  again once the end of the newest item in the new version is on screen.
- The native client retries failed requests after 2, 4, 8, 16, then 30 seconds, capped at 30 seconds, reading the current
  feed version on each attempt. It cancels retries when the newest item's end leaves the screen, or the chat is covered or no longer in front.

### 7.6 `POST /app/v1/chat/<n>/away`

`{"at": int}` (Unix ms on the device): the chat left the screen (the app went to the background, the phone locked, the
operator opened another chat). Pushes for it reach this device again at once rather than after `WATCH_S`
(`web_chat.unwatch`). A held long-poll sent before `at` stops marking it watching. `{"ok": true}`.

## 8. Files

### `GET /app/v1/file/chat/<cid>/<k>/<j>`

### `GET /app/v1/file/message/<file>/<j>`

A file the operator sent with a turn (`<k>` the turn, `<j>` its index in `attachments`), or one a logged message
carries (`<file>` URL-encoded). Served through `web._serve_attachment`: only paths a message names, inside an
instance's folder or the operator's inbox attachments, read without following a link.

- **Response 200:** the bytes. An image (by its magic bytes: JPEG, PNG, GIF, WebP) as `image/*`,
  `Content-Disposition: inline`; anything else as `application/octet-stream`, `attachment`, with the file's name
  (`filename*=UTF-8''…`).
- **Errors:** `404 not_found` (no such turn, index or message, or a file that is not there any more or not a plain
  file).
- There are no server-side thumbnails: the app downsizes the image itself (`bytes` says what it will fetch).

## 9. Feedback

### `POST /app/v1/feedback/<n>`

The thumbs on an answer or a sent message: the web's `POST /feedback/<n>`, with the same rules
([web-and-api.md](web-and-api.md), "Reply feedback").

- **Request (JSON):**

  | `op` | Other fields | |
  |---|---|---|
  | `rate` | `kind: "chat"`, `cid`, `k`; or `kind: "message"`, `file`. `thumb: "up"\|"down"`, `reason` (optional, at most 2000) | a new rating (it replaces this target's) |
  | `clear` | `id` | remove the rating |
  | `send` | `id`, `reason` (optional: an updated reason makes a replacement rating first) | deliver it: thumbs down on a reply of the open chat waits for the next message; everything else goes to the inbox |
  | `unpend` | `id` | keep it but take it off the next message |

- **Response 200:** `{"said": str, "id": str or null}`: `id` is the current rating (a replacement after `send` with a
  new reason), null after `clear`.
- **Errors:** `400 bad_request`; `409 refused` ("That rating is no longer current for this instance.", "That feedback
  was already delivered.", "That feedback is already waiting for your next message.", "Only an assistant reply can be
  rated.").

```json
// {"op": "send", "id": "f-6a0c2e4b8d1f3a5c7e9b0d2f", "reason": "Exactly the summary I wanted"}
{"said": "Feedback delivered to its inbox.", "id": "f-6a0c2e4b8d1f3a5c7e9b0d2f"}
```

## 10. Documents and comments

The instance's `notes/docs/` (the web's documents panel). Paths are relative to `notes/docs/`, with `/` between
folders and ending `.md`; they go in the query (`?path=`), URL-encoded.

### 10.1 `GET /app/v1/docs/<n>[?cid=<cid>]`

Every document, flat; the app groups them into folders, sorts and sizes them (the web's `docs_panel`).

| Field | Type | |
|---|---|---|
| `dir` | str | `"notes/docs"` |
| `max_bytes` | int | 262144: a larger document can only be downloaded (§10.3) |
| `docs` | list | `{path, bytes, mtime, sha, too_big, open_comments, in_chat}` |
| `path` | str | |
| `bytes` | int | |
| `mtime` | timestamp | |
| `sha` | str | its content's SHA-256 (hex); `"too-big"` when `too_big` |
| `too_big` | bool | over `max_bytes` |
| `open_comments` | int | open comment threads on it ("💬 2") |
| `in_chat` | bool | a reply of the chat (`cid`, else the current one) created or changed it (the ● mark) |

`500 internal` "documents not readable: …" when the folder cannot be read.

### 10.2 `GET /app/v1/docs/<n>/doc?path=<p>[&cid=<cid>]`

One document, with its comment threads.

| Field | Type | |
|---|---|---|
| `path` | str | |
| `text` | str | its Markdown |
| `sha` | str | what a save or delete must send back |
| `in_chat` | bool | as in the list |
| `threads` | list | open threads first, then resolved, each in creation order (below) |
| `overwritten` | object or null | lines the operator wrote that a later write of the instance dropped (the web's warning), null when none: `{lines, ts, diff, text}` — how many lines, when they saved, the unified diff of their version against now, and their whole version (Restore yours = save `text` with the current `sha`) |

A thread:

| Field | Type | |
|---|---|---|
| `id` | str | `c<n>` |
| `doc` | str | |
| `by` | str | `"operator"` or the instance's name |
| `state` | str | `"open"` or `"resolved"` |
| `created` | timestamp | |
| `anchor` | object or null | null: on the whole document. Else `{exact, prefix, suffix, start}`: the text it is on, up to 32 characters either side, and its offset — all in the document's **rendered** text (the web's `comments.js`: the visible text of the rendered Markdown, offsets in UTF-16 code units). Where `exact` is no longer found, the app says "text no longer in the document" |
| `doc_changed` | bool | the document changed since the comment ("the document changed since this comment") |
| `posts` | list | below, oldest first |
| `unsectioned` | list of str | chat turns that answered it without a `### c<n>` section, as `"<cid>#<k>"` ("answered in the chat, without a section →": opens that turn, key `a<k>`) |

A post:

| Field | Type | |
|---|---|---|
| `pid` | str | |
| `by` | str | `"operator"` or the instance's name |
| `text` | str | Markdown |
| `ts` | timestamp | |
| `unsent` | bool | not delivered yet (`doc_comments.delivered` false): only such a post can be edited, deleted or sent ("not sent") |
| `sent` | str or null | how it was sent: `"<cid>#<k>"` (a chat turn), `"inbox/<file>"` ("⏾ for its next cycle", file = a message file), `"message:<file>"` ("in a message") |
| `turn` | str or null | the instance's own post: the chat turn or message that wrote it, same forms |
| `seen` | str or null | answered from the copy without being sent: the turn that answered it |

- **Errors:** `404 not_found` (not there any more); `409 too_big` (download it: §10.3); `409 refused` (a path that
  is not a document's).

```json
{
  "path": "ledger.md", "text": "# Ledger\n\nNet daily totals are rounded once, at the end.\n",
  "sha": "3b9f0c…e41a", "in_chat": true, "overwritten": null,
  "threads": [
    {"id": "c3", "doc": "ledger.md", "by": "operator", "state": "open", "created": "2026-11-03T13:50:12+01:00",
     "anchor": {"exact": "rounded once, at the end", "prefix": "Net daily totals are ", "suffix": ".", "start": 28},
     "doc_changed": true, "unsectioned": [],
     "posts": [
       {"pid": "p-1e2d3c4b5a697887", "by": "operator", "text": "Per account or overall?", "ts": "2026-11-03T13:50:12+01:00",
        "unsent": false, "sent": null, "turn": null, "seen": "web-3f9a1c0d7e2b4a65#1"},
       {"pid": "p-9a8b7c6d5e4f3021", "by": "thelos", "text": "Per account, then summed.",
        "ts": "2026-11-03T14:01:12+01:00", "unsent": false, "sent": null, "turn": "web-3f9a1c0d7e2b4a65#1", "seen": null}]}
  ]
}
```

### 10.3 `GET /app/v1/docs/<n>/raw?path=<p>`

The document's bytes as a download (`text/markdown; charset=utf-8`, `attachment`), whatever its size: the way to a
`too_big` one. `404 not_found` when it is not there.

### 10.4 `POST /app/v1/docs/<n>/save`

Edit a document, or create one (`web._save_doc`: `shaft docs set` with its compare-and-set). The instance hears of it
at once while it replies, else with the operator's next message.

- **Request:** `{"path": str, "text": str, "sha": str, "dir": str}`. `sha`: the one the app read; `"none"` to create.
  `dir` (optional): a folder the new document goes in. `.md` is added when missing.
- **Response 200:** `{"said": str, "path": str, "sha": str}`: the path as stored and the new sha ("saved · thelos gets
  the change with your next message").
- **`409 stale`**: the document changed since the app read it (or, creating, it exists). `error.sha` is what it has now
  and `error.diff` what it has that the app's text has not (`yours` → `now`); both null when creating. Save again with
  `error.sha` to replace it with the app's text knowingly, or take its version.
- **`409 refused`**: a bad path or folder, too large, the CLI refused it.

### 10.5 `POST /app/v1/docs/<n>/delete`

`{"path": str, "sha": str}` → `{"said": str}` ("deleted · …"). `409 stale` when the sha is not current; `409 refused`
when it is not there any more.

### 10.6 Comments

Each answers `{"said": str, "thread": str}` (the thread id), or `409 refused` with the reason. After any of them the
app reads the document again (§10.2).

| Route | Request | |
|---|---|---|
| `POST /app/v1/docs/<n>/comment` | `{pid, doc, anchor, sha, text, send}` | a new thread on `doc` (`anchor` null: the whole document; `sha`: the document's, as read) |
| `POST /app/v1/docs/<n>/comment` | `{pid, thread, text, send}` | a reply in `thread` |
| `POST /app/v1/docs/<n>/comment/edit` | `{pid, text}` | edit an unsent post |
| `POST /app/v1/docs/<n>/comment/delete` | `{pid}` | delete an unsent post (its thread goes with its last post) |
| `POST /app/v1/docs/<n>/comment/send` | `{pid, when}` | send a stored post: `when` `"now"` (a chat message: "Send to <n>") or `"cycle"` (its inbox, for its next cycle) |
| `POST /app/v1/docs/<n>/comment/state` | `{thread, state}` | `"resolved"` or `"open"` (Resolve / Reopen) |

- `send` on a new post: `""` keeps it unsent (the instance sees it in `notes/comments/`; "kept, not sent: …"), `"now"`
  or `"cycle"` sends it at once. A send that fails after the post is stored answers 200 with "saved, but not sent:
  …".
- `pid` is made by the app (§1.3); posting the same `pid` again returns the thread that already holds it.
- `text` at most 20000 characters; `anchor.exact` at most 2000, `prefix`/`suffix` at most 32, `start` ≥ 0.

## 11. Push

Native push goes through APNs (`supervisor/apns.py`, PLAN §2.2). The notification carries only the title ("thelos
replied", "thelos's reply failed", "thelos asks"), the badge and the instance; the app's notification extension fetches
the body from `/push/latest`, over the tailnet.

### 11.1 `POST /app/v1/push/register`

This device's APNs token (the app sends it at every launch: APNs may change it).
Retries stop when the device unpairs, unregisters or replaces its token. `BadDeviceToken`, `Unregistered`,
`DeviceTokenNotForTopic` and HTTP 410 remove only the matching registration. `ExpiredProviderToken` and
`InvalidProviderToken` clear the cached provider JWT for the next attempt, keeping the device token.
Held-message digests reach APNs-only operators too, when APNs is configured. While native push is off, stored APNs
registrations do not count as recipients; digests with no Web Push recipients remain eligible once APNs is configured.

- **Request:** `{"token": str, "env": str, "topic": str, "platform": str}`: the token as hex, `env` `"sandbox"` (Xcode
  builds) or `"production"`, `topic` the bundle id, `platform` `"ios"` or `"macos"`.
- **Response 200:** `{"push": {"on": bool, "env": str or null, "reason": str}}` (`apns.status`).
- **Errors:** `503 push_off` when `apns.configured()` names what is missing (key id, team, topic, key path: the
  message says which); `400 bad_request` when `apns.register` refuses it (a token that is not hex, another env, a topic
  that is not `SHAFT_APNS_TOPIC`, another platform).

### 11.2 `DELETE /app/v1/push/register`

Stop native push to this device. `{"removed": bool}`.

### 11.3 `GET /app/v1/push/status`

`{"on": bool, "env": str or null, "reason": str, "problem": str or null}`: `apns.status(device)`, and `problem` from
`apns.configured()` (null when the server is ready).

### 11.4 `GET /app/v1/push/latest`

What the notification shows: the events queued for this device (`webpush.take`), oldest first, taken off the queue.
With nothing queued, the last one shown again; with nothing ever, a plain one.

The notification extension sends `?instance=<n>` from the push payload. This takes only events whose URL is
`/i/<n>/chat`, leaving other instances queued. With none queued for that instance, only its own last event is
shown again, or the plain one if it has none. `instance` must be a valid instance name (a lowercase letter followed
by at most 30 lowercase letters, digits or hyphens); otherwise `400 bad_request`. Without it, all queued events
are taken as before.

| Field | Type | |
|---|---|---|
| `events` | list | `{title, body, url, tag, instance}` |
| `title` | str | "thelos replied", "thelos's reply failed", "thelos asks", "thelos says" |
| `body` | str | its first line, at most 160 characters |
| `url` | str | the web path it opens (`/i/thelos/chat`) |
| `tag` | str | replaces an earlier notification with the same tag |
| `instance` | str or null | the chat to open: parsed from `url` (`/i/<n>/chat`); null for the plain one |
| `badge` | int | as §12 |

```json
{"events": [{"title": "thelos replied", "body": "October's report is ready: 214 accounts reconciled.",
             "url": "/i/thelos/chat", "tag": "thelos", "instance": "thelos"}], "badge": 1}
```

It does not mark the device as watching, and it is a `GET` with an effect (it takes the events): only the bearer
reaches it, so no other page can spend them.

## 12. Unread and the badge

- The PWA keeps its unread marks in the browser; the apps need the server to know, for the badge. The PWA keeps its own.
- `.supervisor/web/seen.json` holds, per device and instance, the version it last marked seen and the instance's `last`
  stamp and count of instance items at that stamp at that version.
- An instance is **unread** on a device when the instance's `last` (`web_chat.summary(n)["last"]`: its newest answer or
  message to the operator) is newer than the stamp the device marked, or the same stamp has more instance items.
  New answers and messages in the same second therefore make it unread again. Old marks without the count keep
  their timestamp-only comparison until the next seen. The operator's own messages never make it unread.
- An instance the device never marked counts from the device's pairing time: what came before it is not unread.
- The badge is the number of unread instances (`web_seen.badge(device)`), sent with `/instances`, `seen`,
  `/push/latest` and every APNs push.
- The native apps mark a chat seen when the newest item's end is on screen and the app is in front, retry failures
  (§7.5), and discard list answers from polls that began before a successful seen request.
- A device that unpairs loses its marks.

## 13. The Python side (server lane, M1)

### 13.1 `web_chat.feed(name: str, cid: str | None = None, whole: bool = False) -> dict`

The one model of what a chat shows. `cid` None: the current chat (`shown(name)`); else that chat of `name` (`None`
returned when it is not one: the routes answer 404). `whole`: every turn (`?all=1`).

- It loads the chat and its span's messages (`sent_during`) and calls `web_chat.build`.
- `web_chat.build(name: str, chat: dict | None, sent: list[dict], archived: bool = False, whole: bool = False) -> dict`
  is the pure part, which the tests and `web_views.chat_feed` call with a chat they hold.
- The dict is the JSON of §4.2 (full), with two differences that the serialiser removes:
  - times are the stored local ISO strings (or epoch floats), not yet offset;
  - an attachment has `path` (`"chat/<cid>/<k>/<j>"` or `"message/<file>/<j>"`) instead of `url`. The HTML maps it to
    `/chatfiles/<cid>/<k>/<j>` and `/files/<file>/<j>`; the JSON to `/app/v1/file/<path>` (the file URL-encoded).
- `version` and `live` are computed in `build`, over the whole chat, before the window.
- Internal `last_count` counts instance items at `last` over the whole feed; `summary` counts the same span.
  The serialiser removes it, and it does not enter the revisions.
- `last` and `web_chat.summary(name)["last"]` come from one helper (`web_chat.last_from(chat, sent)`), so unread never
  drifts from the rail; a test pins them equal.
- `web._sent_during` moves to `web_chat.sent_during(name, chat, archived=False)`; `web._sent_during` stays as the web
  routes' one-line caller of it (the web tests patch it there).

### 13.2 The HTML views on the model

- `web_views.chat_feed(name, chat, sent, archived=False, whole=False)` keeps its signature (the existing web tests stay
  unchanged) and renders `web_chat.build(...)` (`web_views.feed_html(model)`): every `data-k` it writes is an item's
  `key`. Its HTML is byte-identical to what it was before the model.
- `web_views.live_activity(chat)` renders the model's `reply` (and, once it ended, the last answer's `activity`).
- `_reply_failure`'s sentences and choices move into the model (`failure`, §4.4); the HTML renders them.
- `_overwritten` splits into `web_views.overwritten(saved, sha, text) -> dict | None` (the §10.2 object) and its HTML.
- `chat_header`'s "what a message does now" becomes `web_views.busy_note(name, state) -> str | None`, used by the header
  and `/app/v1/instances`.
- The HTML ETags stay as they are.

### 13.3 `supervisor/app_api.py`

- `route(req: web.Request) -> web.Response`: the entry point. `web._route` calls it for every path under `/app/v1/`
  after the login and host checks and before the cookie check; it does its own bearer check (§1.2) and answers every
  path under `/app/v1/` (an unknown one: `404 not_found`). It catches everything and answers `500 internal` with the
  error, logged, never an empty body.
- `device(req) -> str | None`: the paired device of the bearer (refreshing `last_seen`).
- `stamp(ts: str | float | None) -> str | None` (§1.6) and `to_json(model: dict) -> dict` (stamps and URLs, §13.1).
- `error(status: int, code: str, message: str, **extra) -> web.Response`.
- The long-poll (§4): a loop on `web_chat.feed` every 0.5 s up to `wait`, refreshing `web_chat.watching` every 2 s only when `at` is supplied;
  without `at`, only the initial watching mark is made, so a later `/away` stays effective;
  each held request is one thread of the web server's `ThreadingHTTPServer`.
- It calls the same functions the web routes do (`web_chat.send/queue/background/unqueue/stop/end/clear`,
  `web._serve_attachment`, `doc_comments`, `apns`, `webpush.take`, `web_seen`). Where a web route builds its answer as
  HTML inline, the logic moves into a function both call:
  - `web.feedback_action(name, login, form) -> tuple[str, dict | None]` (said, the current rating; `ValueError` when
    refused), from `web._post`'s `/feedback/<n>` branch;
  - `web.save_doc` / `web.delete_doc` / `web.comment_action`, returning data and raising `DocStale(sha, diff)` or the
    module's `Refused`, from `_save_doc`, `_delete_doc`, `_comment`.
- `web.Handler._serve` allows a send's body size on `/app/v1/chat/<n>/send` too, and answers its 413 as JSON there.
  The server takes `DELETE` (`do_DELETE`).
- `web.paired_device(dev, secret) -> bool` is the one device check: the cookie's (`web._paired`) and the bearer's.
- The document functions raise `web.DocStale(message, sha, diff, changed)` (a save or delete refused: `changed` when
  the document is not what the app read, else the CLI refused the text) and `web.DocRefused` (the CLI refused a
  deletion); `docs.Refused` for a bad path or folder. `docs.raw(name, path)` reads a document whatever its size.
- The push routes import `supervisor/apns.py` directly; `apns.configured()` reports missing settings or a key file.
- `web.unpair` also calls `apns.unregister(device)` and `web_seen.forget(device)`.
- Pairing reuses `web._use_pairing` and `web._token_from_link`; the device record gains `"platform"`.

### 13.4 `supervisor/web_seen.py`

- `seen(device: str, name: str, version: str) -> bool`: True when `version` is the current feed's (`web_chat.feed(name)`);
  then it records `{version, last, last_count, at}` for the device and instance. False otherwise, recording nothing (the route
  answers `409 stale_version`).
- `unread(device: str, summaries: dict | None = None) -> dict[str, bool]`: per instance (§12); `summaries` (each
  instance's `web_chat.summary`) when the caller has them already (`/app/v1/instances`).
- `badge(device: str) -> int`: how many are unread. The APNs lane calls it for each push.
- `forget(device: str) -> None`: drops the device's marks.
- The store `.supervisor/web/seen.json`, written atomically (mode 600) under a lock, as `web._write` does.

### 13.5 Tests and fixtures

- `supervisor/tests/test_app_api.py` drives `web.respond` with requests built in a temporary `SUP_DIR`, never the real
  `.supervisor/`:
  - auth: no bearer, a wrong secret, an unpaired (stale) device, a wrong login, a wrong host — each refused with its
    code;
  - the HTML and the JSON of the same chat have the same keys in the same order;
  - the long-poll answers on a version move, on a live move (partial), and `304` after `wait`;
  - `seen` with a stale version is refused; unread follows `summary`'s `last`.
- The same test writes the fixtures the Swift Kit decodes, from the API's own answers, to
  `supervisor/tests/app_api_fixtures/<name>.json`, and fails when a checked-in one differs (`SHAFT_UPDATE_FIXTURES=1`
  rewrites them): `idle`, `live`, `live-partial`, `queued`, `quotes`, `attachments`, `sent`, `feedback`, `failed`,
  `earlier`, `ended`, `instances`, `docs`, `doc-comments`, `push-latest`, `errors`, `pair`, `chats`, `send`,
  `feedback-response`, `retried`, `plan`, `cycles`, `cycle`. SHAFTKit reads only this folder, or
  `SHAFT_APP_API_FIXTURES` when set; it has no copied contract fixtures. Their times are stamped in
  `Europe/Rome` (the test sets `TZ`), so they carry `+01:00`. `errors` maps each code to `{status, body}`.

## 14. The HTML feed, piece by piece

Every piece `web_views.chat_feed` and `live_activity` draw, and where it is in the JSON.

| HTML (`chat_feed`) | `data-k` | JSON |
|---|---|---|
| Empty chat ("Chat with <n>") | `empty` | `chat` null |
| "Show the N earlier messages" | `more` | `earlier` > 0 |
| The operator's turn: text with quotes and code, time | `u<k>` | `items[]` `user`: `text`, `ts` |
| "· during its reply" | | `user.interjected` |
| "✎ edited <path>" / "🗑 deleted <path>" | | `user.edits` / `user.deleted` |
| "💬 c3" (comments sent) | | `user.comments` |
| "👎 feedback" | | `user.feedback` |
| Its attachments (picture or 📎 file) | | `user.attachments` |
| `data-mid` | | `user.mid` |
| The answer: Markdown, time, "incomplete" | `a<k>` | `assistant`: `content`, `ts`, `incomplete` |
| "(no answer)" | | `content` `""` and `failure` null |
| "what it did (n)" | | `assistant.activity` |
| Document cards ("📄 ledger.md — changed") | | `assistant.docs` |
| "💬 answered c3" / "💬 opened c4" | | `assistant.answered` / `assistant.opened` |
| Failure note, Retry, Activity · Services, Technical details | | `assistant.failure` |
| Thumbs, their reason, "sent HH:MM", Send… | | `assistant.rating`, `sent.rating` |
| Copy kept in view on the last answer | | `assistant.last` |
| "The first try failed and was sent again" | `a<k>` | `retried` |
| "<n> sent you a message / a question (answered)" | `s<file>` | `sent`: `kind`, `read`, `text`, `attachments` |
| "you … ⏾ next cycle · waiting/read" | `b<file>` | `later`: `read`, `text`, `attachments`, `comments`, `mid` |
| "Ended …" / "This conversation ended on …" | `ended` | `ended` (+ `archived`) |
| Day separators | `day-<date>` | none: drawn by the app from `ts` |
| "replying since 14:05" and the clock | `live` | `reply.started` |
| The live activity, "· n steps", what it does now | `act` | `reply.activity` |
| The typing dots / the streaming answer | `wait` / `ans` | `reply.answer` `""` / not `""` |
| Stop the reply | | `can_stop`, §7.2 |
| A queued message, Cancel | `queue-<mid>` | `queued[]`, §7.1 |
| "👎 on its reply of … goes with your next message", × | `queue-feedback-<id>` | `pending_feedback[]`, §9 `unpend` |
| "✎ Your changes to … go to <n> with your next message" | `pend` | `pending_edits` |
| End the conversation | `acts` | `can_end`, §7.3 |
| The composer's sent ids (`data-sent-mids`) | | `mids` |
| New chat (header) | | `can_clear`, §7.4 |
| Earlier chats (header menu) | | §5 |
| State and "what a message does now" (header) | | `/instances`: `state`, `since`, `busy_note` |
| Next cycle's hint (composer) | | `/instances`: `next_cycle` |


## 15. Cycles

Three read-only routes use the same `supervisor.cycles` models as the web Cycles page. They need a paired bearer
(§1.2). Each returns `200` with an `ETag`; a matching `If-None-Match` returns `304` with an empty body.
An unknown instance returns `404 not_found`. Missing cost and duration stay `null`.
Live `doing`, tool arguments, tool results, and the web-only `where` field do not enter these responses.

### 15.1 `GET /app/v1/plan/<n>`

| Field | Type | Meaning |
|---|---|---|
| `now` | object | `{state, since, goal}`, described below |
| `check` | object | `{next, skipped, paused}`, described below |
| `next_step` | object or null | Latest next action: `{at, action, revise_if}`, described below |
| `unfinished` | list of str | What that cycle left unfinished |
| `commitments` | list | Promises to others, sorted by due date: `{id, promise, due, status}`, described below |
| `commitments_error` | str | Empty when read, otherwise why the ledger could not be read |
| `queue` | object | Described below |
| `goals` | str | The instance's goals Markdown |
| `goals_error` | str | Empty when read, `"no goals file"` when missing, otherwise why it could not be read |
| `improvement` | object or null | Latest Sleep or Reflection improvement plan: `{at, text}`, described below |

`now.state`, `now.since`, and `now.goal` are strings or null. `since` follows §1.6.
`goal` is non-null only in state `"in a cycle"`. `check.next` is next check text, and `check.skipped` is last skip text.
`check.paused` is a bool. Next can be `"paused"` or `"unknown"`, with a reason in skipped.

`next_step.at` is its cycle's offset timestamp or null. `action` is text, and `revise_if` is text or null.
Commitment `id`, `promise`, `due`, and `status` are strings, except an unparseable due is null.
Status is `"open"` or `"overdue"`. `improvement.at` is an offset timestamp or null, and `text` is the plan's text.

Queue fields:

| Field | Type | Meaning |
|---|---|---|
| `state` | str | `"rows"`, `"none"`, or `"unreadable"` |
| `open` | int | All open tasks |
| `overdue` | int | All overdue tasks |
| `error` | str | Empty when read or missing, otherwise why it could not be read |
| `rows` | list | At most ten open tasks, overdue first, then earliest due, then unparseable dates |
| `rows[].id` | int | Queue task id |
| `rows[].task` | str | Task text, at most 200 characters |
| `rows[].due_at` | str or null | Offset timestamp, plain date, or null (§1.6) |
| `rows[].due_at_text` | str, optional | Original nonempty text when due_at cannot be parsed; otherwise omitted |
| `rows[].overdue` | bool | Whether the task is overdue |

### 15.2 `GET /app/v1/cycles/<n>?before=&filter=`

`filter` is `""` (All), `"yours"` (For you), `"short"` (Fell short), or `"delivered"` (Delivered).
`before` is an inclusive server date in `YYYY-MM-DD` form. Omitted or empty values start at the newest day.
Any other filter, or an invalid nonempty date, returns `400 bad_request` in the §1.5 error form.

| Field | Type | Meaning |
|---|---|---|
| `days` | list | At most seven days with matching rows, newest first |
| `next` | str or null | Next inclusive `before` cursor as a plain server date; null when there are no more days |
| `judge_error` | str | Empty when read, otherwise why judgments could not be read |
| `days[].date` | str | Plain server date, `YYYY-MM-DD`, never a timestamp |
| `days[].skipped_lines` | int | Unparseable records skipped in that day's log |
| `days[].counts` | object | Counts for all cycles in that day, before filtering; fields below |
| `days[].rows` | list | Matching rows, newest first; fields below |

Counts are integers: `cycles` is the number of live cycles; `yes`, `partial`, `no`, and `none` count the instance's
claims, with `none` counting missing or unknown claims. `judged` counts cycles with a verdict, and `for_you` counts
cycles with operator origin. Judge verdicts are separate and never added to claim counts.

Each row:

| Field | Type | Meaning |
|---|---|---|
| `id` | str | Raw stored timestamp, also the detail route's ts |
| `at` | timestamp or null | Offset timestamp (§1.6) |
| `kind` | str | `"cycle"`, `"sleep"`, or `"reflection"` |
| `goal` | str or null | Goal, at most 300 characters |
| `claim` | str or null | Instance's own claim, usually `"yes"`, `"partial"`, or `"no"` |
| `verdict` | str or null | Judge's verdict, separate from the claim |
| `delivered` | bool | A cycle delivered through a nonempty method other than `"none"` |
| `origin` | str or null | Instance's recorded origin |
| `ended_by` | str or null | How the cycle ended |

### 15.3 `GET /app/v1/cycles/<n>/<ts>`

`ts` must match `YYYY-MM-DDTHH:MM:SS` exactly, without an offset or fractional seconds.
An unknown cycle, an invalid timestamp, or another path shape returns `404 not_found`.

| Field | Type | Meaning |
|---|---|---|
| `id` | str | Raw stored timestamp |
| `at` | timestamp or null | Offset timestamp (§1.6) |
| `kind` | str | `"cycle"`, `"sleep"`, or `"reflection"` |
| `content` | str or null | Sleep or reflection text; null for a cycle |
| `planned` | object or null | Recorded intent; null for sleep, reflection, or missing intent |
| `did` | object | Recorded action and usage, fields below |
| `result` | object or null | Claim and judgments; null for sleep or reflection |
| `left` | object or null | Remainder; null for sleep or reflection |

`planned` contains `goal`, `success_criterion`, `origin`, and `origin_ref` (strings or null), `confidence`
(number or null), `predictions` (list containing only string predictions), and `attention` (list of strings).
Missing predictions or notes are empty lists. Structured attention joins `source` and `why` as
`"source: <source>; why: <why>"`.

`did` contains `what_happened` (string or null), `duration_s` (integer or null), `usd` (number or null),
`tool_calls` (integer count only), `ended_by`, `wrapped_up`, and `delivered_via` (strings or null).

`result` contains `claim` (string or null), `surprises` and `doubts` (lists of strings), `judge_error`
(string, empty when read), and `judge` and `second` (objects or null). Structured surprises join `expected`,
`observed`, and `so_what` as `"expected: <expected>; observed: <observed>; so what: <so_what>"`.
Structured doubts use their `text`. Other note shapes become strings, as on the web page.
Each judgment contains `verdict`, `claim_supported`, `first_error`, `reasoning`, `model`, and `error`.
`claim_supported` is a bool or null, and `first_error` is an integer or null. The remaining fields are strings
or null, except `error` is always a string.
A failed judge can have a null verdict and a nonempty error. The instance's claim remains separate.

`left` contains `unfinished` (list of strings), `next_action` (the recorded object or null), and `next_goal`
(string or null). A next action normally contains `action` and `revise_if` text.
