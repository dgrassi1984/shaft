# SHAFT — Plan: a web UI to manage the instances

*Drafted and signed off 2026-10-19 (decisions in §6). M0–M5 built and merged the same day (§4). "My call" marks a choice I made that you did not state.*

## 0. Why, and where it stands

What the operator asked for, 2026-10-19:
- "a webUI to manage instances etc. It should have the same (and more) functionalities as the TUI";
- "configure each instance's configs as well";
- chat with an instance in the browser;
- "The WebUI chat should also support attachments, images, etc".

Today (read in the code, 2026-10-19):

**`shaft top`** (`top.py`, `top_views.py`, rich in `.venv-ui`) is the control panel:
- 10 tabs: overview, instance, judge, trials, benchmarks, changes, messages, costs, services, transcripts.
- Data: `dashboard.snapshot()` and `panel.py`, both stdlib only, both reusing the supervisor's own readers.
- Most actions run a CLI subcommand through `top_views.run_now` / `run_detached`, logged as `top: operator …`.
- Two actions run inside the TUI instead:
  - recap: `recap.start` (`top.py:233`) calls a model from threads, guarded only within that process
    (`recap._running`);
  - dismiss alerts: `panel.dismiss` (`top.py:237`).

**Chat** goes through the API (`api.py`):
- `/v1/chat/completions` and `/v1/chat/interject`, on 127.0.0.1, exposed with `tailscale serve`.
- Requires a bearer key and a login in `SHAFT_API_LOGINS`.
- A conversation is identified by a hash of the instance name and the first user message (`api.py:76`).
- Only the activity streams (`reasoning_content`). The answer is held until the step ends and sent whole, because
  text before a tool call is narration, not the answer (`api.py:390-436`).
- Telegram is a second front end. It calls the local API with the key (`telegram.py:482`).

**Attachments:**
- A file the operator sends a bot is saved with `messages.store_attachments` in
  `instances/<name>/attachments/from-operator/<time>/` (at most `ATTACH_MAX_BYTES`, 20 MB a file). The message
  gets a line "[The operator attached, saved in my folder:] <path>" (`telegram.py:455`).
- The instance sees an image natively, if its model accepts images, or with `describe_image` (PLAN-vision.md).
- Files an instance sends the operator with `send_message` are listed in that message's `attachments`. Telegram
  forwards them after the text (`telegram.py:610`).

**Config:**
- `config.env` has two writers: the operator, directly, and the instance through `set_config`
  (`broker.py:1186`, a read-then-write with no lock).
- The runner commits the operator's edits on their own, before a run's commit (`runner._commit_outside_config`).
  `shaft update` does the same under `run_lock(wait=False)` (`__main__.py:187`).
- `drive.md` has one writer, the operator (`runner._commit_outside_drive`).
- The operator's settings (tools, limits, jobs, share, mail and calendar grants, battery, pause) are in
  `.supervisor/` and are set only by CLI subcommands.
- Nothing edits `config.env` or `drive.md` except a text editor.

## 1. Design decisions

### The web UI adds no authority

As with the TUI:
- Every read goes through `dashboard` / `panel`, so the web, the TUI and the CLI cannot disagree.
- Every action and every config write runs a CLI subcommand, so it inherits that command's refusals.
- `run_now`, `run_detached` and `service_guard` move from `top_views.py` to a shared `supervisor/actions.py`,
  with the log prefix as a parameter (`top:` / `web:`). The TUI keeps its behaviour and its tests.
- The two in-process TUI actions get CLI verbs, used by both UIs:
  - `shaft recap`: its in-flight guard becomes a file lock, so the TUI and the web never pay for the same recap
    twice;
  - `shaft alerts [dismiss ID…]`.

### Config edits go through new CLI verbs

`shaft config <name>` lists the keys, their values and the comment block above each key in that instance's own
`config.env`.

