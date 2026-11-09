# PLAN — messages and comments the instance handles in its next cycle

Status: signed off by the operator 2026-10-22 ("build it"); built on branch `claude/background-messages`.

## Why

Today everything the operator writes in the web UI is answered now: a chat message starts (or interjects into) a
reply, and holds the instance until it ends; a sent document comment is a chat message too. Sometimes the operator
wants to hand something over — "look into this", "rework this paragraph when you get to it" — without holding the
instance or waiting for an answer. The instance should take it up in its next scheduled cycle, the way it takes up
its inbox.

## How each path reaches the instance today (research)

| Path | Code | What happens |
|---|---|---|
| Chat message | `web_chat.send` → `_send` | A user turn in the chat; a reply through the API (`/v1/chat/completions`), or `/v1/chat/interject` while one runs. Holds the instance while it replies. |
| Queue after reply | `web_chat.queue` | Kept on the chat (`queued`), flushed into one user turn when the running reply ends (#70). |
| Document comment | `doc_comments` + `web_chat.send_comment` | Sent: a chat turn carrying `doc_comments.block(...)`; the answer is parsed from the final chat reply (`### c<n>` sections, `parse_answer`). Unsent: kept; the instance sees it in `notes/comments/<doc>` and the next chat message mentions it (`waiting`). |
| Operator → instance inbox | `messages._deliver` (CLI `shaft send` / `shaft reply`) | A file `instances/<name>/inbox/<stamp>-from-operator.md` and a row in `.supervisor/messages.jsonl`. |
| Scheduled wake | `autonomy.serve` → `supervisor wake <name>` → `template/shaft/live.py` | The cycle reads every `inbox/*.md` at its start (`read_inbox`), puts them in its first prompt (images listed in them are attached, `images.with_attached`), and moves them to `memory/messages/` after the cycle. It answers with `send_message`. A conversation also tells it "you have N unread message(s) in inbox/" (`talk.since_last_talk`); a message it reads whole in a conversation (`read_file`) is archived to `memory/messages/` after that reply, so the next cycle does not show it again as new (`talk.archive_read_inbox`, REVIEW-autonomy A6). |
| Instance → operator | `broker._send_message` → `_deliver` | Operator inbox + log row with its `alert` channel (`channel.route`) + push/Telegram; the web chat shows it as a "sent you" card with Reply (`web._sent_during`, `_sent_cards`). |

**The cleanest channel is the existing operator→instance inbox.** It is delivered now (the file is in the instance's
folder and logged), and handled in the next cycle with no new mechanism: the cycle already reads it, already attaches
images listed in it, already archives it, and already answers with `send_message`, whose answer already reaches the
chat as a card and the phone as a push. Nothing touches the API, the runner, the run lock or
`runner.open_conversations`: a background message never opens or holds a conversation.

## Design

### 1. Chat composer

A third button, **Next cycle**, beside *Send now* (and *Queue after reply*, which the concurrent `claude/unread-scroll`
work shows only while a reply runs). Always shown. Its title says when that is: "Not answered now: {name} reads it
in its next cycle (next wake ≈ 14:20)" — or "{name} has no scheduled cycles: it reads it when you next talk" /
"{name} is paused". No keyboard shortcut (Enter and ⌘-Enter keep their meaning); one can be added later.

It posts the same form to `/chat/<name>/send` with `background=1`. `web_chat.background(name, login, text, files,
mid)`:

- refuses an empty message; text at most `MAX_TEXT`;
- files: `messages.from_operator(name, files, "web")`, as a chat message's (same folder, same lines in the text, so a
  cycle attaches images the same way);
- quotes: a `> ` quote of an instance's question answers it (`messages.answer_quoted`, archived from the operator
  inbox, as now) and the inbox file gets `Kind: reply` and `In-Reply-To:` that question's file, as `shaft reply` does;
- delivers with `messages._deliver(...)`, extended with `via="web-background"` and `mid`: the log row carries both;
  the file header gets a line `Via: the web UI, for your next cycle — nobody is waiting; answer with send_message if
  it needs an answer.`;
- `mid` deduplicates a retry (the page resends after an answer that never arrived): a log row with that `mid` already
  there → "already sent", nothing delivered twice;
- never touches the chat file: no turn, no reply, no interjection. Pending document edits and comment notices stay
  for the next chat message (they belong to the conversation).

What it answers (toast): "for {name}'s next cycle (≈14:20)", or the no-wakes / paused / cycle-running variants
("a cycle is running now: this goes in the next one" — the running one read its inbox at its start).

### 2. What the chat shows

The log is the record (single source): `web._sent_during` also takes the operator's rows to this instance with
`via == "web-background"` in the same time span, and the feed renders them in time order as a card of their own:

