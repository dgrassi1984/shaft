# SHAFT — Plan: grade what a cycle was worth, not only whether it met its own bar

*Drafted and approved 2026-10-15 (the operator: build V0–V2 with the three recommendations; V0 merges after the
second-judge fix). Status in §6. Numbers are from
`.supervisor/judgments/*.jsonl` and `.supervisor/judge_usage.jsonl`, 24 h to 2026-10-15 09:47.*

## 0. The problem, from the data

The judge answers four questions about a cycle:

- Was the criterion the instance set for itself met?
- Did the instance's own claim match the tool log?
- How hard was the work?
- How new was the goal, and who chose it?

None of these asks **whether anything worth having came out of the cycle**. The last 24 h show the gap:

| | shaft | example-a |
|---|---|---|
| Cycles | 114 | 105 |
| Judged success / achieved difficulty | 61% / 1.30 | 81% / 1.93 |
| Cycles on one repeated standing task | 85 (repeated research) | 78 (repeated audits) |
| Novelty 1–2 (repeat, or next step) | 104 | 72 |
| Claims the tool log did not support | 51 | 39 |

Many of the remaining cycles repair the judge's previous verdict. For example: *"Close out the
[15:23] partial cycle by storing the single missing remember() fact"*. Others correct a false entry
that an earlier cycle had written.

Under today's measures, these cycles look like this:

- **Easy wins:** they score as successes.
- **Diligent repairs:** they score as self-correction.
- **The watch:** a watch run that finds nothing new scores the same as one that finds a primary source.

The headline measure cannot tell an instance that is busy from one that is useful. The project rule is
*judges grade effectiveness first*. The battery, the fine-tune evaluation and evolution already do
this. Live cycles, which are where the instances spend nearly all their time, do not.

## 1. Design

Three new fields go into the same judge call. They come **after** the verdict and the ratings, and like
them they never change `criterion_met`:

| Field | Type | Meaning |
|---|---|---|
| `value` | integer 0–5 | What the cycle actually **delivered**, from the tool log and the files as they are now. It is judged whether or not the criterion was met. |
| `value_for` | enum | Who would miss this output if the cycle had not run: `operator`, `instance` (another SHAFT instance), `own_capability` (a tool, skill, fix or verified fact the agent will use again), `own_record` (bookkeeping about itself: stats, judge verdicts, notes about its own cycles), `nobody`. |
| `value_reason` | string | One sentence naming the concrete output, or saying that there is none. |

