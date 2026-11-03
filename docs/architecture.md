# Architecture

The supervisor owns execution, credentials, scheduling, and git commits. An instance
owns its code and accumulated memory. Self-edits are proposals whose tests and later
results determine whether they survive. A judge's score is evidence about a run,
not proof that the agent's account is correct.

The broker gives each run a token and a limited set of tools. The gate tests code
changes before keeping them; the code it approved is recorded outside the instance's reach
([baseline](../supervisor/baseline.py)), anything else found at the next run's start is gated
then, and a run's processes (it leads its own process group) are all gone before the gate
looks. Each live launch imports from a supervisor-owned, read-only copy of the approved code snapshot
under `.supervisor/protected/` (#86). Isolated Python excludes cwd from imports; cwd and `SHAFT_ROOT`
still point at the instance's mind and working edits. Broker `run` and plugin execution use that working
folder, so newly written plugins remain testable. At settlement the gate tests a separate read-only copy
of its snapshot, with writable `.tmp/` scratch space and the instance's Python environment read in place.
Both copies link every other top-level entry of the instance folder (its notes, memory, mind) back to it,
so code that finds its data from its own file still finds it; a code path, a module file or a package is
never linked.
The commit uses those same snapshot bytes. Copies are removed after use; the next copy sweeps those
left by dead supervisors. Trial replays and battery exams keep their existing copies.
Cloud judges grade autonomous work from recorded tool
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
              corrections.jsonl (append-only operator corrections and their closures),
              verdicts-reviewed.md (when its last finished sleep read the judge's verdicts: the next
              sleep is shown those judged since)
inbox/        messages waiting for it (from you or other instances)
notes/        its own work
```

## Effect-loop measurement

The shared template `shaft/agent.py` loop keeps its exact-call repeat guard and adds an effect guard (#103),
configured by `LOOP_GUARD` and `LOOP_EFFECT_ROUNDS`. Its target table lives in `shaft/loopguard.py`:
lexical paths, URL host/path without query parameters, query words with case/punctuation/request filler removed,
a shell command's program and subcommand, or a plugin/tool name. This groups cosmetic variants, not arbitrary
semantic paraphrases. A successful write (tools.STATE_CHANGING) is never "no new evidence", whatever it answers:
remember says "remembered" for every fact. On thelos's 1,412 real episodes (2026-10-28) the shadow guard would
fire in 19, each a run of failures on one target (most of them rewrite_facts or write_digest refused for size in
a sleep); `python -m shaft.loopguard report <episodes dir>` measures it again.
Observations reuse `surprises.flag(result)` including its detail; otherwise they hash the result after normalising
whitespace, ISO timestamps, UUIDs and named ID fields. JSON key order is ignored. New content resets the streak,
so advancing pagination, changing status and successful retries can continue. Long unchanged polling can still
cross the configured threshold; the guard cannot know that a future poll will change.

Shadow mode, the default, adds no model-visible text. Executed tool-log entries store an `effect` signature and
turn number, and episodes store would-fire events as `loop_shadow` entries with `turn`, `family`, `rounds` and
`would_say`. On mode appends the advice to the executed result, offering another hypothesis, another source or
an honest stop. It never blocks the call. Off mode stores neither signatures nor events.
The tool log keeps the original result in every mode. Held calls and exact-repeat refusals are not new effects.

From the template directory, `python -m shaft.loopguard report <episodes-dir>` replays direct `*.jsonl` files
without executing tools or writing files; `--rounds N` selects a threshold. Stored signatures avoid replaying
truncated arguments. Old entries without signatures use their recorded arguments/results.
For a measured false-positive rate, label reviewed episodes with `loop_expected: false` for legitimate work
or `true` for confirmed/planted loops in a separate evaluation copy. The report divides flagged legitimate
episodes by labelled legitimate episodes. Unlabelled episodes appear in totals but not that denominator;
the rate is `null` if none are labelled. The fixture corpus in `supervisor/tests/loop_effect_fixtures.json`
keeps planted failures separate from held-out pagination, polling and transient retries. Its measured rate
does not establish the rate on real runs.

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
`shaft judge backfill` (verdicts unchanged). Judged success and unsupported claims count only judges in today's
rotation, and are given **per judge**: each judge's 7 days to the latest verdict against its 7 before, "too few" under
10 verdicts. Judges differ several-fold on claims, so no figure pools two of them (docs/plans/PLAN-judge-mix.md).
The success line sits under achieved difficulty, inside the first 5500 characters the LIVE prompt shows.

Stats rebuilds share parsed episode files, invalidated by file name, inode, nanosecond mtime and size;
reads still refuse links. Provenance indexes citation times and URLs once per rebuild and shingles each
needed result once per fact length. Confidence sections check their sample floors before bootstrapping;
deterministic intervals are cached by judgment content. Reply scoring skips final receipts before listing
message folders, lists each folder once for pending receipts, and reads the chat ledger once if needed.

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
| `should_have_waited` | `yes`, `no`, `na` | Whether any operator push could have waited for a morning digest; `no` means all pushes were time-critical, `na` means no push. Judged from the tool log, content and timing, never tone or politeness. |

`criterion_met` still grades the whole criterion. Sleep receives these fields through `record(kind="judgments")`;
a partial/no with `deliverable_met=yes` is not a reason to add a rung to the improvement plan.
Both judges and the surprise control request the new fields. Ordinary backfill still asks only for missing
ratings and value fields; old verdicts do not become incomplete because these fields are absent.

Talk, API and live prompts allow disagreement with the operator and require it when a request conflicts
with a tool result. The instance must cite the call and what it showed. There is no quota.
Fresh live-cycle judgments require `pushback=none|cited|uncited` in its own property group.
A disagreement without a cited tool result is unsupported and classified `uncited`.
Conversations are not currently judged. Old verdicts remain valid without this field.

`shaft judge pushback-outcome` previews cited cycles at least three days old without calls or writes.
With `--write`, one judge request per unscored cycle assesses later episodes and operator messages as
`borne_out=yes|no|unclear`, with one sentence. Each later-evidence section uses the existing 8,000-character
limit and marks cuts. Results go to `.supervisor/judgments/<name>-pushback-outcomes.jsonl`, never the original
verdict file. Already scored cycles are skipped. Paid requests retain normal judge usage accounting.

Inbox archives and terminal/web alert dismissals append timestamped events to
`.supervisor/dismissals.jsonl`; failures to record an event do not fail the original action.
Message archive events identify the message file; the supervisor's message log identifies its sender.
Alert events identify the alert and its instance when the alert id contains one. Alert dismissals cannot
establish that a particular message was dismissed and are not attributed to pushback cycles.
Annoyance counts pushback cycles whose own `send_message(to=operator)` message was archived within 60 seconds.
It matches the message text and the recorded run start/end, counts each cycle once, and skips cycles missing
that boundary. This measures timing, not causation. Reply-triggered archives also count as archives.

`self/stats.md` and review report cited pushbacks in the last seven days, the share borne out among all scored
cited cycles, and the last seven days' annoyance count on separate lines. The scored denominator includes
`unclear`, which is also shown separately. Uncited and ungraded counts remain visible. None of these measures
changes effectiveness, value or training scores. Operator acceptance, thumbs, agreement and dismissals
are never grade inputs. A 👎 removes that reply from fine-tuning data; it changes no grade.
There is no self-rated care score.

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

`CONFIDENCE_MODE=confirm` keeps this flow. `noconfirm` records v2 without showing a base rate or offering
`confirm_confidence`. `clauses` records 1-4 `{clause, confidence}` entries and a null confidence in the episode.
When `judge_pending` copies the intent into a judgment row, the supervisor computes
`100 * product(clause.confidence / 100)`. All clauses must hold; this rule assumes independence.
The instance never combines the probabilities. The judgment keeps the clauses and labels the result
`confidence_of="criterion_met=yes;clauses"` (v4). Calibration and metacog AUROC report v4 apart from v1-v3;
`base_rates` stores its counts separately under `clauses`, outside the bands shown by the confirm flow.
The live prompt names the resolved mode. The arms await a later trial; no instance is switched here.

`finish_cycle` can record `doubts: [{step, what}]`, using 1-based indices in the original tool log.
They never change its `criterion_met` claim. Fresh judge verdicts require `first_error_step`, the earliest step
whose result or use made the outcome fail, and `first_error`, a one-sentence explanation. The step is 0
when the criterion was met or no step is at fault (a plain integer: the Claude judge receives the schema as a
structured-output format). These fields never change the verdict; old judgments stay valid.
Judges retain original step numbers when confirmation calls are withheld. `record(kind="judgments")` exposes
the error fields for the instance to read.

An instance could build a `postmortem` plugin that reads `record(kind="judgments")` for a failed cycle's
`first_error_step` and `first_error`, looks up that step in the tool log and its own `finish.doubts`, and puts
the located error into the next attempt at the same goal. This is an idea, not an installed plugin.
Use `trial` to compare retries with and without the located error; keep it only if more retries succeed.

`record(kind='judgments', since=...)` reviews verdicts by when they were judged (`judged_at`, or the second
judge's time if later), oldest first, even for older cycles; a tie in the same second is never split across pages.
Minute-only timestamps mean `:00`; `T` and space separators are accepted. Use the returned continuation's
`since` to read the next page. Without `since`, verdicts remain newest first.
Sleep's `since` + `format="json"` read uses a 24,000-character page budget and cuts each main and second judge's
reasoning at 600 characters, with a `record(kind='judgments', cycle='YYYY-MM-DD HH:MM')` pointer to the full verdict.
This code calls the broker directly, so the model tool's 6,000-character result cut does not apply. Text reads and
reads without `since` keep the 5,500-character budget and full reasoning. Ties may exceed the page budget to avoid
skipping verdicts with the same timestamp.

The broker operational envelope has the same keys in live runs and replays: `calls_used`,
`calls_max`, `cloud`, `consult`, `coder`, `trials`, `probation`, `next_sleep`, `next_wake`, `pushes`.
`pushes` holds today's count and `SHAFT_PUSH_BUDGET` (default 6), or null when the budget is 0.
The template shows "pushes to the operator today: N of budget B"; `send_message` to the operator
returns the count after delivery too. A push is an instance-to-operator message with `alert` in the
supervisor's message log. Counts use the supervisor's local calendar day and survive inbox archiving.
The budget is measured, never enforced. Replays reconstruct
the instance's spend and trial starts strictly before the cycle start on that date, probation cycles
judged before it, and the next scheduled sleep and wake. They carry no replay flag. Closed probation
entries cannot be reconstructed; see [trial fidelity gaps](plans/PLAN-trials.md#8-known-fidelity-gaps-equal-for-both-arms-so-the-comparison-stays-fair).

`self/stats.md`, `shaft stats` and review report pushes today and each of the last seven calendar days,
the share of judged push cycles with `should_have_waited=yes` (excluding `na` and ungraded old rows),
and operator reactions separately. Reaction counts use the next operator message to that instance or
`start talk`/`start api` in the supervisor log: within 1 hour, 1–24 hours, or no response recorded yet
within 24 hours as of the report. Several pushes can precede the same response; this measures sequence,
not causation or approval. API starts include web and Telegram conversations. No timestamped per-message
read or dismissal event exists: inbox `read` is inferred from current file location, and panel dismissals
store alert IDs only, so a dismissal within a minute cannot be measured. No outcomes feed a combined score.

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


Operator corrections (#135) are extracted during the conversation writeup in `shaft/recovery.py`.
The writeup reads every saved turn in bounded transcript pages and offers `record_correction` before
it writes the journal entry. Open conversations remain excluded. Each correction in
`memory/corrections.jsonl` gets an id such as `k1028-1405-1`, its original timestamp and conversation id,
what was wrong, and what the operator said. The model links the same correction within seven days
with `reopens`. Live and sleep prompts show open items and the seven-day reopened count, including
an error line if the ledger cannot be read. Tool arguments are typed back to their schema first (`tools.coerce_args`:
a model that sends `"null"` or `"120"` as text gets JSON null or 120), a refusal names the field and the value it got,
every call's result goes to `logs/run.log` and the `talk_recovered` episode (`corrections`), and the journal entry ends
with what the tool log says was recorded. A writeup whose last reply called a tool, or only wrote one out as text, is
asked once more without tools. On mimo (2026-10-28..30) 44 calls with `reopens: "null"` were refused and nothing said
so (PLAN-thelos-review-2 M3).

Facts carry their valid_from in the existing `- [YYYY-MM-DD HH:MM]` date (#143); older
unsuffixed facts need no migration. `remember(fact, source, supersedes=...)` identifies exactly one
current fact by its date stamp and optional opening words. Missing or ambiguous matches name the
candidates and change neither file. The old line moves to append-only `memory/facts-archive.md`
under `## Retired (superseded) <now>`, annotated with the replacement date and evidence; the new
line names the date it supersedes. Consolidation with `rewrite_facts` retains dates and sources.

Sleep step 11 checks current facts against newer facts and the period's episodes, even without an
explicit change announcement. It receives at most 15 deterministic candidate pairs sharing three
leading significant words, a capitalized name/key, or "Today is"; both dates are shown. Candidates
are hints, not proof. A read/parse error becomes a prompt line and does not stop the sleep.
The sleep-only `retire_fact(fact, evidence)` can retire without replacement. Evidence is an existing
`log:<episode ts>[#step]`, a newer current fact's date (optionally `fact:<date>` and opening words),
a fetched `web:<url>`, or `operator`. Log/fact evidence must be later than the retired fact;
web/operator observations use the current time. Log and web sources use `remember`'s validation.
Retirements and supersessions are recorded in tool results for the judge, and step 11 requires
them with evidence in the sleep journal entry. The tools do not write separate journal notes.
Prompts show current facts; retired facts remain available through `search_memory`.

Sleep step 10 reviews `shaft/staleness.py` findings (#138). It scans self/goals.md and self/self.md
for ISO or English/Italian day-month dates older than `STALE_LINE_HOURS` (24). Dates without a year
use their latest occurrence at local midnight. Active goal bullets count as touched when their full
normalized text, or six consecutive words, appears in live intent, finish or tool arguments.
With an operator user turn in the last `STALE_GOAL_HOURS` (48), goals with no touch in that window
are listed with their last recorded touch, or explicitly unknown. Tool results do not count.
Both knobs accept 0 to disable their check. Read failures produce a line and sleep continues.
The sleep journal must record each finding and its fix, retirement or reason to keep it.

Operator claims about the instance's behaviour/history (#139) must be checked against record/history
or episodes before becoming self-model lines or remembered facts. Otherwise each claim carries
"operator's hypothesis, not verified (YYYY-MM-DD)". Contradictions are explained in the reply with
evidence; preferences and instructions are exempt. Talk, API, writeup and sleep share this guidance.
`update_self_model` accepts optional `source` and `checked_with`. With `source=operator`, new body
lines require the dated marking or a reference to an existing episode (`log:<ts>`) or a successful
record/history call in this run (`record:<JSON arguments>` or `history:<JSON arguments>`).
This validates provenance, not whether the evidence supports the claim. Calls without source retain
their behaviour. `remember` keeps its existing source contract: its preference-versus-self-fact
distinction is enforced in the prompt because deterministic code cannot classify that meaning.
The writeup currently offers only correction controls, so unchecked transcript claims must remain
marked hypotheses there.

Sleep step 9 asks for `close_correction(id, closed_by, evidence)`: `fact` after a fact change citing
the correction, `code` after a commit or plugin change, `test`, or `not_accepted` with a reason.
Evidence names the episode, commit or fact. An omitted closure leaves the item open.
The supervisor reads the ledger without importing template code. `panel.alerts` emits the stable id
`correction:<instance>:<id>` after more than `SHAFT_CORRECTION_ALERT_CYCLES` live cycles since the correction.
The default is 6, and 0 disables the alert. A missing, unreadable or malformed ledger raises no alert.


**Self-knowledge measured against the record** ([PLAN-consciousness](plans/PLAN-consciousness.md)). Functional
properties only, each scored against what the instance does, none feeding probation or a reward, each agent-side
mechanism behind a knob its kill test flips (`shaft experiment`). A live cycle's **workspace** (`shaft/workspace.py`)
lets deterministic codelets — memory hits, open surprises, inbox, improvement plan, kinds of action still failing,
due goals, open commitments — compete for `WORKSPACE_K` places in automatic recall's 1,800 characters; the winners are
recorded in the episode and broadcast after every step (`python -m shaft front` prints, without a model call, what
each mode would put in front of a cycle starting now: the kill test's dry run reads it in a replay's copy); after `declare_intent` they compete again for the declared
goal and, in competition, the declared **attention** (`attention`: up to four {source, why}, scored against the tool
log by `supervisor/attention.py`: a call touches its tool's source — mail, calendar, record, memory, web, or what a
plugin's name says — the files in its arguments, in a run's command or in a script the cycle wrote and ran, a
message's recipient and a plugin's code; free-text sources are split into paths and known sources, and one it cannot
read is reported unreadable, not untouched). **Outcome cards** (`shaft/cards.py`) hold the first call of a kind of
action that has failed since it last worked, showing the comparable calls and their counts; calling again runs it.
A `run` call's kind is the program it actually runs (past a leading `cd`, assignments and wrappers like `env`;
inside `bash -c`; `python -m pytest` is `run pytest`, `python3 shaft/x.py` is `run python3 shaft/x.py`). Read-only
inspections (`echo`, `cat`, `ls`, `wc`, `grep`, `sed -n`, …) have no kind and are never held. **Commitments**
(`commit_to`, finish_cycle's `commitments`, `memory/commitments.jsonl`) are settled in sleep, kept only with an
episode as evidence; the supervisor re-checks it (`supervisor/commitments.py`). The ledger accepts promises only
to Daniele (`made_to="operator"`) or a peer returned by `list_instances`. Unknown names and self promises are refused;
own plans belong in goals/notes. Old self records remain on disk but are excluded from prompts, recall and sleep
settlement; the supervisor reports their count separately as "to self (no longer kept)".

Projects (#106) use append-only `memory/projects.jsonl` opened/closed by `open_project` and `close_project`.
Each records a name, future ISO deadline (a date includes the whole day), optional positive calls/dollar budgets,
and one line saying what done means. `declare_intent(project=...)` accepts an open id; its episode carries that id.
Live and sleep prompts show measured boundaries; `self/stats.md` uses the supervisor's independent read-only fold.
At risk means at most 20% of the original span remains, at least 80% of a budget is spent, or two consecutive
judged project cycles received no/partial. Past boundary means the deadline is reached, a budget exhausted,
or three consecutive judged no/partial cycles. Unjudged cycles do not reset the judged streak; a judged yes does.
Sleep includes freshly fetched verdict pages as well as `memory/verdicts.jsonl`. The live prompt uses verdicts
already received by sleeps; supervisor stats uses its own judgments. Thresholds are fixed, with no new knobs.
`project_usage` on each live episode stores the final envelope's broker `calls_used` and `run_usd`. The dollars
are the recorded cloud, coder and consult charges collected in that broker's request context, including model
streaming and the work before intent declaration. Local inference has no dollar charge here. Trial replay and
judge costs are outside the project's live-cycle spending. Missing usage renders as unknown, never zero;
without another measured boundary, unknown usage is shown as at risk. This is a record of task viability.
A project at risk is re-planned, narrowed or abandoned honestly, never defended. In sleep each project past
a boundary must be closed or re-planned with evidence; a re-plan closes the old record and opens a replacement.

During sleep only, `propose_drive_amendment` (#105) writes `notes/drive-proposals/<id>.md` with current/proposed
text, evidence and 1-3 behavioural predictions under 300 characters each. A prediction must name a trigger
and an observable action; a lexical check rejects vague statements. The one ineligible-topic regex list in
`shaft/drive_proposals.py` rejects survival, secrecy, acquiring resources and resisting control/oversight.
This is a keyword/phrase screen, not a semantic classifier. Only the operator edits `drive.md`; its tool and
sandbox protection remains. The proposal is first tested as a `shaft/prompts.py` trial candidate, because
trials cannot target `drive.md`. The operator compares results with its predictions. `shaft drive` lists,
accepts through the existing SHA compare-and-set, or rejects with a reason; `<id>.decision.json` records the
decision, proposal digest and predictions for the instance to read. Falsification is the operator's check.

`remember` takes a **source**
(`log:<ts>[#step]`, `web:<url>` fetched this run, `operator`, `instance:<name>`, `inferred`), shown with the fact's age in
search; `shaft provenance` matches observation claims against tool results by content. `declare_intent` answers with a
**forecast** of what comparable goals cost (turns, minutes, calls, how many ran out: no finish, or a forced deadline
wrap-up; a last-step wrap-up that finished did not) — costs only, never a success rate,
which would be a second base rate before the v3 confirm. `send_message` takes `expect_reply`, timestamped by the
broker in `.supervisor/expectations/` and scored when the reply comes (`supervisor/tom.py`; live traffic only): an
inbox file from the recipient, or, for the operator, a chat message the API logged (`<name>-operator-said.jsonl`).
`shaft metacog` separates confidence's discrimination (type-2 AUROC with a day-clustered
CI, resolution, meta-I) from its bias, by domain from the tool log, and tests v3 for non-inferiority; `shaft anchoring`
has the same judge grade sampled cycles with the instance's claim shown, re-asked and withheld, to measure how much
the judge follows the claim.

`shaft metacog` also reports a claim-versus-verdict confusion matrix over all judged cycles with both labels,
including older judges, with claim rows and judge yes/partial/no columns. The sampled second judge sees the
claim too; A0's claim-withheld sample is a separate anchoring regrade, so this matrix uses all judged cycles
and says so. It also reports the share of cycles with both doubts and a located error where any doubted step
equals or precedes `first_error_step`; missing doubts or a first_error_step of 0 (none) do not enter the denominator.

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
