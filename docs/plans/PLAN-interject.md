# PLAN-interject — a Telegram message sent while the instance is replying reaches that reply

## Problem (today)

`telegram.Chat.handle` holds `Chat.lock` for the whole `ask_api` call. A second message from the operator waits on
the lock until the first reply has ended, then starts a second, separate reply. While example-a spends ten tool steps
on message 1, message 2 ("actually, skip the calendar") is invisible to it.

## What changes

A message that arrives while a reply is in progress is **added to that reply at its next step**: the instance
sees it as a new user message right after its last tool result, before its next model call, and its answer
covers both. Nothing is cut off: a model call or tool already running finishes first (a cut-off call would
throw away its thinking — up to ~12 minutes on the local model).

If the reply is already writing its final answer (no further step), that answer is sent and the new message is
answered **straight after**, in a follow-up reply that starts at once (not queued behind a lock as now).

## Path

```
Telegram service                 API server (api.py)                     instance agent (template/shaft)
Chat.handle(msg2)  ── POST /v1/chat/interject {model, messages} ──▶  live[talk_id] → append to
  (reply 1 in flight)                                                  .tmp/interject-<talk_id>.jsonl
                                                                                │  read at each step boundary
                                                                     agent.run: messages.append(user msg2)
                                                                     emits {"type":"heard"} · logs the episode
reply 1 returns ◀── response carries "interjections_heard": k ◀──── API counts heard events
history: u1, msg2…(first k), answer; any leftover → one follow-up reply right away
```

- **Supervisor ↔ agent channel**: a file in the instance's `.tmp/`, the same pattern as `.tmp/yield` and
  `.tmp/pause-<pid>` (written with `safe_write`). Keyed by the talk id, which both sides already know. No stdin
  protocol change, so an instance not yet updated simply never reads it: it reports 0 heard and every
  interjected message becomes the follow-up reply — degraded to today's behaviour, never lost, never a hang.
- **API** (`supervisor/api.py`): a `live` registry of in-flight replies by talk id (messages that arrive before
  the agent process starts — e.g. while it waits for another conversation — are written as soon as it starts, so
  they are read at step 1). New route `POST /v1/chat/interject`: 202 if a reply for that conversation is in
  flight, 409 if not. Same auth as chat (bearer key + tailnet login). The file is removed when the reply ends.
  `heard` events become a reasoning line ("↪ the operator added: …") for streaming clients and a count in the final
  JSON (`interjections_heard`).
- **Agent** (`template/shaft/agent.py`, `talk.py`): `agent.run(..., interjections=callable)` drains new messages
  at the top of every step (before the pause/rollback mark, so a paused-and-redone step keeps them). `answer()`
  supplies a reader of `.tmp/interject-<talk_id>.jsonl` that logs each message as a talk episode (so the journal
  write-up has it) and marks it in the prompt as written by the operator while I was replying; images it lists are
  attached like the first message's.
- **Telegram** (`supervisor/telegram.py`): `Chat` keeps an in-flight list under a short mutex. A message during
  a reply is appended there and POSTed to interject (mutex held across the local POST, so a message can never
  land in two replies). The in-flight thread, when its reply returns, puts the first k heard messages into the
  history before the answer; the rest (409s, the final-answer race, an old agent) are merged into one user
  message and answered at once. Commands (`/status`, `/new`, …) are unchanged.

## Not in scope

- `shaft talk` (terminal): queued messages are still sent when the reply ends, as its help text says. The same
  agent hook makes it a small follow-up if you want it.
- Cutting off the model call in progress (see above).

## Tests (regression, named for the bug)

- agent: a message interjected while a tool runs is in the very next model call, after the tool result; one
  arriving during the final answer is not consumed (heard = 0).
- api: interject → 202 and written to the file in order; 409 with no reply in flight; buffered before the
  process starts; file removed at the end; `interjections_heard` in the response.
- telegram: second message during a slow fake reply does not wait for the lock, reaches `/interject`; history
  order u1, u2, answer; leftover becomes one follow-up reply; nothing answered twice.
- Full suite after.

## Delivery

Branch `claude/interject` + `template-interject` in a worktree (main has uncommitted panel/recap work). Verify
end-to-end with the real Telegram bot of one instance: send a message that triggers tool use, then a second one
mid-reply, and check the single answer addresses both. Merge to main/template and `shaft update` of the
instances only on your go-ahead.