`shaft config <name> set KEY=VALUE [KEY=VALUE …] [--expect KEY=OLD …]` and `unset KEY [--expect …]`:
- Several keys go in one write and one commit, so a model switch never runs half-applied (`LLM_MODEL` together
  with `LLM_BASE_URL`).
- Validated as `set_config` validates (`CONFIG_KEY`; one line, at most 500 characters, no `"`).
- Written with `config_with` on the file as it is now, so every other line and comment is kept.
- The changed line gets a fresh inline comment, `# the operator, <date>`. Its old inline comment is dropped; git keeps
  it. *(My call: a stale comment on a changed value is a lie.)*
- `--expect` refuses if a value is no longer OLD, and says what it is now.
- **A lock makes `--expect` real.** The broker's `set_config` and the CLI verb take the same lock on that
  instance's `config.env` (`.supervisor/locks/config-<name>.lock`) for their read-modify-write. Without it, an
  operator write landing inside `set_config`'s read and write loses the instance's change silently.
  *(My call. It touches the broker, but it only serialises: the instance's path is unchanged.)*

`shaft drive <name> set --file F --expect-sha SHA` is the same compare-and-set for `drive.md`.

Both verbs commit at once when the instance is free: `run_lock(wait=False)`, then `_commit_outside_config` /
`_commit_outside_drive`, as `cmd_update` does. When it is busy, the edit stays in the file and the run commits it
as the operator's before its own commit, as today.

### Chat goes through the local API, like Telegram

- The web server calls `/v1/chat/completions` (stream) and `/v1/chat/interject` on 127.0.0.1 with
  `SHAFT_API_KEY`, passing the operator's `Tailscale-User-Login`. The key stays in the server process; the
  browser never sees it.
- This reuses the API's reply loop: `Live` (interjections), the narration rule, waiting while busy.
- Two small API changes:
  - **An explicit conversation id.** An optional `X-Shaft-Conversation` header keys `Conversations` instead of the
    first-message hash. Two web chats that both open with "hi", or a web chat and a Telegram chat, are then never
    merged. Without the header, the API behaves as today.
  - **`POST /v1/chat/end`** (by conversation id or by messages) marks the conversation written and calls
    `spawn_recover`. "End" writes it up now instead of after `SHAFT_IDLE_MINUTES`.
- What the page shows while a reply runs:
  - the activity streaming in a collapsible strip;
  - the answer appearing whole when the step ends (the narration rule), not token by token.
- The web server owns each chat's message list, in `.supervisor/web/chats/<id>.json`. The browser only renders
  it, so a reload or a second device shows the same chat. Sending from two devices at once queues the second
  message as an interjection, as the API does.
- **Attachments:**
  - Pick, drag and drop, or paste into the message box; at most `ATTACH_MAX_BYTES` a file.
  - Telegram's `receive` becomes one helper in `messages.py` (store under `attachments/from-operator/<time>/`, return
    the "[The operator attached, saved in my folder:]" lines). Telegram and the web both use it, so the instance gets a
    web attachment exactly as it gets a Telegram one, images included (native or `describe_image`).
  - Images show as thumbnails in the chat.
  - Files the instance sends the operator (`send_message` to the operator) while a chat is open also appear in that
    chat, after its text, as on Telegram. They appear in Messages too.
