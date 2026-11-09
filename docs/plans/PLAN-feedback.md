# PLAN-feedback — 👍 / 👎 on an instance's replies in the PWA

Status: **built: F1–F3** (2026-10-29).

## Problem (today)

- The operator has no quick way to say "that reply was wrong or useless". A correction reaches the instance only if
  it notices one in the conversation and calls `record_correction` (`template/shaft/corrections.py`).
- Conversations are not judged (docs/architecture.md, pushback section), so a bad reply leaves no trace anywhere.
- `finetune.build_dataset` (`supervisor/finetune.py:65`) trains on "my side of every talk" with no filter: a reply
  the operator disliked is training data like any other.

## What changes

Every instance reply in the web chat (`.turn.it`) and every message it sent the operator (`.turn.sent` cards, and the
`/messages/<file>` page) gets two small buttons beside Copy / Reply: **👍** and **👎**. The same two buttons are
added to the message's action menu (long-press on touch, right-click on desktop; `web.js`, PLAN-pwa-market decision 2).

- **👍** is stored on one tap. The lit thumb then shows a small **Send…** link. It opens the same kind of sheet
  ("What was good? (optional)", **Send to example-a** / Cancel), and a sent 👍 always goes **in the background**: to the
  inbox, read in its next cycle, never into a chat in progress (see *Delivery*). Not sending is the default, so
  rating stays one tap.
- **👎** opens a small sheet at once:

  ```
  ┌ What was wrong? (optional) ─────────────────┐
  │                                             │
  └─────────────────────────────────────────────┘
  [ Send to example-a ]   [ Just keep it ]   Cancel
  ```

  - **Just keep it** stores the 👎 and its reason. The instance never sees it.
  - **Send to example-a** stores it **and** tells the instance (see *Delivery*).
  - **Cancel** stores nothing.
- The chosen thumb stays lit on the reply. A sent thumb shows "sent HH:MM". Tapping a lit thumb clears it. Clearing a
  sent 👎 is stored, but does not unsend the message, and the page says so.
- A stored thumb of either kind can be sent later: its Send… reopens the sheet with the reason filled in.

## Delivery (Send only)

The instance gets a message from the operator, marked as feedback:

```
[Operator feedback: 👎 on your reply of 2026-10-29 14:02 (web chat web-3f…):
 «first 300 characters of the reply…»
 What was wrong: <reason, or "no reason given">.
 If it names something you got wrong, record it with record_correction.]

[Operator feedback: 👍 on your reply of 2026-10-29 14:02 (web chat web-3f…):
 «first 300 characters of the reply…»
 What was good: <reason, or "no reason given">.
 This is information about what was useful to the operator. It is not a score and changes no grade.]
```

The 👍 note's last line is there on purpose. A 👍 the instance reads is the one place a thumb could push it toward
pleasing the operator, so the note frames it as "this was useful", not as a reward to chase.

The note is added by the supervisor in the message, not in the instance's prompts, the same way `DOCS_NOTE` is, so
every instance gets it whatever its own code says. Where it arrives:

| Where the reply is | How the feedback reaches the instance |
|---|---|
| **👍, anywhere** | Inbox at once (`messages.deliver_background`), read in its next cycle. Never rides a chat. |
| 👎, web chat still open (idle or replying) | Rides with the operator's **next message in that chat**, like document edits (`pending_feedback`, next to `pending_edits` in `web_chat.py`). The feed shows "👎 on its reply of 14:02 goes with your next message", removable with ×. A message sent while it replies is not interjected for this: a 👎 waits for the next message. |
| 👎, that chat ends (End, or idle reap) with feedback still pending | Delivered to the inbox at once (`messages.deliver_background`), read in its next cycle. |
| 👎, chat already ended, or a `send_message` card / `/messages` page | Inbox at once (`messages.deliver_background`). |

Every delivered message is logged in `.supervisor/messages.jsonl` as today, with `kind: "feedback"`.

## Storage — one source of truth

`.supervisor/feedback.jsonl`, append-only, written only by the web service (one locked write per line):

```json
{"ts": "...", "event": "rated", "id": "f-<hex>", "instance": "example-a", "login": "...",
 "target": {"kind": "chat", "chat": "web-…", "k": 4} | {"kind": "message", "file": "20261029-…md"},
 "thumb": "up" | "down", "reason": "...", "reply_ts": "...", "reply_sha": "<sha256 of the reply text>"}
{"ts": "...", "event": "sent", "id": "f-…", "via": "chat" | "inbox", "message": "<inbox file>"}
{"ts": "...", "event": "cleared", "id": "f-…"}
```

