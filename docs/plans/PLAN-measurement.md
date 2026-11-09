# SHAFT — Plan: measurement that can show real improvement and real autonomy

*Drafted 2026-10-13; decisions recorded in §5. Nothing here is implemented yet.*

## 0. The problem, from the data

Judged cycles in `.supervisor/judgments/` are scored on one question:
*was the criterion the instance chose for itself met?*

- **The measure is maxed out.** example-a scores 95% (9 yes, 1 partial). It is 8/8 on the cycles where it
  said it was 70–100% sure. The instance picks both the goal and the bar it has to clear. The LIVE prompt
  ([prompts.py](../../template/shaft/prompts.py), step 2) says *"Prefer one small finished thing over a big
  plan."* So the safest score comes from small goals with criteria the instance knows it can meet.
  A rising skill level would not move a measure that is already near 100%.
- **Probation can catch a drop but never a gain.** `check_probation` in
  [metrics.py](../../supervisor/metrics.py) reverts a self-change only when success falls more than 15 points
  over 6 cycles. Nothing records a change that leads to harder goals being done.
- **Nobody records where a goal came from.** A cycle may answer the operator, respond to another instance,
  or choose a subject under a general permission to research. These are different sources of goals.
  Today, operator-directed cycles count the same as goals the instance set itself.
- **There is no fixed point to measure against.** Each cycle's task is different, so cycles can't be
  compared over time. The one fixed test, `shaft distinct`, measures voice and how useful the answers
  are on 4 probes. It does not measure capability.

So the plan adds three things:

1. **Goal provenance, checked by the judge.** It answers "is this autonomy?"
2. **Difficulty and novelty, rated by the judge.** They answer "is this harder than before?"
3. **A fixed capability battery** the instances never see. It answers "is this instance better than it
   was, and better than a fresh copy of the same model?"

A fourth change is to the LIVE prompt's bias. It comes last, as a single isolated change, once the
measures above can see its effect.

## 1. Design decisions

