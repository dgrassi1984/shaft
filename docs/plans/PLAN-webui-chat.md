# SHAFT — Plan: a chat-centric web UI, live documents, supervisor settings

*Drafted and signed off 2026-10-19 (§7). Built the same day: C0, C1, D0, D1, D2 on `claude/webui-chat` (§8) and
S0, S1 on `claude/nervous-meitner-3c6951` (§4), both merged into main on the operator's word. Follows PLAN-webui.md (M0–M5,
merged). "My call" marks a choice I made that you did not state.*

## 0. What you asked

1. "Add support for MD artifacts (markdown) in the chat in the web UI, so that the user can collaborate with the
   agent live."
2. "Select part of a message and 'reply' to it, which adds it, quoted, in the message." **Built today** (§5).
3. "Rethink the UI a bit so that it is chat-centric … All the rest is something that gets used only from time to
   time."
4. "It is not clear to me where in the web UI I can configure which models are used [the judge] … the
   supervisor-level config should also be available here."
5. "Drag-and-drop for files, … and anything else that comes to mind to improve the chat experience."

What the code says today:
- **Judge models.** There is nowhere to configure them, in the web UI or anywhere else that lasts.
  - `SHAFT_JUDGE_MODELS` and ~45 other supervisor settings are `knob()`s: an environment variable or a built-in
    default, read once when a process starts (`core.py:39`).
  - The launchd services are started with only `PATH` in their environment (`autonomy.py:75`). Every service
    therefore runs on the defaults. Setting a knob in a shell affects only CLI commands run from that shell.
- **Drag and drop already works, but only onto the composer box.** Dropping a file anywhere else on the page
  opens it in the browser instead. To you that reads as "no drag and drop".
- **The chat is one tab of ten under each instance.** The landing page is the Overview.

## 1. Chat-centric layout (request 3)

- **`/` opens the chat**, with the instance you last talked to. The Overview moves to `/overview`. *(My call: the
  last-used instance, remembered by a cookie, falling back to the first instance.)*
- **Three columns on a wide screen:**
  - **Left, a rail of instances.** Each shows:
    - a state dot: idle / replying / in a cycle / paused;
    - an unread mark when a reply landed while you were elsewhere;
    - its last line.

    Clicking one switches the chat. Below the instances there is one small "Manage" group: Overview, Messages,
    Trials, Benchmarks, Costs, Services, Settings. Alerts show as a count on Overview.
  - **Middle, the chat**, full height. The composer is pinned to the bottom.
  - **Right, the documents panel** (§3), collapsible. It is hidden when the instance has no documents.
- **Instance management is moved out of the way.** The instance's own pages (Summary, Config, Drive, Access, Judge,
  Changes, Journal, Activity, Transcripts) sit behind an "⋯ example-b" menu in the chat header, not a tab bar above
  it.
  - The header itself shows only the essentials: model, state, and today's spend against its cap.
  - The quick actions (cycle, pause) go in that menu too.
- **At phone width:**
  - the chat is full screen;
  - the instance switcher is a dropdown in the header;
  - documents are a second tab next to the chat;
  - Manage sits in the same menu.
- Every existing page keeps its URL and its content. Only the frame around them changes, so the TUI-parity tests
  stay as they are.

## 2. A better chat (request 5)

- **Drop anywhere.** A file dropped anywhere on the chat page attaches to the message. The page shows a "Drop to
  attach" overlay while you drag. Dropping elsewhere never navigates away from the chat.
- **Attachment chips before sending.** Each picked, pasted or dropped file shows as a chip (a thumbnail for an
  image) with × to remove it. Today it is a comma-separated list of names.
- **Your message shows at once,** marked "sending", instead of after the next poll. If the send fails, the text and
  the files stay in the composer.
- **The composer grows with its text.** The draft is kept per instance in the browser, so it survives a reload.
  *(Browser storage: a convenience only. The chat itself stays on the server.)*
