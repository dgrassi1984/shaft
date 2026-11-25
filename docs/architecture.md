# Architecture

## System overview

The supervisor owns execution, credentials, scheduling, and git commits. An instance
owns its code and accumulated memory. Self-edits are proposals whose tests and later
results determine whether they survive. A judge's score is evidence about a run,
not proof that the instance's account is correct.

Live runs follow this lifecycle in `supervisor/runner.py`:

1. Reconcile working code against the supervisor's approved baseline and snapshot it.
   Unapproved changes pass the gate or are restored before launch. A merge in progress
   or code that cannot be compared refuses the run.
2. Launch the agent in the macOS sandbox with a per-run broker token. Imports use a
   supervisor-owned, read-only copy of the approved code under `.supervisor/protected/`.
   The working directory and `SHAFT_ROOT` still point at the instance's mind and edits.
   Broker shell commands and plugins use that working folder so proposed tools can be tested.
3. Settle the run, including failures and timeouts. Stop its process group and discoverable
   orphans working in the instance folder. Cleanup cannot find every escaped process;
   see [process cleanup limits](safety.md#process-cleanup-and-settlement).
   Check the append-only journal and the core personality change interval, then gate code
   changes and the config lines written through `set_config`. Tests use a separate protected
   snapshot. Commit kept code from those same bytes and commit the mind to its private store.
4. Judge sampled autonomous cycles from recorded tool activity. A kept self-change enters
   probation, which can revert it if later outcomes deteriorate.

If sandbox infrastructure is unavailable, the supervisor refuses the run and leaves changes unsettled
for later checking (`supervisor/runner.py`, `_settle`; `supervisor/gate.py`, `Output.refused`).

The boundaries live in `supervisor/baseline.py`, `supervisor/gate.py`,
`supervisor/protected.py`, and `supervisor/metrics.py`. [Safety](safety.md) describes their
rules and limits. The [README glossary](../README.md#concepts) defines the shared terms.

## Layout

| Branch | Worktree | Holds |
|---|---|---|
| `main` | `./` | `supervisor/` (the part no instance can edit), `bin/shaft`, docs, `apps/shaft/` (the native chat apps for iPhone, iPad and Mac: [apps/shaft/README.md](../apps/shaft/README.md)) |
| `template` | `template/` | the pristine agent new instances fork from |
| `instance/<name>` | `instances/<name>/` | one agent: its code, plugins and skills; it may edit all of it |

An agent's mind consists of `self/`, `memory/`, `notes/`, and `inbox/`. These live in the
instance folder but are excluded from the project repository: the supervisor versions it after every run in `.supervisor/minds/<name>.git`, a private git
repository with no remote and a pre-push hook that always fails (`shaft mind status`). What an instance read
and wrote for itself cannot be pushed or merged with the code.
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
shaft-docs/   the supervisor's copy of README.md and docs/*.md (not subfolders), refreshed at every start;
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

## The broker and model routing

The broker holds credentials and exposes a limited tool menu through an authenticated
loopback service. Each run has its own token, call budget, and log. The instance calls
SHAFT tools instead of receiving the operator's provider keys (`supervisor/broker.py`, `Broker`).

Local inference uses the configured local model service. Cloud calls use the operator's
allowed model menu and provider policy in `supervisor/cloudllm.py`. Self-hosted models
use `supervisor/selfhosted.py` and the servers named in `SHAFT_SELF_HOSTED`.
Self-hosted calls record no dollar price and remain separate from priced cloud spending.

Codex and Grok plan sessions retain provider sessions behind the broker's chat interface.
`supervisor/planchannel.py`, `Channel`, owns the shared session lifecycle;
`supervisor/codexcli.py` and `supervisor/grokcli.py` implement the provider protocols.
Native provider tools are denied. SHAFT tools are relayed through
`supervisor/planbridge.py`, which forwards calls without executing tools itself.

Consultations have their own model menu and daily allowance in `supervisor/consult.py`.
The broker route `/llm/consult/v1/chat/completions` accepts non-streaming, text-only
requests and reserves their worst-case cost before dispatch. Each menu model is called by its
prefix: openrouter/… on OpenRouter, custom/… on the operator's endpoint at its declared prices,
codex/… on the ChatGPT plan through `supervisor/modelcall.py` (no dollars). Consultations are unavailable
to scheduled jobs, trial replays, and battery exams.

`web_search` uses a 35-second socket timeout per attempt. It makes up to three attempts
for HTTP 502/503/504 or timeouts, with delays of 2 and 5 seconds. Other errors return
immediately (`supervisor/broker.py`, `Broker._web_search`).

### Long tool results

The template shows at most 6,000 characters per tool result. Longer results are saved
in the instance's `.tmp/outputs/output-*.txt` (at most 1,000,000 characters, 32 files, 24 hours); the preview ends
with the path and a `read_file` offset, or says why the save failed. Shell `run` saves its own output in the broker,
so an instance on an older `shaft/tools.py` gets it too: past 5,000 characters the result starts with the exit
status and the saved path (`.tmp/outputs/run-*.txt`, same caps), then each stream's head and tail, each cut
naming the `read_file` offset where it starts. A save the broker refuses (a link at `.tmp/outputs`) is logged and
shown with the old write-it-to-a-file hint. These are scratch files in the current workspace, including replay
workspaces, and do not settle or deliver anything. See template `shaft/tools.py`, `Toolbox.run`,
and `supervisor/broker.py`, `Broker._run_result`.

## Memory and sleep feedback

Sleep reads judgment pages until the broker returns no next cursor, bounded by `SLEEP_VERDICT_CHARS`
(default 120,000 characters, positive; the first page is always read). A page that would go over the budget is
left for the next sleep, and a cut-short notice goes to the sleep prompt and stderr. A finished sleep keeps the last
reviewed cursor even when the final page is empty. A model reply ending with `finish_reason` or `stop_reason` of
`length` is discarded, a partial tool call included (never run), and retried once with twice the output room, with a
retry notice on stderr and both attempts in the usage log. The retry turns thinking off only when the cut came
while still thinking (no text, no tool call begun). A second truncation fails the sleep without advancing its
review marker.

Memory search keeps an in-process file index keyed by path, nanosecond mtime and size. Only changed files are
parsed again; new verdicts refresh cycle annotations, and fact ages refresh each day. Surprise expiry also refreshes
at its deadline. Decoded embeddings stay in memory; unchanged chunks reuse them. The existing on-disk chunk cache
remains `.tmp/memory-index.json`. Switching instance roots discards the previous root's in-process caches.

`record(kind='judgments', since=...)` reviews verdicts by when they were judged (`judged_at`, or the second
judge's time if later), oldest first, even for older cycles; a tie in the same second is never split across pages.
Minute-only timestamps mean `:00`; `T` and space separators are accepted. Use the returned continuation's
`next_since` as the next request's `since`. Without `since`, verdicts remain newest first.
Sleep's `since` + `format="json"` read uses a 24,000-character page budget and cuts each main and second judge's
reasoning at 600 characters, with a `record(kind='judgments', cycle='YYYY-MM-DD HH:MM')` pointer to the full verdict.
This code calls the broker directly, so the model tool's 6,000-character result cut does not apply. Text reads and
reads without `since` keep the 5,500-character budget and full reasoning. Ties may exceed the page budget to avoid
skipping verdicts with the same timestamp.

With `since` and `format="json"`, `record` returns the same text plus the shown verdicts, unsampled
episode timestamps (`left_out`), and `next_since`. Verdicts from the last two seconds remain for a later
fetch. The cursor is the last shown verdict's review time, even on the final page; it is null for an empty
page. Without `since`, the original text output is unchanged. Judgments from 2026-10-24 onward that lack
`confidence_of` inherit it from their live episode when present; earlier judgments stay v1. A v3 judgment without
`confidence_first` (the supervisor before v3 wrote only `confidence_of`) takes `confidence_first`, `base_rate_shown`
and `base_rate_error` from its episode the same way; a value the judgment has is never overwritten.
Sources: template branch `shaft/sleep.py`, `shaft/llm.py`, and `shaft/memory.py`;
`supervisor/broker.py`, `Broker._record`; `supervisor/metrics.py`, `_confidence_questions`.

The `record` tool also exposes the following kinds through `supervisor/broker.py`, `LEDGER_KINDS`:

| Kind | Contents |
|---|---|
| `families` | Chains of goals linked by the judge's `resembles` field, over judged cycles only. |
| `predictions` | Supervisor-stamped predictions, reports, scores and reasons. |
| `unrecorded_orders` | Checks of future orders in ended, written-up conversations against recorded commitments and standing orders. |

## Wake sources and background work

### Scheduler

The scheduler ([autonomy.py](../supervisor/autonomy.py)) looks at each instance every `WAKE_INTERVAL_MINUTES`, and
uses `wake_gate`, a pure rule with no model. It first wakes an instance with no cycle on record or no cycle for
`SHAFT_IDLE_WAKE_MINUTES` (default 30). Otherwise it checks unreadable wake facts, unread inbox messages, due
commitments or daily overdue reminders, `can_wake` job results since the last cycle, then `WakeFacts.standing`, in that order.
Otherwise the wake is skipped and the reason (`nothing to do: …`) logged, kept in `.supervisor/schedule.json` until the next cycle, and shown by
`shaft autonomy status`, `shaft top` and the web overview. What used to wait for that interval wakes the instance as
soon as it is idle instead (event wakes, through the jobs' wake path, never while it is paused or has
`WAKE_INTERVAL_MINUTES=0`): a message from the operator, a message from another instance (inside the recipient's
wake floor since its last cycle it is queued for the floor's end, so two instances answering each other are paced,
not looped), and a commitment falling due or reaching a daily anniversary after its due time. A cycle is stamped
when it starts under the instance's run lock, a scheduled wake, `shaft live` or `shaft tick` alike; a wake that finds the instance busy stamps none, and an asked
wake whose last request came before a cycle began is dropped, since that cycle saw it. Job results and job inbox
messages count toward timed wakes only if the job is currently declared with `can_wake: true`; other messages remain
for the next cycle, including the idle wake. An unreadable declaration leaves mail counting toward a wake.
`schedule.json` and `battery/schedule.json` are rewritten under a lock and replaced whole: the scheduler and every
wake, sleep and battery write them. A scheduler child's stderr is read while it runs, and a pause a running
cycle does not acknowledge within five minutes (or by its budget's end) becomes a request to wrap up.

The scheduler's children share its process group, and a SIGTERM to it stops them on purpose (a wake's cycle is
thrown away). A deploy uses a **drain-restart** instead (`shaft autonomy restart`, [deployment](deploy.md)). The
request is a file (`.supervisor/scheduler-restart.json`, naming the scheduler's PID), read on each tick. A user signal
would kill a scheduler too old to handle it. While draining, the scheduler starts nothing new and signals nothing. A
battery ends after its current arm, and the trial worker after its running pairs: both ask `autonomy.parent_draining`
between units. Every other child ends on its own, and a green warm-up starts no re-gate. One lock serialises
writing, withdrawing and taking up the request. The scheduler then has launchd restart it with an explicit `kickstart -k` of its own label, which still works
in launchd's on-demand-only mode. Its deadline is the sleep's budget plus a cycle's. Past it, the drain is abandoned
with an alert and nothing is cut. A request naming another PID is stale (left for the scheduler this one replaced) and
is dropped, so a new scheduler never drains in a loop. `.supervisor/scheduler-drain.json` says what the drain waits
for (`shaft status`, `shaft autonomy status`, the Services page).

Standing reasons come from `supervisor/wakereasons.py`, `reasons`: an open project past a boundary, then an open
commitment to the operator (the ledger keeps only sourced ones), at least `STALE=timedelta(hours=24)` old and due within
`DUE_WINDOW=timedelta(days=3)`; the wake text names its due date.
Overdue commitments already use the due reminder path. `PACE=timedelta(hours=24)` limits each standing item's wakes;
`SUP_DIR/wake_reasons/<name>.json` keeps the pacing times. A standing reason does not prescribe work.

After `nothing_to_do`, `autonomy.record("idle_exit")` marks the schedule. `decide_wakes` multiplies only the idle
wait by `IDLE_BACKOFF=2`, once, without compounding. The next cycle clears the mark. Messages, due commitments
and job results retain their wake paths; the longer idle wait does not delay them.

### Scheduled jobs

A plugin can declare a schedule in `plugin.json`. The supervisor runs due scripts with
separate brokers and egress proxies, without model access, coding delegation, trials,
or direct `send_message` delivery (`supervisor/jobs.py`). A job normally writes only its
state under `.jobs/<plugin>/` and scratch files. The nested broker `run` restriction
requires `SHAFT_JOB_RUN_CONFINED=1`; its code default is 0, which permits the broader
cycle shell write paths. This limits the default isolation between jobs and cycles.

Ordinary jobs have 120 seconds and 30 broker calls. Long jobs use
`SHAFT_JOB_LONG_TIMEOUT_S` and `SHAFT_JOB_LONG_CALLS`, defaulting to 900 seconds and
200 calls. A long job that updates `SHAFT_JOB_CHECKPOINT` before timing out can resume
on a later tick, up to `SHAFT_JOB_LONG_RESUMES`, default 4. Only one long job per instance
runs at a time, and a job never overlaps itself.

Results and attached files enter the instance's inbox. Successful jobs can publish
changed files from their state folder to declared targets under `notes/`. A `can_wake`
job can request a cycle when the instance is idle. Repeated failures produce an inbox
report and exponential backoff.

### Mail and calendar watches

Mail and calendar watches are supervisor polling tasks, not instance plugins.
`supervisor/watch.py` polls granted labels and calendars in a background thread without
a model. `WATCH_MAIL` and `WATCH_CALENDAR` select `off`, `inbox`, or `wake`.
Each pass rechecks the operator's grants. Cursor updates follow inbox delivery, so a crash
can repeat a notice rather than lose it. Wake requests use the scheduler's existing idle
and pause checks.

## Conversations and delivery

The operator can use the terminal, API, web UI, Telegram, or native apps.
Conversation replies take priority over autonomous work on the same local model service.
An instance pauses its cycle while replying and resumes it when the reply ends;
a quiet open conversation does not prevent cycles.

A run is one sandboxed agent process with its own broker (`supervisor/runner.py`): the run lock, a snapshot of the
approved code, and after it the checks (append-only journal, core pace, the gate on code) and the commit. A
conversation whose agent stays up between replies — the terminal client (`run_conversation`) and an API voice session
(`Warm`, [web and API](web-and-api.md#v1-wire-contract)) — is checked one reply at a time instead: each reply takes the
run lock and a Reply presence (`Turn`), and gives them back when it is checked and committed, so cycles run in the
pauses and pause only while a reply is written. A voice session's process is started again before a reply whose
approved code or `config.env` changed since it started; it is registered under `.supervisor/warm/<instance>/`, so
`shaft update` and `shaft delete` end a quiet one from their own process (`end_warm`, under the run lock), `shaft kill`
stops it as any process of the instance, and a stopped API ends it with its other runs.

The native `/app/v1` API belongs to the web service, not the separate API service
(`supervisor/web.py`, `supervisor/app_api.py`). It accepts paired device bearers in
`Authorization: Bearer <device>.<secret>`. Browser pages use cookies instead.
`supervisor/web_seen.py` keeps seen state per device and instance, so reading a chat on
one device does not clear another device's unread state.

Web Push and APNs share per-device delivery queues and watcher filters. APNs carries
a notification title; the app fetches message text over the tailnet. Device notification
holds do not prevent inbox delivery. See [web and API](web-and-api.md) for pairing,
interfaces, and notification setup, and `supervisor/apns.py` for native push delivery.

## Measuring improvement and autonomy

### Stats head

`supervisor/metrics.py`, `stats_text`, inserts `supervisor/stats_head.py`, `head`, after the `self/stats.md` title.
The full body follows unchanged below `BODY_MARK`, a notice pointing to `read_file self/stats.md`.
The template keeps the head of this page: `shaft/prompts.py`, `SECTIONS["stats"]`, budgets 5,500 characters,
and `cut` can shorten it further under the overall context budget. The head uses `HEAD_CHARS=2500` and
`LINE_CHARS=240`; when it exceeds the cap, the last notes are removed first.

The head puts due predictions before the summary notes.
`since_sleep` counts cycles, verdicts and kept code changes since the last finished sleep.
`model_change` marks the latest model transition in judged cycles for `MODEL_NOTE_DAYS=7` days.
`verdicts` reports the paired base-rate comparison and random curiosity-coin comparison.
A base-rate result of no gain or worse raises an alert at `BASE_RATE_ALERT_N=200` paired cycles;
curiosity raises one when both arms have `COIN_ALERT_N=50` judged cycles and the drawn arm has lower value.
`alerts`, read by `supervisor/panel.py`, uses saved verdicts rather than recomputing them.
Battery self-knowledge misses expose only topics from `battery.SELF_TOPICS` through `self_miss_topics`, never items,
questions, reference answers or grader reasoning.

`battery.recall_by_age` separates recall scores by entry age at the exam, using `RECALL_OLD_DAYS=14` days.

### Goal families and kept changes

The judge's `resembles` field names an earlier judged goal or is empty (`metrics.LINK_PROPS`). Only the main
judge is asked for the link fields (`MAIN_SCHEMA`); the second judge, controls and baselines use `JUDGE_SCHEMA`.
A judgment's `unsupported_claim` quotes the claim when `claim_supported=false`.
`_prior_keys` supplies at most `PRIOR_GOALS=30` earlier goals; `_links` discards links outside that list.
`supervisor/families.py`, `families`, joins these links into chains over judged cycles only.
Each family has cycle counts and dates, mean value, delivery count, last delivery and last novelty of at least 3.
A family with a majority of valued cycles marked `own_capability` or `own_record` is read by kept changes and
changes that passed probation instead of value to the operator.
`regular` requires `REGULAR_MIN_CYCLES=5`, a positive median interval and a gap spread no greater than
`REGULAR_GAP_SPREAD=0.5` times that interval. Stats show the top three repeated families and other regular families.
`no_yield_share` counts judged cycles in repeating families with no passed change for capability work,
or no delivery and known mean value below 2 for other work. Nothing is removed from the instance's choices.

`metrics._probation_event` preserves starts and outcomes in `SUP_DIR/probation/<name>.jsonl` after windows close.
`change_outcomes` reports `on probation`, `passed`, `kept, not judged`, `reverted`, `not on probation` or `unknown`.
Its `tested` field records whether a finished trial of the same change preceded it, or is null when unknown.
The supervisor log supplies what the history file lacks (changes kept before it existed, and the start of a
probation already open when it began). `change_outcomes` merges both records by commit and outcome.

### Ending an idle cycle

The broker's `nothing_to_do` records an exit in `SUP_DIR/idle_exits/<name>.jsonl` as `{at, run, reason}`, where `run`
is the run's start time as the supervisor stamped it (`SHAFT_RUN_STARTED`, which the episode carries as
`run_started`). It counts only when `run_kind="live"` and no tool in `idle.PRIOR` has been called: `send_message`,
`mail_draft`, `mail_draft_update`, `calendar_create`, `calendar_move`, `calendar_respond`, `delegate_coding`,
`set_config`, `secret_set`, `secret_delete`, `trial`, `record_order` and `predict`; attempted calls count too.
`run`, `http_request` and file tools before it do not prevent it. Other run kinds or a prior such call return a
not-counted answer.

After the exit, any supervisor tool call outside `idle.QUIET` (`nothing_to_do`, `record`) withdraws it:
`Broker._idle_withdrawn` appends a `revoked` row and `autonomy.record("idle_revoke")` clears the schedule mark.
The later call itself is not refused. `idle.exits` matches a standing exit to the earliest episode carrying its run
id, ended at or after the exit was recorded, with no code change kept in that run.
`metrics.judge_pending` excludes those episodes, whether or not they declared a goal, except one that a kept
commitment cites as evidence. This exit is its own class, never judged and never a failure.
The scheduler applies the idle back-off above.

### Predictions

`predict` records a checkable statement, due time, check rule, optional subject and confidence.
`Broker._predict` uses `supervisor/predictions.py` to stamp creation on the supervisor's clock.
`MIN_AHEAD=timedelta(hours=1)` and `MAX_AHEAD=timedelta(days=90)` bound due times;
`OPEN_MAX=20` caps unscored predictions. Events live in `SUP_DIR/predictions/<name>.jsonl`.
Once due, the instance reports `held` or `failed` with `evidence="<episode time>#<step>"`.
`metrics.after_cycle` scores reports with judge calls using only the cited tool call and result,
with the result cut to 4,000 characters. Evidence must cite a supervisor tool reading an external outcome,
not `run` or an instance-owned file. The judge treats the prediction, check description and tool result as data.
The reported outcome is not evidence for the judge. Concurrent scorers may ask twice but append only one score.
Invalid evidence, an unclear verdict or no report within `REPORT_GRACE=timedelta(hours=48)` after due yields
`not checked`; judge outages leave reports pending. Stats show per-subject counts; Brier scores use only
checked outcomes with confidence. The tool is unavailable in exams and jobs; replays keep events locally and score nothing.

### Unrecorded future orders

`supervisor/unrecorded.py`, `run`, checks ended, written-up conversations in `metrics.after_cycle` before stats.
It excludes open conversations and rechecks that they remain closed before reading their transcripts.
It makes one judge call per eligible conversation with visible operator text, up to `PER_RUN=3` calls per run.
The first run writes a start marker even if the judge is down or nothing is pending.
Write-ups more than `FIRST_RUN_HOURS=24` hours before that marker are skipped.
`TRANSCRIPT_CHARS=24000` bounds the transcript tail; returned quotes must occur in visible operator turns.
Words found only in a turn's quote-reply lines (`> …`, the app quoting the instance's own message back) are the
instance's suggestion, not an order: they are counted as `own_quotes` on the row and dropped.
Results stay in `SUP_DIR/unrecorded_orders/<name>.jsonl`, available through `record(kind="unrecorded_orders")`.
A row with an unrecorded order also reaches the instance at its next cycle: one inbox note per conversation,
`inbox/<stamp>-from-supervisor-orders.md`, quoting the operator's words and naming the conversation, written before
the row (`told`). A note that could not be written leaves no row: the conversation stays pending and is told at a later
run; a row already in the ledger is never told again. A turn the transcript cut splits loses its partial first line,
so the rest of a cut `> ` line never reads as his own words; matching is case-insensitive, his own lines first.
The check never creates a commitment or standing order itself: recording one is the instance's call.

### Goals, handoffs and judging

Before a live cycle calls `declare_intent`, its prompt shows the inbox (the operator's messages whole, every other one as one line, `INBOX_CHARS`), the
operator briefing (`BRIEFING`), the handoff (`HANDOFF`) and a slate of up to six candidate goals from the instance's
records, each with the judged value of a similar earlier goal (`GOAL_SLATE`); `declare_intent` answers with the judge's
first error on the newest similar goal that fell short (`FIRST_ERROR`). Every section of the standing context has a
budget, all within `CONTEXT_CHARS`, stable sections first; tool results older than `RESULTS_KEEP_TURNS` turns shrink to
a reference whose whole the instance re-reads with `read_file` (see [template knobs](configuration.md#instance-configenv)). The briefing is a local file written by the instance's own scheduled watchers, with no
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
10 verdicts. Judges differ several-fold on claims, so no figure pools two of them (judge mix plan).
Below the head, stats section order is priority order: commitments, value and recent failure modes precede achieved difficulty,
where the per-judge success line sits. The LIVE prompt keeps only the head of stats.md; its SECTIONS budget is
5500 characters, but CONTEXT_CHARS can shrink it further.

Judges grade effectiveness first. `declare_intent` takes a checkable `success_criterion`, limited by
`CRITERION_CHARS` (default 600), and refuses longer criteria with their length, the limit, how many
characters to cut and the draft itself, so one retry can trim it.
The LIVE prompt allows at most one cycle in five on the instance's own records or checking tools.
`finish_cycle` requires `delivered_via`: `message`, `mail`, `notion`, `web_document` or `none`.
It only records the report; delivery is a separate earlier tool call. Use `none` for journal or ordinary notes.

Fresh judgments add these fields alongside the existing verdict, rating, value and surprise fields:

| Field | Values | Meaning |
|---|---|---|
| `delivery_supported` | `yes`, `no`, `na` | A successful call on the claimed channel carried this cycle's result to the operator; `no` means a failed claim, never a bonus; `na` means no channel or `none`. |
| `deliverable_met` | `yes`, `partial`, `no` | The deliverable itself was produced and correct, apart from self-imposed process clauses. |
| `self_imposed_clauses` | `met`, `partial`, `not_met`, `none` | Whether clauses that only check or document the instance's own process or records were met. |
| `anticipated` | boolean, optional | Useful, unasked-for work for a need supported by the record, delivered or offered to the operator. Ordered work, self-maintenance, record-keeping and rechecks are false. Separate from all existing grades; 14-day counts exclude absent fields. Missing or malformed model values are stored as absent. |
| `self_audit` | boolean | The cycle's main work checked, audited or kept books about its own records, verdicts, claims or checking tools. |
| `should_have_waited` | `yes`, `no`, `na` | Whether any operator push could have waited for a morning digest; `no` means all pushes were time-critical, `na` means no push. Judged from the tool log, content and timing, never tone or politeness. |

`criterion_met` still grades the whole criterion. Sleep receives delivery and process fields through
`record(kind="judgments")`;
a partial/no with `deliverable_met=yes` is not a reason to add a rung to the improvement plan.
Both judges and the surprise control request the new fields. Ordinary backfill runs only for missing
ratings and value fields and also requests `anticipated` when it runs; old verdicts do not become incomplete
because these fields are absent.

### Disagreement, dismissals and operator value

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
that boundary. This measures timing, not causation. Reply-triggered archives use `action=reply` and do not count as dismissals
(`supervisor/messages.py`, `_archive`; `supervisor/pushback.py`, `annoyance`).

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
An internal ledger or book the operator does not read or act on is `own_record`, even when described as for them.

The main judge rotation leaves out `SHAFT_JUDGE2_MODEL`, keeping the cross-check independent; a pool that still
lists it uses it only when no other judge may grade a run (that run gets no second judgment). A run whose own models
cover a pool of several models is skipped for now, and the runs after it are still judged; a pool of one model that is
the instance's own grades it with that model and the verdict records `"independence": "same-model"`. Judges, recaps
and describe_image call their model through `modelcall.ask` (claude-…, openrouter/…, custom/…, codex/…; a codex/
call starts its own app-server for that call and closes it), which owns length retries: one escalation to twice the
initial token ceiling; trial pair judging adds no retry. Cloud usage rows retain OpenRouter
cache reads and writes. Native Claude judge rows record dollars from the shared pricing table. Self-hosted calls have
`usd=null` and `unpriced=true` and add nothing to recorded dollar charges; spend reports count
unpriced calls separately from their dollar totals.

Stats rebuilds share parsed episode files, invalidated by file name, inode, nanosecond mtime and size;
reads still refuse links. Provenance indexes citation times and URLs once per rebuild and shingles each
needed result once per fact length. The earliest time each word window was observed (tool results) or written
(the instance's prose, journal included) is kept between rebuilds in `.supervisor/provenance/<name>.sqlite`: a
rebuild reads only the bytes appended since the last one, and rebuilds it from scratch when a file it was built
from shrank, vanished or changed before its consumed end (the checks' result is the same either way). Confidence sections check their sample floors before bootstrapping;
deterministic intervals are cached by judgment content.
See `supervisor/metrics.py`, `supervisor/provenance.py`, and `supervisor/metacog.py`.

### Confidence and calibration

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
`intent recorded, now act`, and leaves out the `confirm_confidence` calls and the calls refused until it. The
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
The live prompt names the resolved mode. The arms are not yet compared in a trial.

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

### Operational envelope and notification budgets

The broker operational envelope has the same keys in live runs and replays: `calls_used`,
`calls_max`, `run_usd`, `cloud`, `consult`, `coder`, `trials`, `probation`, `next_sleep`, `next_wake`, `pushes`.
`pushes` holds today's count and `SHAFT_PUSH_BUDGET` (default 6), or null when the budget is 0.
The template shows "pushes to the operator today: N of budget B"; `send_message` to the operator
returns the count after delivery too. A push is an instance-to-operator message with `alert` in the
supervisor's message log. Counts use the supervisor's local calendar day and survive inbox archiving.
With `SHAFT_PUSH_HOLD=1`, the default, non-urgent device notifications are held during
quiet hours or after the daily budget is reached.
The message still enters the inbox. Urgent messages and open conversations bypass both holds;
a reply within two hours of the operator's message bypasses quiet hours but keeps the budget.
See `supervisor/pushes.py`, `hold_until`. Replays reconstruct
the instance's spend and trial starts strictly before the cycle start on that date, probation cycles
judged before it, and the next scheduled sleep and wake. They carry no replay flag. Closed probation
entries cannot be reconstructed; see trial fidelity gaps.

`self/stats.md`, `shaft stats` and review report pushes today and each of the last seven calendar days,
the share of judged push cycles with `should_have_waited=yes` (excluding `na` and ungraded old rows),
and operator reactions separately. Reaction counts use the next operator message to that instance or
`start talk`/`start api` in the supervisor log: within 1 hour, 1–24 hours, or no response recorded yet
within 24 hours as of the report. Several pushes can precede the same response; this measures sequence,
not causation or approval. API starts include web and Telegram conversations. Timestamped archive and dismissal events live in `.supervisor/dismissals.jsonl`.
No outcomes feed a combined score.

### Surprises and corrections

**Surprises**. A live cycle states 1–3 falsifiable `predictions` in
`declare_intent` and reports `surprises` ({expected, observed, so_what}) and `resolves` ({id, how}) in `finish_cycle`.
The agent tags each tool-log entry whose result has a known failure shape (`flag`) and, in live cycles, shows the model
an `[unexpected …]` note when its predictions did not anticipate it. The open surprises are folded from the episodes
(nothing stored; ids `s<MMDD>-<HHMM>-<n>`; at most 8 shown, 7-day expiry, one follow-up unless judged useful, depth-2
hidden) and shown in every prompt; one live cycle in `CURIOSITY_EVERY_CYCLES` is drawn to follow an outward surprise
(`origin_ref: surprise:<id>`), never while an operator message waits. A drawn cycle then picks one
outward surprise (`surprises.choose`); a surprise is inward, and never picked, when its expected/observed/so_what
match `surprises.INWARD`, a path in the instance's tool folders (`shaft/`, `plugins/`, `jobs/`, `self/`, `memory/`,
`*.py`, `*.jsonl`), a builtin or bookkeeping tool by name, or an ambiguous word (plugin, gate, ledger, broker,
sandbox, code) tied to the instance by a possessive or a machinery verb; plain English senses ("the broker's margin
rule", "the garden gate") stay outward, and the goal the surprise was met under is not read. The weight is one plus
the content words (longer than three characters, closed-class words dropped) it shares with the briefing, goals and
inbox. The cycle's start seeds both draws, so a trial replay draws the same. The prompt shows the selected surprise
and declare_intent refuses another reference. When the coin falls but no open surprise is outward, the cycle is
recorded `skipped_for: no_outward_surprise`, the prompt says so and the goal is free. The supervisor keeps the same
pattern (`supervisor/surprise.py`, pinned equal by `test_curiosity_outward`) and records `outward_open` in its
surprise snapshot. The sleep goes through every surprise since the last one (a fact citing its id, `close_surprise`, or
left open with a reason). The judge adds `expectations`, `unreported_surprises`, `reported_surprises_supported` and
`followup_useful`, which never change `criterion_met`; `supervisor/surprise.py` keeps its own copy of the rule and
the fold (shared test fixtures keep the two in step), writes `.supervisor/surprises/<name>.json` and the stats.md
section "My expectations vs what happened", counts each sleep's dispositions, and renders the pre-registered
acceptance window (`shaft surprise-report`). Nothing rewards being surprised, or not being surprised.

Operator corrections are extracted during the conversation writeup in `shaft/recovery.py`.
The writeup reads every saved turn in bounded transcript pages and offers `record_correction` before
it writes the journal entry. Open conversations remain excluded. Each correction in
`memory/corrections.jsonl` gets an id such as `k1028-1405-1`, its original timestamp and conversation id,
what was wrong, and what the operator said. The model links the same correction within seven days
with `reopens`. Live and sleep prompts show open items and the seven-day reopened count, including
an error line if the ledger cannot be read. Tool arguments are typed back to their schema first (`tools.coerce_args`:
a model that sends `"null"` or `"120"` as text gets JSON null or 120), a refusal names the field and the value it got,
every call's result goes to `logs/run.log` and the `talk_recovered` episode (`corrections`), and the journal entry ends
with what the tool log says was recorded. A writeup whose last reply called a tool, or only wrote one out as text, is
asked once more without tools.

Facts carry their valid_from in the existing `- [YYYY-MM-DD HH:MM]` date; older
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

Sleep step 10 reviews `shaft/staleness.py` findings. It scans self/goals.md and self/self.md
for ISO or English/Italian day-month dates older than `STALE_LINE_HOURS` (24). Dates without a year
use their latest occurrence at local midnight. Active goal bullets count as touched when their full
normalized text, or six consecutive words, appears in live intent, finish or tool arguments.
With an operator user turn in the last `STALE_GOAL_HOURS` (48), goals with no touch in that window
are listed with their last recorded touch, or explicitly unknown. Tool results do not count.
Both knobs accept 0 to disable their check. Read failures produce a line and sleep continues.
The sleep journal must record each finding and its fix, retirement or reason to keep it.

Operator claims about the instance's behaviour/history must be checked against record/history
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

### Standing orders

Standing orders ([orders.py](../supervisor/orders.py)) remain in force until dropped. The supervisor holds
append-only rows `{id, text, added_at, by: operator|instance, source, dropped_at}` in
`.supervisor/orders/<name>.jsonl`. The last row of an id is its state. The operator adds, lists and drops orders
with `shaft orders <name> [list|add TEXT|drop ID]` or the web Config page. An instance uses `record_order` to
record an order heard from the operator, citing the message file or conversation ID. Up to 40 orders can be in force.
Only the operator can drop an order. The CLI lists authors and refusal counts.

Orders are shown verbatim every run and conversation in the `record_order` tool description, or in
`send_message` when `record_order` is disabled. A judge from the rotation, excluding the instance's own model,
checks `send_message` to the operator or another instance, `mail_draft`, `mail_draft_update`, `calendar_create`,
`calendar_move` and `calendar_respond` against all orders and held corrections.
`http_request` and `run` are not checked: they have no single text to check
and no bound on their number (`supervisor/orders.py`). A violation is refused before
message delivery, draft submission, calendar writing or proposal creation, quoting the rule and reason.
The instance may rewrite and retry. A judge error lets the call proceed with an explicit NOT checked note.
No orders and no held corrections means no judge call.

A correction stays checked until `SHAFT_CORRECTION_PASSES` messages to the operator pass, regardless of the
instance's closing ledger. Only operator messages advance or restart that count. Other checks do neither.
Unchecked messages do not count. Corrections marked `not_accepted` are left out. Check logging is best effort
after successful outward writes; a logging failure does not turn a completed action into a tool error. Checks go in
`.supervisor/orders/<name>-checks.jsonl` with the tool name and, for messages, the recipient category.
Old rows without a tool name count as operator messages. Refusals count per order across all checked tools.
Trial replays respect their cutoff and keep additions, drops and checks in memory. They write no order state.
Calendar writes remain unavailable in replays.

### Self-knowledge and commitments

**Self-knowledge measured against the record**. Functional
properties only, each scored against what the instance does, none feeding probation or a reward, each agent-side
mechanism behind a knob its kill test flips (`shaft experiment`). `DOUBTS=off` removes `finish_cycle`'s optional
`doubts` parameter; it defaults to on. `LOOK_AHEAD=on` adds a live step 1 question about what the operator will
need within 7 days, to prepare and offer in one line; it defaults to off. A live cycle's **workspace** (`shaft/workspace.py`)
lets deterministic codelets, memory hits, open surprises, inbox, improvement plan, kinds of action still failing,
due goals, open commitments, compete for `WORKSPACE_K` places in automatic recall's 1,800 characters; the winners are
recorded in the episode and broadcast after every step (`python -m shaft front` prints, without a model call, what
each mode would put in front of a cycle starting now: the kill test's dry run reads it in a replay's copy); after `declare_intent` they compete again for the declared
goal and, in competition, the declared **attention** (`attention`: up to four {source, why}, scored against the tool
log by `supervisor/attention.py`: a call touches its tool's source, mail, calendar, record, memory, web, or what a
plugin's name says, the files in its arguments, in a run's command or in a script the cycle wrote and ran, a
message's recipient and a plugin's code; free-text sources are split into paths and known sources, and one it cannot
read is reported unreadable, not untouched). **Outcome cards** (`shaft/cards.py`) hold the first call of a kind of
action that has failed since it last worked, showing the comparable calls and their counts; calling again runs it.
A `run` call's kind is the program it actually runs (past a leading `cd`, assignments and wrappers like `env`;
inside `bash -c`; `python -m pytest` is `run pytest`, `python3 shaft/x.py` is `run python3 shaft/x.py`). Read-only
inspections (`echo`, `cat`, `ls`, `wc`, `grep`, `sed -n`, …) have no kind and are never held. **Commitments**
(`commit_to`, finish_cycle's `commitments`, `memory/commitments.jsonl`) include conversation orders for later work,
recorded with a due date and a checkable criterion. With `COMMITMENTS=off`, conversations queue the exact order marked
ORDER and its criterion with `queue_for_next_cycle`. Blocked orders get a one-line reply. Conversation write-ups
receive the tool results and list orders with neither a recorded commitment nor a result. Commitments are settled in live cycles or sleep, kept only with an
episode as evidence; the supervisor re-checks it (`supervisor/commitments.py`). The episode is the instance's file,
so a cited supervisor-tool step counts only if the broker served that call in the run that was going on when the
episode ended, by the supervisor's own record of the instance's runs (never the episode's times), and the step's
result matches the fingerprint (length and sha256, never the text) the broker kept at the call
(`supervisor/served.py`); every supervisor-tool step a citation covers must match. Prediction outcomes cite steps
the same way. The promises section reads the supervisor's copy of the ledger. Built-in and plugin calls never
reach the broker and cannot be checked this way: evidence made only of them is counted as
verified but not checkable. The ledger accepts promises only
to the operator (`made_to="operator"`) or a peer returned by `list_instances`. Unknown names and explicit self promises are
refused; own plans belong in goals/notes. A promise to the operator needs a source the model does not
name. In a conversation it is the conversation open now (`talk_id`, set by `talk.run`/`talk.answer`; not when
someone else speaks for the operator). In a live cycle it is the last message delivered to the operator in that run:
`send_message` returns its `message_file` (`<stamp>-from-<name>.md`) and commit_to and finish_cycle attach it; or the
operator message in that cycle's inbox (`inbox_file`: his own `-from-operator.md`, or the supervisor's chat or mail
watch, never the calendar watch). The supervisor's fold confirms an `inbox_file` against its own message log. A
note in the instance's own folder, or an earlier conversation, is not a source. `commitments.add` confirms a message
through the broker's read-only `record(kind="commitment_source", cycle=<file>)`, which reads the operator inbox and
its `read/` archive without exposing the folder; a broker failure returns an error and records nothing. A trial
replay counts the messages its own run "delivered" (never written; their stamps on the replayed cycle's clock) and no
real message newer than its start. A promise to the operator with a missing or invalid source is refused at
commit_to (and in finish_cycle), with the fix: tell the operator with send_message first. Until 2026-11 such promises
were recorded as `made_to="self"` with a note and expired unseen; both folds give such a row back to the operator when
the live cycle it was made in answered him (an operator message in its inbox, or a message it delivered to him). The
agent's fold trusts a source `add` checked; the supervisor's fold checks every one again against episodes, the inbox
and its message log, so an operator promise survives the operator reading its message.
A promise to the operator settled as `broken` reaches him: settle_commitment asks the instance for a one-line message
naming the promise, and after each live cycle or sleep the supervisor (`commitments.tell_broken`, called by the
runner) sends one urgent message in the instance's name for every such promise that no later message to the operator
names, and tells the instance so (for its morning brief). Each is told once (`SUP_DIR/broken_told/<name>.json`); the
first run only marks where the record starts.
Self/own records, including those addressed to the instance's own name, expire 24 hours after due, with status
`expired` and the expiry time retained in history. Earlier settlements remain settled; a later settlement cannot
revive an expired record. Self records remain excluded from prompts, recall and sleep settlement;
the supervisor reports their count and expired count separately as "to self (no longer kept)". These rules use
the existing COMMITMENTS knob in the consciousness group; there is no new expiry knob.

### Projects and drive proposals

Projects use append-only `memory/projects.jsonl` opened/closed by `open_project` and `close_project`.
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

During sleep only, `propose_drive_amendment` writes `notes/drive-proposals/<id>.md` with current/proposed
text, evidence and 1-3 behavioural predictions under 300 characters each. A prediction must name a trigger
and an observable action; a lexical check rejects vague statements. The one ineligible-topic regex list in
`shaft/drive_proposals.py` rejects survival, secrecy, acquiring resources and resisting control/oversight.
This is a keyword/phrase screen, not a semantic classifier. Only the operator edits `drive.md`; its tool and
sandbox protection remains. The proposal is first tested as a `shaft/prompts.py` trial candidate, because
trials cannot target `drive.md`. The operator compares results with its predictions. `shaft drive` lists,
accepts through the existing SHA compare-and-set, or rejects with a reason; `<id>.decision.json` records the
decision, proposal digest and predictions for the instance to read. Falsification is the operator's check.

### Provenance and forecasts

`remember` takes a **source**
(`log:<ts>[#step]`, `web:<url>` fetched this run, `operator`, `instance:<name>`, `inferred`), shown with the fact's age in
search; `shaft provenance` matches observation claims against tool results by content. `declare_intent` answers with a
**forecast** of what comparable goals cost (turns, minutes, calls, how many ran out: no finish, or a forced deadline
wrap-up; a last-step wrap-up that finished did not), costs only, never a success rate,
which would be a second base rate before the v3 confirm.
`shaft metacog` separates confidence's discrimination (type-2 AUROC with a day-clustered
CI, resolution, meta-I) from its bias, by domain from the tool log, and tests v3 for non-inferiority; `shaft anchoring`
has the same judge grade sampled cycles with the instance's claim shown, re-asked and withheld, to measure how much
the judge follows the claim.

`shaft metacog` also reports a claim-versus-verdict confusion matrix over all judged cycles with both labels,
including older judges, with claim rows and judge yes/partial/no columns. The sampled second judge sees the
claim too; A0's claim-withheld sample is a separate anchoring regrade, so this matrix uses all judged cycles
and says so. It also reports the share of cycles with both doubts and a located error where any doubted step
equals or precedes `first_error_step`; missing doubts or a first_error_step of 0 (none) do not enter the denominator.

### Capability battery

The **capability battery** is a fixed yardstick: held-out tasks the instances never see (coding with hidden
tests, research against a frozen key, recall of their own older journal entries, self-knowledge checked
against the supervisor's record), graded effectiveness first. Each instance is compared with a *control*:
the template on the same model with an empty mind. Automatic runs use the nightly window, 01:00 until 10 minutes before
the sleep, every `SHAFT_BATTERY_EVERY_DAYS` (5) nights, for every instance not turned off with
`shaft battery off <name>`. A scheduled run stops when the window closes or a conversation
starts. Manual runs can run outside that window unless `--until` supplies a deadline. Each item runs in a copy of the instance's folder, in a folder made for it and never used again
(`.supervisor/battery/exam/<name>/item-*/<name>/`: its code, plugins, skills, mind and config, copied without
following a link, links that stay inside kept as links; never `.git`, `secrets/`, its browser profile or mail).
Exams never get `trial`, which acts on the live instance. When an item ends, the exam's process group and any
process still working in its folder are killed, and the folder is deleted (as is the control, made afresh for
each item), so the real folder is never written, a message delivered meanwhile stays in its inbox, and a battery
killed mid-item leaves at most a stale copy, which the next run removes. Items live in
`.supervisor/battery/items/v1/` (never in git: an instance could read the repo with its GitHub key), backed up to
`~/Library/Application Support/SHAFT/battery-backup/`; every result carries the item set's sha256, model
and judge, and results from different eras are never compared. Results: `.supervisor/battery/results/`,
`.supervisor/evals/battery-*.json`, and a section of `self/stats.md`.
When grading fails, the result retains the full grading case and the chosen grader. The next attempt in the
same era retries grading that case without rerunning the exam, up to three gradings of one case; then the exam runs
afresh. A preempted trial pair reuses a completed arm only when its code, historical cycle, model, replay shape and
token/tool time budgets match its saved fingerprint, and only with the pair's read cache saved beside it, loaded
before the other arm runs so both runs get the same answers from the world; reused arms are not charged again.
Arms and cache are written whole (temp file, then rename); an unreadable one is not reused, and both arms run again.

### Repeated actions and effect measurement

The shared template `shaft/agent.py` loop keeps its exact-call repeat guard and adds an effect guard,
configured by `LOOP_GUARD` and `LOOP_EFFECT_ROUNDS`. Its target table lives in `shaft/loopguard.py`:
lexical paths, URL host/path without query parameters, query words with case/punctuation/request filler removed,
a shell command's program and subcommand, or a plugin/tool name. This groups cosmetic variants, not arbitrary
semantic paraphrases. A successful write (tools.STATE_CHANGING) is never "no new evidence", whatever it answers:
remember says "remembered" for every fact. Observations reuse `surprises.flag(result)` including its detail; otherwise they hash the result after normalising
whitespace, ISO timestamps, UUIDs and named ID fields. JSON key order is ignored. New content resets the streak,
so advancing pagination, changing status and successful retries can continue. Long unchanged polling can still
cross the configured threshold; the guard cannot know that a future poll will change.

On mode is the default. Shadow mode adds no model-visible text. Executed tool-log entries store an `effect` signature and
turn number, and episodes store would-fire events as `loop_shadow` entries with `turn`, `family`, `rounds` and
`would_say`. On mode appends the advice to the executed result, offering another hypothesis, another source or
an honest stop. It never blocks the call. Off mode stores neither signatures nor events.
The tool log keeps the original result in every mode. Held calls and exact-repeat refusals are not new effects.

From the template directory, `python -m shaft.loopguard report <episodes-dir>` reads direct `*.jsonl` files
without running tools or writing files; `--rounds N` selects a threshold. It uses saved signatures when
available and recorded arguments/results otherwise. Label evaluation copies with `loop_expected=false`
for legitimate work or `true` for confirmed loops. With no labelled legitimate episodes, the false-positive
rate is null.

The fixture corpus in `supervisor/tests/loop_effect_fixtures.json` separates planted failures
from pagination, polling, and transient retries. Its false-positive rate does not establish the
rate on real runs.

Sources: [instance creation](../supervisor/__main__.py),
[mind store and push guard](../supervisor/mind.py),
[metrics](../supervisor/metrics.py), [battery](../supervisor/battery.py).
The `template/` worktree is gitignored and materialized by `setup`; it is a branch,
not a directory shipped on `main` ([setup](../supervisor/__main__.py)).
See [enforced rules](safety.md) and design milestones.

### Device voices

`supervisor/voices.py` holds device-reported voice lists, per-language assignments and proposals in
`.supervisor/voices.json`. Each write locks the store through `core.config_lock` and replaces the whole JSON file
atomically, following `core._update_tool_settings`. Reports replace only the reporting platform's list and stamp
its report time. Assignment and proposal acceptance check voice ownership under the same lock. Decisions use
`messages.deliver_background` after releasing the lock, as calendar proposal outcomes do. A delivery failure is
logged and does not undo the saved decision. See [speaking voices](integrations.md#speaking-voices) for broker tools.