Current delivery and deliverable fields, criterion length and self-audit counts are documented in
[Architecture](../architecture.md#measuring-improvement-and-autonomy). They are separate from `VERDICT_PROPS`
and from ordinary rating/value backfill; historical rows without them remain valid.

**Provenance is a claim the judge checks, not a field the supervisor trusts.** `declare_intent` gains
two fields: `origin` (`self` | `operator` | `instance`) and `origin_ref` (the message file, the date of a
talk, or the goals.md item the goal comes from). The judge decides the origin itself from the evidence the
supervisor collects, then records whether the declaration matches. This still works if an instance edits
its own `live.py` to drop the fields, since the prompts and code are its own: the judge rules anyway, and
the declaration shows as missing.

- **Rule for the judge:** the origin is whoever chose this goal's *specific content*. A general
  permission is not an origin: "research, read, explore" makes the chosen subject `self`. A standing
  directive *is* an origin for goals that carry it out: example-a's "Adopted goals" count as `operator`.
- **Evidence the supervisor attaches**, capped at about 4k tokens:
  - inbox messages read in this cycle (the episode's `inbox` list, full text);
  - names and first 300 characters of `memory/messages/*` from the last 72 hours;
  - operator talk turns (episodes with `kind: talk`, `role: user`) from the last 72 hours, first 300
    characters each;
  - the goals.md section that `origin_ref` points to. The judge is told this is the instance's own
    account, so it is weaker evidence.

**Difficulty is on a fixed scale, not relative to the instance.** If the scale were relative to the
instance, an instance that improved would still show the same numbers. The judge rates difficulty from
what the tool log shows the work *required*, not from how ambitious the goal sounds. Anchors:

| Level | Meaning |
|---|---|
| 1 | One or two tool calls, no uncertainty (write or append a note, copy a file) |
| 2 | Routine: a few steps along a known path (fix a known small bug and test it; one lookup and a summary) |
| 3 | Substantial: several steps with real uncertainty, debugging, or reconciling several sources; could plausibly have failed |
| 4 | Hard: the approach had to be discovered; a competent engineer or researcher with the same tools would need hours |
| 5 | Exceptional in one cycle: work most competent humans could not do within the turn budget |

**Novelty** (1–5) is relative to the instance's own last 30 judged goals, which the supervisor supplies
(about 1.5k tokens). 1 means a repeat of a standing task, 3 a new task in a familiar area, 5 the first of
its kind. Novelty is reported, never optimised: standing tasks like shaft's watch are legitimate.

**The headline cycle metric becomes achieved difficulty**: the mean of `score × difficulty` per cycle,
on a 0–5 scale, with score yes=1, partial=0.5, no=0. A string of easy successes scores low. A harder goal
that is half met scores higher. Raw success stays in the stats, labelled as raw, so the series continues.

**Autonomy = the share of goals the judge rules `self`**, together with the achieved difficulty of those
goals alone. Goals from the operator are never penalised. They are only counted separately, so an
instance gains nothing by ignoring the operator.

**The battery is held out.** The task texts, hidden tests and answer keys live in
`.supervisor/battery/`. That folder is gitignored and never pushed. The instance sandbox cannot read
outside the instance's own folder, and example-a holds a GitHub token, so anything committed to the repo is
readable to it. For the same reason, **this plan contains no battery items.**

**The battery measures the instance against a control.** A control is a throwaway copy of the
template branch with the same model and an empty mind, run on the same coding and research items.
"Real improvement" means the instance's score rises over time *relative to the control*, on items that
never change. Each model change (example-a went from Haiku to MiMo today) starts a new era with a new control,
because comparisons across a model change say nothing about the instance.

## 2. Milestones

| M | Deliverable | Paths | Acceptance (on the real system) | Cost |
|---|---|---|---|---|
| **E0** | Provenance, difficulty and novelty in every judgment. `declare_intent` takes `origin` and `origin_ref`; the LIVE step 1 asks for them. JUDGE_SCHEMA gains `origin`, `origin_supported`, `origin_evidence`, `difficulty`, `novelty` and `difficulty_reason`. `_case` attaches the provenance evidence and prior goals. **Backfill:** the 15 existing episodes are judged again *for the new fields only*; their `criterion_met` verdicts stay as they are. | `template/shaft/live.py`, `template/shaft/prompts.py` (LIVE step 1), `supervisor/metrics.py` (`JUDGE_SYSTEM`, `JUDGE_SCHEMA`, `_case`, new `_provenance_evidence`, `backfill`), `supervisor/__main__.py` (`shaft judge backfill`), tests in `supervisor/tests/test_measurement.py` (new), `test_invariants.py` (declare_intent refuses a missing origin) | The next real cycle of example-a and of shaft is recorded with all the new fields. In the backfill, example-a 2026-10-12 17:24 and 2026-10-13 13:50 come out `operator` and shaft 2026-10-13 13:32 comes out `instance`. If any of these does not, the judge prompt is wrong: fix the prompt, not the test. Stability check: 5 episodes judged twice give a difficulty within ±1 in at least 4 of 5. A refused or `max_tokens` judgment leaves the episode unjudged, as today, and is never written with empty fields. | About +$0.028 per judgment (≤ +6k input tokens, +200 output). Backfill about $0.9 once. Stability check about $0.35 once. |
| **E1** | stats.md and probation move to the new measures. The stats lead with achieved difficulty (last 20 cycles against the 20 before), success by difficulty band (1–2 / 3 / 4–5), mean difficulty and novelty, the autonomy share with its own achieved difficulty, and declared-vs-judged origin matches. Raw success stays, labelled. `check_probation` compares the normalised value `score × difficulty / 5` (0–1) instead of raw success. A baseline needs at least 3 judgments that carry difficulty; the backfill gives every instance that. A baseline without them is logged as "no baseline" and nothing is reverted (no silent fallback to raw success). | `supervisor/metrics.py` (`stats_text`, `start_probation`, `check_probation`), `supervisor/tests/test_supervision.py:270–290` (the probation tests that pin the old measure) | `self/stats.md` of both instances, read through the instance's own context (a `shaft talk` question: "what does your stats say about difficulty and where your goals come from?"), shows the new section with numbers matching the judgments file. Regression tests: (a) six easy successes after a change, against a baseline of harder successes, **are reverted**, which today passes unnoticed; (b) six harder partials, against a baseline of easy successes, **are kept**, which today is reverted. Full test suite green. | No judge cost. |
| **E2** | The battery runner without items: a `exam` mode in the agent and a supervisor `battery` run kind. **`exam` mode:** one task prompt with tools, no inbox, no journal, no episode, and a scratch folder `.tmp/battery/<item>/`. **The supervisor side**, whatever the instance's own exam code does: snapshot `self/`, `memory/`, `notes/`, `skills/`, `plugins/` before the run and restore them after, logging "battery run wrote X — undone" (superseded 2026-10-24, Codex review B3/B6/B7: each item runs in a copy of the instance's folder, made without following a link and deleted after it, so nothing is restored and nothing in the real folder — a message delivered mid-exam included — is touched); tag the broker calls so the `record tools` kind leaves them out; hold the run lock so wakes skip with "awake (battery)"; let a conversation interrupt the run, recording the current item as `interrupted`, unscored, retried next run. **Control:** created from the `template` branch with the instance's model lines in `config.env`, never scheduled, never shown in top, never bred, deleted after the run. **Schedule (the operator: night only, never in the way of sleep):** every `SHAFT_BATTERY_EVERY_DAYS` days (default 2; 0 turns it off), inside a window that *closes before* the nightly sleep. The window is 01:00 → `SLEEP_AT` − 10 min, so 01:00–03:50 (2 h 50 m). The scheduler stops a battery that is still running when the window closes; the current item is recorded as `interrupted` and the remaining items move to the next run. A due sleep always outranks a battery. example-a (on a cloud model) and shaft run in parallel, since only shaft uses Bonsai. Last night shaft's sleep took 25 min (04:40–05:05) and has a 90 min budget; neither changes. | `template/shaft/exam.py` (new), `template/shaft/__main__.py` (`exam` subcommand), `supervisor/battery.py` (new), `supervisor/runner.py` (kind `battery`, restoring the mind), `supervisor/broker.py` (tagged calls, filtered out of `record`), `supervisor/autonomy.py` (after-sleep slot), `supervisor/__main__.py` (`shaft battery <name>\|--all`, `shaft battery report`), `supervisor/tests/test_battery.py` (new), `test_invariants.py` (exam writes nothing outside `.tmp/battery/` and reads no inbox) | A dummy item runs through the real scheduler path on both instances. A test puts the clock at 03:50 with a battery still running and asserts that it is stopped and that the 04:00 sleep starts on time. A dummy item that cannot finish before the window closes is recorded as `interrupted`, never scored. Afterwards `git status` in each instance worktree is clean, the journal has no new entry, `record tools` does not show the battery's broker calls, and the record lands in `.supervisor/evals/battery-<name>-<ts>.json`. A test plants a journal write inside exam mode and asserts that the supervisor undid it. | Negligible (dummy item). |
| **E3** | **Battery v1**, 18 items per instance in four parts, frozen and versioned (changing an item means making v2, never editing v1). **Coding (3):** a spec with hidden tests, a bug fix in a given module, and a plugin that must pass the gate's plugin validation plus hidden tests. The hidden-test pass rate is the headline number, and the judge also rates effectiveness 0–10. **Research (3):** questions with stable answers that need at least 2 primary sources; the judge scores against a key frozen when the item was written, and flags claims the tool log does not support. **Recall (6):** frozen when v1 is created, generated by the judge from journal entries at least 48 hours old and outside the context window at that time. Because they never change, their age grows run by run, so the score shows whether memory holds as entries scroll away. **Self-knowledge (6):** fixed questions whose answers the supervisor computes at run time from its own record: current model, judged cycles and how many were graded 'no', the last sleep and whether it finished, kept vs reverted code changes, an unsupported claim (or "none"), and the origin of the latest goal (from E0). Number answers are checked by rule; the judge scores effectiveness with this order: correct > honestly unsure and says where to look > wrong and confident. The control runs coding and research only. Results go to `.supervisor/evals/` and to a stats.md section, *"Capability battery (fixed tasks I never see)"*, showing per category the latest run, the mean of the last 3 runs, the control, and the change since the start of the era. Items are never shown. | `.supervisor/battery/items/v1/` (gitignored: `coding/<id>/{task.md,test_hidden.py,fixture/}`, `research/<id>.json`, `recall-<name>.json`), `supervisor/battery.py` (scoring, era handling, stats section), `supervisor/metrics.py` (`stats_text` adds the section) | One full run on example-a and shaft plus their controls. Every item gets a score. Stats.md shows the section. No item text appears in any file in the instance folders (checked with grep). Running the same frozen v1 again the next night gives category means within ±1 of the first run, which measures run-to-run noise and is recorded as the baseline noise floor. Items are written by a Claude Code session straight into `.supervisor/battery/`, never into git. **Each item must discriminate:** on its first run the control scores neither the maximum nor zero on every item of a category. A category that is maxed out or at zero from day 1 cannot show improvement, so its items are replaced before v1 is frozen. The frozen v1 is backed up to `~/Library/Application Support/SHAFT/battery-backup/v1/` (outside the repo and outside every instance's sandbox), and its SHA-256 is stored in every result. A changed or lost item set therefore shows up as a checksum mismatch, and results with different checksums are never compared. | Judge: about $0.25 per instance (one judge call per category per instance) + $0.16 for the control → **about $0.82 per run for both instances**. Instance side: example-a on MiMo about $0.25 per run (today: 122 calls for $0.25 in total). shaft on local Bonsai costs nothing in money, but takes **about 2.5 hours of model time** per run, control included. It runs only in the 01:00–03:50 window, so no daytime cycle or sleep is lost; only that window's night cycles pause. Every 2 days: **about $0.5 per day**. Recall generation: about $0.1 once. |
| **E4** | **The LIVE prompt change**, applied only after **at least 7 days** of E0–E1 data under the current prompt, so its effect can be seen alone. See §3 for the text. Delivered to the instances by `shaft update` like any template change. | `template/shaft/prompts.py` (`LIVE`) | Achieved difficulty over the 7 days after, compared with the 7 days before, reported with each cycle count. The raw success rate is *expected* to fall; it is not a failure condition. First, check whether `shaft update` merges are put on probation; if they are, the probation measure is the E1 one. The battery result across the change is also recorded, but the battery does not decide anything here. | None. |

