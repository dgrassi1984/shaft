# The native apps' API: `/app/v1`

The contract between the SHAFT web service and the native chat apps for iPhone, iPad and Mac.
The server side is `supervisor/app_api.py` (M1). Every field
below is what the server sends; the Swift side (`SHAFTKit`) decodes the fixtures the server's own serialiser writes
(Appendix A.5), never a hand-written copy.

The API shows a chat exactly as the PWA's chat page shows it. One function says what a chat shows
(`web_chat.feed`, Appendix A.1); the HTML feed renders it and this API serialises it, so a fix to one cannot miss the other.
§14 maps every piece of the HTML feed to its JSON field.

## 1. Basics

### 1.1 Where

- The web service (`shaft web on`), on the same port as the web UI: `https://<mac>.<tailnet>.ts.net:8444/app/v1/…`
  through `tailscale serve`. Tailnet only, never Funnel.
- The app learns the base URL from the pairing link (§2): everything before `/pair`.
- On loopback, off by default: with `SHAFT_WEB_LOCAL=on`, the Mac app on the same Mac reaches
  `http://127.0.0.1:8785/app/v1/…` (or `localhost`, at `SHAFT_WEB_PORT`) without Tailscale (§1.2).

### 1.2 Auth

Every request passes three checks, in this order:

1. **The tailnet login.** `tailscale serve` adds `Tailscale-User-Login`; it must be in `SHAFT_API_LOGINS`, as for
   the web UI (`web._route`). Refused: `403 login`.
2. **The host.** `Host` must be in `web.allowed_hosts()`: the recorded tailnet host, or the
   `SHAFT_WEB_HOSTS` override when set. Refused: `403 host`.
3. **The device.** `Authorization: Bearer <device>.<secret>`: the same pair a paired browser's cookie holds, checked
   against `.supervisor/web/devices.json` (the secret's SHA-256, compared in constant time). Refused when the header is
   missing or malformed, the device is unknown (never paired, or unpaired since) or the secret is wrong: `401 unpaired`.
   The app then shows its pairing screen. `POST /app/v1/pair` (§2) is the one route without a bearer.

**On loopback** (`SHAFT_WEB_LOCAL=on`, `Host` one of `web.local_hosts()`: `127.0.0.1:<port>` or `localhost:<port>`,
and none of the headers `tailscale serve` adds: `X-Forwarded-For`, `Forwarded`, `X-Forwarded-Host`,
`Tailscale-User-Login`, `Tailscale-User-Name`), checks 1 and 2 are skipped and only `/app/v1` is served; the pages keep
their refusals. The bearer (check 3) is then the only credential, and the request acts as the device's recorded
`login`; a device unpaired meanwhile is `401 unpaired`. A request with any of those headers came through the tailnet,
whatever `Host` it names, and must pass all three checks as above. Such a device is made by `shaft web pair --local`
(after `shaft api on --local` and `shaft web on --local`); with `--json` it prints one line,
`{"ok": true, "base_url": "http://127.0.0.1:8785", "device", "secret", "name", "login": "operator@local"}`, or
`{"ok": false, "error": …}` and exit 1 when `SHAFT_WEB_LOCAL` is off. `POST /app/v1/pair` on loopback is refused:
`403 local_pair`.

- `/app/v1` reads no cookie, and the web UI's routes accept no bearer. A browser page can never reach the API with an
  ambient credential, so there is no CSRF token and no origin check. No route sends CORS headers.
- `last_seen` in `devices.json` is refreshed as the cookie refreshes it (every 5 minutes at most), so
  `shaft web status` shows the app in use. `shaft web unpair <device>` revokes it; its next request gets `401
  unpaired`.

### 1.3 Requests

- **Bodies.** `POST` bodies are JSON (`Content-Type: application/json`). Chat send uses `multipart/form-data` for
  attachments. Meeting chunk uploads use `PUT` with `application/octet-stream` (§20.2).
- **Size.** Chat send admits `ATTACH_MAX_TOTAL + 1_000_000` bytes. The meeting chunk route admits 8,454,144 bytes at
  the HTTP gate and accepts up to 8,388,608 bytes of audio per chunk. All other bodies are limited to 1,000,000 bytes.
  The server returns `413 too_large` before reading a body over its limit.
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
  the type says so). Partial feeds (§4.2) omit fields as marked there. Queue rows omit `due_at_text`
  when no unparseable due text exists. Only scheduler service rows have `drain`; API rows omit it (§15.2).
- **Status codes:** `200` success; `304` a long-poll or a conditional `GET` with nothing new (empty body); the errors
  below.

### 1.5 Errors

Every error answers `{"error": {"code": str, "message": str}}`. `message` is plain English for the screen, and says
what to do where it can; it is never empty. A few codes carry one or two more fields inside `error`, listed here.

| Status | `code` | When | Extra fields |
|---|---|---|---|
| 400 | `bad_request` | Malformed JSON or multipart, a field missing or of the wrong type, `wait` out of range, a bad `mid`, a bad push token, env, topic or platform | — |
| 401 | `unpaired` | No bearer, a malformed one, an unknown device, a wrong secret | — |
| 403 | `login` | The tailnet login is missing or not in `SHAFT_API_LOGINS` | — |
| 403 | `host` | `Host` is not in `web.allowed_hosts()` | — |
| 403 | `pair_expired` | The pairing token is unknown, used, or older than 10 minutes | — |
| 403 | `local_pair` | `POST /app/v1/pair` on loopback (§1.2): pair with `shaft web pair --local` | — |
| 404 | `not_found` | No such route, instance, chat, turn, attachment, document | — |
| 405 | `method` | The route exists, the method does not | — |
| 409 | `refused` | Malformed comment post or thread ids, unavailable ratings, or a settings writer, service command, setup verb (§18) or action that refused the request: `web_chat.Refused`, `doc_comments.Refused`, `docs.Refused`, `voices.Refused` (§17), a feedback `ValueError`. The message says why ("it is still replying: stop the reply first") | `POST /setup/instance`: `done`, the steps already completed (§18.1) |
| 409 | `not_replying` | The stream (§6.1) with no reply running and nothing buffered after `after` | — |
| 409 | `stale` | A document save or delete with an old `sha`, or a setting save with an old `expect` | Documents: `sha`, `diff` (§10.4). Settings: `settings`, the fresh settings (§15) |
| 409 | `busy` | An API restart would interrupt a run or reply; the message is the service guard's reason | — |
| 409 | `restarting` | Another restart of the same service is still running | — |
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
  own time zone. Cycles (§16) is an exception: `days[].date` and `next` are server dates in `YYYY-MM-DD` form.
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

The Mac app on the same Mac as the server pairs without a link, over loopback (§1.2): its setup (or, with an install
already there, its first launch) runs `shaft web pair --local --name <name> --json` from `~/SHAFT` and keeps the
printed `base_url`, `device` and `secret` (the bearer) in its Keychain. A loopback pairing the server no longer knows
(`401 unpaired`) is paired again once on its own. The pasted link below is its "Connect to an existing SHAFT".

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

- **Errors:** `400 bad_request` (neither `link` nor `token`, or a bad `platform`); `403 pair_expired`; `403
  local_pair` on loopback (§1.2).
- **Effect:** the device is a paired device in `devices.json` (with `"platform"`), as a browser is: `shaft web status`
  lists it, `shaft web unpair` revokes it.

```json
// POST /app/v1/pair  {"link": "https://mac.tail1234.ts.net:8444/pair?t=Zq3…", "platform": "ios"}
{"device": "8c1f4e2a9b07d3e5", "secret": "kP0v…", "name": "iphone-app", "login": "operator@example.com"}
```

### `POST /app/v1/unpair`

