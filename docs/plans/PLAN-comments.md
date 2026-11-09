# PLAN-comments — comments on the documents in the web UI

Status: **built 2026-10-20, branch `codex/comments`, not merged, not deployed.** Signed off 2026-10-20 ("ticked = sent
now" confirmed). M0–M4 done; checked in a real browser against a local copy of the web UI with a scripted instance
(§11). Not yet checked: a real instance answering, and Safari on the iPhone.
## 1. What you get

Like comments on a Claude artifact.

- **You comment on a passage.** Select text in a document open beside the chat and press **💬 Comment**. Or press
  **💬** in the document's title bar to comment on the whole document.
- **"Send comment to <instance>"**, a checkbox on every comment, ticked by default (the operator, 2026-10-20):
  - **Ticked:** the instance gets it at once. While it is idle the comment is a message of its own and starts a
    reply. While it is replying it goes in as an interjection.
  - **Unticked:** no reply starts, but the instance can read it (§12, 2026-10-20: it was "only you see this").
    Leave several, then ask for a review. You can edit or delete it, or send it later with **Send to
    <instance>**, until it is answered.
- **It answers in the thread.** Its answer shows there and in the chat. Your turn in the chat shows a "💬 c7" chip,
  which opens the document at that thread.
- **It opens threads too** (the operator, 2026-10-20), to point you at a passage of its own document or to ask about it.
- **The commented text stays highlighted.** Clicking a highlight opens its thread.
- **You resolve a thread** when it is done. Resolved threads fold away and lose their highlight. The instance is
  told with your next message; resolving does not start a reply of its own.
- **Each document in the list shows its count of open threads.**
- **It works on the phone**, inside the documents sheet.

## 2. What it is not

- **No new tool for the instance and no prompt change** (the project rule: instances build their own tools). It
  learns the convention from the comment itself: every comment block carries a one-line contract (§4).
- **No comments on the chat itself.** "↩ Reply" on a selection already covers that.
- **No edits to a sent comment.** It is already in the instance's transcript. A correction is a reply in the
  thread, and the thread says so.
- **Not tracked to a version of the document.** The thread keeps the document's sha from when you commented, only
  to say "the document changed since this comment".

## 3. Where comments live

### The store

`SUP_DIR/web/comments/<instance>.json` (beside the chats in `SUP_DIR/web/chats/`). Written by atomic rename, mode
0600, under a lock of its own. **Never held together with a chat lock** (§6).

*(My call; both reviewers agreed.)* It is not in the instance's `notes/`: the instance rewrites files whole, and one
`write_file` there would lose your comments.

```json
{"next": 8,
 "threads": {"c7": {"doc": "plan.md", "by": "operator", "state": "open | resolved", "sha": "…",
                    "anchor": {"exact": "…", "prefix": "…", "suffix": "…", "start": 1234} | null,
                    "posts": [{"pid": "p-…", "by": "operator", "text": "…", "ts": "…", "sent": "web-…#4" | null},
                              {"pid": "p-…", "by": "aria", "text": "…", "ts": "…", "turn": "web-…#5"}]}},
 "notices": ["[The operator resolved c7 on notes/docs/plan.md]"]}
```

- **Thread ids** are per instance (`c1`, `c2`, …), short enough to name in prose.
- **`anchor: null`** means a comment on the whole document.
- **Post ids** (`pid`) are made by the page. They are also the message's `mid`, so a retried request is never sent
  twice. `send()` already deduplicates on `mid`.
- **`notices`** are things the instance should hear but that need no reply of their own (resolve, reopen). They go
  with the next message.

### A read-only copy in its mind (the operator, 2026-10-20)

- After every change the supervisor rewrites `notes/comments/<doc path>.md`: each thread of that document, as
  Markdown. The instance can look comments up later, and its `history` keeps them.
- It is a copy, rebuilt whole from the store each time. An edit the instance makes to it is gone at the next change.
- ~~Unsent notes are left out~~ (§12): every post of an open thread is in it, sent or not; of a resolved thread,
  only what was sent.
- Written through a CLI verb, `shaft docs <name> comments --sync PATH`, with the same containment as `docs set` (the
  web adds no authority).

## 4. The instance's side

### What it is sent

One block per comment, appended to the turn's `content`. It is never shown in your bubble; the chip stands for it.

```
[The operator commented on notes/docs/plan.md (c7), on the text "the selected text":]
> his comment
[Answer it in your final reply under a line "### c7". Open a thread of your own with a line
 ### comment on notes/docs/<doc>.md: "exact text from it"  followed by your comment.]
```

- **The contract is in every block**, not only in the chat's first message: a rule stated once is forgotten in a
  long chat full of tool calls.
- **A reply in an existing thread** quotes the thread's last answer, then his reply.
- **A whole-document comment** says "on the whole document".
- **An ambiguous quote:** when the text occurs more than once, the block adds its neighbours.
- **On-screen text:** when the quote is not literally in the file (rendered Markdown), the block says it is the text
  as shown on screen.
- **Size:** a quote is capped at 2,000 characters when you select it. A block that would not fit `MAX_TEXT` is
  refused in the page, so it can never sit unsent forever.

### How its answer is read

Done at the relay end (beside `docs.changed`), on the reply's final text only: text before a tool call is narration
and never reaches the answer (`api.py:432`).

- **One canonical form:** a line that is exactly `### c<n>`. Its section runs to the next heading of level 1–3,
  or to the end.
  - Parsed outside fenced code only.
  - Only ids already sent to the instance count. Others are left as text.
  - The first section for an id wins.
- **A thread with no section gets no answer copied in.** Its thread shows "answered in the chat, without a section
  for c7 →" with a link to that turn.
  - The one exception: the reply was started by exactly one comment and nothing else (no text, no interjection
    heard). Then the whole answer is that thread's answer, since nothing else could be.
- **`### comment on notes/docs/X.md: "…"`** opens a thread by the instance, anchored on that text, or detached if
  it is not found.
- **When an answer is attached:**
  - only when the reply ended normally — Stopped, failed and interrupted replies attach nothing;
  - never to a resolved thread;
  - at most once per `(chat, turn, thread)`, so re-running the relay end can never attach twice.
- **The chat still shows the whole answer.** The `### c7` headings read naturally there.

## 5. What you see

### Selecting

- **Only text inside `.docmd`** (the rendered document) offers 💬 Comment. The title, its buttons and the threads
  do not.
- **The selection button becomes a group:** "💬 Comment · ↩ Reply" in a document, "↩ Reply" in the chat. It is
  above the phone's documents sheet.
  - Today `#quote-reply` (z-index 20) sits under that sheet (55), so "↩ Reply" on a document cannot be reached on a
    phone. That is fixed here.
- **The quote is captured before focus moves**, by one walk of `.docmd`'s text:
  - text nodes in order;
  - `\n` at each block (p, h1–h6, li, pre, tr, blockquote, br) and `\t` between table cells;
  - from that walk: `exact`, 32 characters of prefix and suffix, and `start`.
  - The same walk is used to find the quote again later.
  - Not `Selection.toString()`: Safari's differs across blocks (WebKit 263302).

### The comment box

- **One floating box, outside the polled panel** (like the composer, outside the feed). The 1.5 s refresh replaces
  the panel's HTML, so a box inside it would be wiped while you type, or would freeze the panel.
- The same box serves a new comment, a reply in a thread, and editing an unsent note.
- It holds the "Send comment to <instance>" checkbox. Submitting is idempotent (the `pid`).
- **On the phone** it is a sheet above the documents sheet.

### The threads

- **A "💬 3 open" list** under the document's title, in a `<details data-key>` so it stays open across refreshes.
  - Order: open threads, then detached ones, then resolved ones folded away.
  - Each shows: the quote; the posts; Reply; Resolve / Reopen; "document changed since this comment" when the sha
    differs.
  - An unsent note also shows Edit, Delete and Send to <instance>.
- **The thread data is in the fragment** (`data-threads` on `.docview`). A comment-only change therefore changes the
  ETag and reaches the page; nothing is changed only locally.

### Highlights

- **Repainted on page load and on each `shaft:swapped`** (an actual replacement), not on every poll: a 304 or an
  unchanged body leaves them as they are.
- **Matching** is an exact match of `exact` in the walk's text:
  - 0 hits: the thread is detached ("text no longer in the document"). It is never resolved on its own: an
    unrelated rewrite must not close it.
  - 1 hit: highlighted, even if its neighbours changed.
  - several hits: the one whose neighbours match, else the one nearest `start`. A tie highlights none and marks the
    thread "ambiguous".
- **Drawn with the CSS Custom Highlight API only** (Safari 17.2+, Chrome 105+). It leaves the DOM alone, so the next
  poll's compare stays honest.
  - One `Highlight` for open threads and one for the thread being read, which is registered last so it paints on
    top.
  - Ranges are split per text node.
  - No `<mark>` fallback: `surroundContents` throws on a range that crosses elements. Where the API is missing,
    threads still list; there is just no in-text highlight.
- **A click on a highlight** finds the thread with `caretPositionFromPoint`: the shortest range containing that
  point.

## 6. Who writes what, and in what order

**`supervisor/doc_comments.py`** (new): the store, add / reply / edit / delete / resolve, notices, the message block,
the answer parser, and the `notes/comments/` copy.

**Lock order:** instance lock (`web_chat._inst_lock`) → comment-store lock, released → chat lock. The comment-store
lock and a chat lock are never held together. A ticked comment:

1. Under the store lock: write the post with `sent: null`, then release.
2. `web_chat.send(name, login, "", comment_block=…, mid=pid)`.
3. On success, under the store lock: `sent = "<chat>#<turn>"`.

If it fails or crashes between steps, the post stays unsent with **Send to <instance>**. A retry is deduplicated by
`mid`.

**`web_chat.py`:**
- `_send` takes a `comment_block` and the store's notices (taken under the store lock, put back if sending fails).
- The empty-message guard accepts a comment or notices as content.
- The turn records `comments: [ids]` for its chip.
- The relay end calls the parser with the reply's text, the ids sent so far, and whether this reply was started by
  exactly one comment.
- Unsent notes and notices belong to the instance, not to a chat, so nothing needs adopting.

**Routes (web.py)**, all POST behind the CSRF check:
- `/docs/<name>/comment` — `{pid, doc, anchor|null, sha, text, send}` or `{pid, thread, text, send}`
- `/docs/<name>/comment/edit` — `{pid, text}`, unsent only
- `/docs/<name>/comment/delete` — `{pid}`, an unsent post. A thread left with no posts goes with it.
- `/docs/<name>/comment/send` — `{pid}`
- `/docs/<name>/comment/state` — `{thread, state}`

Each route checks that the thread belongs to this instance, the id and path forms, `docs._rel` on `doc`, and field
lengths.

**Views:** `web_views.docs_panel` gets the threads list, `data-threads` and the counts in the list; `chat_feed` gets
the 💬 chips.

**CLI:** `shaft docs <name> comments [PATH] [--sync]`, registered in `__main__.py` beside `docs`.

### 6.1 The interface between the backend (Codex) and the page (Grok)

Fixed here so both can build at once.

- `doc_comments.threads(name, doc=None) -> list[dict]`, open threads first, each:
  `{"id": "c7", "doc": "plan.md", "by": "operator" | <instance>, "state": "open" | "resolved", "sha": str,
  "anchor": {"exact", "prefix", "suffix", "start"} | None, "created": ts,
  "posts": [{"pid", "by", "text", "ts", "sent": "web-…#4" | None, "turn": "web-…#5" (its answers only)}],
  "unsectioned": ["web-…#5", …]}`. `unsectioned` lists replies that heard the thread but gave it no section: the
  page links them.
- `doc_comments.open_counts(name) -> {doc: n}`.
- A user turn in a chat carries `"comments": [{"id": "c7", "doc": "plan.md"}]`. An assistant turn whose answer
  went into threads carries `"answered": [{"id": "c7", "doc": "plan.md"}]`. Thread ids opened by the instance in
  that answer are in `"opened": [...]`, same shape.
- `web_views.docs_panel(..., threads: list | None = None, counts: dict | None = None, thread: str = "")`: `thread`
  is the one to show open and active (from `?thread=c7`).
- The routes take form fields (the page's `data-post` forms):
  - `pid` — `p-` + 16 hex, made by the page;
  - `doc`;
  - `anchor` — JSON text, or empty for the whole document;
  - `sha`, `thread`, `text`, `state`;
  - `send` — `"1"` when ticked.
- They answer like `/docs/<name>/save`:
  - 200 with `<p>what happened</p>` and a header `X-Thread: c7`;
  - 409 with `<p>refused: …</p>`.

## 7. Milestones

| | What | Acceptance (tests named) |
|---|---|---|
| **M0** | `doc_comments.py`: store, ops, block, parser, copy | add/reply/edit/delete/resolve; unsent→sent; ids per instance; block text incl. ambiguous/on-screen/whole-doc; parser: one section, two, none, unknown id, id never sent, `### c7` inside a fence, duplicate id, heading-level boundary, `### comment on …` opens a thread, fallback only for a reply started by exactly one comment; `notes/comments/` copy leaves out unsent notes and is rebuilt after an instance edit |
| **M1** | `web_chat` + routes + CLI | a ticked comment starts a reply when idle and interjects when replying; its answer lands in its thread once; Stop/error/restart attach nothing; mixed heard/unheard interjections and a follow-up reply; a notice rides the next message; a send that fails leaves the post unsent and a retry is not sent twice; two tabs deleting and sending the same post; route ownership/validation/CSRF; a comment-only change changes the fragment's ETag |
| **M2** | UI: selection group, comment box, threads list, resolve, chips, list counts, phone sheet | in the real web UI at desktop width and 375 px: comment → instance answers in the thread → resolve; a draft survives a refresh that brings changes; ↩ Reply now reachable on the phone |
| **M3** | Highlights, detached, ambiguous | a highlight survives refreshes and an instance rewrite that keeps the quote; a rewrite that drops it shows the thread detached; a cross-block and an inside-`**bold**` selection highlight; overlapping highlights open the shorter |
| **M4** | README, PLAN-webui-chat §3 pointer, code map | the docs say what was built |

**Verification:** drive the real web UI (built-in browser, desktop and 375 px) with a real instance answering. On
the iPhone, check what the browser pane cannot: selection handles and the Highlight API under Safari.

## 8. Who builds it

- **Codex (write mode, in a worktree I create): M0 and M1.** They are backend work with sharp test lists — its
  strength, and fast.
- **Me: M2 and M3.** They need a browser to verify, and the panel and refresh JS is subtle.
- **Grok: reviews the whole diff** before I call it done. That is a different family from the one that wrote most
  of it.
- **I read every file myself before merging.** Nothing merges to main until you say so.

## 9. Decided

1. Instance-opened threads: **yes**.
2. A read-only copy in its mind: **yes**, `notes/comments/`.
3. A "Send comment to <instance>" checkbox on every comment. **My reading: ticked means sent now**, as on a Claude
   artifact. Say if you meant "with my next message".

## 10. Review (v1 → v2)

Both reviewers' claims were checked against the code before being taken.

| Finding | From | Change |
|---|---|---|
| The store was not under `SUP_DIR` (tests repoint it; `core.py:28`) | both | §3 path |
| A whole-answer fallback copies an unrelated answer into threads | both | §4: no copying; a link instead; one safe exception |
| Only the last step is the answer (`api.py:432`); the rule was stated only once | Grok | §4: the contract in every block |
| Which comments a reply heard is not recorded well enough to attribute by turn | Codex | §4: attribute by the ids the instance names, never by adjacency |
| Two locks crossed can deadlock; pending lost or doubled on failure | both | §6: lock order, `sent` after success, `mid` = `pid` |
| A box inside the polled panel is wiped or freezes it (`web.js:95`, `:186`) | both | §5: one box outside the panel |
| Highlights are rebuilt only on actual swaps; state must be in the fragment for its ETag | Codex | §5 |
| `selection.toString()` differs from a rematch on Safari; `<mark>` throws across elements | Grok | §5: one text walk; Highlight API only |
| Selection accepted anywhere in `.docview` | both | §5: `.docmd` only |
| `#quote-reply` under the phone sheet (`web.css:238`, `:437`) | Grok | §5: fixed, including today's ↩ Reply |
| Resolve was silent to the instance; no whole-document comment; no counts in the list | Grok | §1, §3 notices |
| Retry, validation, restart and two-tab cases missing from the milestones | Codex | §7 test lists |

## 11. What was built (2026-10-20)

As §1–§7 describe. Codex built M0, M1 and the page script (`comments.js`: selection, box, highlights). Grok built
the threads list, chips and their styles, then reviewed the whole branch. Claude reviewed each step, wired the panel,
and ran every test outside the sandboxes, whose localhost was closed.

**Differences from the plan:**
- The relay end holds the instance lock while it attaches answers, so a fast reply cannot attach before its comment
  is marked sent. The copy in `notes/comments/` is rewritten after that lock is released, because it is a CLI
  subprocess.
- The `started_by` fallback is dropped when any interjection was sent during the reply, not only one that was heard.
- An ambiguous quote's neighbours go to the instance on one line, as JSON strings.

**Checked in the browser** — this branch's web server with throwaway data, a stand-in instance and a scripted model,
desktop and 375 px:
- selecting text; the "💬 Comment · ↩ Reply" pair;
- a ticked comment sent and answered into its thread under `### c1`;
- an unticked note: Edit with its exact text, then Send to demo;
- a comment on the whole document;
- a thread opened by the instance;
- Resolve, and its notice riding the next message;
- a highlight on the second of two identical sentences;
- a click on a highlight opening its thread;
- a thread detached by a rewrite, seen live;
- "the document changed since this comment";
- chips in the chat opening their thread;
- the counts in the list;
- on the phone, both selection buttons above the documents sheet and the box as a bottom sheet.

**Tests:** 25 store/parser, 17 chat wiring, 11 routes, 18 views, 2 Node checks of the matcher and walk, and
touch/PWA checks. The full suite adds no failure. The 4 that fail on main too (calendar, mail, conversations,
invariants) come from the template's operator-neutral wording, 2026-10-20.

**Grok's review findings:**
- Fixed with regression tests:
  - the code block's Copy button was read as document text;
  - a post was sent again after a failed `mark_sent` and an ended chat.
- Left: a crash between taking the notices and saving the turn drops resolve/reopen notices. The copy in
  `notes/comments/` still shows the state.

**Open:**
- Check with a real instance after deploy. Does it answer under `### cN`?
- Check on the iPhone: Safari selection handles and the Highlight API.

## 12. Unsent comments are visible (the operator, 2026-10-20)

**Why.** Collaborating on a document, you want to leave a batch of comments and then ask for a review. Before, an
unticked comment was private: the instance never saw it, and the only way to deliver several was one message each.

**What changed:**
- **The copy holds every open comment.** `notes/comments/<doc>.md` lists each post of an open thread, sent or not
  ("not sent as a message"), with the `### c<n>` convention at its top. Resolved threads keep only what was sent.
- **It is rewritten on every change**, not only on send, resolve and answer: adding, replying, editing and deleting
  an unsent comment sync it too (`web._comment`).
- **Each message names them.** While any open thread holds an operator post not sent as a message, every message
  you send carries one line: which documents and threads, and where the copy is (`doc_comments.waiting`). The post
  that message itself carries is left out. "Review my comments" therefore works whatever the chat's length.
- **Answers land.** `### cN` attaches to any open thread, not only to one with a sent post. The unsent posts it
  answered are marked `seen` (the answering turn). Like `sent`, that freezes them: no edit, delete or send. Send to
  <instance> on one says it was already answered.
- **Wording.** The thread shows "not sent" instead of "only you see this"; saving an unticked comment says the
  instance sees it in `notes/comments/` and answers it when you ask. The chat's first-message note says where
  comments are.

**Not changed:** no chip and no comment block for an unsent comment (it was never a message); no reply starts on
its own; `unsectioned` links and the single-comment fallback still apply only to threads a reply was sent.