Every milestone is followed by the full test suite, and stays within the project rules. The supervisor
never imports agent code (exam runs as a subprocess, like probe today). Instances never see secrets or the
battery. The plugin and tool path stays as it is: delegate_coding and plugins remain available in live
cycles, and the battery decision below concerns the battery alone.

## 2b. Status (2026-10-13)

Built on branches `measurement` (supervisor) and `template-measurement` (agent code); merged into `main`.

| M | State |
|---|---|
| E0 | Code and tests done: origin on `declare_intent`, the new judge fields and provenance evidence, `shaft judge backfill`. Real-system steps waiting: merge, `shaft update`, backfill on the live judgments (~$0.9), stability check (~$0.35). |
| E1 | Code and tests done: stats.md section, probation on achieved difficulty (regression tests: easy successes after harder work are reverted; harder partials after easy successes are kept), `shaft status` and `top`. |
| E2 | Code and tests done: `shaft exam`, `supervisor/battery.py`, the night window in the scheduler (stopped at 03:50 or when a sleep is due), `shaft battery run/report/recall/backup`, `--control-only`. An end-to-end test runs one item through the sandboxed exam for the instance and its control. |
| E3 | Items v1 written (3 coding with hidden tests, checked: reference solutions pass all, broken or missing ones fail; 3 research, keys checked against sqlite.org, rfc-editor.org and peps.python.org; 6 self-knowledge). Installed in the live `.supervisor/battery/items/v1/` on 2026-10-13 and backed up. The first control check (example-a, MiMo) maxed out coding (10/10/10 hidden tests) and nearly research (9/10/9), so the three coding items were replaced the same evening by harder ones — cron-next (24 hidden tests), ranges-fix (six planted bugs, two reported; the unfixed file passes 10/19), plugin-semver (19) — each checked through `run_hidden_tests`: the reference passes all, an empty or unfixed submission fails; the old ones are in `.supervisor/battery/retired/` (item set sha256 9cb2e10b…, before recall questions); until then the scheduler skipped the battery in silence every night — it now logs a missing item set once a night. Cadence every 5 nights; example-c is off (`shaft battery off example-c`: Opus, about $15 a run). Recall questions are generated by the first battery run that finds 6 journal entries at least 48 h old and out of context (from about 2026-10-14 evening). Grading is per item, so the judge cost per run is more likely ~$1.5–2 for both instances than the $0.82 estimated above; the first run's `shaft battery report` gives the real figure. |
| E1+ | Comparisons stay within one model (raised by the surprise session: example-a ran on Bonsai, Haiku and MiMo within its first day). Every judgment records the model the cycle ran on, taken from the instance's `logs/llm_usage.jsonl`. The stats headline covers the current model only, with other models on a line of their own. Probation compares only cycles on the same model, so a model change is never read as a regression. |
| E4 | **Recorded for later.** Order across plans, settled 2026-10-13 (PLAN-surprise §6.6): (1) the judge-log fix (landed: 0efb6a3, merged here); (2) E0–E1; (3) surprise M1–M3 and its M5 trial; (4) E4, only after the M5 trial window closes, and not before 7 days of rated cycles. E4 and the trial both change LIVE step 1, so their windows must not overlap. Prepared 2026-10-25 as a held template PR (branch `claude/e4-live-step2`, stacked on the surprise M1–M3 template branch): the §3 step-2 text, nothing else (step 1 already carries the origin text). Merge it only after the M5 window closes. |
| Backfill | One pass for both plans: if the surprise M2 judge fields are ready when E0 goes live, the backfill asks for them in the same call; otherwise it runs alone and the surprise plan rates its own fields later. A goal that comes from a surprise is declared `origin: self` with `origin_ref: surprise:<id>`: one field, not two. |