- **The live reply shows how long it has been going** ("replying · 0:42"), and what it is waiting for, as now.
- **Copy and Reply icons:** on each operator and instance message with text, using its shown Markdown source.
  Reply quotes the whole message into the composer; selection replies share the same quoting behavior.
  Code blocks and documents use the same clipboard icon.
- **"↓ New reply" button** when you have scrolled up and something new arrives. Today the page jumps only if you
  were already at the bottom.
- **The browser tab title says "● example-b replied"** when a reply lands while the tab is hidden.
- **Desktop notifications.** Opt in with one click: a reply that lands while the tab is hidden notifies you.
  *(My call: opt-in, never asked unprompted.)*
- **Keyboard shortcuts:**
  - Enter sends (as now);
  - Esc closes a menu;
  - `/` focuses the composer;
  - Alt-↑ / Alt-↓ switches instance.

## 3. Live Markdown documents (request 1)

### A document is a Markdown file in the instance's `notes/docs/`

- **No new supervisor tool.** The instance writes a document with the `write_file` it already has. A broker
  `artifact()` tool would only duplicate `write_file`, and this follows the project rule that instances build
  their own tools.
- **It has history.** `notes/` is mind: committed with the instance's runs and kept in its mind store, so the
  instance's `history` covers it.
- **Limits:** `notes/docs/**/*.md`, at most 256 KB each, no links followed, the path kept inside that folder.

### The instance learns about it from the chat itself

The first message of each web chat carries one extra line, which you don't see. It is the same mechanism as the
"[The operator attached…]" lines:

> [Web chat: a Markdown file you write under notes/docs/ appears beside this chat as a document the operator sees live
> and can edit. Write it whole (write_file with replace=true when you drop lines). His edits reach you as a diff
> in their next message.]

This works for every instance whatever its own prompts say, and changes no prompt. *(My call: a prompt change
would have to be merged into each instance's own `shaft/`.)*

### What you see

- **The documents panel** lists the instance's documents, newest change first. Those changed in this chat are
  marked. Each file has its own row with its size and open-comment count. Opening a file replaces the list;
  **← Documents** clears the selected document from the address and returns to the list. It asks before dropping
  unsaved edits. Without a selected document it opens the one this chat last changed (so what the instance
  writes shows as it writes it); after **← Documents** it stays on the list.
- **The open document is rendered** with the safe `markdown()` and polled every 1.5 s. Each write the instance makes
  mid-reply shows as it lands, without waiting for the answer.
- **A reply that created or changed documents shows a card for each** ("📄 plan.md — changed"). It opens the
  document in the panel.
  - How it is found: a `{path: sha}` snapshot of `notes/docs/` taken when the reply starts, compared when it ends.
- **Reply on a selection works in a document too.** The quote names the document it came from.
- **Comments on a passage, answered in threads:** [PLAN-comments](PLAN-comments.md) (2026-10-20).

### Editing it yourself

- **Edit** shows the raw Markdown in a textarea.
- **Save** is compare-and-set on the sha you loaded, like `shaft drive set --expect-sha`. If the instance changed
  the file meanwhile, Save is refused and shows the diff. Your text stays in the box.
- **New document** asks for a name and creates `notes/docs/<name>.md`.
- **Delete** asks for confirmation, then removes the document only if its sha still matches. The drawer returns
  to the list. The instance hears a short deletion notice, with the next message or immediately while replying;
  it replaces any pending edit for that document. Comment threads and their copies remain, and links to the
  removed document show the missing-document message.
- **The instance learns of your edit:**
  - Saved while it is idle: a diff of the change goes with your next message, as `[The operator edited
    notes/docs/plan.md:]` plus a unified diff. Your turn shows an "edited plan.md" chip, not the diff.
  - Saved while it is replying: the diff goes at once as an interjection, so it reads your edit before its next
    write.
  - Several saves before you send are folded into one diff per document.
- **If it overwrites your edit anyway** (a write that raced your save): the web chat keeps the last version you
  saved. The panel offers to restore it or show the diff.
- **Who writes the file:** `shaft docs <name>` lists documents; `show PATH` reads one,
  `set PATH --file F --expect-sha SHA` writes one, and `rm PATH --expect-sha SHA` deletes one.
  The web adds no authority, and you can do the same from a shell.
- **The commit:** the instance's next run commits your edit with the rest of its mind. *(My call. Say if you want
  your edits as separate commits, as `drive.md` has.)*