- **Serving files back** (`/files/…`):
  - Only a path that a message's `attachments` names, resolving inside `instances/` or the operator inbox's
    `attachments/`, with no link below that folder. *(Corrected in M1: files instances send you are kept in
    `.supervisor/operator-inbox/attachments/`, not in the instance's folder.)*
    `instances/<name>/` with no links followed.
  - Images go inline, with the type sniffed from the bytes (JPEG, PNG, GIF, WebP). Everything else, SVG and HTML
    included, goes as a download (`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`). A file an
    instance wrote never runs as a page.

### The server

- `shaft web serve`: a stdlib `ThreadingHTTPServer` on 127.0.0.1, at `SHAFT_WEB_PORT`, default **8785**. 8782 is
  the embed service, which every run may reach.
- **Stdlib only, run with the system Python like the other launchd services.** That follows the rule at
  `core.py:20` that launchd jobs must not depend on a venv. *(My call: no Jinja2 and nothing to pin.)*
- Pages are built by Python functions over a small `Html` type that escapes every value unless it is already
  `Html`. Instance-written text is everywhere, and this page changes configs: it must never run as markup.
- Markdown (`stats.md`, journal, `drive.md`) is rendered by a small safe subset: escape first, then headings,
  lists, emphasis, code and `http(s)` links only.
- No HTMX (changed while building M0): `web_static/web.js`, ~100 lines of plain JS, polls fragments and posts
  forms with the CSRF header. Nothing third-party to download or vendor, and none of HTMX's swap pitfalls.
- Live parts poll their own fragment (`data-poll`, every 2 s) only while the browser tab is visible. A failed
  refresh is shown above the stale content, never silently.
  loads one. Polling stops while the browser tab is hidden.
- A launchd service `web` (`com.shaft.web`), in all four service lists: `autonomy.SERVICES`,
  `dashboard.SERVICES`, `panel.SERVICE_LOGS` and `top_views.SERVICE_NAMES`.
- `shaft web on|off|status`: the service, plus `tailscale serve` on `SHAFT_WEB_HTTPS_PORT` (default 8444),
  tailnet only, never Funnel.

### Security

- **Who may use it:** your tailnet login *and* a paired-device cookie (§6, decision 2).
- **Instances cannot reach it.** A run's sandbox allows only listed localhost ports (`runner.py:438`), and egress
  refuses non-public addresses. A test pins that the web port is never in a run's allowed ports.
- **Forged requests.** Every POST needs:
  - `Sec-Fetch-Site: same-origin`, or an `Origin` equal to the served host;
  - a `Host` equal to the tailnet name (no DNS rebinding);
  - a per-session CSRF token header.

  Anything else gets 403.
- **CSP:** `default-src 'self'`, no inline script, `frame-ancestors 'none'`.
- **What stays CLI-only, as in the TUI:**
  - secret values: secret *names* may be listed, as `shaft secret <name> list` does;
  - `new`, `breed`, `finetune`, the Google logins and `mind cutover`.

### Guards

Every action keeps the TUI's guards:
- a confirmation that states the exact effect, and the cost where there is one (spend today against the cap);
- long actions run detached, with output in `.supervisor/web-actions.log`, and outlive the browser;
- the scheduler restart is refused while a scheduled run is in progress, and `update` is refused on uncommitted
  edits, from the same functions.

### Layout

- One left nav: the instances, then the global pages.
- It works at phone width (375 px); tables become stacked rows. *(My call.)*

## 2. Pages

| Page | Shows (same source as) | Actions |
|---|---|---|
| Overview | Every instance's state, now-doing, goal, calls, tokens, cost, judged rate, schedule, trial; services; events; spend vs caps; alerts (`dashboard.snapshot`, `panel.alerts`, `panel.costs`). Live. | cycle, pause/resume, sleep now, update, write, reply, recap all, dismiss alerts |
| Instance › Summary | `panel.instance_detail`: `stats.md`, config line, tools off, shared folder, journal headings, probation, kept changes, version | same instance actions; open chat |
| Instance › Chat | The open conversation: activity live, answer when ready, attachments both ways, busy state; past conversations read-only (transcripts, thinking on/off) | send (with files), interject while replying, stop, end |
| Instance › Config | `config.env` as a form, one field per key, with the comment block above it as help. `LLM_MODEL` is a dropdown (`SHAFT_CLOUD_MODELS` + self-hosted + local) that sets `LLM_BASE_URL` with it. Other keys are text fields, validated as `set_config` validates. Keys outside the template, plus a free key/value row. The instance's own recent `set_config` changes (git log of `config.env`). | save (`shaft config set --expect`), unset |
| Instance › Drive | `drive.md` rendered and as text | save, with a diff first (`shaft drive set --expect-sha`) |
| Instance › Access | Operator settings: supervisor tools on/off, wake floor and cloud cap, jobs on/off and the job list, shared folder, mail label grants, calendar grants, battery on/off, secret *names* | each through its CLI verb: `tools`, `limits`, `jobs`, `share`, `mail`, `calendar`, `battery on/off` |
| Instance › Judge | `panel.judgments`, a sparkline of achieved difficulty (inline SVG), full verdict on click | — |
| Instance › Changes | `panel.changes`, `panel.probation_events`, template merges | — |
| Instance › Journal | Journal entries (`core.journal_entries`), newest first, rendered | — |
| Instance › Activity | Egress connections (`.supervisor/egress/<name>.jsonl`), plugins (name, description from `plugin.json`), jobs and their last runs, browser profile, mind status | — |
| Trials | `panel.trials`, progress, report on click, caps | queue an A/A calibration trial (with cost) |
| Benchmarks | `panel.battery_status`, `panel.evals`, `panel.surprise_summary` | run the battery now (when installed, with cost) |
| Messages | `messages.history` with attachments (images inline, other files as downloads), filter by instance | write, reply |
| Costs | `panel.costs`: today and 7 days, per component and instance, against caps, judge by purpose | — |
| Services | `panel.services_detail` (now with `web`), log tail, autonomy on/off | start/restart, stop (guarded), autonomy on/off |

"More than the TUI": Chat with attachments, Config, Drive, Access, Journal, Activity, and images in Messages.

## 3. Milestones

| M | Deliverable | Acceptance |
|---|---|---|
| M0 | `supervisor/web.py`: server, auth, same-origin + Host + CSRF on POST, CSP, the `Html` escaping type, `web.js`, layout and nav, live Overview (read-only). `shaft web serve`, the `web` service in all four lists, `on/off/status` with `tailscale serve`. | Tests: a request without an allowed identity → 403; a cross-site POST, or a wrong `Host` → 403; instance text with `<script>` and `<img onerror>` renders escaped; the web port is not in a run's allowed ports. Live: opened at the tailnet URL in a browser; the overview's numbers equal `shaft top --once` at the same moment; it renders at 375 px. |
| M1 | Read-only pages: Instance Summary, Judge, Changes, Journal, Activity, Trials, Benchmarks, Messages (with attachments served under the `/files` rules), Costs, Services, past conversations. | A test per page: the same values as its TUI tab on the fixtures of `test_control_panel.py`. `/files` refuses a path no message names, a link, a path outside the instance; an `.svg` or `.html` is a download, never inline. Live: every page clicked through on the real system, no empty page where the TUI shows data; an image an instance sent shows inline. |
| M2 | `actions.py` (TUI moved onto it, its tests unchanged); `shaft recap` and `shaft alerts dismiss`; actions at parity with the TUI, each with a confirmation stating effect and cost. | Each action tested with the subprocess mocked, asserting the exact CLI command, and refusing where the CLI refuses. All of `test_control_panel.py` passes. Two recaps started at once (TUI and web) make one model call. Live: pause then resume one instance; a scheduler restart refused while a cycle runs. |
| M3 | `shaft config` and `shaft drive` verbs with the shared `config.env` lock; Config, Drive and Access pages. | Tests: `set` keeps every other line and comment byte for byte; a multi-key set is one write and one commit; `--expect` refuses a stale value and names the current one; a `set_config` and a `shaft config set` racing on one file both survive; an edit while busy is committed by the run as the operator's and is not undone by the gate when the run's change fails probation (the example-c 2026-10-13 case); an edit while idle is committed at once and `shaft update` is not blocked after it. Live: switch one instance's `LLM_REASONING_EFFORT` from the page, see it in `git log` as the operator's, see the next run use it, set it back. |
| M4 | Chat: `X-Shaft-Conversation` and `/v1/chat/end` in the API; the shared attachment helper (Telegram moved onto it, its tests unchanged); the chat page with activity, answer, interject, stop, end, busy state, attachments both ways; the reply keeps going when the browser drops (§6). | Tests: two chats opening with the same text get two talk ids; without the header the API behaves as today (`test_api.py` unchanged); `/v1/chat/end` writes up exactly that conversation; a web upload lands where a Telegram one does, with the same lines; an oversized file is refused before anything is written. Live: from the browser, a short conversation with one instance including a photo, which it describes correctly; an interjection it acknowledges; a file it sends back that shows in the chat; End, and its journal entry written. |
| M5 | README section, this plan's status, code map. | README describes `shaft web on`, the URL, pairing (if chosen), what is CLI-only and why. |

Verification on the real system is in a browser (Claude's built-in browser pane), clicked through and read back,
not a curl call.

## 4. Status

**M0 built (2026-10-19), on branch `claude/webui`, not merged and not installed.**

What was built:
- `supervisor/web.py`: the server, auth, pairing, CSRF/Host/same-origin checks and CSP;
- `supervisor/web_pages.py`: the `Html` escaping and the overview;
- `supervisor/web_static/`: CSS and JS;
- `shaft web on|off|status|pair|unpair|serve`, and the `web` service in every service list;
- `panel.extras`, `dashboard.clean` and `dashboard.event_tone`, shared with the TUI.

Tests: `test_web.py`, 31 tests. Each guard was checked by removing it: the tests fail without escaping, without
the CSRF check, without the Host check, and when a pairing link can be used twice. The full suite passes.

Driven live, from the worktree on the real `.supervisor/` and `instances/`, through `tailscale serve` on :8444:
- Tailscale passes the login and the tailnet Host. An unpaired browser is refused with the pairing hint.
- Pairing works in two browsers: the built-in pane and Chrome.
- The overview equals `shaft top --once` taken one second apart: spend, the five alerts, states, next wakes,
  recaps, messages, events.
- The 2 s refresh works. A stopped server shows "Not refreshed: HTTP 502 — the web service is not answering" over
  the stale content, and the note clears once the server is back.
- No console errors.
- Tested at 411 px, Chrome's smallest window: no horizontal scroll, one card per row.
- A POST with the page's token passes; one without it, or with a wrong one, is refused (403) and logged.

Found while driving it:
- **The built-in browser pane blocks every subresource of the tailnet origin** (`ERR_BLOCKED_BY_CLIENT`), so pages
  load there without CSS or JS. That is the pane's own policy; Chrome, Safari and phones are not affected.
- Fixed: cards now fill a wide screen (auto-fit), the pending dot has a space after it, and a 502 says what it
  means.

Afterwards: the worktree server was stopped and `tailscale serve --https=8444` removed. The two paired browsers
(`claude-pane`, `chrome`) stay in `.supervisor/web/devices.json` (`shaft web unpair --all` revokes them).

**M1 built (2026-10-19), same branch, not merged.**

What was built:
- `supervisor/web_views.py`: every read-only page;
- in `web.py`: the routes, `/files` and `activity()`.

Pages:
- instance summary (live state refreshed), judge with sparklines and each verdict in full, changes, benchmarks,
  transcripts (filters, each in full, thinking on demand), journal (paged), activity (egress, plugins, jobs, Python,
  browser, mind);
- trials and each report, messages (filter, each in full, text attachments inline, images inline), costs, services
  (live, with each log).

Tests: `test_web_pages.py`, 27 tests. They use the fixtures of `test_control_panel` and assert, for each page, the
strings that file asserts for its TUI tab. They also pin the `/files` rules: a link, `..`, an absolute path or another
`.supervisor/` file are refused; an SVG or an HTML file named `.png` is a download; an image is inline, its type
judged by its bytes. Each guard was checked by removing it. The full suite passes: 813 tests.

Fixed on the way:
- `test_control_panel.DETAIL` lacked `browser` and `jobs`, so `test_every_tab_renders_its_data` had been failing
  since the browser merge. It runs only under `.venv-ui`; it passes again.
- Under `.venv-ui` the branch fails exactly what `main` fails: the sandboxed tests, which cannot run under a venv in
  the home folder.

Driven live in Chrome, on the real data:
- every page of all four instances returns 200, plus the global pages and fragments;
- each page looked at;
- a real PNG attachment shown inline;
- at 411 px, tables stack into labelled rows with no horizontal scroll.

Changed while driving it:
- An instance's open questions list the oldest 3, as the TUI does, then link to Messages.
- List cells are cut at 240 characters and two lines; the full text is one click away. The longest pages were
  0.5 MB of single-row text.

**M2 built (2026-10-19), same branch, not merged.**

What was built:
- `supervisor/actions.py`: `run_now`, `run_detached`, `service_guard`, the trial cost estimate and the shared
  confirmation texts. The TUI is moved onto it; its tests pass unchanged.
- New CLI verbs for what the TUI did in-process:
  - `shaft cycle <name>` (`autonomy.start_wake`);
  - `shaft recap [names]`, whose in-flight guard is now a per-instance file lock across processes;
  - `shaft alerts [dismiss ID…]`.
- `supervisor/web_actions.py`: every TUI action, through `POST /action/<verb>`. Each one shows its confirmation
  text, its cost where there is one, and why it can't run now. Guards are checked again when the POST arrives.

The actions:
- cycle, pause/resume, sleep and update, on the overview cards and the instance page;
- write and reply on the instance page, outside the refreshed part, so a half-typed message survives;
- tool switches;
- recap, and dismissing alerts;
- an A/A trial per instance, the battery;
- start, restart and stop for each service, and autonomy on/off. The web UI refuses to stop or restart itself.

Tests: `test_web_actions.py`, 33 tests. Each action is checked for the exact CLI command, logged as `web`, and for
each refusal. Breaking any guard, or the logging, makes a test fail. 849 tests pass; the TUI suite passes under
`.venv-ui`.

Integration checks covered pause/resume logging and service guards:
- While cycles run, scheduler restart, stop and autonomy-off identify the runs they would interrupt.
  The model, embedding and API services are guarded while in use. The later design offers a confirmed
  **Force …** action that names the affected runs and logs the choice.
- A POST to stop the web service is refused (409).
- Not driven live, as in the TUI's M3: send, reply, sleep, update, cycle, battery, trial, service restarts. Each
  would act on a live instance or service; their exact commands are pinned by the tests.

Found while driving it:
1. **Fixed — an M1 bug: the instance page showed its journal headings as raw `<ul><li>…`.** `Html + Html` went
   through `str.__add__`, which returned a plain `str` that was then escaped again. `Html` now keeps its type when
   added to, escaping only the text side. A new test sweeps every page for escaped markup of its own; it fails on
   the old code. The M1 test had only checked that the words appeared.
2. **Fixed — one instance's update check raising took the whole overview down.** The trigger was `version.instance`
   raising `SystemExit` for an instance the worktree's git does not know, an artefact of driving it from a worktree.
   That check now disables only that instance's Update button, with the reason. Every page handler also catches
   `SystemExit`, which `core.instance_root` raises for an unknown instance.
3. **Open — "service model is not running" alerts while the model server is idle.** The Bonsai server exits after
   16 idle minutes by design and starts on demand, but `panel.alerts` raises an alert whenever it is down. The TUI
   shows the same alert. This predates the web UI; not changed here.

**M3 built (2026-10-19), same branch, not merged.**

What was built:
- `supervisor/opconfig.py`, behind `shaft config <name> [set KEY=VALUE… | unset KEY…] [--expect KEY=VALUE]
  [--expect-unset KEY]` and `shaft drive <name> [set --file F | remove] --expect-sha SHA`:
  - one write and one commit per save; a model switch sets its `LLM_BASE_URL` with it;
  - validated as `set_config` validates (the check is now `core.config_set_line`, shared with the broker), plus
    the model list, the efforts the instance's own `shaft/config.py` accepts (read as text) and whole numbers;
  - committed as the operator's at once when the instance is free, by its run otherwise.
- `core.config_lock`: one writer of a `config.env` at a time, taken by the broker's `set_config`, the gate's
  `revert_config` and `shaft config`.
- `supervisor/web_config.py`:
  - **Config**: one field per key; the model and effort as choices; secrets masked, no form; add a key; the
    `git log` of the file.
  - **Drive**: rendered and editable, saved after its diff.
  - **Access**: tool switches (moved here from the summary), wake floor and cloud cap, jobs, shared folder,
    mail and calendar grants, the battery, key names only.

Tests:
- `test_opconfig.py`, 16 tests. They cover:
  - only the named line changes, byte for byte;
  - a stale value is refused and named;
  - everything `set_config` refuses, plus secrets;
  - the race: an operator edit landing inside `set_config` survives;
  - through the real cycle harness of `test_config_writers`, an edit while example-c runs is committed by its run as
    the operator's and survives the gate reverting a failed change (the example-c 2026-10-13 case); an edit while it is
    free is committed at once and leaves `shaft update` unblocked.
- `test_web_config.py`, 11 tests: the exact CLI command per save, no secret value on any page, the drive diff.
- Removing the broker's lock, the stale check or the model/base-URL pairing makes a test fail.

Driven live, through the tailnet URL in Chrome:
- example-c's effort went from medium to low on the Config page. example-c's branch got
  `a561b81 SHAFT supervisor: example-c: config.env changed from outside the instance … medium -> low`, and its worktree
  stayed clean.
- A second save from the same, now stale, page was refused: "it is now 'low' (you saw 'medium')".
- Reloaded and set back to medium: `7341da6`. The net change is the line's new comment, `# the operator, 2026-10-19`.
- Not observed: a run using the new effort. The effort only changes the local model's chat template, example-c runs on
  Opus and is paused, and no record shows the effort a run used.
- Drive: the diff of an edit was previewed, and the save was declined, so example-b's `drive.md` is untouched.
- Access viewed.

Found and fixed while building and driving it:
1. **Blank form fields were dropped** (`parse_qs`). A field the page showed unset lost its stale-edit check.
2. **The supervisor run from a linked worktree could not find any instance.** `_worktree_admin_dir` read
   `PROJECT/.git/worktrees`, but there `.git` is a file naming the main repository. It now follows that file;
   a test pins it.
3. **A save whose commit failed was reported as refused**, though the file was written. It now says "written,
   not committed (why)".
4. **`MAX_TOKENS_*` and `LLM_CONTEXT_TOKENS` were masked as secrets.** Secret names are now `*_KEY`,
   `*_TOKEN`, `SECRET` or `PASSWORD`; a test lists both kinds.
5. **The drive diff preview was cut at 300 characters** (the toast's limit). It is shown whole now.

**M4 built (2026-10-19), same branch, not merged.**

What was built:
- **API:**
  - an optional `X-Shaft-Conversation` header keys a conversation by its client's name. Without it, the API
    behaves as before: all of `test_api.py` passes unchanged.
  - `POST /v1/chat/end`: 202 writes it up now, 404 if none is open, 409 while it replies.
  - the stream's last chunk carries `interjections_heard`.
- **`messages.from_operator`:** Telegram and the web store the operator's files the same way, with the same
  "[The operator attached, saved in my folder:]" lines.
- **`supervisor/web_chat.py`**, a relay thread per reply in the web server:
  - it saves the activity as it streams and the answer when it ends, so a reply outlives the browser;
  - Stop closes the API connection;
  - a message sent mid-reply is an interjection, and one heard too late gets a reply of its own;
  - End goes to `/v1/chat/end`;
  - at startup, a reply the previous server was relaying is marked interrupted.
- **The chat page:** the conversation, files both ways, the live activity, and Stop / End. The composer takes
  pasted, dropped or picked files; Enter sends.

Tests:
- `test_web_chat.py`, 20 tests, against a fake streaming API: the relay with no browser watching, history,
  interjections (heard, too late, refused), Stop reaching the API, End, the reaper, files in as Telegram's, an
  oversized file refused before anything is written, multipart, the routes, escaping, `/chatfiles` rules, an
  older API.
- `test_api.py`: 5 new tests.

Driven live in Chrome, with example-b, against today's API service (`main`'s code):
- A message with an image drawn in the page went through the composer. The image was saved in
  `attachments/from-operator/…`, the API paused example-b's cycle for the reply, and example-b answered "The number 42 in
  white on a teal square." — correct.
- A message sent mid-reply was passed in as an interjection ("example-b reads it at its next step").
- Asked again as a normal message, example-b sent `webui-check.txt` with `send_message`. The file shows in the chat
  before its reply, "sent".
- The upload's thumbnail renders (240×240).
- End is refused, rightly: today's API has no `/v1/chat/end`. Its idle write-up closes the conversation instead.

Not observed live, because it needs the branch's API running: End writing up at once, and the follow-up reply to an
interjection heard too late. Both are pinned by the tests; check them once the API is restarted on the merged code.

Found and fixed while building and driving it:
1. **Stop did nothing.** The API answers HTTP/1.0, so `http.client` gives the socket to the response and sets
   `conn.sock` to `None`. The relay now keeps the socket. Found by its test.
2. **A message sent as a reply ended got no reply.** It is now counted as heard-too-late, so a reply follows.
   Found by its test.
3. **Against an API older than the web UI**, End would have read "no route" as "already written up", and every
   interjection would have triggered a second reply. Both are now handled; tests pin them.
4. **An upload recorded with an absolute path** (`store_attachments`' fallback) was not served. It is now, inside
   the same folders only, with both spellings (`/var` and `/private/var`) matched and links still refused.

**M5 done (2026-10-19): merged into main on the operator's word ("Merge so I can run it properly").**

- The README documents `shaft web` and the new verbs (`cycle`, `recap`, `alerts`, `config`, `drive`), plus the
  API's `X-Shaft-Conversation` and `/v1/chat/end`. The code map is regenerated.
- `main`'s on-demand model server (86b2d72) is merged in first. Its "idle, not down" state is shown by the web UI
  as by the TUI: dimmed, never NOT RUNNING. A test pins it.
- Nothing is installed or restarted by the merge. To run it: `shaft web on`, `shaft web pair --name …`. Restart the
  API on this code (`shaft api on`) for chat's End and its interjection follow-ups.

## 5. Left to the builder

- Page styling: system fonts, the system's light/dark colours.
- How the chat keeps its scroll while activity streams.
- How the page shows the "waiting: busy" state the API already sends.

## 6. Decisions

1. **A reply keeps going when the browser drops** (a reload, or the phone locking the tab). The web server keeps
   the upstream API stream open and saves the reply into the chat; you see it when you come back. Only "Stop"
   stops a reply. M4 tests this: the page closed mid-reply, then reopened, shows the finished answer.
2. **A request is yours when it carries both:**
   - your tailnet login in `SHAFT_API_LOGINS`;
   - a paired-device cookie. `shaft web pair` prints a one-time link that you open once per browser. It sets a
     long-lived HttpOnly, Secure, SameSite=Strict cookie, and only its hash is stored in `.supervisor/web/`.
     `shaft web unpair [device|--all]` revokes it.

   This is M0. Its acceptance gains: a valid login without the cookie → 403; a used or expired pairing link →
   403.
3. Nothing in the TUI to change ("Nothing in particular"): the web UI keeps its behaviour, and the TUI stays.