> **you, for its next cycle** 14:02 · *waiting* — or *read* once the cycle has moved it out of `inbox/` (`history()`'s `read`)

with the text, the attachments (download links, as sent cards), Copy and Reply. It is not a chat turn, so the API
conversation never sees it twice (the cycle reads it; a later chat does not replay it).

### 3. How the answer comes back

The instance answers in its cycle with `send_message(to="operator")` — the inbox prompt already says so. That lands
as now: operator inbox, push or Telegram per `channel.route`, and a "sent you" card in the chat with Reply. Nothing
new on that side.

### 4. Document comments

The comment box's checkbox ("Send comment to X") becomes three choices under **Send to X**: **now** (as today),
**in its next cycle**, **don't send** (as today's unchecked box: no reply starts, but the instance's copy of the comments holds it, so it can read it). A kept post's buttons get **Next cycle** beside *Send to X*.

`web_chat.send_comment_background(name, pid)` delivers `doc_comments.block(...)` through §1's path, with the contract
line changed for a cycle: `[Answer it with send_message to the operator, under a line "### c<n>".]`, then
`doc_comments.mark_sent(name, pid, "inbox/<file>")` — sent, so frozen (no edit/delete), and the dedupe of a retry
(`send_comment` already refuses a post with `sent`).

The answer: `Broker._deliver` to the operator runs `doc_comments.parse_answer(name, text, [], None,
"msg:<file>")` on the message — only explicit `### c<n>` sections attach (no whole-message fallback: a cycle sends
unrelated messages too), then syncs the copies. `ReplayBroker` overrides `_deliver`, so a trial never attaches
anything. A chat reply that answers `### c<n>` still attaches as today (every open thread with a sent post is
eligible). In the thread, the post shows "⏾ for its next cycle" and an answer from a message "in a message", each
linking to `/messages/<file>`.

### 5. Edge cases

- **No scheduled cycles** (`WAKE_INTERVAL_MINUTES=0`): delivered anyway; the toast and button title say it is read
  when you next talk (a conversation mentions unread inbox messages).
- **Paused instance**: delivered; read after `shaft resume`.
- **A cycle running now**: it read its inbox at its start; this one waits for the next (said in the toast).
- **A chat reply running**: unaffected — the background message is not an interjection.
- **Chat ended or cleared**: the card still shows (same span rule as sent cards; a cleared chat shows only unread ones).
- **Telegram**: out of scope (a `/later` command could reuse `web_chat.background`'s core later).
- **Trials**: a replay never delivers (unchanged); `ReplayBroker._deliver` does not parse comment answers.
- **Secrets**: nothing new reaches the instance; the inbox file is in its own folder, written with `safe_write`.

`autonomy.next_wake(name)` is factored out of `dashboard.py` (one rule for "next wake ≈ HH:MM" everywhere).

## Tests (regression, named for the behaviour)

- `web_chat.background`: writes one inbox file + one log row with `via`/`mid`; no chat turn, no relay, no API call
  (a fake API that fails the test if called); a retry with the same mid delivers once; empty refused; files land
  under `attachments/from-operator/` and are listed in the text; a quoted question is archived and gets `In-Reply-To`.
- the chat feed renders a background card (waiting → read when the inbox file moves), not a "you" turn; an archived
  chat keeps its span.
- the `/chat/<name>/send` route with `background=1` reaches `background`, not `send`.
- comments: a background send marks the post `sent` = `inbox/<file>` and freezes it; a second send says already sent;
  a `send_message` to the operator with `### c<n>` attaches the answer to that thread; one without a section attaches
  nothing; `ReplayBroker` attaches nothing.
- the template cycle reads the background file in its inbox (it already reads every `*.md`; a test pins the header
  format so a change to `read_inbox` cannot silently drop it).
- `autonomy.next_wake` matches the old dashboard values.
- Full suite: `python3 -m unittest discover -s supervisor/tests -t .`.

## Verification

A sandboxed copy of the web UI (scratch project, fixture instance, `SHAFT_WEB_PORT`/`SHAFT_WEB_HOSTS`/
`SHAFT_API_LOGINS`, a tiny proxy adding `Tailscale-User-Login`, ports outside 8785–8798): send a background message
with an image and a quote, see the card *waiting*; run the fixture's cycle (or move the file as the cycle does) and see
it *read*; send a comment for the next cycle, then a `send_message` with `### c1` through the broker, and see the
answer in the thread and the card in the chat.

## Out of scope

- Waking the instance early for a background message (the web UI's *Start a cycle* button already does that by hand).
- Telegram (above).