## 4. Supervisor settings in the web UI (request 4)

**Status (2026-10-19): S0 and S1 built** on branch `claude/nervous-meitner-3c6951`, merged into main after a review
(one fix: `SHAFT_MODEL_IDLE_MINUTES` took a parser that refused its documented 0 = never exit; it takes 0 again). Signed off with
The operator's answer to question 3: a gitignored `.supervisor/settings.env`, env > file > default.

- **S0.** `core.knob()` reads env > `settings.env` > default; a value its parser refuses is warned about and the next
  layer used. Every call registers a description, a Settings group, the services that read it and whether the web
  may set it: 55 knobs (the ~45 counted here, plus five that were read straight from `os.environ` and are knobs now:
  `SHAFT_OPERATOR_PER_DAY`, `SHAFT_WEB_HOSTS`, `SHAFT_THINK_BUDGET`, `SHAFT_MODEL_TEMP`, `SHAFT_PROMPT_CACHE_GB`).
  Parsers now refuse what the code could not run on: a model the supervisor cannot call (`gpt-5`; a Claude model for
  the OpenRouter-only second judge; an unpriced coder model), an empty judge pool, a negative timeout, JSON that is
  not an object. `shaft settings [set|unset] [--expect KEY=OLD|--expect-unset KEY] [--json] [--from-web]`
  (`supervisor/settings.py`): one write under the config lock, compare-and-set on the file's value, each value
  checked before anything is written, every change logged.
- **Which services must restart** comes from what each one actually read, not from a list. Each service (api,
  scheduler — which recorded nothing before —, telegram, web, model, embed) records every knob it read and the value
  (`.supervisor/services/<name>.json`), and the record is rewritten when a module imported later reads more. *My
  call:* a service holds the knobs of every module it imported, used or not, so the list errs towards a restart. A
  service started before this code has no such record: it is taken to hold the defaults of the knobs it is declared
  to read.
- **Correction to the S0 acceptance.** A verdict is given in the run's own process (a wake, a trial, a battery),
  which the scheduler starts fresh: it reads the file when it starts, so the next verdict uses a new judge pool
  without any restart. The scheduler and the web UI still hold the pool (the scheduler imports `battery`, the web's
  stats count verdicts by the judges in the pool), so the page asks to restart them.
- **S1.** `/settings` (`supervisor/web_settings.py`), one Settings link in `GLOBAL_PAGES`. The listing comes from
  `shaft settings --json` in its own process, so the web never imports every module just to list them (its record
  would then name every knob). Groups as in the table above, plus "Ports and paths" (shown, terminal only). The
  judge pool and the cloud models are ticked boxes over the known cloud models plus an "other" field; single models
  have a suggestion list. Save runs `shaft settings set … --from-web --expect`; "Use the default" runs `unset`. The
  "Saved, not live yet: restart …" panel offers each restart with the Services page's guard; the web UI cannot restart
  itself, as before (`shaft web on`). `.env`: key names only.
- **Tests:** `test_settings.py` (precedence, a launchd-like process reading the file, refusals write nothing,
  `--expect`, `--from-web`, not-live, the records, every `knob()` registered, no `SHAFT_` read around `knob()`) and
  `test_web_settings.py` (groups with source and default, the multi-select, terminal-only rows, secrets, the restart
  panel and its guard, the exact CLI commands, a long value fitting a phone).