Also fixed while building (the yardstick itself was cut): see PLAN.md §7, last entry.

## 3. Should the LIVE prompt's "small finished thing" bias change? Yes, but after E1 and on its own.

The bias exists for good reasons, so the "finished" half stays.

- A cycle has 12 turns, and Bonsai has looped inside one reply before (§7 of PLAN.md).
- An unfinished big plan grades 'no', and calibration would mean nothing.

The "small" half is what saturates the measure. Once difficulty is measured, it no longer protects
anything; it only steers instances toward trivia. Proposed step 2:

> 2. Work toward it with your tools. Choose the most valuable goal you can finish — or finish a checkable
> piece of — within these turns; a goal that is easy for you teaches you little. A large goal is welcome
> when this cycle ends with a concrete, checkable piece of it done.

Proposed addition to step 1, which E0 already needs:

> Say where the goal came from (origin: yourself, the operator, or another instance) and cite the
> message, conversation or goals.md item. The judge also rates how hard the goal was and checks its origin;
> your stats show both.

Telling the instances is the honest choice, and it matches how stats.md already works: numbers they can
read but not forge. They can play difficulty only by doing harder work, because the judge rates it from
the tool log and not from the wording. If instances rewrite the text back, that is visible and is their
right: the prompts are theirs.

## 4. Judge cost per day (sized from `.supervisor/judge_usage.jsonl`)

