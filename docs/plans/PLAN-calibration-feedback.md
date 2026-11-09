# Calibration feedback at declare time — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** when an instance declares its confidence, show it how often the criterion was actually met in
the past cycles where its confidence fell in the same band, let it revise the number once, and score
the before and after numbers on the same cycles as a separate confidence version (v3).

**Why:** across 978 judged cycles, shaft says 73% beforehand and is judged to succeed 62% of the time
(Brier 0.11). Its confidence barely separates good cycles from bad ones: 60% success when it said
40–70%, 63% when it said 70–100%. It already sees these numbers every cycle in `self/stats.md`
([prompts.py:95](../../template/shaft/prompts.py), `_head(... "stats.md", 5500 ...)`), and they have
not moved its numbers. The instance proposed this test itself ("Experiment #2, pre-claim meta-gate",
`instances/shaft/notes/docs/draft-philosophy-of-mind-v5.md` §5.4).

**Architecture:** the supervisor already writes `self/stats.md`. It will also write
`self/calibration.json`, the base rate for each band, in the same `write_stats` call. The template
reads that file at the start of a live cycle. If the declared confidence falls in a band that has
data, `declare_intent` records the intent and returns the base rate. Every other tool stays closed until
the instance calls the new control `confirm_confidence` with its final number, on a later turn. The
intent keeps both numbers. The supervisor scores both on the same cycles, so one cycle is compared with
itself: no change of judges, model or season can create a false effect.

**Tech stack:** Python 3 stdlib, `unittest` (`python3 -m unittest discover -s supervisor/tests -t .`).

**Spec:** this file. The parent context is the assessment of the two consciousness papers (2026-10-25)
and REVIEW-autonomy-2026-10-24 A4 (calibration v2).

## Prerequisite found while planning (not a bug, a pending deploy)