- **Checked live** in Chrome on a server from this branch over a copy of `.supervisor` (tailnet port 8446, removed
  after): ticked the pool to `claude-sonnet-5-5,openrouter/z-ai/glm-5.3-flash` and saved; the page showed it
  `[settings.env]` and "not live yet: restart api, web"; a process started with PATH alone read the new pool and
  picked only from it; after restarting the test web server the panel dropped `web`. A save after a CLI change was
  refused as stale, `claude-sonnet-5-5` as second judge and a JSON list as provider policy were refused with their
  reasons, "Use the default" removed the key. At 400 px the page first scrolled sideways (JSON in wide inputs);
  long values are textareas now, and it fits. **Not done live:** pressing Restart (it would restart the operator's live
  API), and a real judged cycle on the new pool.

### Make the settings real first

- **A settings file:** `knob()` reads `.supervisor/settings.env`, after the environment and before the default
  (env > file > default). It is the operator's, gitignored, and written only by a new CLI verb.
  - `shaft settings` lists every knob: value, source (env / file / default), default, and what it does.
  - `shaft settings set KEY=VALUE … [--expect KEY=OLD]` and `unset KEY` write it, with the same lock and
    compare-and-set as `shaft config`.
  - Each value is validated by its own knob's parser before it is written. A bad value is refused with the reason,
    never written to fall back silently.
- **One registry of knobs.** Each `knob()` call registers its name, default, parser, a one-line description and the
  services that read it, so the CLI, the web page and the docs list the same set. Today there are ~45 knobs spread
  over 15 modules, with no list of them.
- **Applying a change:** knobs are read at process start. Saving says which services must restart to apply it and
  offers the restart, with the same guard as the Services page (refused while a scheduled run is in progress).
  - Until a restart, the page shows "saved, not live yet: restart api, scheduler".
  - It tells this apart because each service records the settings it started with.

### The Settings page

Grouped by what they control, each field showing its default and what it does:

| Group | Knobs |
|---|---|
| Judging | `SHAFT_JUDGE_MODELS` (multi-select from the cloud model list), `SHAFT_JUDGE2_MODEL`, `SHAFT_JUDGE2_EVERY`, `SHAFT_JUDGE_ALERT_USD` |
| Models and spend | `SHAFT_CLOUD_MODELS`, `SHAFT_CLOUD_DAILY_USD`, `SHAFT_CODER_MODEL`, `SHAFT_CODER_DAILY_USD`, `SHAFT_RECAP_MODEL`, `SHAFT_DESCRIBE_MODEL`, `SHAFT_DECISION_MODELS`, `SHAFT_OPENROUTER_PROVIDER` |
| Trials | `SHAFT_TRIALS_PER_DAY`, `SHAFT_TRIAL_DAILY_USD`, `SHAFT_TRIAL_FALLBACK_MODEL`, `SHAFT_TRIAL_PARALLEL`, `SHAFT_TRIAL_N_MAX`, local-model limits |
| Runs and conversations | `SHAFT_CYCLE_TIMEOUT_S`, `SHAFT_SLEEP_TIMEOUT_S`, `SHAFT_IDLE_MINUTES`, `SHAFT_BROKER_MAX_CALLS`, `SHAFT_PROBATION_CYCLES`, `SHAFT_CORE_MIN_HOURS`, `SHAFT_MAX_IMAGES` |
| Inbox and benchmarks | `SHAFT_INBOX_*`, `SHAFT_BATTERY_*`, `SHAFT_JOB_MIN_MINUTES` |
| Local model | `SHAFT_MODEL_IDLE_MINUTES`, `SHAFT_MODEL_START_TIMEOUT_S`, `SHAFT_SELF_HOSTED*`, `SHAFT_FT_BASE_MODEL` |

- **Shown but not editable from the browser:** ports, interpreter paths and `SHAFT_TELEGRAM_API`. A wrong value there
  cuts the web UI off or breaks a service, so they stay CLI-only. *(My call.)*
- **Secrets stay CLI-only, as before:** `.env` key names are listed, never their values.

## 5. Built today: reply to part of a message (request 2)