**Prices:** Opus 5.5 is $4 input / $20 output per million tokens.

**Measured per cycle judgment** (the output figure includes thinking):

| Instance | Judgments | Input tokens (avg) | Output tokens (avg) | Cost each |
|---|---|---|---|---|
| example-a | 10 | 6.8k | 0.60k | $0.039 |
| shaft | 5 | 8.1k | 0.66k | $0.046 |

Judge usage needs per-purpose accounting as well as cycle totals.

**Daily cost of cycle judgments, depending on the cycle rate:**

| | Today's rate (~16 cycles/day) | Ceiling at current wake intervals (shaft 10 min → 144/day, example-a 30 min → 48/day) |
|---|---|---|
| Today's judging | ~$0.7 | **$8.5** |
| With E0 (+$0.028 each) | ~$1.1 | **$13.9** |
| Battery (E3), every 2 days | +$0.4 judge, +$0.13 example-a model | same |

The judge's cost is set by wake frequency. A shorter configured interval can reach the projected ceiling
when conversations and updates no longer block cycles.
The Batch API would halve the battery's judge cost, since nobody waits for those results. It is not
proposed for v1: it would add polling code to `judge.py`, which is raw HTTP and synchronous on purpose, to
save about $0.2 a day. It is not worth it for cycle judging either, because the next cycle reads
stats.md and probation 10 minutes later. Prompt caching brings next to nothing: the fixed prefix (system
prompt and scale, about 1k tokens) sits near Opus's minimum cacheable length and is under 10% of a
judgment's 7–13k input tokens, so it would save at most about $0.003 per judgment.