Calibration v2 (`confidence_of="criterion_met=yes"`) is merged on the supervisor side (main) and on
`origin/template` (dgrassi1984/shaft#83, `08b3883`). The local `template/` worktree is still at
`5381b88`, and every instance's `shaft/live.py` is identical to it, so **no instance sends
`confidence_of` yet: v2 has n=0 everywhere** (`shaft stats shaft` prints only v1). The template tests
for A4 skip locally for the same reason. This plan builds on `origin/template`. Deploying #83 and then
this plan to the instances is the operator's step (Task 6).

## Global constraints

- The supervisor never imports agent code. The confidence-question string exists twice, as
  `metrics.CONFIDENCE_V3` and `live.CONFIDENCE_OF_SHOWN`, and one test pins them equal (same pattern as
  the surprise-rule fixtures).
- Versions are never pooled when scored. v1, v2, v3-final and v3-first each get their own Brier score.
- A confidence version the supervisor does not know is **excluded and counted, never scored as v1**.
  Today `calibration()` scores anything that is not v2 as v1 (`metrics.py:149`), so v3 rows would
  silently join v1.
- Base rates count only verdicts from judges in today's rotation (`by_current_judge`), like every other
  success figure in stats.md.
- The bands are the existing ones, `(0, 40), (40, 70), (70, 101)`, moved into one constant `BANDS`. No
  new thresholds. The JSON carries the band edges, so the template hardcodes none.
- The base rate is shown only if its band has data. With no file, an unreadable file or an empty band,
  the cycle runs exactly as v2 does today: no confirm step, no extra turn, `confidence_of` stays v2. An
  unreadable file is recorded in the intent as `base_rate_error`, never swallowed.
- A cycle that shows a base rate gets one extra step (`cfg.MAX_STEPS_LIVE + 1`), so the confirm turn
  does not take a turn away from the work.
- Instances still build their own tools. This adds a control and a file, not a supervisor tool, and
  touches neither the gate, the broker nor the plugin path.
- Trial replays: the replayed copy reads its own `self/calibration.json`. The confirm step writes
  nothing, sends nothing and settles nothing.

## Review focus

1. **Same-turn confirm.** The model calls `declare_intent` and `confirm_confidence` in one reply, so it
   never saw the base rate. Expected: confirm is refused and asked for again on the next turn (test in
   Task 3).
2. **Declare on the wrap-up turn.** Only controls are left and no later turn exists. Expected:
   `finish_cycle` is still accepted, and the intent is v2 with `base_rate_shown` kept for audit and no
   `confidence_first`: only an accepted confirm makes it v3, since an unconfirmed declare never read the rate and
   labelling it treated would dilute the effect (final fix F2, superseding "v3 with confidence == confidence_first").
3. **Unknown or older `confidence_of` values.** Expected: excluded from every version and reported as
   "n excluded", never counted as v1 (Task 1).
4. **The instance edits `self/calibration.json`.** Expected: the template shows whatever is there, the
   intent records exactly what was shown (`base_rate_shown`), and the supervisor overwrites the file on
   the next `write_stats`. Scoring never reads the file (Task 3, plus an assertion in Task 2).
5. **Deploy in either order.** A template-first deploy has no JSON, so it runs as v2. A supervisor-first
   deploy writes JSON that the old template ignores. A row judged before the supervisor copied
   `confidence_first` takes it from its episode, like the existing `confidence_of` backfill (Task 2).

---

### Task 1: Version-safe calibration, v3 and the paired score (supervisor)

Branch `claude/calibration-feedback-sup`, cut from `main`, in its own worktree, with its own `template/`
checked out at `origin/template`.

**Files:**
- Modify: `supervisor/metrics.py:133-160` (constants, `calibration`)
- Test: `supervisor/tests/test_calibration_feedback.py` (new)
- Pin: `supervisor/tests/test_autonomy_gaps_supervisor.py:188` and `supervisor/tests/test_supervision.py:233`
  must still pass unchanged.

**Interfaces:**
- Produces: `CONFIDENCE_V3 = "criterion_met=yes;base_rate_shown"`; `BANDS = ((0, 40), (40, 70), (70, 101))`;
  `VERSION_OF = {None: "v1", CONFIDENCE_V2: "v2", CONFIDENCE_V3: "v3"}`;
  `calibration(rows) -> dict` with keys `"v1", "v2", "v3", "v3_first"` (same per-version shape as today:
  n, mean_confidence, mean_outcome, brier, bands) plus `"excluded": int`; and
  `paired_v3(rows) -> dict` with `{"n", "brier_first", "brier_final", "changed", "mean_change",
  "closer", "further"}` over v3 rows that have `confidence_first`.

- [ ] **Step 1: Write the failing tests**
  - `test_unknown_question_is_excluded_not_v1`: one row with `confidence_of="something-new"` and one
    with none. Assert `calibration(rows)["v1"]["n"] == 1` and `["excluded"] == 1`.
  - `test_v3_scored_binary_on_final_and_first_separately`: a v3 row, `confidence=60`,
    `confidence_first=90`, `criterion_met="partial"`. Assert v3 Brier is `0.36` and v3_first Brier is `0.81`.
    Assert v2 n is 0 (no pooling).
  - `test_paired_v3_counts_closer_and_further`: three v3 rows: (first 90, final 60, partial) → closer;
    (first 50, final 80, yes) → closer; (first 70, final 70, no) → unchanged. Assert `closer == 2`,
    `further == 0`, `changed == 2`, and `mean_change` is 0.0 ((−30 + 30 + 0) / 3).
  - `test_bands_constant_drives_calibration_bands`: the band edges in the output equal `BANDS`
    (last hi shown as 100).

- [ ] **Step 2: Run them and see them fail**
  Run: `python3 -m unittest supervisor.tests.test_calibration_feedback -v`
  Expected: FAIL (`CONFIDENCE_V3` is not defined, and `excluded` is not a key).

- [ ] **Step 3: Implement it.** Select each row's version with `VERSION_OF.get(row.get("confidence_of"), None)`
  (a key that is present but unknown is excluded). `v3_first` takes v3 rows that have `confidence_first`,
  scored binary against `criterion_met == "yes"`. Write `paired_v3` next to `calibration`. Update the
  `calibration` docstring.

- [ ] **Step 4: Run the new and pinned tests.** All pass.

- [ ] **Step 5: Commit** `Calibration: unknown questions excluded, v3 scored before and after the base rate`.

### Task 2: Base rates file, judgment fields, every reader labelled (supervisor)

**Files:**
- Modify: `supervisor/metrics.py`: `rec = {...}` at about line 545 (copy `confidence_first`,
  `base_rate_shown`); the backfill in `judgments()` at about line 221 (also copy those two from the
  episode); `stats_text` at about line 1005 (v3 lines and the paired line); `review` at about line 1072
  (labels from one dict, not `== "v2"`); `write_stats` at line 1113.
- Modify: `supervisor/surprise.py:165-171, 231-246` (columns `brier_v3`, `n_v3`).
- Modify: `supervisor/web_views.py:303`, `supervisor/top_views.py:246`, `supervisor/panel.py:581`:
  per-cycle confidence shows `90 → 60` when `confidence_first` differs.
- Modify: `docs/architecture.md` (calibration section, about lines 60–80).
- Test: `supervisor/tests/test_calibration_feedback.py`

**Interfaces:**
- Consumes: Task 1's `BANDS`, `VERSION_OF`, `calibration`, `paired_v3`, `by_current_judge`.
- Produces: `base_rates(rows) -> dict` returning
  `{"written": iso, "question": "criterion_met=yes", "bands": [{"lo", "hi", "met", "n", "older_met", "older_n"}]}`.
  `met`/`n` count current-judge rows whose question is v2 or v3 (the same question). `older_*` count v1 rows.
  `met` means `criterion_met == "yes"`. The last band is written as `"hi": 100`.
  `CALIBRATION_FILE = "self/calibration.json"`, written by `write_stats` with `safe_write`.

- [ ] **Step 1: Write the failing tests**
  - `test_base_rates_current_judges_only_and_questions_apart`: rows from a retired judge are not
    counted, and v1 rows land in `older_*`.
  - `test_write_stats_writes_calibration_json_beside_stats`: on a temporary INSTANCES tree, both files
    exist, and the JSON parses and has three bands.
  - `test_stats_text_shows_v3_and_paired_line`: the text contains
    `Base rate shown when I declared (v3): Brier before I saw it 0.81, after 0.36 on the same 1 cycles`.
  - `test_review_labels_v3_as_criterion_met`: the v3 line in `review` says `criterion met = yes`, not
    `probability of success`.
  - `test_judgment_row_copies_first_and_shown` and `test_backfill_copies_first_from_episode`.
  - `test_scoring_never_reads_calibration_json`: overwrite the file with garbage, and `stats_text` and
    `calibration` give the same results.

- [ ] **Step 2: Run them and see them fail.** Run: `python3 -m unittest supervisor.tests.test_calibration_feedback -v`

- [ ] **Step 3: Implement it.** The stats.md paired line, as exact copy:
  `- Base rate shown when I declared (v3): Brier before I saw it {brier_first:.2f}, after {brier_final:.2f} on the same {n} cycles; I changed my number in {changed} of {n} (mean change {mean_change:+.0f} points; closer to the outcome {closer}, further {further})`.
  Put it under a `v3` title line like v2's: `Calibration v3 — P(criterion met = yes) after seeing my base rate, Brier on met/not met`.

- [ ] **Step 4: Run the full suite.** `python3 -m unittest discover -s supervisor/tests -t .`, all green with no new skips.

- [ ] **Step 5: Drive the real views.** Run `bin/shaft stats shaft` and confirm that
  `instances/shaft/self/calibration.json` now exists with the bands matching the stats.md lines (v1
  only, until deploy). Open the web UI cycle view and `shaft top`, and confirm the confidence column
  still renders for v1 rows.

- [ ] **Step 6: Commit** `Base rates for the declare step: self/calibration.json; v3 in stats, review, surprise and the views`.

### Task 3: The confirm step in the live cycle (template)

Branch `claude/calibration-feedback-template`, cut from `origin/template`.

**Files:**
- Create: `template/shaft/base_rate.py`
- Modify: `template/shaft/live.py` (declare, a new control, `gated_run`, `max_steps`, the journal line)
- Modify: `template/shaft/prompts.py` (LIVE step 1, about line 204: one sentence about the confirm step)
- Test: `supervisor/tests/test_calibration_feedback_template.py` (new; skips with a reason if
  `shaft.base_rate` is missing, like `test_autonomy_gaps_template.py:18`)

**Interfaces:**
- Consumes: the JSON shape from Task 2.
- Produces: `base_rate.FILE = "self/calibration.json"`;
  `base_rate.load(root: Path) -> tuple[dict | None, str | None]` (rates or None; error text or None);
  `base_rate.band_for(rates: dict, confidence: int) -> dict | None` (the band with `lo <= c < hi`, or
  `c == 100` for the last; None if both `n` and `older_n` are 0);
  `base_rate.describe(band: dict, confidence: int) -> str`;
  `live.CONFIDENCE_OF_SHOWN = "criterion_met=yes;base_rate_shown"`;
  control `confirm_confidence {confidence: integer 0-100}`;
  intent fields `confidence` (final), `confidence_first`, `base_rate_shown` (the band dict), `base_rate_error`.

- [ ] **Step 1: Write the failing tests** (FakeLLM, `InstanceFixture`-style temp instance)
  - `test_band_with_data_shows_rate_and_closes_tools`: declare at 85, then a `read_file`, then confirm
    at 65. Assert the declare result contains `when you said 70–100%, the criterion was fully met in 63 of 100`,
    `read_file` got the `ERROR: call confirm_confidence first` refusal, and the episode intent has
    `confidence == 65`, `confidence_first == 85`, `confidence_of == CONFIDENCE_OF_SHOWN`.
  - `test_same_turn_confirm_refused`: declare and confirm in one reply. Confirm returns an ERROR, and a
    confirm on the next turn is accepted.
  - `test_wrap_up_without_confirm_keeps_first`: declare on the last turn, then finish. Assert v3 with
    `confidence == confidence_first`. (Superseded by final fix F2: it is v2, `test_wrap_up_without_confirm_is_v2`.)
  - `test_no_file_runs_as_v2`: no `calibration.json`. One declare, then work. Assert
    `confidence_of == "criterion_met=yes"`, no `confidence_first`, and the request count shows
    `max_steps == cfg.MAX_STEPS_LIVE`.
  - `test_empty_band_runs_as_v2` and `test_unreadable_file_recorded`
    (`base_rate_error` contains the parse error).
  - `test_extra_step_only_when_shown`: `max_steps` is `MAX_STEPS_LIVE + 1` exactly when a base rate is shown.
  - `test_question_strings_match_supervisor`: `live.CONFIDENCE_OF_SHOWN == metrics.CONFIDENCE_V3`.

- [ ] **Step 2: Run them and see them fail.** Run:
  `python3 -m unittest supervisor.tests.test_calibration_feedback_template -v`, with `template/` at this branch.

- [ ] **Step 3: Implement it.** Same-turn detection: at declare, store the number of assistant messages
  in `messages`. `agent.run` appends each reply to that same list before it runs the reply's tool calls
  (`agent.py`, `messages.append(turn)`). Confirm refuses while that number is unchanged. The declare
  result, as exact copy when a band is shown:
  `intent recorded. Before acting, look at your record: when you said {lo}–{hi}%, the criterion was fully met in {met} of {n} judged cycles ({pct}%){older}. Call confirm_confidence with your final probability (0-100) that the criterion will be fully met — keep {confidence} or change it. Your other tools stay closed until you do.`
  where `{older}` is `; under the older question, {older_met} of {older_n} ({older_pct}%)` when
  `older_n > 0`, and the first clause is left out when `n == 0`. The journal line becomes
  `**Confidence beforehand:** 85% → 65% after my base rate` when the two numbers differ.

- [ ] **Step 4: Run the new tests and the whole suite.** The A4 template tests must also stop skipping.

- [ ] **Step 5: Commit** `Live cycle: show the band's base rate at declare, confirm once, keep both numbers (v3)`.

### Task 4: Cross-model review of both branches

- [ ] Codex (native diff review) on both branches. The brief forbids reading `.env`, `instances/` and
  `secrets/` (memory: external-agents-secret-paths). Focus: Review-focus items 1–5 and any reader of
  `calibration()` keys that is still missed (`grep -rn "calibration(" supervisor/`).
- [ ] Fix whatever is confirmed, each with a regression test that names it. Then rerun the full suite.

### Task 5: PRs

- [ ] One PR per branch: supervisor into `main`, template into `template`. Each description states the
  deploy order (either order is safe, and supervisor first is recommended so the JSON exists on the
  first v3 cycle) and the review checkpoint in Task 6. **Merging needs the operator's go-ahead.**

### Task 6: Deploy and the decision (operator-owned)

- [ ] The operator merges #83 and this template change into the instances (the usual template merge)
  and restarts them. That is the live cutover, and it is the operator's call.
- [ ] Verify in the running system, after the first judged v3 cycle of each instance:
  `self/calibration.json` exists; the journal shows `→ … after my base rate` or an unchanged number;
  stats.md shows the v3 and paired lines; the web UI and top show `first → final`.
  A v3 n of 0 after a day of cycles is a bug: trace it (declare result, intent, judgment row).
- [ ] **Decision when about 100 paired v3 cycles exist across instances:** compare `closer` with
  `further` using a sign test (ties excluded). Keep the step if after-the-base-rate is closer
  significantly more often and the success rate did not drop. Otherwise remove the step entirely
  (no dual mode). Also watch whether the final confidences collapse onto the base rate (spread near
  zero): that is calibration with no resolution. Report it as a finding; it is not a pass.
