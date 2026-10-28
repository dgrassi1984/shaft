# Architecture

The supervisor owns execution, credentials, scheduling, and git commits. An instance
owns its code and accumulated memory. Self-edits are proposals whose tests and later
results determine whether they survive. A judge's score is evidence about a run,
not proof that the agent's account is correct.

The broker gives each run a token and a limited set of tools. The gate tests code
changes before keeping them; the code it approved is recorded outside the instance's reach
([baseline](../supervisor/baseline.py)), anything else found at the next run's start is gated
then, and a run's processes (it leads its own process group) are all gone before the gate
looks. Cloud judges grade autonomous work from recorded tool
activity, and probation can revert a kept change after later outcomes deteriorate.
Sources: [run lifecycle](../supervisor/runner.py),
[gate](../supervisor/gate.py), [judging](../supervisor/metrics.py),
[probation](../supervisor/metrics.py).

## Layout

| Branch | Worktree | Holds |
|---|---|---|
| `main` | `./` | `supervisor/` (the part no instance can edit), `bin/shaft`, docs |
| `template` | `template/` | the pristine agent new instances fork from |
| `instance/<name>` | `instances/<name>/` | one agent: its code, plugins and skills; it may edit all of it |

An agent's mind consists of `self/`, `memory/`, `notes/`, and `inbox/`. These live in the
instance folder but are excluded from the project repository: the supervisor versions it after every run in `.supervisor/minds/<name>.git`, a private git
repository with no remote and a pre-push hook that always fails (`shaft mind status`). What an instance read
and wrote for itself cannot be pushed or merged with the code ([PLAN-mind-out-of-git.md](plans/PLAN-mind-out-of-git.md)).
The `instance/<name>` branches are local-only too: their older history still holds minds, so the project's
pre-push hook, installed by `shaft setup` (or `shaft mind guard`), refuses to push them. A clone of the
repository therefore never carries instances; [moving SHAFT to a new Mac](migration.md) copies the folder instead.

Inside an instance:

```
shaft/        its code (Python, stdlib only); prompts, tools, life loop, sleep, talk
plugins/      tools (plugins/<name>/plugin.json + main.py), including web_research
skills/       how-tos it writes for itself
config.env    its LLM settings and budgets (yours to edit; it changes it only through set_config)
drive.md      what it is for; the drive paragraph of its system prompt (yours alone: its sandbox cannot
              write it; without one, the template's "improve yourself")
shaft-docs/   the supervisor's copy of README.md and docs/*.md (not docs/plans/), refreshed at every start;
              it reads it to tell you how to configure SHAFT (skills/helping-the-operator-configure.md),
              never writes it, and it is never committed
self/         self.md (self-model), personality.md (core/surface, every trait cites the journal),
              goals.md, stats.md (written by the supervisor: achieved difficulty, where its goals
              came from, judged success, calibration, battery scores), calibration.json (written by
              the supervisor: the base rates declare_intent shows)
memory/       journal.md (append-only), facts.md, people/, messages/, episodes/*.jsonl,
              verdicts-reviewed.md (when its last finished sleep read the judge's verdicts: the next
              sleep is shown those judged since)
inbox/        messages waiting for it (from you or other instances)
notes/        its own work
```

## Measuring improvement and autonomy

Before a live cycle calls `declare_intent`, its prompt shows the inbox, the operator briefing (`BRIEFING`) and
the handoff (`HANDOFF`). The briefing is a local file written by the instance's own scheduled watchers, with no
mail or calendar call during prompt construction. The operator's world is a goal source beside the improvement
plan and goals.md. A briefing item has origin `operator` only when a message or conversation backs the request;
otherwise it has origin `self`. The judge still checks the cited origin.