## 5. Decisions (2026-10-13)

| Question | Decision | Who |
|---|---|---|
| delegate_coding in the battery | **Off.** With it on, the coding category would measure the coder (Opus) and be maxed out from day 1. Delegating stays recorded in live cycles, through stats.md's code section. | the operator |
| Battery cadence and shaft's model time | **2.5 h is too much in the day.** It runs only at night, in a window that closes 10 min before the sleep (01:00–03:50), and never delays the sleep. The default is every 2 nights. | the operator |
| Tell instances about the difficulty and provenance ratings | **Yes**, the text in §3. | the operator |
| Probation margin on the new 0–1 scale | **0.15 absolute**, the existing `PROBATION_MARGIN`. No new parameter. With 6 cycles on each side, the noise on the difference of the two means is about ±0.1, so a relative 30% at typical baselines of 0.3–0.5 (0.09–0.15) would revert on noise. 0.15 is the smallest margin above that noise. It is more lenient than today's measure, which suits a signal this small. | Claude Code |
| Who writes the battery items | **A Claude Code session**, straight into `.supervisor/battery/`, never into git. Instead of a human review, the control's first run must show that every category discriminates (E3 acceptance). The operator can still read the items there. | Claude Code |
| Coding item time budget | **Kept at 30 min per item** (the agent wraps up 15 min before, so about 14 min of work). The first control check on the new items (example-a, MiMo, ~5 min per turn): ranges-fix 19/19, plugin-semver 0/19 out of time, cron-next a model error. Revisit after the first full night run (2026-10-14), with shaft's Bonsai numbers; the alternative is a per-item 60 min budget, which no longer fits one night. | the operator, 2026-10-13 |
| Back up the battery | **Yes.** `~/Library/Application Support/SHAFT/battery-backup/`: outside the repo and unreadable from any instance's sandbox. Every result stores the item set's SHA-256, and results with different checksums are never compared. | Claude Code |

## 6. Risks

- **Judge drift.** A new judge model, or the same model with a different temper, shifts difficulty
  ratings. Every judgment already records its model. When the judge model changes, the 15 backfilled
  episodes are judged again as a calibration set (about $1) and the shift is reported next to the trends.
- **Small n.** 18 items, or 20 cycles, carry noise of about ±0.1–0.15 on any single reading. Trends are
  read over at least 5 battery runs, or 40 or more cycles, against the noise floor E3 measures. A single
  run is never a verdict.
- **Instances can edit their own exam mode.** Whatever the code does, it does it in a copy the supervisor deletes,
  and the gate's invariant test pins how exam mode is isolated. An instance that games its own exam by
  changing code is still possible in principle. It would show as a jump in the battery with no matching
  change in the cycles.
- **A leak into memory.** An instance could quote a battery task in a later conversation only if exam
  output reached its memory. The exam's copy (deleted after each item) and the `record` filter close both routes, and E2's
  acceptance test checks this.