- **Select text in any message of the chat and a "↩ Reply" button appears below the selection** (below, because a
  phone puts its own menu above).
  - It puts the selection in the composer as `> ` lines, at the caret.
  - A second quote goes below the first.
- **The feed's 1.5 s refresh skips a tick while you have text selected in it.** Without that, the refresh during a
  running reply wiped the selection before you could click.
- **Quotes render as quotes:**
  - in your own turns, the quoted lines show as a quote and the rest stays as you wrote it — except a fenced code
    block (```` ``` ```` or `~~~`), which shows as code with its Copy button, as in an answer (2026-10-29);
  - `markdown()` gained blockquotes, so the instance's own quotes render too.
- **Fixed in passing:** the static files' cache key was a constant bumped by hand (`STATIC_VERSION = "10"`). A
  change to `web.js` without the bump left browsers running the old script for up to an hour. It is now a hash of
  the files' content.
- **Tests:** quote rendering (escaped, one blockquote, the marker not shown), your turns' quote/plain split, and the
  cache key changing with the content. Full suite: 929 OK.
- **Checked live in Chrome on the running service:**
  - selecting a paragraph shows the button while example-b was replying, and the selection survived the refresh;
  - Reply put `> …` in the composer, with the caret after it;
  - a second, two-line quote went below the first.

  Nothing was sent. Not observed live: a sent quoted message rendering in the feed (covered by its test).

## 6. Milestones

| M | Deliverable | Acceptance |
|---|---|---|
| S0 | Knob registry; `.supervisor/settings.env`; `shaft settings` list/set/unset with `--expect`; services record their settings at start. | Tests: env > file > default; a bad value refused and not written; `--expect` refuses a stale value; every `knob()` in the code is in the registry (a test greps for unregistered ones). Live: set `SHAFT_JUDGE_MODELS` by CLI, restart the scheduler, the next verdict uses the new rotation. |
| S1 | Web Settings page over S0, with restart prompts. | Tests: each group renders with source and default; save runs the CLI verb with `--expect`; ports are read-only. Live: change the judge pool from the browser; the page says "not live yet" until the restart. |
| C0 | Chat-centric frame: `/` is the chat, the instance rail with state and unread, the "⋯" instance menu, Manage group, phone layout. | Tests: every existing page still renders at its URL with the new frame; the TUI-parity tests unchanged. Live: open the URL on desktop and on a phone; switch instance from the rail; reach every old page within two clicks. |
| C1 | Chat UX of §2. | Live: drop a file onto the feed (not the composer) and it attaches; remove a chip; reload keeps the draft; a hidden tab's title marks a landed reply; copy an answer. |
| D0 | `shaft docs` verbs with path, size and compare-and-set guards. | Tests: outside `notes/docs/`, a link, `..`, not `.md`, oversize → refused; a stale sha → refused with the diff. |
| D1 | Documents panel, live render, reply cards, the first-message line. | Tests: the line reaches the API and is not shown; a reply that wrote a document gets its card; `<script>` in a document renders escaped. Live: ask an instance for a plan document and watch it fill in while it works. |
| D2 | Edit, Save, New, edits as diffs (next message or interjection), restore after an overwrite, quoting from a document. | Tests: the diff goes once; a save mid-reply is an interjection; a stale save keeps your text. Live: edit a section, ask it to continue, and it builds on your edit. |
| R | README section, PLAN-webui.md status, this plan's status. | — |

**Order:** S0 → S1 (independent of the rest; can run in parallel, in its own worktree) and C0 → C1 → D0 → D1 →
D2 (the chat page, one writer). Everything goes on a branch, merged when you say.

## 7. Decisions ("Go ahead … your defaults for the three questions")

1. Documents in `notes/docs/`; their edits committed by the instance's next run, not as separate commits.
2. An edit saved while it replies goes at once, as an interjection.
3. Settings in a gitignored `.supervisor/settings.env` (env > file > default).

The questions as asked:

1. Documents in `notes/docs/`, with your edits committed by the instance's next run (not as separate commits)?
2. An edit you save while it replies goes at once as an interjection (my default), or waits for your next message?
3. Settings: is a gitignored `.supervisor/settings.env` (env > file > default) right, or do you want them in the
   repo, versioned?

## 8. Status

**Built 2026-10-19 on `claude/webui-chat`; merged into main (fe3729b) on the operator's word.** Commits: quote-reply
and this plan; C0; C1; D0; D1; D2; fixes found live.

What was built — as §1–§3 describe, with these differences:
- Phone: the documents panel stacks below the composer (a card or 📄 Documents scrolls to it), not a second tab.
  *(My call: one less control; say if you want the tab.)*
- 📄 Documents in the chat header shows the panel when there are none yet (otherwise New document was unreachable).
- The header shows today's spend, not spend against a cap.
- Unread chats: the tab title counts them, "(2) example-b · chat", rather than "● example-b replied".

Tests: `test_docs.py` (8), and in `test_web_chat.py` / `test_web.py` / `test_web_pages.py` the front page, the rail,
the chat frame, the chat UX hooks, documents in the chat (the note once, cards, the panel, saves, diffs, interjection,
folding, refusals, overwrite warning). The guards were checked by breaking them: the link guard, edits sent once,
folding across an instance's write, a reply surviving an unreadable documents folder. Full suite on the branch:
994 tests OK, 55 skipped (run with `template/` linked as in the main checkout: a bare worktree has neither
`template/` nor `.supervisor/`, which the sandbox and plugin tests need).

Driven live in Chrome, from the branch's server on a copy of `.supervisor/` (port 8795, tailnet :8445; your
service on :8444 untouched):
- `/` → the last chat; the rail's state dots, last lines, unread mark and title count; the ⋯ menu (pages, actions,
  Notify, earlier chats; Esc closes it); the last ended chat shown with the composer; the feed filling the window.
- Files dropped on the feed (not the composer) attach, with chips and a thumbnail; × removes one; the draft survives
  a reload; Copy puts the answer's exact Markdown on the clipboard (checked with pbpaste); `/` and Alt-↓.
- Phone width (411 px): no sideways scroll; rail as chips; composer on screen; documents below.
- Documents, on a scratch instance the side server alone saw: New document; an instance-style write rendered within
  2 s without a reload, `<script>` as text; Edit → Save; the "lines you wrote are no longer in it" warning and
  Restore; a stale Save refused with what changed, then Save again; Reply quoting a document line with its name.

Found and fixed while driving it: the copy fallback (the Clipboard API refused in Chrome); the overwritten `hidden`
attribute; New document unreachable with no documents; a refresh under a focused field; edits folded across an
instance's write (it would have been told your edit created its own plan); Save refused forever after one refusal;
nested code fences in the diff; a one-line code block's scrollbar; the hand-bumped static cache key. Found by the
full suite: a reply failed when the documents could not be read (`instance_root` says so with SystemExit, which
`except Exception` let through), and the chat tests read the real instance's folder instead of their own.

**Not observed live** (pinned by tests only): an instance reading the note and writing a document (cards under its
reply), your diff reaching it, a save during a reply as an interjection, the reply clock, the "sending…" turn, desktop
notifications. Every instance was in a cycle (example-c paused by you, example-b in a conversation with you) — a test chat
would have paused real work. To check once merged: in a web chat ask an instance for a short plan in
`notes/docs/plan.md`, edit a line, and ask it to continue.

**Checked live after the merge, on the real service, with shaft (its one idle moment):** it read the first message's
note and wrote `notes/docs/ui-test.md`; its reply had the card and the panel opened the document; the operator's edit (made
in the panel) reached it as the diff in the next message — the "sending…" turn, the "✎ edited" chip and the reply's
clock seen live; it named the added line and appended its own below it, keeping the edit. The test conversation was
ended (written up); `notes/docs/ui-test.md` stays in shaft's notes. Not seen live yet: a save mid-reply as an
interjection, desktop notifications.
