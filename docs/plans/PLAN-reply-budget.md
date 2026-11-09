# PLAN — the instance sets its own conversation reply budget, under a ceiling

Asked 2026-11-08: "give more budget for conversations, but also give the instance flexibility in determining, given the
question, what the right budget is. Over a certain threshold, it should be queued for the next cycle."

## Why (measured)

- Today the budget is fixed: `TALK_REPLY_CALLS=3` tool calls or `TALK_REPLY_S=45` s per reply
  (`template/shaft/config.py:34`, enforced in `template/shaft/talk.py:408`). After that, the next model call is
  offered only `queue_for_next_cycle`.
- A reply that needs two searches and four page fetches can exhaust this budget in one batch.
  A measured browser page load took about 46 s, more than the whole 45 s reply budget.
- Reply-first requires a ceiling: a reply must release the instance within a bounded time.
  This rationale also appears in (a private review record, not published).

## Design

1. **New conversation control `plan_reply(calls, seconds, why)`.** It sits next to `queue_for_next_cycle` in
   `talk.py`. The instance calls it, usually first, to set this reply's budget from what the question needs.
   Both numbers count from the start of the reply. It may call it again to change the plan.
2. **Ceiling.** New knobs `TALK_REPLY_MAX_CALLS` and `TALK_REPLY_MAX_S`. A plan above the ceiling is refused with
   the ceiling and the instruction: answer with what you can now, and `queue_for_next_cycle` the rest. The previous
   budget stays in force. Work expected to go past the ceiling is therefore queued up front, not cut off halfway.
3. **Default when no plan is set.** `TALK_REPLY_CALLS` / `TALK_REPLY_S` are raised (numbers below).
   Enforcement is unchanged: once the budget is used, the next model call gets only `queue_for_next_cycle` plus the
   "reply now" nudge.
4. **The operator sees the plan.** `view.status("planned", "up to N calls / S s: <why>")` is shown in the web chat,
   the terminal, the API and Telegram, so the wait is announced, not silent.
5. **It is recorded.** The talk episode gets `reply_plan`: {calls, seconds, why, used_calls, used_s, refused,
   queued}. That lets us check later whether the plans fit the questions.
6. **Prompt.** `prompts.reply_budget()` states the default, the ceiling and `plan_reply`. It notes that a
   `browse` page costs about 45 s, and says to queue when the expected work is above the ceiling.

## Numbers (for sign-off)

| Knob | Today | Proposed |
|---|---|---|
| `TALK_REPLY_CALLS` (default, no plan) | 3 | 6 |
| `TALK_REPLY_S` (default, no plan) | 45 | 90 |
| `TALK_REPLY_MAX_CALLS` (ceiling) | — | 20 |
| `TALK_REPLY_MAX_S` (ceiling) | — | 300 |

0 still disables each knob.

## Constraints kept

- A conversation holds the instance only while it replies (`runner.open_conversations`). The ceiling bounds that
  hold, so a cycle waits at most `TALK_REPLY_MAX_S` plus the call already running.
- Trial replays: no change to the ReplayBroker. A replayed talk turn uses the same control and is recorded only.
- The instance's own tools/plugins path is untouched. `browse` is simply affordable in a conversation now.

## Milestones

- **M0 (worktree `reply-budget`)**: `plan_reply` + ceiling + default knobs in `template/shaft/talk.py` and
  `config.py`; prompt text in `prompts.py`; `template/config.env`.
  Accept: unit tests, each naming the bug it guards:
  a plan inside the ceiling is honoured (a 4th call is allowed after `plan_reply(8, …)`);
  a plan above the ceiling is refused and the old budget holds;
  with no plan the default applies;
  a used-up budget offers only `queue_for_next_cycle`;
  `reply_plan` lands in the episode.
  The tests pinning 3/45 (`supervisor/tests/test_m3_talk.py`, `test_prompt_knobs_template.py`) are updated, and the
  full suite plus the gate pass.
- **M1**: the status line shows in the web chat, terminal, API and Telegram views.
  Docs: `docs/configuration.md` §TALK_REPLY, `docs/web-and-api.md`.
  Accept: in the real web chat, ask example-b something that needs about 5 fetches or one `browse`. The status line
  shows its plan and the reply is answered without queuing. Then ask for something bigger than the ceiling: it
  queues up front and says so.
- Merge only on the operator's word. The template change reaches instances at their next wake.

## Decided (operator, 2026-11-08)

- The numbers above: "Fine".
- **"Take your time" or anything like it means: do it in a cycle.** The instance reads that intent from the operator's
  words. No keyword match in code. It replies briefly with what it will do, calls `queue_for_next_cycle` with the full
  task, and does not spend the conversation budget on it. The ceiling is never lifted. The prompt text in
  `reply_budget()` says this in so many words, and a prompt test pins that sentence.