Folded by `supervisor/feedback.py` (latest `rated` per target wins; `cleared` removes it). The fold is cached on the
file's (inode, mtime, size), the same as `web_chat.chats()`, because every feed refresh reads it. `reply_sha` is the
key that links a chat reply to the instance's `talk` episode, since web chat ids and talk ids differ.

## What uses it

1. **The instance**, only for thumbs the operator chose to send (above).
2. **Fine-tuning**: `build_dataset` cuts a conversation **before** any assistant turn whose text hash has a 👎, sent
   or kept, and keeps the turns before it. A 👎'd `send_message` is not in the dataset today, so nothing changes
   there. 👍, sent or kept, changes nothing: training only on liked replies would reward pleasing the operator.
3. **The operator**: `shaft feedback [name] [--days N]` lists ratings, newest first, with the reply's first line,
   the reason and whether it was sent.

What it is never used for: verdicts, ratings, value, achieved difficulty, probation or any judge input. The current
rule stays word for word ("Operator acceptance, thumbs, agreement and dismissals are never evidence for any grade",
`metrics.py:131`, `pushback.py:18`, docs/architecture.md). A sentence is added next to it: "A 👎 removes that reply
from fine-tuning data; it changes no grade." No count of thumbs appears in `self/stats.md` or anywhere else the
instance reads, so it cannot steer toward the 👍 rate.

## Milestones

| | What | Acceptance |
|---|---|---|
| **F1** | `supervisor/feedback.py` (record, fold, cache); `POST /feedback/<name>` in `web.py` (rate / clear / send, CSRF as other posts); buttons and 👎 sheet in `web_views.py` + `web.js` + `web.css`; state on chat turns, sent cards and `/messages/<file>`. "Just keep it" only, no Send yet. | In the real PWA (local throwaway server): 👍 lights and survives reload; 👎 sheet → Just keep it → lit, reason stored; clear works; an ended chat's replies can be rated; nothing appears in the instance's folder. |
| **F2** | Send: `pending_feedback` on an open chat, merged in `_flush_queue` / the send path the same way `_take_edits` merges edits; composer chip with ×; inbox delivery for ended chats, cards and pending feedback at chat end/reap; 👍 Send… link and sheet, always to the inbox; `kind: "feedback"` in the log; "sent HH:MM" on the reply. | Open chat: 👎 → Send → chip shows → next message carries the note (seen in the API request and the talk episode). Open chat: 👍 → Send… → an inbox file appears, and nothing is added to the next chat message. Ended chat / card: an inbox file appears with the note. End with pending → inbox file. Retrying a send never delivers twice (`mid`). |
| **F3** | `build_dataset` cut at 👎 turns; `shaft feedback` verb; docs (`web-and-api.md`, `architecture.md` sentence, `cli.md`). | Dataset built from a real instance with a 👎 reply: that turn and the later ones are missing, the earlier ones are still there, and the count is printed. |

## Tests (regression, named for what they pin)

- `test_feedback_kept_never_reaches_instance`: "Just keep it" writes nothing under `instances/<name>/`.
- `test_feedback_send_rides_next_message_once`: an open chat's pending 👎 is in the next user turn exactly once,
  then cleared.
- `test_feedback_thumbs_up_send_goes_to_inbox_only`: a sent 👍 on an open chat writes one inbox file and leaves
  `pending_feedback` empty, so the next chat message carries nothing.
- `test_feedback_pending_delivered_at_chat_end`: End with pending feedback → one inbox file, nothing lost.
- `test_feedback_fold_latest_wins_and_clear`, plus an unreadable or malformed line, which is skipped and never
  breaks the feed.
- `test_dataset_cuts_at_thumbs_down`, and `test_dataset_thumbs_up_changes_nothing`.
- `test_judge_never_sees_feedback_counts`: no thumbs figure in `stats.md` or in any judge prompt.

## Not in scope

- Telegram reactions (the Bot API's `message_reaction` updates could feed the same file later).
- `shaft talk` in the terminal.
- Judging conversations, and checking judges against the operator's thumbs. This is possible once conversations are
  judged; it would be reported only, never an input.

## Open decisions for sign-off

1. ~~Is 👍 store-only acceptable?~~ Decided 2026-10-29: 👍 can be sent, always in the background (inbox, next cycle).
2. Should a **kept** 👎 also cut fine-tuning data (proposed: yes, since the reason for not sending is usually
   "don't bother it now", not "the reply was fine")?
3. Open chat: is riding with the next message right, or should Send go to the inbox at once in every case
   (simpler, but the instance may repeat the mistake within the same chat)?