This device unpairs itself (the PWA's "Unpair this browser"). Its APNs token and web subscription go with
it. Shared read marks remain. Request `{}`. Response 200 `{"said": "This device is unpaired."}`. The bearer stops working at once.

## 3. The chat list

### `GET /app/v1/instances`

The rail has one row per instance in the operator's saved order (`supervisor.order.instances`). New instances go last. An instance
the operator paused on its own (`.supervisor/paused-<name>`) has no row: it is listed under `paused` instead, and the
app offers Resume from Settings. Pause-all hides nothing.

- **Auth:** §1.2.
- **Conditional:** the response carries an `ETag`; with a matching `If-None-Match` the answer is `304`. The app polls
  it every few seconds while on screen.
- **Response 200:**

  | Field | Type | |
  |---|---|---|
  | `instances` | list | the rows below |
  | `instances_rev` | int | saved instance order revision |
  | `badge` | int | listed instances with something the operator has not seen (`web_seen.badge`, §12) |
  | `paused` | list | instances paused on their own, in the saved order: `{"name": str, "since": str \| null}` (§1.6 stamp of the pause flag; absent from an older server) |

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
  | `threads` | int | number of open threads |
  | `threads_rev` | str | short hash of ordered thread ids, titles, lifecycle states and the saved order revision |
  | `replying` | bool | a reply is running in any open thread |
  | `unread` | bool | any thread or Main has something the operator has not seen (§12) |
  | `unread_main` | bool | Main only, the `from` conversation |
  | `unread_threads` | int | number of unread threads |
  | `next_cycle` | str | what Next cycle does now (`autonomy.next_cycle`): the composer's Next cycle hint; `""` when it cannot be read |
  | `voice_mode` | str | `"on"`: offer voice for this instance (the chat's Talk button, §6.1); `"off"` otherwise, also when its `config.env` has no `VOICE_MODE` or anything but `on` (`opconfig.voice_mode`) |

```json
{
  "instances": [
    {"name": "example", "state": "in a cycle", "since": "14:01", "model": "cloud claude-sonnet-5",
     "busy_note": "Your message pauses its cycle; the cycle resumes after the reply.", "usd_today": 0.42,
     "line": "Merged the ledger fix; the nightly report is in notes/docs/ledger.md.",
     "last": "2026-11-03T14:06:40+01:00", "replying": false, "threads": 1, "unread": true,
     "next_cycle": "a cycle is running and read its inbox when it began: this starts the next one once it ends",
     "voice_mode": "on"},
    {"name": "example2", "state": "idle", "since": "", "model": "qwen3.6-35b", "busy_note": null, "usd_today": 0,
     "line": "you: thanks", "last": "2026-11-02T22:10:05+01:00", "replying": false, "threads": 1, "unread": false,
     "next_cycle": "for example2's next cycle, which this starts now", "voice_mode": "off"}
  ],
  "badge": 1
}
```

### 3.1 `GET /app/v1/threads/<n>`

Returns `{"threads": [...], "from": {"line": str, "last": timestamp or null, "unread": bool, "state": str,
"waiting": str or null, "failed": bool}}`. Main's `state` and `waiting` describe its own conversation, like a topic's (`idle` before its first message).
The response also has `threads_rev`, the same string returned in the instance row. It changes on creation,
rename, close, state changes including waiting, failed outcomes, and manual order changes. It stays stable otherwise.
Threads are open conversations in the operator's saved order. New threads go first. Activity never changes their
order. Open threads missing from storage go first, newest creation first. Each row has:

| Field | Type | Meaning |
|---|---|---|
| `id` | str | The chat id used by feed and action routes |
| `title` | str | The chosen title, or first-message title |
| `created` | timestamp or null | When the thread opened |
| `last` | timestamp or null | Newest instance item in this thread's feed |
| `line` | str | First line of the newest text item, at most 80 characters |
| `state` | str | `idle` or `replying` |
| `waiting` | str or null | The running reply's waiting text |
| `failed` | bool | The last assistant turn has a failure note, excluding stopped and retried replies |
| `unread` | bool | The operator has unseen instance items in this feed |
| `empty` | bool | Nothing is in it yet: closing it ends no conversation, so the apps close it without asking |

The `from` object represents Main and uses the same preview, unread, and failed rules. Lifecycle `state` remains unchanged. Unknown instances return `404 not_found`.

### Pending next-cycle notes: `GET /app/v1/notes/<n>`

Returns `{"notes": [...]}` from `supervisor.web_chat.pending_notes`, across the full instance history.
Only operator BACKGROUND rows addressed to the instance, unread and not cancelled, are included.
Each note has these fields:

| Field | Type | Meaning |
|---|---|---|
| `file` | str | Inbox filename used for cancellation |
| `mid` | str or null | Original message id, when present |
| `text` | str | Shown text, falling back to the stored text |
| `ts` | timestamp or null | Offset timestamp, like other app API times |
| `cid` | str | Open topic id, otherwise `from` for Main, including ended topics |
| `title` | str | Topic title or instance name |
| `cancellable` | bool | The note can be taken back, subject to a fresh cancellation check |
| `reason` | str or null | Why Cancel is unavailable |

Notes with delivery side effects cannot be taken back. A running reply or cycle also makes Cancel unavailable.
Cancellation uses the existing `POST /app/v1/chat/<n>/uncycle` with `mid` or `file`.
The web uses `POST /chat/<n>/uncycle` with the same identifiers and CSRF protection.
A run that begins after the read can refuse cancellation. Clients retain the note and show the refusal.
Unknown instances return `404 not_found`.

### 3.2 Order moves

- `POST /app/v1/threads/<n>/move`: JSON `{"id": str, "after": str or null}`.
- `POST /app/v1/instances/move`: JSON `{"name": str, "after": str or null}`.

Both require the paired bearer from §1.2. Response 200 is `{"rev": int, "ids": [str, ...]}`, the authoritative
order of all current rows. `after: null` moves to the top. A missing or ended anchor also moves to the top.
A self-anchor leaves the revision unchanged. Moves apply to the latest saved order, so sequential moves from two
devices both apply. Unknown or ended threads, `"from"`, and unknown instances return `409 refused`.
Ended threads and removed instances are ignored on reads and pruned on the next write.
Clients discard order responses older than a revision they already hold. After an ambiguous network failure,
refetch the order. `rev` is the saved order counter; `threads_rev` is a content hash, not a counter.

## 4. The feed

### `GET /app/v1/chat/<n>` — the current chat

### `GET /app/v1/chat/<n>/<cid>` — a selected thread

The current chat is the one the web chat page shows (`web_chat.shown`): the most recently active open thread, else the last one, unless the
operator cleared it (then none: `chat` is null). `<cid>` names any chat of `<n>`; an ended one is read-only
(`archived: true`), an open thread reads as the current chat does.

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
- **Watching:** a request for an open thread, with or without an explicit cid, marks this device as having that thread on screen unless
  `watch=0` is supplied (`web_chat.watching(name, device, at, cid=cid)`), and refreshes it every 2 s while held only if `at` is supplied.
  Without `at`, it marks watching initially but does not refresh it while held. A watching
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
| `chat` † | object or null | `{id, title, state, created, ended}` below; null when no chat is shown |
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
| `can_clear` † | bool | a chat is shown and not archived (Clear this thread; refused while replying) |

`chat`:

| Field | Type | |
|---|---|---|
| `id` | str | `web-…` |
| `title` | str | explicit title, else the first line of the operator's first message, else `"New thread"`; at most 80 characters |
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
line, as the HTML sorts them). The conversation's own turns always keep the order they were written in, even when the
Mac's clock was set back between them; the other items go among them by time.

**`user`**: a message from the operator (key `u<k>`).

| Field | Type | |
|---|---|---|
| `k` | int | the turn's index |
| `ts` | timestamp | |
| `text` | str | what the operator wrote (`shown`), `""` for a turn that is only a comment or a document edit. Not Markdown: lines starting `> ` are a quote block, a fenced code block (```) is code, the rest is plain text with its line breaks (`web_views._my_text`) |
| `mid` | str or null | its message id |
| `interjected` | bool | sent while a reply ran ("· during its reply") |
| `via` | str or null | `"voice"`: spoken and transcribed on the device (sent with `voice=1`, §6; the HTML says "· spoken"), so it may hold recognition errors; null: typed |
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
(14:03)." Fields: `k`, `ts`. The messages it answered are not items: Retry sent them again as one.

A new instance's Main opens with a request from setup that no feed shows (a hidden turn: no item, excerpt, title or
`mids` entry), so its first item is the instance's welcome. When that reply fails, `retry.text` is
"Ask for the welcome again": sent back as any Retry (§6), it asks for the welcome again, still hidden, never as a
message of the operator's. Its `retry_of` is always `k`: the re-ask is known by it, and the same words sent without it
are an ordinary message. As any Retry, it is one tap folding the failed try (`confirm` null) only when nothing ran;
else `confirm` is set (ask first) and the failed try is kept.

**`sent`**: a message or question the instance sent the operator (`send_message`) over the chat's span (key
`s<file>`).

| Field | Type | |
|---|---|---|
| `file` | str | its file name in the message log: rating target, `/messages/<file>` in the web UI |
| `ts` | timestamp | |
| `kind` | str | `"message"` or `"question"` |
| `choices` | object or null | `{options: [{label, detail}], multiple}`; 2–6 options, `detail` is text or null, `multiple` defaults to false. Other free text is always offered by the UI. |
| `answer` | object or null | `{picked: [int], other: str or null, ts: timestamp}`; sorted zero-based option indices and the operator's answer time. |
| `open` | bool | question still waiting in the operator inbox, neither answered nor archived; always false for a message. |
| `from_cycle` | bool | written later, in a cycle, for work queued in this thread: part of the thread (shown "⏾ from its cycle") |
| `re` | object or null | in From: `{"cid", "title"}` of the ended thread it answers (shown as a link to that earlier chat) |
| `read` | bool | it has left the operator's inbox, by reply or archive; `open` distinguishes questions still waiting. |
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
| `read` | bool | false: waiting in its inbox; true: no longer in its inbox (check `cancelled` before showing "read") |
| `cancelled` | timestamp or null | the operator cancelled this note before a run read it |
| `cancellable` | bool | not cancelled, its inbox file still exists, and it is a composer note that did nothing when sent (not an answer to a quoted question, a comment's, a rating's or the supervisor's own); a running or paused cycle, or a reply, can still refuse cancellation |
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
| `text` | bool | `.md .markdown .txt .csv .json .log .py .yaml .yml .toml .html .xml`: the app can preview it as text (the web previews up to 200 KB; the native apps draw `.md` and `.markdown` up to 256 KB of UTF-8 as a document, any other file in the system's preview) |
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

Instance names, timestamps, identifiers, and payload values in examples are illustrative.

A full feed: an idle chat with a quoted answer and a picture, an answer that made a document and was rated, a question
the instance sent, a Next-cycle message, and a failed reply with a safe Retry.

```json
{
  "instance": "example",
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
     "mid": "0b8e6c3a-5d1f-4c2e-9a7b-2f4d6e8a1c3b", "interjected": false, "via": null,
     "attachments": [{"name": "ci-run.png", "bytes": 48213, "image": true, "text": false,
                      "url": "/app/v1/file/chat/web-3f9a1c0d7e2b4a65/0/0"}],
     "edits": [], "deleted": [], "comments": [], "feedback": []},
    {"key": "a1", "type": "assistant", "k": 1, "ts": "2026-11-03T14:01:12+01:00",
     "content": "The failure is the rounding in `net_daily`. Fixed and pushed; the summary is in **ledger.md**.",
     "activity": ["read_file notes/docs/ledger.md", "run tests: 412 passed"], "incomplete": false, "failure": null,
     "docs": [{"path": "ledger.md", "created": false}], "answered": [{"id": "c3", "doc": "ledger.md"}], "opened": [],
     "rating": {"id": "f-6a0c2e4b8d1f3a5c7e9b0d2f", "thumb": "up", "reason": "", "sent": null, "pending": false},
     "last": false},
    {"key": "s20261103-140230-118204-from-example.md", "type": "sent", "file": "20261103-140230-118204-from-example.md",
     "ts": "2026-11-03T14:02:30+01:00", "kind": "question", "read": false,
     "text": "Should the nightly report keep the per-account lines?", "attachments": [], "rating": null},
    {"key": "b20261103-140301-552019-from-operator.md", "type": "later",
     "file": "20261103-140301-552019-from-operator.md", "ts": "2026-11-03T14:03:01+01:00",
     "text": "Look at the March reconciliation when you have time.", "mid": "5e2d9c1b-7a3f-4e60-8b2d-1c9f0a7e3d54",
     "read": false, "attachments": [], "comments": []},
    {"key": "u2", "type": "user", "k": 2, "ts": "2026-11-03T14:05:50+01:00", "text": "Rerun the report for October.",
     "mid": "a41c7e09-3b2d-4f8e-9c61-0d5b7a2e4f13", "interjected": false, "via": "voice", "attachments": [], "edits": [],
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
  "instance": "example", "partial": false, "version": "41d07be2c95a8f13", "live": "c08e57f2a1b9d364",
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
{"instance": "example", "partial": true, "version": "41d07be2c95a8f13", "live": "7b19e4c0d2a65f38",
 "reply": {"started": "2026-11-03T14:07:02+01:00",
           "activity": ["read_file notes/docs/ledger.md", "run report --month 2026-10", "write notes/docs/october.md"],
           "answer": "October's report is running; so far 214 accounts reconciled."}}
```

### 4.10 `GET /app/v1/from/<n>`

Returns the From feed from `web_chat.from_feed`, serialized as the full feed in §4.2: From's messages and the turns of
the From conversation, its running `reply`, `queued` and `can_stop` as a thread's. `chat` is null until the first
message written in From, then the From conversation (`title` "From <n>"); `can_end` and `can_clear` are always false.
Times and attachment URLs use the same serializer as thread feeds. This route long-polls exactly as §4 does:
`since`, `live`, `wait` (and `all=1`) are parsed and validated the same, and the request is held — 304 after `wait`
with nothing new, 200 with the whole feed when `version` changes, 200 partial when only `live` changes. `watch=0` and
`at` have the same meaning as for a thread feed, scoped to `cid: "from"`; `all=1` returns every item (its last 80
otherwise, `earlier` counting the rest).
Unknown instances return `404 not_found`.
Write in From through `/app/v1/chat/<n>/send` with `cid: "from"` and any mode (§6): `now` and `queue` are answered in
From now, `background` leaves a note for the next cycle that names the From conversation. Its answer `chat` is
`"from"`. Stream, stop and unqueue take `cid: "from"` too; rename, end and clear refuse it (`409 refused`).
Use `cid: "from"` for seen and away (§7.5, §7.6).

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

### `POST /app/v1/chat/<n>/answer`

Answer a question with choices. JSON body: `{cid, file, picked, other, mid}`. `cid` is the thread showing the
card, or `"from"`; `file` is the raw `sent.file`. `mid` follows `/send`'s rules. `picked` is a list of zero-based
option indices, default `[]`; `other` is optional text, at most 2000 characters. Pick at least one option or supply
non-blank Other text. Single-choice questions accept at most one pick. Repeated or out-of-range indices are refused.

Returns `200 {said, answer: {picked, other, ts}}`. The same question and `mid` return the original result.
A different `mid` after an answer returns `409 {error: {code: "answered", message, answer}}`. A question answered or
closed another way (a quoted Reply, Telegram) returns `409 closed`: a late card or notification adds nothing.
Invalid answers, including a question without choices, return `400 bad_answer`. An unknown question or a question
from another instance returns `404 not_found`.

The reply quotes the question and states the selected labels under `Answer:`, with `Other:` on its own line. It
answers its question by file (`In-Reply-To`), so it is final (no Cancel), and its quote closes no other question.
The answer is recorded before it is delivered, and a refused delivery takes it back.
Open threads receive a normal operator turn. From and ended threads receive an inbox reply for the next cycle,
which wakes the instance. The question leaves the operator inbox and its `open` becomes false. Structured answers
persist in `.supervisor/question-answers.json`, keyed by question file, under a lock with atomic replacement.
The web form posts to `/chat/<n>/answer` and calls the same answer logic. It uses `picked` for a radio button or
repeated `picked[]` fields for checkboxes, plus `cid`, `file`, `other`, and `mid`.

### `POST /app/v1/chat/<n>/send`

The web's `/chat/<n>/send` path (`web_chat.send`, `queue`, `background`), multipart. Choice cards use `/answer` above.

- **Request (multipart/form-data):**

  | Field | Type | |
  |---|---|---|
  | `cid` | str | optional open thread of `<n>` for `now` and `queue`; omitted or empty selects the most recently active open thread, or starts one. An unknown, foreign or ended thread answers `409 refused`. `"from"`: the From conversation (made on its first message). With `background`: the thread the note was written in — while open, the note shows there and its answer comes there; `"from"`: From, naming the From conversation; omitted or an ended thread: From |
  | `text` | str | what the operator wrote below the quotes; may be `""` |
  | `mid` | str | required: the message's id (§1.3). The same `mid` again answers as the first send did, without sending twice — also across `now`, `queue` and `background` |
  | `mode` | str | `now` (default): a new reply, or an interjection while one runs ("Send now"). `queue`: after the running reply (sent now when idle). `background`: to the inbox for its next cycle, no reply now |
  | `quotes` | str | JSON list of `{quote, answer, doc}` (↩ Reply cards), `[]` or absent for none. `quote`: the selected text as shown; `answer`: its own answer, may be `""`; `doc`: the `notes/docs/`-relative path when it was quoted from a document, else `""` |
  | `in_reply_to` | str | optional stable `file` identity of the delivered `sent` item being answered. Accepted sends append statistics-only evidence to `.supervisor/reply-links.jsonl`. Unknown, malformed or wrong-instance identities are ignored and logged; they never refuse a send. Absent leaves the send unchanged. |
  | `retry_of` | str | `retry.retry_of` of a failed reply (§4.4), with `mode=now` only; the text must be `retry.text` |
  | `voice` | str | `"1"`: spoken, transcribed on the device. The turn is recorded `via: "voice"` (§4.4), and the reply's request to the API carries `"voice": true` (the instance is told it is heard aloud, and uses `VOICE_REASONING_EFFORT`, §15.1). An interjection (`mode=now` while it replies) carries it too, and a queued one (`mode=queue`) when it goes. `"0"` or absent: typed. Anything else, or `"1"` with `mode=background`: `400` |
  | files | file parts | any part with a filename. At most 10 files, 20 MB each, 50 MB in all |

Reply identity: `sent.file` is the raw `file` key from `.supervisor/messages.jsonl`,
not the URL-encoded feed key. Native clients decode it optionally for older servers.
A Reply card keeps its optional `in_reply_to` through draft and outbox persistence;
`OutgoingMessage` sends the last card with an identity as multipart `in_reply_to`.
The web Reply button carries the same raw identity. Telegram keeps each forwarded
text part's message id mapped to that identity. Outcomes count explicit links and
quotes in operator messages or conversation turns as `followed_up` within 48 hours
of delivery. A quote must identify a single delivered message. After the window,
`no_link` means there is no item-linked evidence. These links have no grade,
probation or reward consumers.

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
  | `said` | str | what happened, for a status line: "sent to example", "example reads it at its next step", "queued — sent when this reply ends", "for example's next cycle, which this starts now", "already sent to example", " · answers its question" appended when it answered a quoted question |
  | `chat` | str or null | the chat it went into (null for `background`) |
  | `file` | str or null | `background`: the inbox file (a `later` item's `file`); else null |

- **Errors:** `400 bad_request` (no `mid`, a bad `mode`, `quotes` not a JSON list of those objects, a bad `voice`); `409 refused`
  ("an empty message is not sent", "a message is at most 20000 characters (…)", a file over the limits, "the API is
  not reachable …"); `413 too_large`.
- If the chat ended meanwhile, the message starts a new chat, as the web's send does.

```json
// mode=now, text="Rerun it", quotes=[{"quote":"Should the nightly report keep the per-account lines?","answer":"Yes","doc":""}]
{"said": "sent to example · answers its question", "chat": "web-3f9a1c0d7e2b4a65", "file": null}
```

### 6.1 `GET /app/v1/chat/<n>/stream` — the reply as it streams

Server-Sent Events of the reply running in the current chat, for voice mode: the app speaks the reply as it is
written. The events come from the web server's own memory (`web_chat.Relay`), appended as the API's chunks arrive. They
never wait for the chat file, written every second, or the feed's 0.5 s look (up to 1.5 s per piece).
The feed (§4) still carries the reply for clients not on the stream.

- **Auth:** §1.2.
- **Query:**

  | Param | Type | |
  |---|---|---|
  | `cid` | str | optional thread id; absent, the current thread |
  | `after` | int | the `seq` of the last event the app has; default 0. A `Last-Event-ID` header stands for it when `after` is absent. Not a whole number ≥ 0: `400` |
  | `reply` | str | the `reply` of the events the app has. When it is not the buffered reply's (a newer one began), `after` is ignored and the whole reply comes |
  | `at` | int | as the feed's (§4): orders the watching mark against `away` |
  | `watch` | `0` | optional: holding the stream does not mark the device as watching (below), so the reply's push still reaches it. The iPhone app sends it when it follows a reply for the Apple Watch |

- **Response 200:** `Content-Type: text/event-stream; charset=utf-8`, no `Content-Length`; the connection closes when
  the stream ends. Each event is `id: <seq>` and one `data:` line of JSON, then a blank line. Lines starting with `:`
  are comments: one names the reply at the start (`: reply <id>`), and `: keepalive` comes every 2 s with nothing else
  to send. The stream replays the buffered events with `seq > after`, then follows live, and ends after `end`.
- **`409 not_replying`:** no reply is running in the current chat and none ended in the last 30 s
  (`STREAM_KEEP_S`), or the one buffered ended and the app has all of it (`after` ≥ its last `seq`). The app listens
  again; it does not retry.
- **Watching:** holding the stream marks this device as watching the chat, refreshed every 2 s while held, as a held
  feed with `at` is (§4); with `watch=0` it never does. A reply that ends while the device listens is not pushed to it. With `at`, an `away` sent
  later (§7.6) wins over the stream. Without `at`, a voice session on a locked iPhone still counts as listening.
- **Leaving:** the server notices within 2 s that the client closed the connection, and the hold ends with it.
- **One reply per stream.** After `end`, the next reply (the operator's next sentence, or the instance's own answer to
  an interjection it heard too late) is a new stream. It has its own `reply` and its `seq` starts at 1 again.

Every event:

| Field | Type | |
|---|---|---|
| `seq` | int | 1, 2, … in order, per reply |
| `type` | str | below |
| `reply` | str | 16 hex: the reply's id, the same on every event of the reply |

| `type` | Other fields | When |
|---|---|---|
| `draft` | `text`: str, a piece | the model wrote text in the current step. It comes before the step is known to be the answer: speak it as tentative |
| `step_end` | `answer`: bool | the step ended. `true`: it was the final answer. `false`: it went on to call tools, so its text was narration |
| `status` | `phase`: str, `detail`: str | what the reply waits on, as the activity says it ("pausing its cycle for you") |
| `tool_start` | `name`: str | a tool call started: start the working sound |
| `tool_end` | `name`: str, `ok`: bool | it finished |
| `answer` | `text`: str | the reply ended: the answer as the chat keeps it (the `assistant` item's `content`); `""` when none, partial when it failed |
| `end` | `failure`: str or null | the last event. null: the reply ended normally. Else the reply's note: `"stopped by you"`, or `"error: …"` |

`draft`, `step_end`, `status`, `tool_start` and `tool_end` come only on a voice turn, when the API sends them.
A typed reply's stream carries only `answer` and `end`. A field the API left out is `null`.

```text
GET /app/v1/chat/example/stream?after=0

: reply 3f0c9e21a4b7d658

id: 1
data: {"seq": 1, "type": "status", "reply": "3f0c9e21a4b7d658", "phase": "model", "detail": "thinking"}

id: 2
data: {"seq": 2, "type": "draft", "reply": "3f0c9e21a4b7d658", "text": "Let me look."}

id: 3
data: {"seq": 3, "type": "step_end", "reply": "3f0c9e21a4b7d658", "answer": false}

id: 4
data: {"seq": 4, "type": "tool_start", "reply": "3f0c9e21a4b7d658", "name": "read_file"}

: keepalive

id: 5
data: {"seq": 5, "type": "tool_end", "reply": "3f0c9e21a4b7d658", "name": "read_file", "ok": true}

id: 6
data: {"seq": 6, "type": "draft", "reply": "3f0c9e21a4b7d658", "text": "It is sunny in Milan."}

id: 7
data: {"seq": 7, "type": "step_end", "reply": "3f0c9e21a4b7d658", "answer": true}

id: 8
data: {"seq": 8, "type": "answer", "reply": "3f0c9e21a4b7d658", "text": "It is sunny in Milan."}

id: 9
data: {"seq": 9, "type": "end", "reply": "3f0c9e21a4b7d658", "failure": null}
```

A voice turn: send with `voice=1` and `mode=now`, then open the stream with no `after` once the send has answered.
The send starts the reply before it answers, so the stream has it. When the send was an interjection, the stream is
the reply already running: continue it with the `reply` and `after` you hold.

## 7. Chat actions

All take JSON and answer `{"said": str}` unless said otherwise; a refused one answers `409 refused` with the reason.
`cid` must be a chat of `<n>` (`404 not_found` otherwise).

### 7.1 `POST /app/v1/chat/<n>/unqueue`

`{"mid": str, "cid"?: str}`: cancel a queued message in that open thread. Without `cid`, uses the most recently active open thread. `{"said": "queued message cancelled"}` (also when it had already gone).

### 7.1.1 `POST /app/v1/chat/<n>/uncycle`

`{"mid"?: str, "file"?: str}`: cancel a Next-cycle note by its page message ID or inbox file name.
When both are present, `mid` selects the note. Returns `{"said": "next-cycle note cancelled"}` or
`{"said": "already cancelled"}`. A running cycle or reply refuses cancellation with `409 refused`.
A note whose inbox file is gone is also refused: "already read in a cycle: it can't be cancelled".
The operator feeds keep the cancelled note; instance dialogue excludes it. Attachments remain stored.

### 7.2 `POST /app/v1/chat/<n>/stop`

`{"cid": str}`: stop the running reply; the conversation stays open. `409 refused` "no reply is running".

### 7.3 `POST /app/v1/chat/<n>/end`

`{"cid": str}`: end the conversation now (journal write-up requested). `{"said": "ended: journal write-up requested"}`.
Refused while it replies, or when it is already over. `{"cid": str, "if_empty": true}` closes it only while it is
still empty (the thread row's `empty`, checked again under the lock): `409 not_empty` otherwise, and nothing ends.
The apps send it when they close an empty thread without asking, and ask first on `not_empty`.

### 7.4 `POST /app/v1/chat/<n>/clear`

`{"cid": str}`: Clear this thread. An open conversation is ended first; the chat is kept (Earlier chats, Transcripts).
A new thread is §7.7: it ends nothing.
Only `cid` is cleared; other open threads stay open. Refused while it replies. An ended chat must still be the
shown chat, else "that is not the chat on this page any more: reload it".

### 7.5 `POST /app/v1/chat/<n>/seen`

`{"version": str, "cid"?: str}`: this device has shown the selected feed up to that version (the app sends it when the newest item's end is
on screen in a window that shows the chat: on the Mac also one beside another app's window in front).

`cid` is a chat id or `"from"`. Omitted, it means the shown chat, as in older builds.
Only that feed is marked. An unknown chat or one belonging to another instance returns `404 not_found`.

- **Response 200:** `{"unread": false, "badge": int}`: the badge to set now.
- **`409 stale_version`**, with the current `version` in `error`, when `version` is not the feed's current one: the
  feed moved since the app drew it, so the app must not mark what it has not shown. Not an error to show: send `seen`
  again once the end of the newest item in the new version is on screen.
- The native client retries failed requests after 2, 4, 8, 16, then 30 seconds, capped at 30 seconds, reading the current
  feed version on each attempt. It cancels retries when the newest item's end leaves the screen, or the chat is covered or no longer shown.

### 7.6 `POST /app/v1/chat/<n>/away`

`{"at": int, "cid"?: str}` (Unix ms on the device): the selected feed left the screen (the app went to the background, the phone locked, the
operator opened another chat). Pushes for it reach this device again at once rather than after `WATCH_S`
(`web_chat.unwatch`). `cid` is a chat id or `"from"`; omitted, it means the shown chat.
Only that feed loses its watching mark. An unknown or foreign chat returns `404 not_found`. A held long-poll sent before `at` stops marking it watching. `{"ok": true}`.

### 7.7 `POST /app/v1/chat/<n>/new`

`{"title"?: str}`: create an open thread without ending other threads. Titles are stripped, joined onto one line,
and limited to 80 characters. An omitted or blank title uses the first message once sent, or "New thread".

Response 200: `{"chat": {"id": str, "title": str}, "said": "thread created"}`.
An unknown instance answers `404 not_found`.

### 7.8 `POST /app/v1/chat/<n>/rename`

`{"cid": str, "title": str}`: rename an open thread. Titles use the same normalization as `/new`.
Response 200: `{"said": "thread renamed"}`. Blank titles and ended threads answer `409 refused`.
An unknown instance or a chat that does not belong to it answers `404 not_found`.

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
| `unsent` | bool | unsent and unanswered (`doc_comments.delivered` false): only such a post can be edited, deleted or sent ("not sent") |
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
       {"pid": "p-9a8b7c6d5e4f3021", "by": "example", "text": "Per account, then summed.",
        "ts": "2026-11-03T14:01:12+01:00", "unsent": false, "sent": null, "turn": "web-3f9a1c0d7e2b4a65#1", "seen": null}]}
  ]
}
```

### 10.3 `GET /app/v1/docs/<n>/raw?path=<p>`

The document's bytes as a download (`text/markdown; charset=utf-8`, `attachment`),
up to `SHAFT_FILE_READ_MAX_MB`. This can download a document too large for the text
view. Exceeding the raw-read limit currently returns `500 internal`, not `409 too_big`.
`404 not_found` when it is not there.

### 10.4 `POST /app/v1/docs/<n>/save`

Edit a document, or create one (`web._save_doc`: `shaft docs set` with its compare-and-set). The instance hears of it
at once while it replies, else with the operator's next message.

- **Request:** `{"path": str, "text": str, "sha": str, "dir": str}`. `sha`: the one the app read; `"none"` to create.
  `dir` (optional): a folder the new document goes in. `.md` is added when missing.
- **Response 200:** `{"said": str, "path": str, "sha": str}`: the path as stored and the new sha ("saved · example gets
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
| `POST /app/v1/docs/<n>/comment/edit` | `{pid, text}` | edit an unsent, unanswered post |
| `POST /app/v1/docs/<n>/comment/delete` | `{pid}` | delete an unsent, unanswered post (its thread goes with its last post) |
| `POST /app/v1/docs/<n>/comment/send` | `{pid, when}` | send a stored post: `when` `"now"` (a chat message: "Send to <n>") or `"cycle"` (its inbox, for its next cycle) |
| `POST /app/v1/docs/<n>/comment/state` | `{thread, state}` | `"resolved"` or `"open"` (Resolve / Reopen) |

- `send` on a new post: `""` keeps it unsent (the instance sees it in `notes/comments/`; "kept, not sent: …"), `"now"`
  or `"cycle"` sends it at once. A send that fails after the post is stored answers 200 with "saved, but not sent:
  …".
- `pid` is made by the app (§1.3); posting the same `pid` again returns the thread that already holds it.
- `text` at most 20000 characters; `anchor.exact` at most 2000, `prefix`/`suffix` at most 32, `start` ≥ 0.

## 11. Push

Native push goes through APNs (`supervisor/apns.py`). The notification carries only the title ("example
replied", "example's reply failed", "example asks"), the badge, instance and `cid`; the app's notification extension fetches
the body from `/app/v1/push/latest` with the device bearer, over the tailnet.

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

A pushed question with choices also carries `question: {file, cid, choices}`. Digest events carry no question.
The latest-push endpoint preserves this object. Web Push ignores fields it does not use.

### 11.4 `GET /app/v1/push/latest`

What the notification shows: the events queued for this device (`webpush.take`), oldest first, taken off the queue.
With nothing queued, the last one shown again; with nothing ever, a plain one.

The notification extension sends `?instance=<n>` from the push payload. This takes only events whose URL is
`/i/<n>/chat`, `/i/<n>/chat?id=<cid>` or `/i/<n>/from`, leaving other instances queued. With none queued for that instance, only its own last event is
shown again, or the plain one if it has none. `instance` must be a valid instance name (a lowercase letter followed
by at most 30 lowercase letters, digits or hyphens); otherwise `400 bad_request`. Without it, all queued events
are taken as before.

| Field | Type | |
|---|---|---|
| `events` | list | `{title, body, url, tag, instance, cid}` |
| `title` | str | "example replied", "example's reply failed", "example asks", "example says" |
| `body` | str | its first line, at most 160 characters |
| `url` | str | the web path it opens (`/i/example/chat?id=web-abc` or `/i/example/from`) |
| `tag` | str | replaces an earlier notification with the same tag |
| `instance` | str or null | the instance to open: parsed from either chat or From URL; null for the plain one |
| `cid` | str or null | Thread id, `"from"`, or null for an untargeted or legacy event |
| `badge` | int | as §12 |

```json
{"events": [{"title": "example replied", "body": "October's report is ready: 214 accounts reconciled.",
             "url": "/i/example/chat?id=web-abc", "tag": "example", "instance": "example", "cid": "web-abc"}], "badge": 1}
```

The APNs payload has top-level `instance` and `cid`; `aps.thread-id` remains the instance name.
The notification extension fetches by instance, preserves each fetched event's `cid`, and opens that thread
or the From feed. If fetching fails, the payload still identifies the destination. Older builds ignore `cid`
and open the shown chat. Legacy queued events without `cid` return null.
A reply skips only devices watching that reply's thread. From messages skip only devices watching From.
`web_alerts._cid_of_message` is the single mapping point for message talk ids: a row routed to a thread, or sent
during one of its replies, opens that thread; everything else, including the From conversation's own, uses
`cid: "from"`. A From reply's push uses `cid: "from"` and `/i/<n>/from`.

It does not mark the device as watching, and it is a `GET` with an effect (it takes the events): only the bearer
reaches it, so no other page can spend them.

## 12. Unread and the badge

- Native apps and web browsers share one read state for the operator.
- `.supervisor/web/seen.json` holds `{"operator": {instance: {cid: mark}}}`. `"from"` is Main's feed key.
  New marks contain `last`, `last_count` and `at`; native writes also retain `version`.
  Legacy marks may omit `last_count`.
- Each thread compares its own `web_chat.feed` last stamp and count with its mark. From compares
  `web_chat.from_feed` last stamp and count. A newer stamp, or more instance items at the same stamp, is unread.
  Marks without a count keep timestamp-only comparison. The operator's own messages do not make a feed unread.
- On first read, per-device marks merge into the operator marks. The newest timestamp wins per conversation;
  equal timestamps use the higher count. A legacy per-instance mark applies to the shown chat and Main.
  New seen writes affect only the requested feed and never move its timestamp or count backward.
  An unmarked feed counts from the earliest paired device's pairing time.
- `/instances` reports `unread_main` for Main, `unread_threads` as the count of unread threads, and `unread` for any.
- An instance is unread when any open thread or From is unread. With no open threads, the shown ended chat
  still counts until cleared, preserving the older single-chat behavior.
- The badge is the number of unread instances (`web_seen.badge(device)`), sent with `/instances`, `seen`,
  `/app/v1/push/latest` and every APNs push.
- The native apps mark a chat seen when the newest item's end is on screen in a window that shows the chat (on the Mac
  also one beside another app's window in front, not hidden, minimized or fully covered), retry failures
  (§7.5), and discard list answers from polls that began before a successful seen request.
- Unpairing retains shared read marks. Push suppression still uses conversation watchers, not read marks.

## 14. The HTML feed, piece by piece

Every piece `web_views.chat_feed` and `live_activity` draw, and where it is in the JSON.

| HTML (`chat_feed`) | `data-k` | JSON |
|---|---|---|
| Empty chat ("Chat with <n>") | `empty` | `chat` null |
| "Show the N earlier messages" | `more` | `earlier` > 0 |
| The operator's turn: text with quotes and code, time | `u<k>` | `items[]` `user`: `text`, `ts` |
| "· during its reply" | | `user.interjected` |
| "· spoken" | | `user.via` `"voice"` |
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
| New thread (header) | | §7.7 |
| Clear this thread (header ⋯) | | `can_clear`, §7.4 |
| The thread's title, Rename (header) | | `chat.title`, §7.8 |
| Earlier chats (header menu) | | §5 |
| State and "what a message does now" (header) | | `/instances`: `state`, `since`, `busy_note` |
| Next cycle's hint (composer) | | `/instances`: `next_cycle` |


## 15. Instance settings and service restarts

These routes use the same tailnet, host and device checks as the chat routes (§1.2). Writes log the paired
device's name. Settings contain only the fields below. They never include secrets, base URLs, tool switches,
plugin grants or global settings.

| Method | Route under `/app/v1` | Success |
|---|---|---|
| GET | `/settings/<n>` | 200, the settings object below |
| POST | `/settings/<n>` | 200, `{"settings": <fresh settings>, "applies": "next run" or "now", "notes": []}` |
| POST | `/settings/<n>/pause` | 200, `{"settings": <fresh settings>}` |
| POST | `/settings/<n>/resume` | 200, `{"settings": <fresh settings>}` |
| GET | `/version` | 200, `{"server": str, "api": int}`, plus `project` on loopback and `local_port`, `local_id` with `SHAFT_WEB_LOCAL` on (§19) |
| GET | `/local` | 200, `{"local_id": str}`, loopback only, no bearer; 404 over the tailnet (§19) |
| GET | `/services` | 200, `{"services": [<api>, <scheduler>], "version": str}` |
| POST | `/services/api/restart` | 200, `{"services": [<api>, <scheduler>], "version": str}` |
| POST | `/services/scheduler/restart` | 200, `{"services": [<api>, <scheduler>], "version": str}` |
| POST | `/services/scheduler/cancel` | 200, `{"services": [<api>, <scheduler>], "version": str}` |

### 15.1 Settings

`GET /settings/<n>` returns these fields, all present:

| Field | Value |
|---|---|
| `instance` | str, the instance name |
| `paused` | bool, including a global operator pause |
| `model` | `{"value": str, "choices": [{"model": str, "label": str}]}` |
| `effort` | `{"value": str or null, "choices": [str] or null}` |
| `wake_interval` | `{"value": int, "floor": int or null, "effective": int}`, in minutes |
| `cloud_cap` | `{"value": float or null, "spent_today": float}`, in dollars |
| `channel` | `{"value": str, "choices": ["auto", "web", "telegram"]}` |
| `operator_name` | `{"value": str}` |
| `voice_mode` | `{"value": str, "choices": ["on", "off"]}`: `VOICE_MODE`, `"off"` when its `config.env` has none |
| `voice_effort` | `{"value": str, "choices": [str] or null}`: `VOICE_REASONING_EFFORT`, the reasoning effort on spoken turns. `""` (also when not set) means its `effort`. Choices are `""` and the effort choices; null when those are |

Model choices come from `opconfig.model_choices()`. Codex, Grok and Antigravity use their cached provider
listings, restricted by their `SHAFT_*_MODELS` settings when nonempty. Hidden Codex models are omitted.
Antigravity offers only Gemini bases with low, medium and high variants. Stale model lists return immediately
while one background refresh runs per provider in the process; without cached data the first read fetches inline.
A current model absent from that list is appended with its
model name as its label. Effort choices are the intersection of the instance's own `shaft/config.py` CHOICES
(parsed as text without importing or executing it) and the selected model's levels, ordered low, medium, high,
xhigh, max, ultra. The supervisor reads Codex levels from app-server `model/list` and OpenRouter levels from
`GET /api/v1/models`. These live listings are cached in `effort_catalog.json` under the supervisor state directory
for `SHAFT_EFFORT_CATALOG_HOURS` (default 24 hours). A failed refresh retains cached data; without cached data the
levels are unknown. Bonsai, Claude, Antigravity and Grok use provider tables probed on 2026-11-13. The supervisor
never offers none or minimal. Unknown choices are null; a model that accepts no levels has an empty list.
Wake `effective` comes from `autonomy.wake_interval`,
including its floor and invalid-value fallback; zero means no scheduled wakes. `value` is the configured interval,
or the scheduler default when missing or invalid.

`POST /settings/<n>` takes exactly `{"field": str, "value": <new value>, "expect": <displayed value>}`.
One field per request. Both values have the setting's type; booleans are not numbers. A missing field, extra field,
unknown field, wrong type or non-finite number gives `400 bad_request`. Domain writers validate allowed values.
The response includes `notes`, an empty list when there are none. A model switch resets incompatible efforts in
the same write and reports each reset in `notes`. The fresh settings include the new model's effort choices.

| `field` | Writer | `applies` |
|---|---|---|
| `model` | `opconfig.set_values`, with `expect` covering `LLM_MODEL`; couples `LLM_BASE_URL`, `LLM_REASONING_EFFORT`, `VOICE_REASONING_EFFORT` | `next run` |
| `effort` | `opconfig.set_values`; null unsets the key | `next run` |
| `wake_interval` | `opconfig.set_values`; the scheduler uses it on its next read | `now` |
| `cloud_cap` | `core.limit_value` and `core._update_tool_settings` with `expect`; null removes the override | `now` |
| `channel` | `channel.set_preference` with `expect`; only `auto`, `web`, `telegram` | `now` |
| `operator_name` | `opconfig.set_values` | `next run` |
| `voice_mode` | `opconfig.set_values`; only `on` or `off`, else `409 refused` | `now` |
| `voice_effort` | `opconfig.set_values`; only `""` or an effort choice, else `409 refused` | `next run` |

Cloud-cap null means the per-instance override is off, matching `shaft limits ... off`. The configured supervisor
default cap still applies. It does not mean unlimited cloud spending.

A stale `expect` gives `409 stale` with `settings` inside `error`, containing the fresh settings. A writer refusal
gives `409 refused` with its message. Config saves retain the domain writer's compare-and-set. Tool writers compare and atomically replace under the tools-file lock, shared with CLI and web writes.

Pause and resume share the web action's command path. A pause applies now; a running cycle stops at its next turn.
Their bodies may be empty.

### 15.2 Services

Each service has `name: str`, `running: bool`, `pid: int or null`, `started: str or null`, `commit: str or null`,
`version: str or null`, and `busy: str`. The row's `version` is the service record's git describe, null when the service
is not running or has no record. The top-level `version: str` is the version of the web service serving `/app/v1`.
It is `"unknown — restart to record"` when that service has no record.
The service record supplies its process and version fields, with timestamps as in §1.6. `busy` is `actions.service_guard(name, "restart")` verbatim, empty when free. API replies hold the run lock and are included.
Only the scheduler also has `drain`, either null or `{"state": str, "waiting": [str], "deadline": str or null}`.
A pending restart request appears as `draining` before the first drain tick only when its PID matches the running scheduler. Leftover requests for dead processes are ignored.

API restart refuses with `409 busy` when its guard is nonempty. There is no force option. Otherwise it runs
`api restart` with `actions.SLOW_S`, as the web service action does for an installed API: it restarts the service in the
mode it runs in (never `tailscale serve`, no login or key change, so a local-only Mac needs no Tailscale), and is
refused while an instance is replying (`409 refused`, "<n> is replying in a conversation right now: try again when that
reply finishes"); a conversation that is open but quiet holds nothing back. Scheduler restart runs `autonomy restart`.
A pending drain is returned without requesting it again, preserving its deadline. Cancel runs
`autonomy restart --cancel`. A second restart while the same service's command runs gives `409 restarting`. The web service action shares this lock.
A failed command gives `409 refused` with the command's message. Successful commands return the fresh service list;
the app confirms an actual restart by polling for a changed PID. A tracked `drained` state keeps polling for up to 90 seconds after first observation; timeout is unconfirmed and a later refresh can still confirm the new PID. An untracked old `drained` record is ignored.

Only `api` and `scheduler` can be restarted here. Other service names, including `web`, `telegram`, `model` and
`embed`, give `404 not_found`. Only the scheduler has a cancel route. Restart and cancel bodies may be empty.

## 16. Cycles

Three read-only routes use the same `supervisor.cycles` models as the web Cycles page. They need a paired bearer
(§1.2). Each returns `200` with an `ETag`; a matching `If-None-Match` returns `304` with an empty body.
An unknown instance returns `404 not_found`. Missing cost and duration stay `null`.
Live `doing`, tool arguments, tool results, and the web-only `where` field do not enter these responses.

### 16.1 `GET /app/v1/plan/<n>`

| Field | Type | Meaning |
|---|---|---|
| `now` | object | `{state, since, goal}`, described below |
| `check` | object | `{next, skipped, paused}`, described below |
| `next_step` | object or null | Latest next action: `{at, action, revise_if}`, described below |
| `unfinished` | list of str | What that cycle left unfinished |
| `commitments` | list | Promises to others, sorted by due date: `{id, promise, due, status}`, described below |
| `commitments_error` | str | Empty when read, otherwise why the ledger could not be read |
| `queue` | object | Described below |
| `projects` | object | The instance's projects (`memory/projects.jsonl`), described below |
| `goals` | str | The instance's goals Markdown |
| `goals_error` | str | Empty when read, `"no goals file"` when missing, otherwise why it could not be read |
| `improvement` | object or null | Latest Sleep or Reflection improvement plan: `{at, text}`, described below |
| `improvement_error` | str | Empty when read or missing, otherwise `"could not read memory/journal.md: <reason>"` |

`now.state`, `now.since`, and `now.goal` are strings or null. `since` is the time-of-day text supplied by live state
(for example, `"18:06"`), sent as given.
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

Projects fields (`supervisor.projects.panel`; an app may get no `projects` from an older server):

| Field | Type | Meaning |
|---|---|---|
| `open` | int | Open projects |
| `error` | str | Empty when read or missing, otherwise `"could not read memory/projects.jsonl: <reason>"`, with no rows |
| `rows` | list | Open projects first, soonest deadline first; then closed ones, newest close first |
| `rows[].id`, `name`, `success` | str | Its id, its name, and what "done" means for it |
| `rows[].status` | str | `"open"`, `"done"`, `"abandoned"` or `"failed"` |
| `rows[].opened_at`, `deadline` | str | Offset timestamps (§1.6) |
| `rows[].health` | str or null | Open: `"on track"`, `"at risk: <boundaries>"` or `"past boundary: <boundaries>"`, as in the stats head; closed: null |
| `rows[].hours_left` | number or null | Open: hours to the deadline (negative once past); closed: null |
| `rows[].calls`, `usd` | number or null | Open: measured use over its cycles; `usd` is null for a plan-billed model. Closed: null |
| `rows[].budget_calls`, `budget_usd` | number or null | Its budgets; null is unbounded |
| `rows[].evidence`, `closed_at` | str or null | Closed: the evidence it gave and the offset timestamp of the close; open: null |

### 16.2 `GET /app/v1/cycles/<n>?before=&filter=`

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

### 16.3 `GET /app/v1/cycles/<n>/<ts>`

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
Structured doubts join `step` and `what` as `"step: <step>; what: <what>"`.
Other note shapes become strings, as on the web page.
Each judgment contains `verdict`, `claim_supported`, `first_error`, `reasoning`, `model`, and `error`.
`claim_supported` is a bool or null, and `first_error` is a sentence string or null. The remaining fields are strings
or null, except `error` is always a string.
A failed judge can have a null verdict and a nonempty error. The instance's claim remains separate.

`left` contains `unfinished` (list of strings), `next_action` (the recorded object or null), and `next_goal`
(string or null). A next action normally contains `action` and `revise_if` text.

## 17. Voices

Each instance speaks with its own voice, one per language (`"en"`, `"it"`). The voices are kept by the supervisor
(`supervisor/voices.py`, `.supervisor/voices.json`), not in the instance's `config.env`, so the iPhone and the Mac use
the same one. Two instances never hold the same voice for a language. The instance proposes its own voice with the
broker tool `propose_voice`; the operator accepts or rejects the proposal here, or picks a voice, which overrides it.
Every route answers `409 refused` with the reason (`voices.Refused`), for example an unknown language, a voice no
device reported, or a voice another instance holds.

| Method | Route under `/app/v1` | Request (JSON) | Success |
|---|---|---|---|
| GET | `/voices/<n>` | | 200, the voices object below |
| POST | `/voices/<n>` | `{"language", "identifier"}`: pick that voice (`by: "operator"`); or `{"language", "clear": true}`: no voice for that language | 200, the voices object |
| POST | `/voices/<n>/proposals/<id>` | `{"accept": bool, "reason": str}` (`reason` optional): the instance is told either way, with the reason on a rejection | 200, the voices object |
| POST | `/voices/report` | `{"platform": "ios" or "macos", "voices": [{identifier, name, language, quality}]}`: the device's installed voices (`AVSpeechSynthesisVoice.speechVoices()`), replacing that platform's list | 200, `{"available": …}` |

Any other shape: `400 bad_request`. `/voices/report` is matched before `/voices/<n>`, so an instance named `report`
cannot have its voices read here.

The voices object:

| Field | Type | |
|---|---|---|
| `voices` | object | `{"en": voice or null, "it": voice or null}`. A voice: `{identifier, name, by, ts}`, `by` `"operator"` or `"instance"` (a proposal accepted), `ts` a timestamp |
| `proposals` | list | the instance's proposals: `{id, language, identifier, name, why, ts, state}`, `state` `"open"` until decided |
| `available` | object | `{platform: [voice]}`: what each platform reported, `{identifier, name, language, quality}` (`language` BCP-47, `quality` `"default"`, `"enhanced"` or `"premium"`) |

The app reports its voices at launch and when the voice settings open. When the chosen voice is not installed on
the device, the app says so on screen and speaks with the system voice for that language.

## 18. Setup

The Mac app's setup wizard creates an instance, stores the provider keys, switches the provider, grants access and
asks the new instance for its welcome. The routes need the bearer like every other route (§1.2), on the tailnet or
on loopback; the wizard uses loopback (`shaft web pair --local`). The routes that decide where model calls go or what
holds a key — `/setup/secret`, `/setup/provider`, `/setup/provider/check`, `/setup/telegram` and
`/setup/telegram/service` — answer only loopback: from the tailnet they are `403 local_only` ("Do this on the Mac that
runs SHAFT") and run nothing, so a device paired over the tailnet cannot point every instance's model traffic at an
endpoint of its choosing (`SHAFT_CUSTOM_BASE_URL` is also terminal-only on the web Settings page). Each state change
runs the CLI verb an operator would
type (`supervisor/actions.py`: `run_json` for the verbs that answer in JSON, `run_now` for the others), logged as
`app <device name>: operator ran shaft …`, so every verb's own checks and refusals apply. A key or a token goes to the
verb's **stdin**, never into its arguments, a log line or an answer.

| Method | Route under `/app/v1` | Request (JSON) | Success |
|---|---|---|---|
| POST | `/setup/instance` | `{name, operator, model, drive, autonomy}` | 200, `{"instance", "done", "paused"}` (§18.1) |
| GET | `/setup/drives` | | 200, `{"drives": [preset]}` (§18.2) |
| POST | `/setup/secret` | `{name, value}` | 200, `{"said": "<NAME> saved"}` (§18.3) |
| GET | `/setup/secrets` | | 200, `{"names": [str]}` (§18.3.1) |
| POST | `/setup/provider` | `{provider, model?, judge_model?, base_url?, price_in?, price_out?, judge_price_in?, judge_price_out?, vision?}` | 200, the coverage (§18.4) |
| POST | `/setup/provider/check` | `{provider}` | 200, one row per model (§18.4) |
| POST | `/setup/telegram` | `{name, token}` | 200, `{"said": <the verb's line>}` (§18.5) |
| GET | `/setup/telegram/status/<n>` | | 200, the Telegram status (§18.5) |
| POST | `/setup/telegram/pair/<n>` | (empty) | 200, the status with `code` and `minutes` (§18.5) |
| POST | `/setup/telegram/service` | (empty) | 200, `{"said": <the verb's line>}` (§18.5) |
| GET | `/access/<n>` | | 200, the access object (§18.6) |
| POST | `/access/<n>` | `{grant, action, alias?, what?, all?, writable?}` | 200, the fresh access object (§18.6) |
| GET | `/google/<alias>/labels` | | 200, `{"alias", "labels": [str]}` (§18.7) |
| GET | `/google/<alias>/calendars` | | 200, `{"alias", "calendars": [{id, title, primary, access}]}` (§18.7) |
| POST | `/setup/hello/<n>` | (empty) | 200, `{"said", "chat": {"id", "title"}}` (§18.8) |
| POST | `/setup/autonomy/<n>` | `{then}` | 200, `{"instance", "then"}` (§18.9) |

A missing or extra field, or a field of the wrong type: `400 bad_request`, and nothing runs. A verb that refuses:
`409 refused` with the verb's own words. The app shows those words as they are, so every verb a route runs says what
to do in the app, never a terminal command: a `--json` verb, and any verb run for an app (`SHAFT_APP_WORDING`,
`actions._run`), uses the app's wording (`core.say`); the terminal keeps its own.

### 18.1 `POST /setup/instance`

Runs, in order, as the device:

1. `new <name> --json --paused-until-first-reply resume|stay` (`autonomy` `"own"` → `resume`, `"talk"` → `stay`), with
   `actions.SLOW_S`. The instance is created paused; its first-reply marker says what the operator's first answered
   reply does (`resume`: the scheduler takes it from there; `stay`: it stays paused, talking only when talked to).
2. `config <name> set OPERATOR_NAME=<operator> LLM_MODEL=<model>`.
3. `drive <name> set --file <temporary file> --expect-sha <sha>`, where `<sha>` is that of the `drive.md` `shaft new`
   left (the template's): a drive.md changed meanwhile is refused. When `drive` is the template's text already,
   nothing is written. The temporary file (`.supervisor/web/drive-*.md`) is removed either way.

It does not send the opening: that is `/setup/hello/<n>`, the wizard's last step, once access is granted.

`name`, `operator` and `model` are one non-empty line each (`name` at most 31 characters; `shaft new` checks the
rest: lowercase letters, digits and `-`, not `operator`, not taken). `drive` is the drive's text, at most 20000
characters. `autonomy` is `"own"` or `"talk"`.

Success: `{"instance": str, "done": ["new", "config", "drive"], "paused": bool}`. A refused step answers
`409 refused` with that verb's message and `done`, the steps already completed, inside `error`:

```json
{"error": {"code": "refused", "message": "refused: LLM_MODEL: no such model", "done": ["new"]}}
```

An instance whose `new` succeeded stays created (and paused); the app shows the message and lets the operator fix the
setting and send the same request again, or `shaft delete <name>`. A request naming an instance that exists and still
has its first-reply marker (a setup made it, and nobody has answered it yet: a later step was refused, or the app quit
before it heard the answer) skips `new` and runs the remaining steps, `done` then listing all three; its first-reply
choice is left as it is (§18.9 changes it). Any other existing name is `new`'s own refusal ("already exists").

### 18.2 `GET /setup/drives`

`{"drives": [{id, title, text, suggests}]}`, four presets from `supervisor/drive_presets.py`, in this order:

| `id` | `title` | `suggests` |
|---|---|---|
| `assistant` | Personal assistant | `mail`, `calendar`, `drive` |
| `research` | Research partner | `web`, `drive` |
| `builder` | Builder | `web`, `folder` |
| `improve` | Just improve yourself | (none) |

`text` is a whole `drive.md`, addressed to the instance; the app shows it editable and sends the edited text with
`/setup/instance`. The `improve` text is the template's own `drive.md`, read when asked (`409 refused` when it cannot
be read). `suggests` names the access rows (§18.6) the Access step pre-ticks: `folder`, `browser`, `web` (web search:
its key through `/setup/secret`), `telegram`, `mail`, `calendar`, `drive`, `chat`.

### 18.3 `POST /setup/secret`

`{"name": str, "value": str}`: `secrets set <NAME> --json`, the value (one line) on stdin. The allowed names are
`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `SCRAPEBADGER_API_KEY`, `SHAFT_CUSTOM_API_KEY`, and `HF_TOKEN`.
Other names get `400 bad_request` with the list of allowed names. The route excludes the server's own credentials
(`SHAFT_API_KEY`, `SHAFT_INBOX_KEY`, `SHAFT_API_LOGINS`), Google clients, and Telegram tokens.
An empty or multiline value gets `400 bad_request`. The answer never carries the value.

#### 18.3.1 `GET /setup/secrets`

Runs `secrets list --json` on loopback only. A tailnet device gets `403 local_only`. The response contains only
names from §18.3 that are set, in that section's order. A verb refusal gets `409 refused`.

#### 18.3.2 Server keys (Mac)

The Mac app's Settings → Server keys lists §18.3's names as Set or Not set and saves a new value with §18.3. Both routes
answer only loopback, so the section needs the server on this Mac, not `~/SHAFT`. A loopback pairing shows it at once. A
pairing over the tailnet shows it when `/version` names `local_port` and `local_id` (§19) and
`http://127.0.0.1:<local_port>/app/v1/local`, asked without a bearer, answers the same `local_id`: the app then sends
both routes there with its bearer. The bearer never goes to that port before the ids match, so another process that
took it (the server being on another Mac) never receives it. A server on another Mac, or this Mac's with
`SHAFT_WEB_LOCAL` off, shows no section; the app asks again each time Settings opens.

### 18.4 `POST /setup/provider` and `/setup/provider/check`

`/setup/provider` runs `provider use <provider> --json` (`openrouter`, `codex` or `custom`) with `actions.SLOW_S`; each
optional field becomes its flag: `model` `--model`, `judge_model` `--judge-model`, `base_url` `--base-url` (strings);
`price_in`, `price_out`, `judge_price_in`, `judge_price_out` (numbers ≥ 0, dollars per million tokens); `vision`
(`true` adds `--vision`). The verb decides which fields a provider takes (`409 refused` with its words, e.g. "custom
needs --model"). Keys are set first with `/setup/secret`. Success:

| Field | Type | |
|---|---|---|
| `provider` | str | the provider |
| `model` | str | the instance model it suggests: a new instance's `LLM_MODEL` (`--model` when given; else OpenRouter's first instance model, or Codex's default) |
| `changed` | [str] | the settings it changed |
| `covers` | [str] | what the provider now runs, of `instances`, `judge`, `judge2`, `coder`, `consult`, `describe`, `recap`, `trial` (`supervisor/provider.py` `FUNCTIONS`) |
| `off` | [{`function`, `why`}] | what stays off with this provider, and why |
| `not_live` | [str] | the running services (`api`, `web`, `scheduler`, `telegram`) that still hold another value of a setting the preset wrote (changed now or by an earlier save): the wizard restarts each (`api restart --json`, `web restart --json`, `autonomy restart`, `telegram restart`, as local processes, web last), waits for the web service to answer again, and only then runs Test |

`/setup/provider/check` (`{"provider"}` alone) runs `provider check <provider> --json` with `2 × actions.SLOW_S`: one
minimal live call per configured model of the provider; a `custom/` model the instances run on is also asked once as a
stream, as the broker asks it (an endpoint that answers a stream request with one `application/json` reply is read as
one complete chunk). `{"provider", "ok": bool, "models": [{ok, model, error}]}`:
`ok` is true when every model answered; a model that failed is a row with the provider's own error, not a refusal.
A check that could not run at all is `409 refused`.

### 18.5 Telegram: `/setup/telegram`, its service and its pairing

A bot works once three things hold: its token is saved, the Telegram service runs (restarted after a new bot, so it
reads it), and the operator's Telegram account is paired with SHAFT's bots (one account for every bot). The wizard's
Telegram row does all three, in that order.

`POST /setup/telegram` `{"name": <instance>, "token": <bot token>}`: `telegram token <name>` with the token on stdin
(the verb asks Telegram who the bot is before it saves it). An unknown instance: `404 not_found`. A refused token: `409
refused` with the verb's line ("not saved: Telegram refused the token (…)"). Success: `{"said": <the verb's last
line>}`.

`POST /setup/telegram/service` (empty body): `telegram on` with `actions.SLOW_S`, which installs the Telegram service
or restarts it. Refused (no bot yet, launchd failing): `409 refused` with the verb's line. Success: `{"said"}`.

`GET /setup/telegram/status/<n>`, read in process (`setup.telegram_status`), never a token or a code:

| Field | Type | |
|---|---|---|
| `instance` | str | |
| `token` | bool | `<n>` has a bot token |
| `bot` | str or null | its bot's username, when it has a token and Telegram told it |
| `paired` | bool | an account is paired with SHAFT's bots |
| `account` | str or null | that account's username (or id) |
| `service` | bool | the Telegram service runs: it has recorded itself, which it does after asking Telegram about every bot (so `telegram on` answering is not yet this) |
| `problem` | str or null | with a bot and no running service: why, in launchd's words (not installed, not loaded, its state and last exit code) |
| `pairing` | bool | a pairing code is out and not expired |

`POST /setup/telegram/pair/<n>` (empty body): with no account paired, `telegram pair --json` issues a one-time code
(`telegram.PAIR_MINUTES`, 10 minutes) and the answer is the status plus `code` and `minutes`; the operator sends
`/pair <code>` to `<n>`'s bot from their account, and the running service pairs it. The wizard first waits (up to
60 s, asking every 2 s) for `service`, then asks every 2 s until `paired`, and records Telegram done only when both
hold. With an account paired already (also one that paired between the route's look and the verb), no code is
issued: `code` and `minutes` are null; the app has no Telegram unpair (the terminal's `shaft telegram unpair`). No bot for `<n>`
yet: `409 refused`. An unknown instance: `404 not_found`.

### 18.6 Access

`GET /access/<n>` answers what the operator has granted `<n>`, all fields present (`supervisor/setup.access`, from
`web_config.access`):

| Field | Type | |
|---|---|---|
| `instance` | str | |
| `folder` | str or null | the folder it may read (`shaft share`) |
| `browser` | `{installed: str or null, run: bool}` | the shared Chromium's Playwright version (null: not installed), and whether its `run` tool is on — it browses from inside `run` |
| `web_search` | bool | `SCRAPEBADGER_API_KEY` is set |
| `telegram` | bool | it has a Telegram bot |
| `mail` | [{`account`, `label`, `draft`: bool}] | Gmail labels; `draft`: it may leave drafts there |
| `calendar` | [{`account`, `calendar`, `name`, `write`: bool}] | calendars by id, with their title when granted |
| `drive` | [{`account`, `reach`}] | `reach` `own` or `all` |
| `chat` | [{`account`}] | Google Chat accounts |
| `meetings` | [{`account`}] (optional) | Accounts whose ready meeting transcripts the instance may read. Omitted when there are no meeting grants. |
| `claude_notes` | bool | its inbox takes notes from the operator's Claude sessions (`shaft claude-notes`, integrations.md "Notes from your Claude sessions") |
| `google_accounts` | [{`alias`, `address`, `scopes`: [str]}] | the operator's Google logins, with the scopes they granted (`gmail.readonly`, `calendar.readonly`, …) |

`POST /access/<n>` takes `{"grant", "action": "grant"|"revoke", …}` and runs the verb, then answers the fresh access
object:

| `grant` | Fields | Verb |
|---|---|---|
| `folder` | `what` (a path; grant only) | `share <n> <what>`; revoke: `share <n> off` |
| `browser` | | `browser install` (only when none is installed, held up to 15 minutes), then `tools <n> on run` when `run` is off. Revoke is `409 refused`: the browser is shared, and switching `run` off would also stop the instance running its own code |
| `mail` | `alias`, `what` (label), `writable` | `mail <n> grant|revoke <alias> <what>`; `writable`: `grant-draft`/`revoke-draft` (revoking the draft keeps the read) |
| `calendar` | `alias`, `what` (`primary`, a title or an id), `writable` | `calendar <n> grant|revoke <alias> <what>`; a writable grant first resolves `what` to the calendar's id once (`calendar.grant_id`; unresolvable: `409 refused`, nothing granted), then runs `grant` and `grant-write` with that id, so a concurrent grant on the same account never receives this request's write; `writable` revoke: `revoke-write` |
| `drive` | `alias`, `all` | `gdrive <n> grant <alias> [--all]` / `revoke <alias>` |
| `chat` | `alias` | `gchat <n> grant|revoke <alias>` |
| `meetings` | `alias` | `meetings grant <n> <alias>` or `meetings revoke <n> <alias>` |
| `claude` | | `claude-notes <n> on`; revoke: `claude-notes <n> off` |

`alias` is a Google account alias (`[a-z][a-z0-9-]{0,30}`); `all` and `writable` are booleans. A field another grant
takes, or a missing one: `400 bad_request`. Web search and Telegram have no grant here: their key and token go
through `/setup/secret` and `/setup/telegram`.

### 18.7 `GET /google/<alias>/labels` and `/calendars`

What a mail or calendar grant may name, read live from Google with the operator's account (`mail.labels`,
`calendar.calendars`). `labels`: the label names, sorted. `calendars`: `{id, title, primary, access}`, primary first.
An account that is not logged in, or whose login lacks `gmail.readonly` / `calendar.readonly`, is `409 refused` with
the command that fixes it; Google's own error (`Google answered HTTP …`) is `409 refused` too.

The account itself is not made through a route: the Mac app's setup wizard runs `shaft google client google --json`
(the Desktop app client's JSON on stdin) and `shaft google login google <address> --client google --json` as local
processes of the installed server, since the consent's loopback listener lives in that process for the minutes it
takes (docs/integrations.md, "Google accounts").

### 18.8 `POST /setup/hello/<n>`

The wizard's last step: the setup's hidden opening to `<n>`'s Main conversation (`web_chat.opening`), as the device's
login. Its text (`setup.opening_text`) says who the operator is (`OPERATOR_NAME`), the instance's `drive.md`, what it
has been granted (§18.6, without any key's value), and asks it to introduce itself briefly and ask two or three
questions to start. The operator never sees it in the feed; the instance's reply is the first thing in Main, and the
operator's first answered reply resumes the instance or leaves it paused, as `autonomy` said (§18.1). Success:
`{"said": "<n> is writing its welcome", "chat": {"id": <Main's id>, "title": <n>}}`. Asked again once the opening
was sent (the app's Try again after it lost the answer), nothing is asked twice: `{"said": "<n> was asked for its welcome
already", "chat"}`, as long as the operator has not written in Main. Once they have: `409 already_answered` ("The
welcome is already in your chat with <n>.", with `chat`), which the wizard treats as done (it opens the chat). Main
holding a conversation that did not start with the opening: `409 refused` ("… has a conversation already").

### 18.9 `POST /setup/autonomy/<n>`

`{"then": "resume"|"stay"}`: `first-reply <n> <then> --json`, the wizard's "How it works alone" choice. The wizard
creates the instance when it leaves "Your instance" (with `autonomy` `"own"`, so the Access step has an instance to
grant to), and this route changes what the operator's first answered reply does: `resume` (the scheduler takes it from
there) or `stay` (it stays paused, talking only when talked to). The verb rewrites `.supervisor/first-reply-<n>` in
place (temporary file, then a rename over it), keeping when it was made, under a lock across processes
(`core.first_reply_lock`) that the web service's first reply also holds while it reads the marker, acts on it and
removes it: the reply applies the choice written last, and a rewrite never lands after the marker was consumed. When
there is none (the first reply has happened already, or `<n>` was not created waiting for one) it is `409 refused` in
the verb's words and nothing is written. An unknown instance: `404 not_found`. Success: `{"instance": <n>, "then": <then>}`.

## 19. Version and compatibility

### `GET /app/v1/version`

`{"server": str, "api": int}`, plus `"project": str` on loopback only (§1.2: the Mac app on the same Mac; a tailnet
client never gets a path on the Mac), and `"local_port": int` and `"local_id": str` to every caller while
`SHAFT_WEB_LOCAL` is on: the port of the loopback path and a random id the web process draws at start, which
`GET /app/v1/local` (loopback only, no bearer, 404 over the tailnet) answers too. A Mac app paired over the tailnet
compares the two before it sends its bearer to that port, and so reaches the loopback-only routes on its own Mac
(§18.3.2). `server` is the release the web process serving `/app/v1` runs, as git describes
it (`v0.60.0`, or `v0.60.0-3-gabc1234` past a tag): its own service record when launchd runs it, else the checkout's.
`project` is the canonical path of the checkout it runs from.
`api` is `app_api.API_VERSION`, bumped on every breaking change to `/app/v1` (a field removed or retyped, a route
removed or moved, a meaning changed); an added field or route is not a break. It needs the paired bearer, like every
route but `/pair`.

The app declares the lowest `api` it works with (SHAFTKit `ServerCompatibility.minimumServerAPI`, 0) and asks this once
per session; a server older than this route (`404 not_found`) is api 0, so every existing install keeps working. The
Mac setup wizard needs api 1 (`ServerCompatibility.setupServerAPI`: its routes, §18, came with it) and asks once per
wizard. A server whose `api` is lower than what is needed gets "This app needs a newer SHAFT server" in place of the
session (or the wizard's step), with the Mac app's Update server action, instead of calls failing one by one;
Check again asks once more, as coming back to the front does. Any other failure (the server unreachable, a 500) does
not block: the calls report it themselves.

### Update server (Mac)

The Mac app offers it only when it is paired over loopback, `~/SHAFT` is installed, and `/version`'s `project` is that
same checkout (symlinks resolved). A server that runs from another checkout gets a line saying which, and that it is
updated from there; one that does not say (`project` absent) gets nothing offered. Settings → Services then runs
`shaft deploy --check --json` with the installed server's own Python (a local process, not this API):
`{"ok": true, "current": str, "latest": "vX.Y.Z" or null, "template_tag": bool, "incomplete": "vX.Y.Z" or null,
"incomplete_step": str or null}`. `latest` is the newest `vX.Y.Z` on origin that also has `template-vX.Y.Z` and is
newer than `current` (git describe of the primary checkout); `template_tag` is false while origin has a newer release
still without its template tag. `incomplete` is a release a deploy moved the checkout to without finishing (a step
failed, or services waited), from `.supervisor/deploy-state.json`, with the first step it did not finish: the app then
offers "Finish updating to vX.Y.Z", which runs `shaft deploy <incomplete> --json` again (the steps already done are
skipped, and origin is not asked again), also after a relaunch. The app remembers the checkout the server was last
seen to run from (its own settings: a path, not a secret). When `/version` does not answer (a deploy may have left the
web service down) and that remembered checkout is `~/SHAFT`, it runs `shaft deploy --check --local --json` (no origin;
`latest` and `template_tag` null) and offers only "Finish updating" for a deploy left unfinished there; anything else,
or no such record, offers nothing and says why. With `latest`, it offers "Update server to vX.Y.Z" (and the release's page) and runs
`shaft deploy <latest> --json`, showing each step
line `{"step", "ok", "detail"[, "error"]}` as it arrives (docs/deploy.md has the steps). The services line names what it
restarted (`"restarted": [str]`); restarting the web service drops the app's connection, so it then shows
"Reconnecting…" until `/version` answers again. A services step that answers
`"waiting": true` (a conversation is open) shows its reason and a Retry that runs
`shaft deploy <tag> --json --services-only`. The iPhone and iPad apps have no Update server: they say to update from
the Mac.

## 20. Meetings

Every route uses the paired-device bearer (§1.2). The supervisor stores meeting files on the Mac that runs SHAFT.
See [meeting storage and CLI](meetings.md).

| Method | Route under `/app/v1` | Request | Success |
|---|---|---|---|
| POST | `/meetings` | `{title?, mode, started, ended?, device, ext, sidecar?, account?}` | 201, `{id, chunk_size, received: []}` (§20.1) |
| PUT | `/meetings/<id>/chunk/<index>` | Raw audio bytes | 200, `{received}` (§20.2) |
| GET | `/meetings/<id>/upload` | | 200, `{received, chunk_size}` (§20.2) |
| POST | `/meetings/<id>/finish` | `{size, sha256, chunks}` | 200, meeting metadata (§20.3) |
| GET | `/meetings` | | 200, `{meetings: [metadata]}`, newest first (§20.4) |
| GET | `/meetings/<id>` | | 200, meeting metadata (§20.4) |
| POST | `/meetings/<id>` | `{account?, title?}` | 200, updated meeting metadata (§20.4) |
| GET | `/meetings/proposals` | | 200, `{proposals: [...]}` (§20.6) |
| POST | `/meetings/proposals` | `{id, approve}` | 200, decided proposal (§20.6) |
| POST | `/meetings/<id>/reprocess` | | 200, queued meeting metadata (§20.4) |
| DELETE | `/meetings/<id>` | Optional `?forget_voices=1` | 200, `{id, deleted: true}` (§20.4) |
| GET | `/meetings/<id>/clip` | `?speaker=<label>&n=0` | 200, `audio/wav` (§20.5) |
| POST | `/meetings/<id>/speakers` | `{label, name}` or `{label, merge_into}` | 200, speaker entry with `label` (§20.5) |
| GET | `/speakers` | | 200, `{people: [{id, name, created, count}]}` (§20.5) |
| POST | `/speakers/<pid>/rename` | `{name}` | 200, `{id, name, created}` (§20.5) |
| DELETE | `/speakers/<pid>` | Optional `?anonymise=1` | 200, `{id, deleted: true}` (§20.5) |

### 20.1 Create

The server assigns an ID of the form `m-YYYYMMDD-HHMM-<4 lowercase hex digits>` using its local time. `mode` is
`online` or `inperson`. `device` is a non-empty string. `ext` is `m4a`, `caf`, or `wav`. `started` is an ISO 8601
date and time. `ended` uses the same format and defaults to `null`. `title` defaults to an empty string. `account`
is a non-empty account alias or `null` for an unfiled meeting.

`sidecar` defaults to `{}` and must be a JSON object. The server stores its fields as supplied, including nested
objects. Its compact UTF-8 JSON representation can occupy up to 65,536 bytes. Larger sidecars return `413 too_large`.

### 20.2 Upload and resume

`index` is a zero-based chunk number. A chunk request uses `Content-Type: application/octet-stream` and a
`Content-Length` header. Send chunks in any order. Repeating an index replaces the previous chunk after the new body
arrives in full. `received` lists saved indices in ascending order. After a connection drops, fetch
`/meetings/<id>/upload` to see which chunks need sending. The server accepts chunk writes in state `uploading` and
returns `409 refused` in later states.

### 20.3 Finish

`size` is the total byte count. `sha256` is the 64-digit hexadecimal digest of the complete audio. `chunks` is the
number of chunks. The server joins indices 0 through `chunks - 1` in order. A missing chunk or a size or digest
mismatch returns `409 refused` with the reason. The meeting stays `uploading` and keeps its chunks.

On success, the server stores the assembled audio, queues the meeting, and deletes the upload directory. If the
finish response is lost, repeating the same finish request returns the current metadata.

### 20.4 Metadata and deletion

Metadata contains `id`, `title`, `created`, `started`, `ended`, `mode`, `device`, `ext`, `channels`, `account`,
`state`, `error`, `size`, `sha256`, `chunk_size`, `chunks_received`, and `processed_with`.
Timestamps follow §1.6. `channels` comes from `sidecar.channels` when present. `size` and `sha256` start as `null`.
`error` and `processed_with` start as `null` and can be set by the processing worker.
The state is one of `uploading`, `queued`, `processing`, `ready`, or `failed`.
Recovered meetings also have a `worker_crashes` count.

When the state is `ready`, `GET /meetings/<id>` also includes `transcript` with `meeting`, `segments`,
`gaps`, `speakers`, `processed_with`, `page`, and `total_segments`. A segment has `start`, `end`,
`channel`, `speaker`, `lang`, `text`, and `words`. Each word has `w`, `start`, and `end`. Times are
seconds from the start of the recording. Channel 1 is the microphone and channel 2 is system audio.

Use `?page=N` to fetch up to 200 segments at a time. The default is page 1. A page past the end has an
empty `segments` array. An invalid page number returns `400 bad_request`.

`POST /meetings/<id>` updates at least one of `account` and `title`. The other field keeps its current value.
`account` must be a configured Google account alias or `null` to unfile the meeting. `title` must be a string.
Changing the account on a ready meeting sends a watch note to eligible instances that have yet to receive it.

`POST /meetings/<id>/reprocess` queues a ready or failed meeting and clears its error and crash count.
Other states return `409 refused`.

Delete removes the meeting, including its audio or any saved chunks. Use `forget_voices=1` to also remove
gallery samples learned from it. The flag defaults to `0`. Routes for an unknown meeting return `404 not_found`.

### 20.5 Speakers

`transcript.speakers` maps labels to entries with `channel` and `seconds`. New transcripts also have `clips`,
an array of up to three `[start, end]` ranges in seconds. Named entries also have `person` (a person ID) and
`name`. Anonymised entries have a generic `name`.

Clip playback returns mono 16 kHz PCM WAV, up to 10 seconds from the label's channel. `n` is a zero-based index
and defaults to `0`. A missing label or clip returns `404 not_found`. Clip playback and label edits require a
ready meeting.

Naming a label confirms its voiceprint for the gallery. Names are matched case-insensitively. Merging labels
moves the source label's segments to the destination and keeps the destination's name, channel, and clips.
Both operations update the saved transcript. Merges apply to the current transcript. Reprocessing creates
labels again from the audio and matches them against the current gallery.

Person IDs have the form `p-<8 lowercase hex digits>`. `count` is the number of confirmed voiceprints. Rename
updates the gallery and saved transcripts. Forget removes the person's gallery voiceprints and records a
tombstone. With `anonymise=1`, it also replaces saved names with labels such as `Speaker S2`. The flag defaults
to `0`.

### 20.6 Speaker proposals

`GET /meetings/proposals` lists pending proposals newest first. Each row includes `id`, `instance`, `meeting`,
`speaker`, `name`, `why`, `created`, and `state`. The name and reason come from the proposing instance.

`POST /meetings/proposals` takes the proposal's `id` and a boolean `approve`. Approval enrols the speaker
voiceprint and returns state `confirmed`. Rejection returns state `rejected`. An unknown ID returns
`404 not_found`. Confirming after reprocessing or reversing a decision returns `409 refused`.

## Appendix A. Implementation notes

These Python server refactor notes describe internal implementation, not additional wire fields.

### A.1 `web_chat.feed(name: str, cid: str | None = None, whole: bool = False) -> dict`

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

### A.2 The HTML views on the model

- `web_views.chat_feed(name, chat, sent, archived=False, whole=False)` keeps its signature (the existing web tests stay
  unchanged) and renders `web_chat.build(...)` (`web_views.feed_html(model)`): every `data-k` it writes is an item's
  `key`. Its HTML is byte-identical to what it was before the model.
- `web_views.live_activity(chat)` renders the model's `reply` (and, once it ended, the last answer's `activity`).
- `_reply_failure`'s sentences and choices move into the model (`failure`, §4.4); the HTML renders them.
- `_overwritten` splits into `web_views.overwritten(saved, sha, text) -> dict | None` (the §10.2 object) and its HTML.
- `chat_header`'s "what a message does now" becomes `web_views.busy_note(name, state) -> str | None`, used by the header
  and `/app/v1/instances`.
- The HTML ETags stay as they are.

### A.3 `supervisor/app_api.py`

- `route(req: web.Request) -> web.Response`: the entry point. `web._route` calls it for every path under `/app/v1/`
  after the login and host checks and before the cookie check; it does its own bearer check (§1.2) and answers every
  path under `/app/v1/` (an unknown one: `404 not_found`). It catches everything and answers `500 internal` with the
  error, logged, never an empty body.
- `device(req) -> str | None`: the paired device of the bearer (refreshing `last_seen`).
- `stamp(ts: str | float | None) -> str | None` (§1.6) and `to_json(model: dict) -> dict` (stamps and URLs, Appendix A.1).
- `error(status: int, code: str, message: str, **extra) -> web.Response`.
- The long-poll (§4): a loop on `web_chat.feed` every 0.5 s up to `wait`, refreshing `web_chat.watching` every 2 s only when `at` is supplied;
  without `at`, only the initial watching mark is made, so a later `/away` stays effective;
  each held request is one thread of the web server's `ThreadingHTTPServer`.
- The stream (§6.1): `web_chat.Relay` keeps the reply's events (`emit`, `events_after`, `close_stream`) under a
  `threading.Condition`; the relay thread appends each `shaft_event` as its chunk arrives, and `answer` and `end` once
  the final turn is saved. `web_chat.stream_of(cid)` is the chat's latest relay, kept `STREAM_KEEP_S` after it ended.
  The stream query accepts optional `cid` to select a thread; absent, it follows the current thread.
  `app_api._stream` answers a `web.Response` with `stream` (an iterator of bytes) set. `web.Handler._pour` writes each
  piece and flushes it on the request's own thread, with no `Content-Length` and `Connection: close`. After each piece
  it checks whether the client closed the socket (readable with nothing to read), then closes the iterator. A
  keepalive comment every `KEEPALIVE_S` (2 s) makes that check happen, and refreshes the watching mark, even while the
  reply is quiet. `tailscale serve` passes `text/event-stream` on unbuffered (Go's reverse proxy flushes a response
  without a length at once). `app_api.held(n)` counts the streams held now.
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
  deletion); `docs.Refused` for a bad path or folder. `docs.raw(name, path)` reads a document up to `SHAFT_FILE_READ_MAX_MB`; exceeding it currently becomes `500 internal`.
- The push routes import `supervisor/apns.py` directly; `apns.configured()` reports missing settings or a key file.
- `web.unpair` also calls `apns.unregister(device)` and `web_seen.forget(device)`.
- Pairing reuses `web._use_pairing` and `web._token_from_link`; the device record gains `"platform"`.

### A.4 `supervisor/web_seen.py`

- `seen(device: str, name: str, version: str, cid: str | None = None) -> bool`: True when `version` is the selected feed's (`web_chat.feed` or `web_chat.from_feed`);
  then it records `{version, last, last_count, at}` for the operator, instance and feed. False otherwise, recording nothing (the route
  answers `409 stale_version`).
- `unread(device: str, summaries: dict | None = None) -> dict[str, bool]`: per instance (§12); `summaries` supplies the instance names when the caller already has them (`/app/v1/instances`);
  unread always reads each feed's own last stamp and count.
- `badge(device: str) -> int`: how many are unread. The APNs lane calls it for each push.
- `forget(device: str) -> None`: migrates old marks and retains shared marks.
- The `device` argument remains for caller compatibility; storage and unread comparisons are shared.
- The store `.supervisor/web/seen.json`, written atomically (mode 600) under a lock, as `web._write` does.

### A.5 Tests and fixtures

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
  `feedback-response`, `retried`, `plan`, `cycles`, `cycle`, `stream` (the events of §6.1, as a list).
  `supervisor/tests/test_app_api_setup.py` writes the setup ones (§18) the same way (`write_or_check_fixtures`):
  `setup-instance`, `setup-drives`, `setup-provider`, `setup-check`, `setup-autonomy`, `setup-hello`, `setup-errors`, `access`,
  `google-labels`, `google-calendars`; `supervisor/tests/test_app_api_settings.py` writes `version` (§19). SHAFTKit reads only this folder, or
  `SHAFT_APP_API_FIXTURES` when set; it has no copied contract fixtures. Their times are stamped in
  `Europe/Rome` (the test sets `TZ`), so they carry `+01:00`. `errors` maps each code to `{status, body}`.