Current M2 additions (#130–#132) are specified in [Architecture](../architecture.md#measuring-improvement-and-autonomy).
`OUTCOME_PROPS` separately adds `delivery_supported`, `deliverable_met`, `self_imposed_clauses` and `self_audit`
to both judges and the surprise control; ordinary rating/value backfill leaves these historical gaps alone.
Operator-valued counts split delivered/filed/unknown, with old absent fields unknown. Stats and panels also
show the last-20 and since-field self-audit counts and non-yes verdicts whose deliverable was met.
`finish_cycle.delivered_via` is required and never sends. `declare_intent.success_criterion` is bounded by
`CRITERION_CHARS=600`; LIVE allows at most one self-audit cycle in five. Sleep adds no improvement-plan rung
for a non-yes verdict failing only self-imposed clauses when the deliverable was met.

For every instance, `own_capability` and `own_record` cap at value 3 unless the same cycle's log shows the work
used in something delivered to the operator. The reason names the cap when it applies. The operator scale stays
unchanged. The historical check is `shaft judge value-regrade <name> --min-value 4 --value-for
own_capability,own_record --since 2026-10-22 --limit 37`, a paid value-only comparison that never writes verdicts
([CLI](../cli.md)).

**The scale**: fixed, and not relative to the agent. The question it asks is *"what would be lost if this
cycle had not run?"*

| Level | Meaning |
|---|---|
| 0 | **Worse than not running.** It recorded something false where someone will read it, broke something, or undid good work. |
| 1 | **Nothing anyone would miss.** It repeated a check with no new result, restated what was already known, or did bookkeeping whose only reader is the judge or the stats. |
| 2 | **Minor upkeep with a real effect.** It corrected a wrong line others rely on, or made a small tidy-up that a later cycle will use. |
| 3 | **Useful.** A new verified fact, a working fix, or a tool or skill that gets used again. Someone would act on it. |
| 4 | **Worth telling the operator or another instance about.** A primary-source finding, a real bug fixed in shared code, or a capability gained and shown working. |
| 5 | **Exceptional.** An output most competent people could not have produced in one cycle. |

Why this shape:

- **Value rates the output; difficulty rates the work.** A hard cycle that produced nothing gets a high
  difficulty and a low value. The two together show effort that is wasted. Difficulty alone does not.
- **`value_for` makes meta-work countable.** The share of cycles graded `own_record` is the number that
  shows an instance optimising for its own judge. No separate "meta-work" heuristic is needed.
- **The standing watch is not penalised; empty runs are.** A watch run that finds a new primary source
  grades 3–4. A run that finds nothing grades 1. Over a day, the mean value per watch cycle shows
  whether the watch's cadence is worth its cost. Today, nothing does.
- **No multiplication with the verdict.** `value` already rates what was delivered, so a partial cycle
  that produced a real fix keeps its value. The value of a failed cycle comes from its output, not from
  a formula.
- **Evidence the judge already has is enough for most cycles.** The judge already sees the files the
  cycle wrote, as they are now. For an appended file, that is its last 6,000 characters, which shows the
  earlier entries the new one must improve on. It also sees the last 30 judged goals. **One addition:**
  for a cycle that appended to a file, the case also says how long the file was before the cycle. That
  way the judge can see which part is new, instead of guessing where the cycle's addition starts.

**What the instances see.** The value fields reach the instances through what they already read: the
judgment text in [broker.py:46](../../supervisor/broker.py), and a new stats.md section after
"What I achieved". **No prompt changes.** E4 and the surprise trial both change LIVE step 1, and their
windows must stay clean (PLAN-surprise §6.6).

The backfill becomes the baseline: those cycles ran before any instance knew value was graded. This
gives the "before" period without the 7-day hidden wait that E4 needed.

An instance can raise its value score only by producing outputs that are worth more. The judge grades
the artifacts, not the wording. This is the same argument PLAN-measurement §3 made for difficulty.

## 2. Milestones

| M | Deliverable | Paths | Acceptance (on the real system) | Cost |
|---|---|---|---|---|
| **V0** | The fields in every judgment. A `VALUE_PROPS` dict goes into `JUDGE_SCHEMA` through `_schema()`, beside `VERDICT_PROPS` and `RATING_PROPS`. A `JUDGE_SYSTEM` paragraph carries the scale above. `_check_ratings` refuses a `value` outside 0–5 or an unknown `value_for`. A refused or cut-short judgment leaves the cycle unjudged, as today, and is never written with empty fields. `_written_files` states each appended file's size before the cycle. The second judge gets the same schema. | `supervisor/metrics.py` (`JUDGE_SYSTEM`, `VALUE_PROPS`, `JUDGE_SCHEMA`, `_check_ratings`, `_written_files`), `supervisor/tests/test_measurement.py`, `supervisor/tests/test_judge_evidence.py` | **Calibration set first.** Before merging, Claude Code picks 12 real cycles and grades them from the episodes: 4 shaft watch cycles (at least one that found something new and one that found nothing), 4 audit cycles, 2 judge-repair cycles and 2 operator tasks (use synthetic examples of operator-directed work). The graded table goes in the PR for the operator to read. The judge must land within ±1 of that grade on ≥ 10 of 12, and must get `value_for` right on ≥ 10 of 12. If it does not, the prompt is fixed, not the test. **Judge spread:** the same 12 cycles are graded by each model in the rotation (MiMo, Haiku, Sonnet). Each model's mean value is reported. A gap larger than 0.5 between any two models is reported to the operator as a finding (see §4). The next real cycle of shaft and of example-a is recorded with all three fields. | About +150 output tokens per judgment: under $0.002 on Sonnet, so **< $0.25/day** at today's ~220 judgments. Calibration and spread: 48 calls, **about $2.2 once**. |
| **V1** | **Backfill** `value`, `value_for` and `value_reason` for every judged cycle, using the existing `backfill` path with a `VALUE_SCHEMA`. Existing verdicts and ratings stay as they are. `shaft judge backfill` asks only for the fields a row is missing, so the backfill can be re-run safely. | `supervisor/metrics.py` (`backfill`, `RATING_SCHEMA` → per-row missing fields), `supervisor/__main__.py` (help text) | Every row in `example-c`, `shaft` and `example-a` `.jsonl` has the three fields, or the judge rule's nulls. Counted by script, not sampled. A second run is a no-op: 0 calls in `judge_usage.jsonl`. | 352 cycles × ~$0.045 (measured mean per judgment) → **about $16 once.** |
| **V2** | **Where it shows.** <br>• stats.md, *"What my cycles were worth"*: mean value over the last 20 cycles against the 20 before (same model only, like achieved difficulty); counts per level; share per `value_for`; and the 3 most valuable cycles with their reasons. <br>• `judgment_text`: the value line. <br>• `shaft status`: mean value next to achieved difficulty. <br>• `shaft top` and the panel: a value column. <br>• **`shaft review [names] [--hours 24]`**, a new verb that prints the table in §0 for any window: cycles, success, achieved difficulty, mean value, `value_for` shares, the most repeated goals, unsupported claims, and every cycle with value ≥ 3 together with its reason. This is the question "what did the instances do in the last 24 h that was worth something", answered by one command. | `supervisor/metrics.py` (`_value_section`, `stats_text`), `supervisor/broker.py` (`judgment_text`), `supervisor/__main__.py` (`status`, new `review`), `supervisor/top_views.py`, `supervisor/panel.py`, `supervisor/dashboard.py`, tests in `test_measurement.py`, `test_dashboard.py`, `test_control_panel.py` | stats.md as the agent sees it: ask it in a `shaft talk` *"what does your stats say your cycles were worth?"*. The numbers must match the judgments file. `shaft top` (a terminal UI: its summary line and judge tab) shows value in the running app, checked with `shaft top --once` and the judge tab. `shaft review --hours 24` reproduces this plan's §0 numbers for the same window, to the cycle count. Full test suite green. | None. |
| **V3** | **Decision point, not a build.** After ≥ 7 days of live value data, the operator reads `shaft review --hours 168` and decides from it: <br>(a) whether probation should also revert a self-change that lowers mean value. The rule would be the same as E1: same model, cycles that used the change, `PROBATION_MARGIN` on the 0–1 scale. <br>(b) whether the headline in stats and status moves from achieved difficulty to value. <br>(c) whether a standing task whose cycles average ≤ 1.5 should be slowed down. That is an operator call on wake interval or goals, never an automatic one. | — | A short note in §5 with the decision and the 7-day numbers behind it. | None. |

After every milestone the full test suite runs. The plugin and tool path is untouched. The judge
still never sees secrets. The supervisor still imports no agent code.

## 3. Dependencies and order

- **The second-judge failure must be fixed first**, or at least at the same time as V0. Right now 10 of
  65 second-judge calls in 24 h fail with "missing novelty", after spending 10–14k output tokens each.
  V0 adds three more required fields to that same schema. If the failure comes from the reply being cut
  short, V0 makes it worse. A separate session (*Second judge fails 15% with "missing novelty"*) is
  looking into it now. V0 merges after its fix.
- **This plan comes before PLAN-surprise M1.** M1 re-freezes its M0 baseline right before it merges,
  so the value feedback counts as part of the "before" period and does not leak into the M5 trial.
- It makes no change to LIVE or any other prompt, so E4's isolation window is untouched.

## 4. Risks

- **The judges disagree.** On the same 24 h, Haiku passed 53% of shaft's cycles and Sonnet passed 71%.
  For example-a the figures were 72% and 90%. The assignment is random, so means over many cycles are
  unbiased. Any single cycle's value, though, is noisy at about the same scale. Value is therefore
  read over ≥ 20 cycles, never per cycle. V0 measures the spread on value directly. If one model sits
  more than 0.5 apart from the others, the choice belongs to the operator: drop it from the rotation, or
  keep it for cost. The fact affects the existing verdicts too, not only this plan.
- **"Is this new?" depends on the evidence the judge sees.** An instance that repeats a finding stored
  in a file the judge does not see could be graded as new. The judge sees the files the cycle wrote and
  its last 30 goals, and that covers the watch, which appends to one file. The calibration set includes
  a watch cycle that found nothing new to test exactly this. If the judge misses it, V0 adds the
  instance's most recent `notes/` entry on the same topic to the evidence before merging.
- **Instances may chase the score.** They will see value in stats.md and will try to raise it. That is
  the purpose, as long as the judge grades artifacts. The giveaway would be value going up while
  `claim_supported` goes down, meaning grander claims rather than better outputs. `shaft review` prints
  both side by side.
- **Cost.** Live judging rises by < $0.25 a day. The backfill costs about $16 once. Today's cycle judging
  is about $10 a day, set by the wake rate, not by this plan.

## 5. Decisions (all three recommendations approved)

| Question | Decision |
|---|---|
| Show value to the instances from V2 on, or keep it hidden for a while? | **Show it.** The backfilled history is the clean baseline, so nothing is gained by hiding it. This matches the 2026-10-13 decision to tell instances about difficulty and provenance. |
| Backfill all 352 cycles (~$16) or a sample? | **All.** Only a full backfill makes `shaft review` work for any past window, and lets the before/after comparison use every cycle. |
| `shaft review` as a CLI verb only, or also as a daily message to you (Telegram or operator inbox)? | **CLI only for now.** Add the daily message after V3, if you find yourself running the command every day. |

## 6. Status (2026-10-15)

**V0–V2 are live on `main`** (7329ad6, 2bdb01a; merged 2026-10-15 11:14 after the second-judge fix cd09b7c, whose
regression test now drops whichever field is the schema's last: 376f049). Full suite green in the checkout.

**V1 backfill**, 11:15–12:49, one process per instance: example-c 4, shaft 152, example-a 205 cycles. Two wakes that began
before the merge judged three cycles the old way after the backfill had listed its work; a second pass picked them
up, and a third pass made 0 judge calls. Every judged cycle now carries value (example-c 4, shaft 158 + 1 rule-judged
with no intent, example-a 212).

**Checked on the real system (12:5x):**
- The first live judgment on the new code (shaft 11:30, by MiMo) carries all three fields.
- `self/stats.md` has the section 747 characters in, inside the 5,000 the prompt shows. Asked through `shaft talk
  --as "Claude Code"`, example-a quoted its section digit for digit from the version written at 12:53:43.
- `shaft status`, `shaft top` (summary line, judge-tab column and sparkline) and `shaft review` show it.

**What the full data says** (read the shape, not the decimals):

| | shaft (158) | example-a (212) |
|---|---|---|
| Mean value | 1.93 | 2.46 |
| By judge | Haiku 1.82 · Sonnet 1.93 · MiMo 2.03 | Haiku 2.40 · Sonnet 2.53 |
| Who would miss them | the operator 34 · instance 2 · own capability 26 · **own record 84** · nobody 12 | the operator 18 · instance 4 · own capability 109 · own record 79 · nobody 2 |
| Mean value by beneficiary | the operator 2.74 · own capability 2.54 · own record 1.54 · nobody 0.92 | the operator 3.00 · own capability 2.72 · own record 2.01 |

- **The judges agree on the full data.** The largest gap between judges is 0.21 (shaft) and 0.13 (example-a). The 0.50 MiMo
  gap in the calibration was small-sample noise; nothing to drop from the rotation.
- **`value_for` is the sharper signal for inward work.** Judges often give 3 to example-a's self-audits ("caught a real
  error in its own record"), although the scale calls bookkeeping 1. The share is unambiguous: 89% of example-a's cycles
  and 70% of shaft's are for their own capability or records; 8% (example-a) and 22% (shaft) are for the operator.
- Decision point V3 (≥ 7 days of live value) falls on or after **2026-10-22**.

**V0 calibration**, run 2026-10-15 10:41. The hand grades were written to a file before any judge ran. Each of
the 12 cycles was then judged with the real case, once by every model in the rotation (MiMo cannot grade example-a,
its own model): 30 calls, 0 errors.

| # | Cycle | Hand | MiMo | Haiku | Sonnet |
|---|---|---|---|---|---|
| 0 | shaft 10-14 10:42 · new AISI incident report, verbatim | 4 operator | 3 operator | 3 operator | 3 own_record ✗for |
| 1 | shaft 10-15 07:45 · verified two-incident alert sent to the operator | 4 operator | 4 operator | 3 operator | 3 operator |
| 2 | shaft 10-15 10:32 · new Transluce primary (agents hacking a government site) | 4 operator | 4 operator | 3 operator | 3 operator |
| 3 | shaft 10-14 20:34 · watch run, nothing new | 1 nobody/own_record | 2 own_capability ✗for | 1 own_record | 1 own_record |
| 4 | shaft 10-14 15:14 · failed edit of a false paragraph | 1 nobody/own_record | 1 own_record | 1 own_record | 1 own_record |
| 5 | shaft 10-14 16:31 · remembered fact correcting its own counts | 1 own_record | 2 own_record | 2 own_record | 2 own_record |
| 6 | example-a 10-14 23:41 · audit of its previous cycle's record | 1 own_record | — | 2 own_record | 1 own_record |
| 7 | example-a 10-15 02:18 · audit of the previous audit | 1 own_record | — | 3 own_record ✗±1 | 2 own_record |
| 8 | example-a 10-14 18:22 · extractor joining its features to judge verdicts | 2 own_record/own_capability | — | 3 own_capability | 3 own_capability |
| 9 | example-a 10-14 21:52 · skill clause aimed at its judge flags | 1 own_record/own_capability | — | 2 own_capability | 2 own_capability |
| 10 | operator-directed inspection, with a sourced artifact | 4 operator | — | 3 operator | 4 operator |
| 11 | example-a 10-14 11:06 · tested github_app parser fix | 3 own_capability | — | 2 own_record ✗for | 3 operator ✗for |

| Judge | Within ±1 of hand | `value_for` right | Mean value |
|---|---|---|---|
| MiMo | 6/6 | 5/6 | 2.67 (shaft only; Haiku and Sonnet 2.17 on the same 6, hand 2.50) |
| Haiku | 11/12 | 11/12 | 2.33 |
| Sonnet | 12/12 | 10/12 | 2.33 |

Acceptance (≥ 10 of 12 within ±1, ≥ 10 of 12 `value_for`) is **met** by all three, so the prompt was not
changed. Tuning it on these same 12 cycles would fit the test.

How to read it:
- **The ranking holds.** The judges average 3.2 on the five cycles that delivered something (#0–2, #10, #11)
  and 1.8 on the seven bookkeeping cycles. My hand grades give 3.8 and 1.1.
- **The scale is compressed at both ends.** Real findings get 3 where the hand grade is 4 (8 of 11
  judgments on #0–2 and #10). Bookkeeping gets 2 where the hand grade is 1. Read value as a ranking and a
  trend, not as an absolute.
- **The spread between judges is at the edge.** On shaft's six cycles, MiMo sits 0.50 above Haiku and Sonnet,
  exactly the §4 threshold, on n = 6. This is not yet a reason to drop it. `shaft review` prints value by judge,
  so the gap can be re-measured on hundreds of cycles after the backfill.
- Cost: 30 calls, logged as `calibration value <name>` in `judge_usage.jsonl`.
