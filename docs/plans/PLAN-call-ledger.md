# PLAN-call-ledger — show an instance the calls it actually made

Status: **M0 done** (2026-10-15, branches `call-ledger` + `template-call-ledger`); M1 waits for `shaft update`.

## Problem

Twice in a row on 2026-10-15, shaft told the operator about a write it never made. Both times it went on to
call it an infrastructure fault:

| Cycle | What shaft said | What its tool log shows |
|---|---|---|
| 16:53–17:01 | "two separate append_file calls … both returned success" | no append_file in its 14 calls; the model never asked for one (`logs/llm_usage.jsonl`) |
| 17:23–17:28 | "I issued append_file … (reported "appended 2029 chars")" | no append_file in its 11 model calls. No tool returned "appended 2029 chars"; the only 2029 in the log is a 10:19 token count |

Its real appends that day all landed, and the byte counts add up: 16:36 (494,936 + 2,150 = 497,086) and
17:16 (marker found once, 498,549 bytes). The runtime didn't drop anything. Every call the model makes is
either run and logged or logged as "(repeat: not run)" (`template/shaft/agent.py:118-150`).

Probable cause (a guess, not proven, because reasoning text isn't logged): Bonsai is shown its earlier steps'
reasoning (`agent.py:108-112`). An append it rehearsed while reasoning, result included, later reads like
something it did. The cycle's real call list does exist, but only after the cycle: the journal's
`**Actions:**` line and the episode record. During the cycle, the only way to know what it did is to
remember it, and that memory is what fails.

## Change

At the end of each step, the runtime adds a short **ledger** under the last tool result. The ledger is
built from `toolbox.log`, which is the record of what actually ran:

```
[runtime] Calls so far this cycle (step 9 of 12): declare_intent, read_file ×2, web_search ×2, run,
search_memory, run ×3, send_message → operator. File writes: none.
```

```
… File writes: append_file notes/ai-misalignment-trend.md → appended 1441 chars;
append_file skills/self-counts-protocol.md → appended 852 chars.
```

- **Source:** `toolbox.log` only, never the model's own words. If a call isn't in the log, the ledger
  doesn't show it.
- **File writes:** the built-in tools that change files, each listed with its path and the first line of
  its result, so ERROR shows up too: write_file, append_file, remember, rewrite_facts, write_digest,
  update_self_model, update_personality. Every other call is listed by name, with consecutive repeats merged
  (`run ×3`). `run` stays a plain name: the ledger doesn't claim what a shell command wrote.
- **Where:** added to the *message* the model sees, not to the logged result. `toolbox.log`, the judge's
  evidence and `metrics.py` (which parses results such as "appended N chars") stay exactly as they are.
- **Modes:** live, sleep and talk ("so far this reply" in talk). **Not exam**: the battery's scores stay
  comparable with earlier runs. One `ledger=` argument on `agent.run`, on by default and off for exam.
- **Size:** about 40–120 tokens per step, against prompts of 20–50k. It is capped at about 600 characters:
  the oldest calls are dropped first, and file writes are never dropped.
- **No new prompt rule:** the line explains itself. I'm not adding instructions to `prompts.py`, so this
  change doesn't add to the prompt's length or tone.

## Not doing (and why)

- **Showing the ledger only in the wrap-up message:** that misses the 17:28 case, where finish_cycle and
  send_message came voluntarily and no wrap-up message was sent.
- **Checking finish_cycle or send_message text against the log:** parsing free text for claims is fragile
  and would refuse honest reports. The judges already grade claims against the log.
- **A `my_calls` tool:** an instance that believes it already wrote the file has no reason to call it.

## Milestones

**M0 — template code + tests** (branch `template-call-ledger` off `template`, its own worktree)
- `template/shaft/agent.py`: build the ledger from `toolbox.log` and add it to the step's last tool message.
  Covers the controls (declare_intent/finish_cycle) and repeated, refused calls.
- `template/shaft/exam.py`: `ledger=False`.
- Tests in `supervisor/tests/test_template_client.py`, using FakeLLM and checking `fake.requests` (what the
  model actually saw):
  1. Regression, named after this bug: a scripted model that never calls append_file sees
     `File writes: none` on its next request.
  2. After a real append_file, the ledger names the path and the "appended N chars" result.
  3. A refused write (PROTECTED path) shows up with its ERROR line.
  4. `toolbox.log` results are byte-identical with the ledger on and off.
  5. Exam requests contain no ledger.
  6. The cap holds on a 40-call reply, and file writes survive it.
- Full suite green.

**M1 — real system** (only after sign-off on M0; `shaft update` is yours to run)
- `shaft update shaft` (gate-tested, idle only), then let one real live cycle run.
- Check `logs/llm_usage.jsonl` (prompt growth per step) and the first live cycle's request bodies (the
  ledger is present and correct).
- Acceptance: over the following day, no operator message from shaft claims a write that its episode
   log does not contain. Roll out to other instances only after this check.

## Risks

- **It might not work:** a model that confabulates can ignore the ledger too. M1's acceptance check is
  what tells us whether it helped. If it didn't, we revert the change; nothing else depends on it.
- **Trials:** a trial compares two code versions. Once this merges, both runs in a trial carry the ledger,
  so comparisons stay fair. A trial that straddles the update would compare old code with new. Don't
  start a trial on shaft between `shaft update` and the end of M1.

## Later

- 2026-10-24 (REVIEW-autonomy A5): when the supervisor serves the operational envelope (`GET /envelope`), each
  step's ledger also ends with the supervisor calls and the minutes left in the run (`agent.call_ledger`,
  `envelope=` / `left_s=`); without them it is byte-identical to before.
- 2026-11-03 (review E2/C7): the ledger and the workspace's focus line stand under the newest step's last result
  only; a step's own copy leaves when the next step adds its own, and the last one when the run ends. Its file writes
  are cut from their oldest end at 800 characters (`LEDGER_WRITES_CHARS`): each write left out is still in its own
  tool result, or in the one-line reference an older result shrinks to (`RESULTS_KEEP_TURNS`).