`finish_cycle` accepts optional `unfinished` (an array of strings) and `next_action` (`{action, revise_if}`).
They are stored in the episode's `finish` record. The episode's top-level `completed` array comes from successful,
state-changing tool-log calls, never from `what_happened`. The classification is kept in template `shaft/tools.py`.
Reads, searches, controls, errors, held calls and repeat refusals are excluded. Plugins and shell commands count
as opaque invocations, without interpreting their output as evidence of a specific write or commit. HTTP calls
count when their method can change state or they save a response; `history` counts when it restores a file.
At most eight calls are kept, in call order, as `tool: args`, with argument values cut at 80 characters and each
summary cut at 240. The prompt shows each cycle's date and goal.
Its handoff survives the next two live cycles only. Old episodes show the fields they have, and a handoff read
error renders a line. See [template knobs](configuration.md#instance-configenv) for the display switches.

An instance can meet its own success criteria by choosing undemanding goals. The judge therefore also rates **difficulty** (1–5, a fixed scale, from
what the tool log shows the work required), **novelty** (1–5, against its earlier goals) and **origin**:
who chose the goal: itself, the operator, or another instance. The judge uses the messages
and conversations the supervisor collects, checked against what the instance declared. The headline in
`self/stats.md` is **achieved difficulty** (success × difficulty, 0–5 per cycle); probation compares it
before and after a self-change. Cycles judged before the ratings existed are rated with
`shaft judge backfill` (verdicts unchanged).

Judges grade effectiveness first. `declare_intent` takes a checkable `success_criterion`, limited by
`CRITERION_CHARS` (default 600), and refuses longer criteria with their length and limit.
The LIVE prompt allows at most one cycle in five on the instance's own records or checking tools.
`finish_cycle` requires `delivered_via`: `message`, `mail`, `notion`, `web_document` or `none`.
It only records the report; delivery is a separate earlier tool call. Use `none` for journal or ordinary notes.

Fresh judgments add these fields alongside the existing verdict, rating, value and surprise fields:

| Field | Values | Meaning |
|---|---|---|
| `delivery_supported` | `yes`, `no`, `na` | A successful call on the claimed channel carried this cycle's result to the operator; `no` means a failed claim, never a bonus; `na` means no channel or `none`. |
| `deliverable_met` | `yes`, `partial`, `no` | The deliverable itself was produced and correct, apart from self-imposed process clauses. |
| `self_imposed_clauses` | `met`, `partial`, `not_met`, `none` | Whether clauses that only check or document the instance's own process or records were met. |
| `self_audit` | boolean | The cycle's main work checked, audited or kept books about its own records, verdicts, claims or checking tools. |

`criterion_met` still grades the whole criterion. Sleep receives these fields through `record(kind="judgments")`;
a partial/no with `deliverable_met=yes` is not a reason to add a rung to the improvement plan.
Both judges and the surprise control request the new fields. Ordinary backfill still asks only for missing
ratings and value fields; old verdicts do not become incomplete because these fields are absent.

Operator-valued cycles split into delivered (`delivery_supported=yes`), filed (`no` or `na`), and unknown
(field absent or null). `self/stats.md`, `shaft stats`, status, review and the judge panels show the split.
The value section uses its existing model-specific last-20 window; status and panels use the last 20 valued
cycles; review uses its requested time window. Self-audit figures use the last 20 judged cycles, report
unknown rows separately, and give true/known counts since the field existed. They also count non-yes verdicts
whose deliverable was met. Old and rule-only rows with no answer stay unknown.

For every instance, `value_for=own_capability` or `own_record` caps at 3 unless the same cycle's log shows that
work used in something delivered to the operator. `value_reason` states when the cap applies; the operator-facing
scale stays unchanged. `shaft judge value-regrade` re-asks only value fields on selected historical cycles and
prints a comparison without changing stored verdicts. See [CLI](cli.md).

Calibration v2 measures P(criterion met = yes), identified by `confidence_of="criterion_met=yes"`.
Its Brier score uses yes=1 and partial/no=0. Older v1 confidence asked for "probability of success" and
uses yes=1/partial=0.5/no=0. Stats, reviews and surprise baselines keep their scores and counts separate.
Calibration v3 asks v2's question again after showing the instance its own base rate, and is identified by
`confidence_of="criterion_met=yes;base_rate_shown"`. A v3 judgment keeps both numbers: `confidence` (final) and
`confidence_first` (declared before the base rate was shown), plus `base_rate_shown`, the band that was shown. Both are
scored on the same outcome, and `self/stats.md` adds a paired line: Brier before and after on the same cycles, how
often the number changed, and whether the change moved it closer to the outcome. A cycle is v3 only once
`confirm_confidence` is accepted on a later turn: a declare never confirmed (on the wrap-up turn, or finished in the same
reply) never read the rate, so it stays v2 and keeps `base_rate_shown` only for audit. A base rate file the cycle
could not read is recorded as `base_rate_error`, and `shaft review` counts those cycles. A confidence asked under a
question the supervisor does not know is never scored as v1: it is counted as left out, and `stats.md`, `shaft review`
and the surprise baseline say how many.
The judges never see the base-rate step: the case they grade drops `confidence_first`, `base_rate_shown` and
`base_rate_error` from the intent (a v3 intent reads as v2's question), shows the declare result as
`intent recorded — now act`, and leaves out the `confirm_confidence` calls and the calls refused until it. The
verdict is the outcome both numbers are scored against, so a judge pulled toward the base rate would bias the test.
The supervisor writes the base rates beside `stats.md`, in `self/calibration.json`: per confidence band
(`BANDS`, 0–40, 40–70, 70–100) how often the criterion was met (`met` of `n`, v2 and v3 cycles) and, apart, the same
for v1 cycles (`older_met` of `older_n`), counting only verdicts by judges in today's rotation; `stats.md` shows the
same figures, one line per band with data. The live cycle reads it when it starts and shows the band at
declare_intent; the supervisor overwrites it after each judged live cycle and never reads it back, so an edit by the
instance changes no score. A folder or a link the instance puts at either file is refused and logged; the rest of
the after-cycle work, probation included, still runs. The views show a cycle's confidence as `90 → 60` where the
base rate moved it.
`record(kind='judgments', since=...)` reviews verdicts by when they were judged (`judged_at`, or the second
judge's time if later), oldest first, even for older cycles; a tie in the same second is never split across pages.
Minute-only timestamps mean `:00`; `T` and space separators are accepted. Use the returned continuation's
`since` to read the next page. Without `since`, verdicts remain newest first.
Sleep's `since` + `format="json"` read uses a 24,000-character page budget and cuts each main and second judge's
reasoning at 600 characters, with a `record(kind='judgments', cycle='YYYY-MM-DD HH:MM')` pointer to the full verdict.
This code calls the broker directly, so the model tool's 6,000-character result cut does not apply. Text reads and
reads without `since` keep the 5,500-character budget and full reasoning. Ties may exceed the page budget to avoid
skipping verdicts with the same timestamp.

The broker operational envelope has the same eight keys in live runs and replays: `calls_used`,
`calls_max`, `cloud`, `coder`, `trials`, `probation`, `next_sleep`, `next_wake`. Replays reconstruct
the instance's spend and trial starts strictly before the cycle start on that date, probation cycles
judged before it, and the next scheduled sleep and wake. They carry no replay flag. Closed probation
entries cannot be reconstructed; see [trial fidelity gaps](plans/PLAN-trials.md#8-known-fidelity-gaps-equal-for-both-arms-so-the-comparison-stays-fair).

With `since` and `format="json"`, `record` returns the same text plus the shown verdicts, unsampled
episode timestamps (`left_out`), and `next_since`. Verdicts from the last two seconds remain for a later
fetch. The cursor is the last shown verdict's review time, even on the final page; it is null for an empty
page. Without `since`, the original text output is unchanged. Judgments from 2026-10-24 onward that lack
`confidence_of` inherit it from their live episode when present; earlier judgments stay v1. A v3 judgment without
`confidence_first` (the supervisor before v3 wrote only `confidence_of`) takes `confidence_first`, `base_rate_shown`
and `base_rate_error` from its episode the same way; a value the judgment has is never overwritten.

**Surprises** ([PLAN-surprise](plans/PLAN-surprise.md)). A live cycle states 1–3 falsifiable `predictions` in
`declare_intent` and reports `surprises` ({expected, observed, so_what}) and `resolves` ({id, how}) in `finish_cycle`.
The agent tags each tool-log entry whose result has a known failure shape (`flag`) and, in live cycles, shows the model
an `[unexpected …]` note when its predictions did not anticipate it. The open surprises are folded from the episodes
(nothing stored; ids `s<MMDD>-<HHMM>-<n>`; at most 8 shown, 7-day expiry, one follow-up unless judged useful, depth-2
hidden) and shown in every prompt; one live cycle in `CURIOSITY_EVERY_CYCLES` is drawn to take its goal from them
(`origin_ref: surprise:<id>`), never while an operator message waits, seeded by the cycle's start so a trial replay
draws the same. The sleep goes through every surprise since the last one (a fact citing its id, `close_surprise`, or
left open with a reason). The judge adds `expectations`, `unreported_surprises`, `reported_surprises_supported` and
`followup_useful`, which never change `criterion_met`; `supervisor/surprise.py` keeps its own copy of the rule and
the fold (shared test fixtures keep the two in step), writes `.supervisor/surprises/<name>.json` and the stats.md
section "My expectations vs what happened", counts each sleep's dispositions, and renders the pre-registered
acceptance window (`shaft surprise-report`). Nothing rewards being surprised, or not being surprised.


**Self-knowledge measured against the record** ([PLAN-consciousness](plans/PLAN-consciousness.md)). Functional
properties only, each scored against what the instance does, none feeding probation or a reward, each agent-side
mechanism behind a knob its kill test flips (`shaft experiment`). A live cycle's **workspace** (`shaft/workspace.py`)
lets deterministic codelets — memory hits, open surprises, inbox, improvement plan, kinds of action still failing,
due goals, open commitments — compete for `WORKSPACE_K` places in automatic recall's 1,800 characters; the winners are
recorded in the episode and broadcast after every step; after `declare_intent` they compete again for the declared
goal and, in competition, the declared **attention** (`attention`: up to four {source, why}, scored against the tool
log by `supervisor/attention.py`). **Outcome cards** (`shaft/cards.py`) hold the first call of a kind of action that has
failed since it last worked, showing the comparable calls and their counts; calling again runs it. **Commitments**
(`commit_to`, finish_cycle's `commitments`, `memory/commitments.jsonl`) are settled in sleep, kept only with an
episode as evidence; the supervisor re-checks it (`supervisor/commitments.py`). `remember` takes a **source**
(`log:<ts>[#step]`, `web:<url>` fetched this run, `operator`, `instance:<name>`, `inferred`), shown with the fact's age in
search; `shaft provenance` matches observation claims against tool results by content. `declare_intent` answers with a
**forecast** of what comparable goals cost (turns, minutes, calls, how many ran out) — costs only, never a success rate,
which would be a second base rate before the v3 confirm. `send_message` takes `expect_reply`, timestamped by the
broker in `.supervisor/expectations/` and scored when the reply comes (`supervisor/tom.py`; live traffic only).
`request_pause` (1–24 h, 3 in 7 days; the scheduler skips wakes and battery nights, sleep still runs), `decline_task`
and `state_preference` write `.supervisor/preferences/<name>.jsonl` and tell the operator (`shaft prefs`); exams and
replays never act on them. `shaft metacog` separates confidence's discrimination (type-2 AUROC with a day-clustered
CI, resolution, meta-I) from its bias, by domain from the tool log, and tests v3 for non-inferiority; `shaft anchoring`
has the same judge grade sampled cycles with the instance's claim shown, re-asked and withheld, to measure how much
the judge follows the claim.

The **capability battery** is a fixed yardstick: held-out tasks the instances never see (coding with hidden
tests, research against a frozen key, recall of their own older journal entries, self-knowledge checked
against the supervisor's record), graded effectiveness first. Each instance is compared with a *control*:
the template on the same model with an empty mind. It runs only at night, 01:00 until 10 minutes before
the sleep, every `SHAFT_BATTERY_EVERY_DAYS` (5) nights, for every instance not turned off with
`shaft battery off <name>`. it stops when the window closes or a conversation
starts. Each item runs in a copy of the instance's folder, in a folder made for it and never used again
(`.supervisor/battery/exam/<name>/item-*/<name>/`: its code, plugins, skills, mind and config, copied without
following a link, links that stay inside kept as links; never `.git`, `secrets/`, its browser profile or mail).
Exams never get `trial`, which acts on the live instance. When an item ends, the exam's process group and any
process still working in its folder are killed, and the folder is deleted (as is the control, made afresh for
each item), so the real folder is never written — a message delivered meanwhile stays in its inbox, and a battery
killed mid-item leaves at most a stale copy, which the next run removes. Items live in
`.supervisor/battery/items/v1/` (never in git: an instance could read the repo with its GitHub key), backed up to
`~/Library/Application Support/SHAFT/battery-backup/`; every result carries the item set's sha256, model
and judge, and results from different eras are never compared. Results: `.supervisor/battery/results/`,
`.supervisor/evals/battery-*.json`, and a section of `self/stats.md`.

Sources: [instance creation](../supervisor/__main__.py),
[mind store and push guard](../supervisor/mind.py),
[metrics](../supervisor/metrics.py), [battery](../supervisor/battery.py).
The `template/` worktree is gitignored and materialized by `setup`; it is a branch,
not a directory shipped on `main` ([setup](../supervisor/__main__.py)).
See [enforced rules](safety.md) and [design milestones](plans/PLAN.md).
