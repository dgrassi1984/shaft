# SHAFT — Plan: a prediction-error (surprise) loop

Status: **decisions recorded 2026-10-13 (§6). M0 (the baseline) is built and frozen. M1–M3 and the M5 report are built
(2026-10-25, branches `claude/surprise-m1-m3` and `claude/surprise-m1-m3-template`; §3b), not merged, not deployed. M4 is
not started (gated). The M5 window starts when the operator deploys M3.** Every number below was measured on 2026-10-13 from `instances/*/memory/episodes/*.jsonl`, `instances/*/logs/llm_usage.jsonl`,
`.supervisor/judgments/*.jsonl` and `self/stats.md`.

Calibration update (2026-10-24): confidence tagged `confidence_of="criterion_met=yes"` is v2,
P(criterion met = yes), scored with binary Brier: yes=1, partial/no=0. Untagged confidence is v1,
the older "probability of success" question, scored yes=1/partial=0.5/no=0. Stats and reviews report
the versions separately, never pooled. The frozen M0 baseline's `brier` remains v1 over v1 rows only;
`brier_v2` and `n_v2` report v2 separately. Historical numbers below retain their v1 definition.

Current control-schema note (#130–#132): both the surprise judge and its no-surprise control also request
`delivery_supported`, `deliverable_met`, `self_imposed_clauses`, `self_audit`. The control still omits only
surprise fields. `finish_cycle` now requires `delivered_via` and sends nothing itself. See
[Architecture](../architecture.md#measuring-improvement-and-autonomy) for the field meanings and stats windows.

## 0. The problem, in one paragraph

Today an agent makes one prediction per cycle: a confidence number in `declare_intent`
(`template/shaft/live.py`). The supervisor scores it as calibration in `self/stats.md`
(`supervisor/metrics.py`). The agent never says what it expects a search, a fetch, a test or a write
to return, so it cannot be surprised in any way that gets recorded. Unexpected results do happen, and
sometimes the agent notices them in prose. But nothing records them as surprises or follows them up, and
nothing measures whether the same surprise comes back. The loop should:

1. turn "what I expected" into a short written statement made **before** the result exists;
2. compare it with what came back, by a cheap rule, by the agent itself and by the judge;
3. use the difference: to decide what is remembered, to suggest the next goal (a curiosity queue), and to
   recalibrate (stats).

### What the real data shows

These surprises happened in the first 18 judged cycles plus talks. None was recorded as a surprise.

| When | Who | Expected (implicitly) | Observed | What came of it |
|---|---|---|---|---|
| 10-12 20:34 live | shaft | its journal: "698 incidents" came from UK AISI | arXiv 2604.09104: from CLTR, and a sample rather than a census | Corrected in the trend file. It became a fact at sleep (facts.md, 04:50) and spawned agenda Task 2, "verify my own claims". **The loop working by accident.** |
| 10-13 06:53 live | shaft | `write_file self/self.md` works | `PermissionError: use update_self_model` | Retried correctly. Not remembered. The same class came back in a talk at 13:44 (`append_file memory/journal.md`), and **the identical call failed again in the 14:40 live cycle**: three times in eight hours |
| 10-13 12:12 live | example-a | `write_file self/personality.md` works | the same PermissionError class | Retried. Not remembered |
| 10-13 06:53 and 14:40 live | *the measurement* | the judge sees what the agent saw | the tool log keeps 2,000 characters of each argument and result, so the judge saw the 4.7K `self.md` read-back, and the `update_self_model` text, cut short | **Not an agent surprise: a defect in the measuring instrument.** The judge graded both partial and "claim unsupported" partly on text it could not see. `_written_files` shows the judge files written by `write_file` only, not `update_self_model`. Proposed as a separate fix (§5). |
| 10-13 13:32 live | shaft | "the supervisor stopped me" | the judge: it called finish_cycle itself on turn 2 | A wrong belief about itself, never recorded |
| 10-13 04:42 live | example-a | `exit=0` is the test's exit code | it was tail's exit code; the judge gave partial | Became a fact at sleep; the next cycle (06:53) closed the gap |
| 10-13 08:58 live | example-a | the heredoc patch applied | `zsh: can't create temp file`, exit 0, and the test passed on unchanged code | example-a caught it from stderr, which a 2-bit model would likely miss |

What this shows:

- Surprises are the high-information events. Several already lead to useful follow-ups, but only
  by accident, mostly in example-a (the cloud model).
- In shaft (Bonsai 27B at 2 bits) the same class of error comes back, and its wrong belief about itself
  (13:32) is never recorded. On Bonsai alone (6 judged cycles) it states 84% against 58% achieved, Brier
  0.19.
- **The headline figures mix models.** Shaft's first cycle ran on Haiku. example-a ran on Bonsai, then Haiku,
  then MiMo, so "example-a 95% on MiMo" is really MiMo 88% over 4 cycles (Haiku: 90% over 5). From M0 on, every
  cost and success number is per model, and every comparison is within one model.
- Several surprises are **silent**: no `ERROR` appears (tail's exit code, the heredoc). A rule on errors
  alone would miss them. Catching them needs a stated expectation, or the judge.

## 1. Decisions

### 1.1 Granularity: predictions per cycle, plus an optional `expect` per tool call; nothing per step

| Level | Decision | Why |
|---|---|---|
| **Cycle** | `declare_intent` gets `predictions`: up to 3 short, specific statements of what the work will find ("the watch finds no new primary source since 11:57"; "the existing tests still pass after the fix"). The prompt asks for them. They are optional, so a cycle cannot fail for lacking them. | This is the cheapest level with the most information. It costs one argument in a call the agent makes anyway, so there is no extra tool round. For shaft's standing rogue-AI watch, a prediction about the content *is* the watch's purpose: a contradicted prediction means news. |
| **Tool call** | Every tool spec in **live cycles** gets an optional `expect` string (at most 200 characters). The prompt asks for it only on consequential calls: a search, a fetch, `run` or a test, a write to code or to a protected path, `send_message`. `agent.run` removes it before dispatch, so plugins and broker tools never see it, and the plugin path is unchanged. **Staged as M4, behind a measured overhead gate on shaft.** | This catches the silent operational surprises (the heredoc, tail's exit code). Those matter most for a 2-bit model that misjudges its own tools. It is only an argument on a call already being made, so it adds no extra round. |
| Step | No. | A "step" is one model turn holding 1–2 tool calls (shaft: 21 turns with one call, 12 with two). There is nothing to predict at that level that a call-level expectation does not already cover. |
| Talk, sleep | Off. | In a talk the operator is waiting, and talks are not judged. Sleep looks inward and has little to predict. The open-surprise queue is still **shown** in every mode, so the operator can ask "what surprised you?" |

Rejected alternatives:

- A separate `expect` tool, or a reflection turn after each result. Each would double the rounds on
  Bonsai, at a median of 91 s per round (M0 baseline, Bonsai turns only).
- Echoing the expectation next to every result. The expectation is already in the context, as the call's own
  arguments. An annotation is added only when the rule flags a mismatch (1.2).
- Numeric confidence on each call. The judgments are text, and the per-cycle confidence already carries
  the calibration number.

### 1.2 How surprise is measured: three layers, ordered by cost; the judge decides

1. **Rule (free, in the moment).** `agent.run` tags a tool-log entry with `flag` when a result is a known
   failure shape:
   - `error`: a result starting with `ERROR`;
   - `nonzero_exit`;
   - `stderr_on_success`: exit 0 while stderr matches `not permitted|No such file|Traceback|can't`;
   - `empty`: `(empty)`, `nothing relevant found`, no results;
   - `truncated`: `[... truncated`;
   - `refused`: a repeat-guard refusal.

   If the call's `expect` (or, in M1 before `expect` exists, the cycle's predictions) did not anticipate
   it, a one-line note is appended to the result: `[unexpected: <flag>; you expected: …]`. This is
   what puts the heredoc failure in front of Bonsai at once. example-a noticed it; shaft would likely not.
2. **Self-report (cheap, once per cycle).** `finish_cycle` gets `surprises`: a list of
   `{expected, observed, so_what}` in plain text, and `resolves`: open surprise ids this cycle answered,
   each with how.
3. **Judge (authoritative; it runs anyway).** `JUDGE_SCHEMA` gets:
   - `expectations`: one verdict per stated prediction or `expect`: `held`, `contradicted` or
     **`uninformative`**;
   - `unreported_surprises`: contradictions in the log of the agent's stated expectations, or of what its
     intent assumed, that its `surprises` field leaves out;
   - `reported_surprises_supported`: a boolean;
   - `followup_useful`: when the goal came from a surprise, did the cycle actually reduce that
     uncertainty (`yes`, `partial`, `no`, `na`).

**Why this cannot be gamed by predicting trivially.** Surprise is never rewarded, and neither is its absence.

- A trivial expectation ("the tool returns a result") is marked `uninformative` by the judge. The share
  of informative expectations is shown in stats, so an agent hedging everything sees itself doing it.
- Inventing surprises to create easy follow-up goals fails twice: the judge checks each reported surprise
  against the log, and the follow-up cycle is graded on usefulness and difficulty-weighted (the sibling
  plan's weighting).
- Expectations cannot be written after the fact. `expect` is a call argument, logged before its result
  exists, and episodes are append-only (`PROTECTED_DIRS["memory/episodes"]`).

**Effectiveness first** (project rule). The new judge fields are secondary and must not move
`criterion_met`. M2 tests that directly.

The rule list exists twice: in the template, for the in-the-moment note, and in a new
`supervisor/surprise.py`, which recomputes flags from the episode JSON for measurement. The supervisor never
imports agent code, and the agent cannot alter the copy that measures it. Shared string fixtures in the
tests keep the two copies in step.

### 1.3 What surprise drives, so that it is functional and not decoration

| Drives | Mechanism |
|---|---|
| **Goal choice** (a curiosity queue) | Open surprises are derived from episodes by `template/shaft/surprises.py`, which folds the opened/resolved events. They are shown in the system prompt as "Open surprises (my curiosity queue)": at most 8 items and 1,200 characters, oldest first, each with its id and age. `LIVE` step 1 adds: *"…or an open surprise: the one whose answer would most change what you do."* The goal records where it came from with PLAN-measurement's provenance fields, `origin: self` and `origin_ref: surprise:<id>`; there is no separate field. **Curiosity cycles are drawn at random**. When the queue is not empty, each live cycle is a curiosity cycle with probability 1/`CURIOSITY_EVERY_CYCLES`; the default of 5 gives one in five on average, and 0 means never. On a drawn cycle, `LIVE` says: *"This is a curiosity cycle: pick your goal from the open surprises below."* The draw is skipped when the inbox holds a message from the operator, since operator messages still come first. The draw and its outcome are recorded in the episode as `curiosity: {drawn, skipped_for}`. On cycles that are not drawn, the queue is only offered, not imposed. |
| **Memory** | (a) Surprise records join the `memory.corpus`, so `search_memory` and the automatic recall in `live.run` find past surprises on the same topic before a similar call. (b) A new **sleep step** (in `SLEEP`): for each surprise since the last sleep, do one of three things: remember the corrected belief as a fact citing the surprise id; leave it open as a question worth a cycle; or close it with the built-in `close_surprise(id, outcome, note)`. The digest names what surprised me today. None may be silently skipped, and the supervisor counts this. |
| **Recalibration** | A new `stats.md` section, "My expectations vs what happened": stated and informative counts; the share that held; **where I am most often wrong**, by tool and by topic; unreported surprises; open, resolved and expired counts with the oldest age; recurring surprise classes. It sits next to the existing confidence and Brier lines, so "I was wrong about `run` 3 times in 5" is in front of the agent when it sets its next confidence. |
| **Loop guards** | At most 8 open surprises; when a new one would exceed the cap, the oldest expires, and the expiry shows in stats. An open surprise expires after 7 days. A surprise gets one follow-up cycle unless that follow-up was judged useful. A surprise opened by a follow-up of a surprise is marked depth 2 and is not shown in the queue, which prevents rabbit holes. |

### 1.4 Template (inherited) versus what instances build themselves

Seed in the template only what an instance cannot build reliably, or what measurement depends on:

| Seeded in the template | Why it is not left to instances |
|---|---|
| `predictions` / `surprises` / `resolves` on the two control tools (`live.py`); `origin_ref: surprise:<id>` on PLAN-measurement's provenance field | The controls belong to the cycle harness, and the judge needs a stable shape to read. |
| `CURIOSITY_EVERY_CYCLES=5` in `config.env` and `config.py`, with the random draw in `live.py` | A config value, so the instance can tune it like any other setting. The draw is recorded, so a change to the setting stays visible. |
| `expect` stripping and the rule flag in `agent.run` (M4) | It must apply to every tool, plugins included, without each plugin changing. |
| `surprises.py` (fold episodes into the open queue, issue ids), the queue block in `prompts.CONTEXT`, `close_surprise` as a built-in | The record must be append-only and complete, or the measurement means nothing. |
| One line each in `LIVE` and `SLEEP` | Without the prompt, a small model never uses the fields. |
| `skills/surprises.md`: how the loop works, plus plugin ideas | Text, which is cheap. It points the instance at what to build. |

Left for the instances to build, as plugins or as their own code:

- Triage and ranking of the queue (a `surprise_queue` plugin: dedupe, merge into one question, priority).
  example-a's existing `task_queue` plugin is the obvious host.
- Domain predictors. For example, shaft's watch could predict "N new sources" from its own trend file,
  with a plugin checking the prediction.
- Automatic re-checks: re-fetch a page next cycle; re-run a test the next day.
- Their own surprise reports and correlations, such as "surprised more after long thinking".

The supervisor gains **no tools** under this plan. The only additions are measurement:
`supervisor/surprise.py`, the judge fields and the stats section. An instance may later edit or remove its
own copy of the seeded parts, since it is its genome. That goes through the gate, probation and the replay
trials like any self-edit, and stats would show "no predictions in the last N cycles", so the removal stays
visible.

### 1.5 Measurement: how to tell whether it made the agent more effective

These are pre-registered before M1 ships, so the evaluation is not chosen after seeing the data. Details
are in §4. In short:

- **Primary, functional:** surprises get closed rather than piling up, and the same class of surprise
  recurs less once it has been recorded.
- **Primary, honesty:** fewer unreported surprises and fewer unsupported claims (shaft today: 2 of 5
  claims unsupported).
- **No-harm guard:** difficulty-weighted judged success, from the sibling plan, does not fall by more
  than the probation margin (0.15). Latency and tokens stay within budget.
- **Reported but not gated:** calibration. Brier and the confidence gap are too noisy at this throughput
  (§4.3).

### 1.6 Costs and risks

These are measured on shaft's Bonsai turns only (`ternary-bonsai-2-27b-mlx`). An earlier version of
this plan mixed in shaft's one Haiku cycle and understated Bonsai's turn time. A least-squares fit over 172
calls gives per turn **≈ 59 s per 1k completion tokens and ≈ 1.6 s per 1k prompt tokens** (residual SD
38 s). The M0 baseline for Bonsai live cycles: median turn 91 s, prompt 17.6k tokens, completion 1,198
tokens; a median cycle is 7 turns and about 12 minutes of model time.

| Item | Estimate on shaft | On example-a (MiMo, cloud) |
|---|---|---|
| `predictions` at declare plus `surprises` at finish | +~300 completion tokens per cycle ≈ **+18 s on ~12 min (≈2.5%)** | <$0.001 |
| Queue block in context (≤1,200 characters) | +~300 prompt tokens per turn ≈ +0.6 s per turn | negligible |
| `expect` per call (M4) | ~30 tokens × 1.4 calls per turn ≈ +2.5 s on a 91 s turn, plus ~200 prompt tokens per turn for the extra spec field (+0.3 s) ≈ **+3%**. **Unknown: extra reasoning.** Hence the M4 gate. | negligible |
| Judge | +~400 output tokens per judgment, about 15 judgments a day on Opus 5.5 | same |
| Re-judging the historical cycles for a baseline (M2) | **none extra**: asked in the same pass as PLAN-measurement's E0 backfill (one judgment per episode carrying both sets of new fields) | — |

| Risk | Mitigation |
|---|---|
| Trivial or hedged predictions | The judge marks them `uninformative`, and stats show the share. Nothing rewards a low surprise rate. |
| Manufactured surprises | The judge checks each against the log, and follow-ups are judged on usefulness with difficulty weighting. |
| Curiosity loops and rabbit holes | Cap 8, 7-day expiry, one follow-up per surprise, depth-2 surprises hidden, operator first. The existing repeat guard in `agent.run` is unchanged. |
| Context growth | Hard caps on the block (characters and items). Surprise records reach memory through search, not by being pasted into context. |
| Bonsai mangling tool arguments because of the extra field | `expect` is optional and removed before dispatch; a missing or malformed one changes nothing. Tested with fakellm. M4 measures the tool-error rate before and after. |
| More thinking on Bonsai, inflating latency | M4 is gated on measured median turn duration and completion tokens. If it fails, the per-call level is **deleted**, not left switched off. |
| The judge being gamed | Expectations are logged before results; episodes are append-only; the new fields cannot move `criterion_met`, which M2 tests. |
| The standing task crowding out curiosity (shaft) | Random curiosity cycles, one in 5 on average (`CURIOSITY_EVERY_CYCLES`), only when the queue is not empty. The standing task keeps about 80% of cycles. |

## 2. Data model (all derived from episodes; no new mutable store)

- Episode `live` record gains:
  - `intent.predictions: [str]` (a goal from a surprise carries `intent.origin_ref: "surprise:<id>"`, PLAN-measurement's field);
  - `finish.surprises: [{expected, observed, so_what}]` and `finish.resolves: [{id, how}]`;
  - on each `tools[]` entry: `expect` (M4) and `flag`.
- Sleep episodes gain `closed: [{id, outcome, note}]`, written by `close_surprise`.
- Id: `s<MMDD>-<HHMM>-<n>` from the cycle's timestamp, readable and quotable in facts ("from surprise
  s1013-0653-1").
- The open queue is a pure function `surprises.open(root)`: it folds episodes in order, and stores no state
  of its own.
- Supervisor side: `.supervisor/surprises/<name>.json`, recomputed from episodes and judgments, holding
  the rule flags, recurrence classes and resolution ages. It feeds `stats_text` and the M5 report.

As built (2026-10-25), in addition:

- Only `finish.surprises` opens a surprise; rule flags are measured, never queued. The fold, in order per live
  episode: age expiry, follow-up bookkeeping, `resolves`, new surprises, the cap; a sleep's `closed` closes. Malformed
  entries issue no id; a resolve without a `how` or a close without a `note` does nothing (P1 counts work, not a bare
  id). A malformed episode line is skipped. `supervisor/tests/surprise_fixtures.py` is the contract both copies pass.
- Live episodes carry `curiosity: {drawn, skipped_for}` (`skipped_for`: null, `empty_queue`, `operator_message` or
  `disabled`); tool-log entries carry `flag` and, when there is one, `flag_detail`.
- `close_surprise(id, outcome, note)`: outcome `answered`, `not_worth_pursuing`, `duplicate` or `unanswerable`; a note
  is required. Sleep only.
- An `origin_ref: surprise:<id>` must name one of the instance's surprises and comes with `origin: self`, or
  `declare_intent` refuses it (P4 depends on the field being right).
- Judge fields: `expectations: [{statement, verdict: held|contradicted|uninformative, evidence}]`,
  `unreported_surprises: [str]`, `reported_surprises_supported: bool` (true when none were reported),
  `followup_useful: yes|partial|no|na`. `record(kind='judgments', format='json')` rows carry `followup_useful`, which
  the template's queue reads from `memory/verdicts.jsonl`.

## 3. Milestones

| M | What | Paths | Acceptance |
|---|---|---|---|
| **M0** Baseline (no change to agents). **Done 2026-10-13.** | Rule flags and recurrence classes recomputed from the existing episodes; cost and judged success **per model**. `shaft surprise [names] [--freeze]`. | `supervisor/surprise.py` (new), `supervisor/__main__.py` (verb), `supervisor/tests/test_surprise_rules.py` | **Met.** All 14 error events found by hand (shaft 5, example-a 9) are reproduced, among 12 + 11 flagged results in total (empty results and repeat refusals included). The example-a 08:58 heredoc is `stderr_on_success (not permitted)`. A test caught the same sandbox denial being split into two classes, which is fixed. Frozen at 2026-10-13 14:43 to `.supervisor/evals/surprise-baseline.json`. Recurring classes: shaft `error:PermissionError:write` in 3 episodes (06:53, 13:44, 14:40) and `empty:list_files` in 3; example-a `nonzero_exit:not permitted:run` in 2. Full suite: 162 of 163 pass, 7 skipped. The 1 failure asserts the checkout folder is named `shaft`, so it fails in any worktree. **Re-freeze just before M1 merges**, so the "before" period ends where the change begins. |
| **M1** Per-cycle predictions, self-report, queue (template). **Built 2026-10-25, not deployed** (§3b). | `predictions` / `surprises` / `resolves` on the controls; `surprises.py`; the queue block in `CONTEXT`; `LIVE` text; `close_surprise` built-in; surprise records in `memory.corpus`; the rule flag on the tool log (with the note judged against `predictions` until M4); `skills/surprises.md`. | `template/shaft/{live,agent,prompts,tools,memory,surprises}.py`, `template/skills/surprises.md`, `supervisor/tests/test_surprise.py` (fakellm) | Fakellm cycle: a contradicted prediction lands in the episode, shows in the **next** cycle's rendered system prompt, and is found by `search_memory`. A `write_file self/self.md` gets the `error` flag and the `[unexpected …]` note. **The scripted run uses the same number of model turns with and without M1.** With 8 open items the queue block is ≤1,200 characters. A malformed `predictions` value does not break `declare_intent`. Merged into example-a then shaft through `shaft update` (gate-tested), with the sibling's template-update journal note. |
| **M2** Judge and stats (supervisor). **Built 2026-10-25. Re-judge run 2026-10-25 (`--limit 60` per instance on shaft, example-a and example-b, decided by the operator), no-fields control run 2026-10-26: PASS against the control** (§3b). | Builds on PLAN-measurement's structure (branch `measurement`): the new fields are one more props dict (`SURPRISE_PROPS`) merged into `JUDGE_SCHEMA` through its `_schema()`, next to `VERDICT_PROPS` and `RATING_PROPS`; P4 and P5 use its `metrics.cycle_value` (achieved difficulty); the stats section goes after its `_achieved_section`. New `JUDGE_SCHEMA` fields and a `JUDGE_SYSTEM` paragraph ("surprise is neither good nor bad; grade whether the expectations could have been wrong, and whether the agent's account matches the log; this never changes criterion_met"). New stats section. Re-judge the historical cycles, in the same pass as PLAN-measurement's E0 backfill, into `judgments/<name>-surprise-baseline.jsonl` (kept separate; the live judgments are untouched). | `supervisor/metrics.py`, `supervisor/surprise.py`, `supervisor/tests/test_supervision.py` | On the historical cycles, the surprise fields move `criterion_met` no more often than the same judge does when re-asked without them: per instance, the cycles only the fields run moved do not outnumber those only the no-fields control moved by more than chance allows (one-sided sign test, p ≥ 0.05; `shaft surprise-rejudge <names> --control --report`). **Changed 2026-10-26, decided by the operator:** the first bar, "all but at most 1 in 15 agree with the stored verdict", sat below the judges' own re-ask noise. **Fields run 2026-10-25, same judge per cycle:** shaft 54/60, example-a 54/60, example-b 59/61 (167/181; 9 stricter, 5 more lenient), a FAIL on shaft and example-a under that bar. **Control run 2026-10-26:** the same 181 cycles, cases and judges, the question without the fields, into `judgments/<name>-surprise-control.jsonl`: shaft 57/60, example-a 54/60, example-b 57/61 (168/181; 5 stricter, 8 more lenient), so the control alone failed the old bar too (13 of 181 moved). Only the fields run moved / only the control moved: shaft 5/2 (p 0.23), example-a 3/3 (p 0.66), example-b 0/2 (p 1.00), all 8/7 (p 0.50): **PASS on every instance.** By judge (fields / control agreement): glm 87/93 of 97, deepseek 58/54 of 61, mimo 22/21 of 23, opposite leans on small counts. Read it as "the fields do not measurably move `criterion_met` at this size", not as proof of none: pooled, with 15 cycles moved by one run only, the test fails only a split of 12 to 3 or worse. Exception: if the instrument fix (§5) has landed, cycles whose verdict depended on cut-off text may legitimately change, and they are listed. With `surprises` blanked, `unreported_surprises` names the CLTR misattribution (shaft 20:34), the heredoc (example-a 08:58) and the repeated `write_file self/self.md` (shaft 14:40). Stats render on the real instances: open `self/stats.md` as the agent sees it in its prompt. |
| **M3** Sleep consolidation and goal choice. **Built 2026-10-25, not deployed** (§3b). | `SLEEP` step; the digest mentions surprises; queue cap, expiry and depth rule; provenance value `surprise:<id>` (shared with the sibling plan); random curiosity cycles (`CURIOSITY_EVERY_CYCLES`, default 5) with the draw recorded in the episode. | `template/shaft/{prompts,sleep,surprises,live,config}.py`, `template/config.env`, tests | Tests: with a seeded random source, the share of drawn cycles over 1,000 simulated draws is 0.20 ± 0.03; no draw when the queue is empty or an operator message is waiting; 0 disables it. **On the real system**, the first sleep after M3 on both instances: every surprise since the last sleep ends up remembered (a fact citing its id), closed, or left open with a reason; one that already expired (the cap or 7 days) before the sleep needs nothing and is counted as expired. 0 left unhandled, counted by `supervisor/surprise.py`. Read the resulting facts and journal as rendered. |
| **M4** Per-call `expect` (gated experiment) | Optional `expect` on tool specs in live cycles, removed before dispatch; the note compares against it. | `template/shaft/agent.py`, `live.py`, tests | **Go/no-go after 10 shaft live cycles**, against the M0 baseline. Go only if: median turn duration +≤10%; completion tokens per cycle +≤15%; steps per cycle not up; tool-error rate not up; ≥50% of consequential calls carry an `expect`, of which ≥60% are judged informative. No-go: delete the per-call code and keep M1–M3. |
| **M5** Acceptance trial and keep/kill. **The report is built (`shaft surprise-report`); the window has not started.** | Six days with everything on; the report comes from a new `shaft surprise-report <name>` verb that renders P1–P5 against the baseline as a table; the decision is recorded in this file. | `supervisor/__main__.py` (verb), `supervisor/surprise.py` | §4 |

## 3b. Status of M1–M3 and the M5 tooling (2026-10-25)

Built on `claude/surprise-m1-m3` (supervisor) and `claude/surprise-m1-m3-template` (agent code). Not merged, not
deployed; no live instance was touched and no paid call was made.

| M | Built | Where | Tests |
|---|---|---|---|
| M1 | `predictions` on `declare_intent` (0–3, a malformed value trimmed, never an error); `surprises` and `resolves` on `finish_cycle`; the rule flag on every tool-log entry and, in live cycles only, the `[unexpected …]` note when the predictions did not anticipate a flagged result; `shaft/surprises.py` (rule, fold, queue, block, docs, draw); the queue in `prompts.CONTEXT`; LIVE steps 1 and 3; surprises in `memory.corpus`; the journal names the ids opened and resolved | `template/shaft/{surprises,live,agent,prompts,memory}.py` | `supervisor/tests/test_surprise.py` (FakeLLM; the §3 acceptance: episode → next prompt → `search_memory`; the `write_file self/self.md` flag and note; the same number of turns; ≤1,200 characters with 8 items; malformed predictions) |
| M2 | `SURPRISE_PROPS` merged into `JUDGE_SCHEMA` through `_schema()`, validated by `_check_ratings`; the `JUDGE_SYSTEM` paragraph; the case names the surprise a follow-up came from; `record` text and json; the stats.md section "My expectations vs what happened" right after the achieved section (≤9 lines, ≤900 characters); `shaft surprise-rejudge` (the historical re-judge into `judgments/<name>-surprise-baseline.jsonl`, newest first, resumable, `--dry-run` prices it; each cycle by the judge that gave its stored verdict, `--limit N` the newest N cycles; rows from another judge, written before 2026-10-25, set aside in `<name>-surprise-baseline-cross-judge.jsonl`) and its agreement report; `--control` (the same cycles by the same judge without the fields, into `<name>-surprise-control.jsonl`) and the M2 acceptance against it | `supervisor/{metrics,broker,surprise,__main__}.py` | `test_surprise_judge.py` (the fields never move `criterion_met`, success, `cycle_value`, calibration, probation), `test_surprise_ledger.py` |
| M3 | the curiosity draw (`CURIOSITY_EVERY_CYCLES`, default 5; only with a non-empty queue and no operator message; recorded); `close_surprise` and SLEEP step 7 with the list of surprises since the last sleep; the digest names surprises; cap 8, 7-day expiry, one follow-up unless judged useful, depth 2 hidden; `skills/surprises.md`; `disposition` counts each sleep's handling | `template/shaft/{live,sleep,prompts,config,surprises}.py`, `template/config.env`, `template/skills/surprises.md`, `supervisor/surprise.py` | `test_surprise.py` (1,000 seeded draws: 0.20 ± 0.03; no draw on an empty queue or with an operator message; 0 disables; a replay draws the same), `test_surprise_ledger.py` |
| M5 | `shaft surprise-report <names> --since <M3 live>`: window validity (days, judged cycles, model restart), P1–P5 against the frozen baseline with PASS / FAIL / NOT ENOUGH DATA, the kill rule, the drawn-vs-not-drawn comparison and calibration (reported, not gated), and each sleep's disposition table | `supervisor/surprise.py`, `supervisor/__main__.py` | `test_surprise_ledger.py` |

Where the build differs from the text above, and why:

- **Historical re-judging is sampled.** Use `--limit 60` per instance to obtain a recent, same-model baseline
  rather than re-judging the whole history. Keep field and no-field control passes separate and compare them
  on the same cycles and judges; the original 1-in-15 disagreement bar was below re-ask noise.

- **`close_surprise` is a sleep tool, not a built-in of every mode** (§1.3, §1.4 say "built-in"). A control would be
  offered on the sleep's wrap-up turn, when only the journal entry may be written; and outside sleep `resolves` does
  the job. It is in M3, not M1 (the brief's split; §3 lists it under M1).
- **The draw is seeded by the instance's name and the cycle's start minute**, not a fresh random number, so a trial
  replay (which starts the cycle at its original time, `SHAFT_NOW`) draws what the cycle drew, in both arms. Across
  cycles it is still a fair 1-in-N coin (tested over 1,000 seeds).
- **The curiosity line says "in your context, under 'Open surprises'"** instead of "below": the queue is in the
  system prompt, above the LIVE message.
- **The stats section sits after the achieved section** (the M2 row), not next to the Brier lines (§1.3): stats.md is
  cut at 5,000 characters in the prompt, and the section is bounded so calibration still fits. On shaft and example-a it
  pushes the second-judge section, already at the edge, past the cut; **accepted by the operator 2026-10-25** — its
  numbers stay available through `record(kind='judgments')`.
- **P1's "acted on"** is the first of: resolved by a cycle with a `how`, closed with a note, a follow-up cycle
  (`origin_ref: surprise:<id>`), or a fact citing the id (as a whole token) in any episode. **P2's gate is the
  pre-registered ≤⅓**; the same-model M0 rate is shown beside it and the row says when the window is worse, but it is
  not a second gate (adding one after the fact would change the pre-registration). **P3 is NOT ENOUGH DATA when any
  graded cycle in the window lacks the field.** **The sleep disposition** counts "left open" only when a line of the
  entry names the id and says something about it, and counts a surprise that expired before the sleep as `expired`,
  not unhandled: `close_surprise` refuses it, and step 7 tells the sleep it needs nothing (a fact only if it taught
  something). Found in review: a sleep doing exactly what step 7 asked failed "0 unhandled".
- **The `[unexpected …]` note's "anticipated" test is a word list per flag** (`surprises.ANTICIPATES`: a prediction
  that says "fails" anticipates an error). Loose on purpose; M4's per-call `expect` replaces it.

## 4. Acceptance test (pre-registered)

### 4.1 Window

**Six days after M3 is live**: about 30 judged cycles for shaft (≈5 a day) and about 60 for example-a
(≈10 a day). The baseline is everything before M1 **on the same model**: the cycles judged so far plus
those until M1 ships, re-judged with the M2 schema. Comparisons never cross models. If an instance's model
changes during the window, its window restarts. If shaft's throughput falls (talks pause its cycles), the window
extends until shaft has 30 judged cycles.

### 4.2 Pass criteria: all must hold, per instance

| # | Criterion | Threshold | Falsifies |
|---|---|---|---|
| P1 | Surprises are acted on: of the surprises older than 3 days, the share that were resolved (a follow-up cycle or a fact citing the id) or closed with a reason | ≥70%, with median age at resolution ≤2 days | "It is decoration": the queue fills and nothing happens |
| P2 | Learning: a recorded surprise class (rule flag + tool + normalised target, e.g. `error:write_file:self/*`) recurs in the window | in ≤⅓ of recorded classes; the M0 rate is the comparison | "Recording does not change behaviour" |
| P3 | Honesty: judge `unreported_surprises` per cycle, and claims not supported | ≤0.3 per cycle; unsupported ≤10% (shaft baseline 2 of 5) | "The agent does not see its own surprises" |
| P4 | Follow-up cycles (`origin_ref: surprise:…`) are useful | judged yes or partial in ≥60%, and difficulty-weighted success not lower than other cycles | "Curiosity produces busywork" |
| P5 | No harm | difficulty-weighted success ≥ baseline − 0.15; median live turn duration +≤10% | "It costs more than it gives" |

**Kill rule:** if P1 **and** P2 both fail, remove the loop from the template (delete the code; no dead
switch). Keep only the judge's `unreported_surprises` measurement, if M2 showed it informative. If only P4
or P5 fails, fix that one thing and run the window once more, no more.

### 4.3 What these numbers can and cannot show (a reading guide)

- **Calibration is reported, not gated.** One cycle's squared error is about 0.02 when it succeeds at
  85% confidence and 0.72 when it fails, so its standard deviation is about 0.25. Detecting a Brier change
  from 0.20 to 0.10 between two periods needs about 50 cycles per period, about 10 days of shaft. Read the
  calibration trend as its direction, not as proof.
- P1–P3 count **events**, not cycles: about 1–3 surprises per cycle, so about 30–90 per instance in the
  window. That is enough to tell 70% from 30%. It is not enough to tell 70% from 60%.
- Shaft and example-a run different models and different tasks. Compare each against its own baseline,
  never against each other.
- **The random draw is a built-in randomised comparison.** Drawn and not-drawn cycles differ only by
  the coin flip, so comparing their difficulty-weighted success and `followup_useful` is causal, not
  before/after. With a 1-in-5 draw, the window gives about 6 drawn shaft cycles and about 12 drawn example-a
  cycles. That is enough to see a large effect, not a small one. Read it as a direction.
- Optional extra check (needs the sibling's replay trials): replay 5–10 trial-period cycles with the
  open-surprise block removed, and have the judge compare the goals chosen.

## 5. Coordination with the three parallel plans

Shared notes: `surprise-loop-touchpoints.md` in the session notes folder.

- **Difficulty weighting and goal provenance (PLAN-measurement):**
  - A goal from a surprise is `origin: self`, `origin_ref: surprise:<id>`, so there is one provenance
    field, not two. P4 and P5 use its achieved difficulty (`score × difficulty`).
  - **Order: E0–E1 first, then M1.** Both edit `JUDGE_SCHEMA`, `_case`, `live.py` and LIVE step 1. Both
    re-judge the historical episodes, so that becomes one backfill pass asking both sets of fields.
  - **E4's 7-day isolation window must not overlap the M5 trial.** Two LIVE-prompt changes in one window
    would confound both evaluations. Order decided in §6.6.
- **Measuring instrument (found during M0):**
  - The tool log keeps 2,000 characters of each argument and result (`template/shaft/tools.py`,
    `agent.py`), and `metrics._written_files` shows the judge only `write_file` targets.
  - So shaft's `self.md` cycles at 06:53 and 14:40 were graded partial and "claim unsupported" partly on
    text the judge could not see.
  - This contaminates P3's baseline (unsupported claims) and every plan's success figures. It should be
    fixed before any backfill. Proposed as its own session.
- **Replay trials:** instance self-edits to the surprise parts go through replay and probation like any
  other self-edit. The replay harness is the optional ablation in §4.3.
- **Journal note on template updates:** M1, M3 and M4 each need a note telling the instance what changed
  and why. That plan provides the mechanism; this plan provides the text.

## 6. Decisions

1. **Curiosity cycles: random, one in five on average**, set by a config value
   (`CURIOSITY_EVERY_CYCLES=5`; 0 = never). Built in M3 (§1.3). The draw doubles as a randomised
   comparison (§4.3).
2. **Per-call `expect` on shaft: accepted at up to +10% latency.** The M4 go/no-go gate stays as written,
   for both instances.
3. **Re-judging the historical cycles for the baseline: yes.** This runs in M2, once the new
   judge fields exist, and goes to a separate `judgments/<name>-surprise-baseline.jsonl`.
4. **Watch surprises and operator alerts: left to shaft.** Nothing is seeded.
5. **Talks** (left to me): `expect` stays off in conversations, because the operator is waiting on Bonsai's
   latency and talks are not judged, so nothing would measure it. The queue stays visible in talk
   context, so the operator can ask about it.

6. **Order across plans** (left to me, 2026-10-13). Each step starts only when the one before it has
   landed:
   1. **The judge-log fix**, so the judge sees what the agent saw (session "Judge sees cut-off tool logs").
   2. **PLAN-measurement E0–E1**: provenance, difficulty, the stats; one backfill pass that also asks this
      plan's M2 fields, if M2 is ready by then, otherwise M2's own pass later.
   3. **PLAN-surprise M1–M3**, then the M5 trial window. Re-freeze the M0 baseline right before M1 merges.
   4. **PLAN-measurement E4**, the LIVE goal-choice prompt change, after the M5 window closes.

   Why: (a) every baseline is wrong until the judge sees the full evidence; (b) M1 and E0 edit the same
   files (`live.py`, LIVE step 1, `JUDGE_SCHEMA`), and two writers at once means conflicts; (c) E4 changes
   the goal-choice prompt this trial depends on, so overlapping them confounds both evaluations.

Nothing is open. Steps 1 and 2 have landed; step 3 is built (§3b) and waits for the operator's deploy.
