# SHAFT — Plan: the supervisor findings of the 10-31 thelos review (S1–S10)

Status: **requested 2026-10-31.** Source: (a private review record, not published).
Design decisions recorded at the time:

- **S1:** hand-off file — keep the job write limit; a job hands whole files to the woken cycle and may publish to
  notes/ paths its manifest declares; the `run` bypass inside jobs closes only once that route exists.
- **S4:** build the enforced push hold now; the outcome-based score and the hidden grade are a plan (§3) for sign-off.
- **S6 and S10:** build both, behind knobs.

Branches: `claude/thelos-review-2` (supervisor, docs, tests) and `claude/thelos-review-2-template` (the agent).
Lanes work on their own branches cut from these and are merged here by Claude after review.

## 1. Milestones

### M1 S1 + S6 + S8(run) — jobs deliver, publish and may run long (supervisor) — lane *jobs*
- **Hand-off.** A job's JSON output may carry `"files": ["<path in .jobs/<plugin>/>", …]` (≤ 5 files, each ≤ 256 KB,
  only inside its own state folder, no symlinks out). The supervisor copies each into the instance's inbox beside the
  message (`inbox/<stamp>-from-job-<plugin>.files/<name>`) and the message lists them with their sizes; the woken
  cycle reads them whole. A refused file is named in the message with why.
- **Cut output points at the rest.** When a job's text exceeds `MAX_OUTPUT`, the whole text is kept as a file beside
  the inbox message and the cut marker names that file (`[… cut at 4000 of N characters; the whole text: inbox/…]`).
- **Publish.** A manifest's schedule may declare `"publishes": {"notes/briefing.md": "briefing.md", …}` (target under
  `notes/` only — never code, plugins, skills, tests, shaft, self, memory; source relative to the job's state
  folder). After a successful run the supervisor copies each source that changed onto its target atomically. It is
  committed with the instance's next notes commit (like operator doc edits). `_invalid` validates the key.
- **Confinement.** Inside a job, the broker's `run` uses the job's write set (state folder, `.tmp/`, browser profile),
  not `RUN_WRITABLE` — behind knob `SHAFT_JOB_RUN_CONFINED` (default `0` in this release; the operator turns it on
  once every instance's scheduled plugin uses `publishes`; the plan's §4 records when).
- **S8 (run):** `run`'s 3,000+3,000-character stream cut says how long each stream was and that the full output can
  be written to a file (`> .tmp/out.txt`) and read with offsets.
- **S6 long jobs.** A schedule may declare `"long": true`. A long job gets `SHAFT_JOB_LONG_TIMEOUT_S` (default 900)
  and `SHAFT_JOB_LONG_CALLS` (default 200), at most one long job running per instance, and the env
  `SHAFT_JOB_CHECKPOINT=.jobs/<plugin>/checkpoint.json`. If it times out with a checkpoint written since it started,
  it is resumed at the next scheduler tick (not its interval), up to `SHAFT_JOB_LONG_RESUMES` (default 4) times,
  with `SHAFT_JOB_RESUME=1`; each run is logged with `resumed: n`; `record(kind="jobs")` shows status
  (running / resuming n of N / done / gave up) and each job's args. Process cleanup unchanged.
- Acceptance (tests): a job writing `notes/x` directly still gets EPERM; a job returning `files` delivers them whole
  and refuses a path outside its folder or over the size cap; a 20 KB output is cut with a marker naming a file that
  holds all 20 KB; a declared `publishes` copies onto `notes/briefing.md` after a successful run and not after a failed
  one, and a target outside `notes/` is refused at validation; with the knob on, `run` inside a job cannot write
  `notes/`, with it off it can (unchanged); a long job that checkpoints and times out is resumed with
  `SHAFT_JOB_RESUME=1` and gives up after N; a non-long job keeps 120 s / 30 calls. PLAN-jobs.md and
  docs/integrations.md updated.

### M2 S2 — the gate tells baseline failures apart and keeps the candidate (supervisor) — lane *gate*
- Classify a failed gate as **baseline** (the shared supervisor tests fail on the warm-up, a stage hits its time
  limit in tests the candidate did not touch, the gate itself errors) or **candidate** (a test fails that passes on
  the baseline). Only a candidate failure counts against the instance.
- On a baseline failure the candidate is **held, not lost**: saved (patch + base sha) under
  `.supervisor/gate-held/<name>/`, the working tree reverted as today, and the instance told plainly
  ("held: the gate's baseline is red — failing: …; it will be re-gated when the baseline is green"). When a later
  warm-up is green, held candidates are re-applied on the current head and re-gated automatically; the instance is
  told the outcome. A held candidate that no longer applies is dropped with a message saying so.
- `record(kind="gate")` (broker): the instance's last gate runs — time, outcome (kept / candidate-failed /
  baseline-failed / held / re-gated), failing test names, the last 3,000 characters of the failing output, held
  candidates. Read-only; no gate logs of other instances.
- Acceptance (tests): a warm-up failure holds the candidate and messages the instance; a green warm-up re-gates and
  keeps it; a genuine candidate failure is reverted as today and not held; a held patch that no longer applies is
  dropped with a message; `record(kind="gate")` lists the runs with failing names and never another instance's.

### M3 S3 + S8(template) — self-knowledge feeds (template, + supervisor where the cause lives) — lane *template*
- **Briefing by section.** `prompts.briefing` renders `notes/briefing.md` like goals.md: whole `## ` sections in
  file order under a budget `BRIEFING_CHARS` (default 6000), a section headed *Corrections* (any case) always first,
  then one line per section left out. Today's 3,000-character head cut goes.
- **Forecast.** Ran out = no finish, `ended_by` steps/repeating **without** a finish, or `wrapped_up == "deadline"`.
  A steps wrap-up that still finished is not running out. Regression test on a finished `wrapped_up: "steps"` episode.
- **Correction ledger.** Invalid tool arguments must fail visibly, and recovery must retain the tool log.
  Reproduce the failure with synthetic arguments, fix the cause, and add a regression test.

- **S8 template:** `read_file` says `[… N more characters; read_file(path, offset=M)]` when it cuts; a plugin whose
  manifest is malformed, or a broker discovery error, is reported once per run in the tool list (name and why), not
  dropped silently.
- Acceptance: tests for each; a 67 KB briefing shaped like thelos's renders ≤ budget + 1,000 with Corrections whole.

### M4 S5 + S7 + S9 — small supervisor fixes — lane *small*
- **S5 mail search pages.** `mail.search` follows Gmail's `nextPageToken` up to the requested limit (≤ 50 per page,
  label check on every thread) and returns whether more exist; `mail_search` output ends with
  `more: yes — mail_search(…, page="<token>")` when Gmail has more, and accepts that page handle.
- **S7 provider alerts.** A run of provider failures that will not clear by retrying — HTTP 402/403 "limit"/"credit",
  429 streaks (≥ 5 in 10 min) — raises one control-panel alert per provider and cause (stable id, dismissable) and a
  push to paired devices once, through the existing alert path (`panel.alerts`, `web_alerts`).
- **S9 egress.** The proxy tries each resolved global address in turn (all are checked public first, as today) and
  reports the last error only if all fail.
- Acceptance: tests for each (fake Gmail with two pages; a 403 key-limit streak gives one alert and one push; a host
  whose first address refuses connects on the second).

### M5 S4a — pushes are held, not just counted — lane *push*
- A push = a notification to the operator's devices for an instance's `send_message`. The message itself is always
  stored and visible in the web UI at once; only the notification is held.
- `send_message` gains `urgent` (bool, default false). Not held: urgent, a reply to the operator's own message or
  question in the last 2 h, or anything in an open conversation. Held: a non-urgent push in quiet hours
  (`SHAFT_QUIET_HOURS`, default `22-07`) or beyond the daily budget (`SHAFT_PUSH_BUDGET`, which now enforces).
- Held pushes go out as **one digest notification** at `SHAFT_DIGEST_AT` (default `07:15`) or at the end of quiet
  hours, listing sender and first line of each; the urgent flag is recorded, and the judge's existing
  `should_have_waited` field reads it. Knob `SHAFT_PUSH_HOLD` (default on).
- Acceptance: tests — a non-urgent push at 23:00 is stored but not notified and appears in the 07:15 digest; an
  urgent one notifies at once; the 7th non-urgent push with budget 6 is held; a reply within 2 h is never held.

### M6 S10 — supervisor-held mail and calendar change wake-ups — lane *watch*
- A supervisor loop (in the scheduler's tick, no model) watches each instance's granted mail labels (Gmail
  `history.list` from a stored `historyId`, filtered by the granted label, every thread label-checked) and granted
  calendars (`events.list` with a stored `syncToken`).
- On a change it writes **one** inbox line per instance per tick (`From: supervisor (mail/calendar watch)`): new
  thread handles with sender and subject, changed events with title and start — only what the broker tools would
  show under the grant. The instance never holds the subscription or a token.
- An instance opts in per kind in its own `config.env` (`WATCH_MAIL`, `WATCH_CALENDAR`: off / inbox / wake), which it
  can set with `set_config`; wake is rate-limited by `SHAFT_WATCH_WAKE_MIN` (default 15 minutes) and uses the same
  wake path as jobs. Global knob `SHAFT_WATCH` (default on; nothing happens for an instance that has not opted in).
- An expired `historyId` / `syncToken` (404/410) resets to "now" with one log line, never a flood of old items.
- Acceptance: tests with fake Gmail/Calendar — a new thread under the label gives one line and, with `wake`, one
  wake; a thread outside the label is never mentioned; two changes within the rate limit wake once; an expired
  token resets without lines; an instance not opted in sees nothing. docs/integrations.md updated.

### M7: integration and verification

Merge the implementation lanes and run the supervisor suite and gate budget.
Use synthetic briefing and episode fixtures, and a scratch job with `files` and `publishes`.

## 2. Lane contracts (shared names)

| Name | Owner | Used by |
|---|---|---|
| job output keys `message`, `wake`, `files`; schedule keys `every_minutes`, `args`, `can_wake`, `publishes`, `long` | jobs | docs, gate tests of plugins |
| `jobs.request_wake(name, plugin)` — reused by watch with `plugin="watch-mail"` / `"watch-calendar"` | jobs (exists) | watch |
| `record(kind="gate")`, `record(kind="jobs")` row shapes | gate, jobs | template prompts (later) |
| `panel.alerts` stable ids: `provider:<name>:<cause>`, `pause:…` (exists) | small | web |
| `send_message(…, urgent=False)` | push | template tool docs |
| instance config keys `WATCH_MAIL`, `WATCH_CALENDAR`, `BRIEFING_CHARS` | watch, template | — |

Files by lane (disjoint where possible; `broker.py` is shared — each lane touches only its own functions):
jobs → `jobs.py`, `sandbox.py`, `broker.py` (`_run`, job wiring, `_record` jobs rows); gate → `gate.py`,
`runner.py`, `broker.py` (`_record` gate rows); small → `mail.py` (`search`), `broker.py` (`_mail_search`),
`egress.py`, `cloudllm.py`/`panel.py`/`web_alerts.py`; push → `pushes.py`, `messages.py`, `broker.py`
(`send_message` spec), web push; watch → new `watch.py`, `mail.py` (history only), `calendar.py` (sync only),
scheduler tick; template → `template/shaft/*`, `supervisor/corrections.py` if the cause is there.

## 3. S4b — score delivered work by what the operator did next (plan, not built)

**Problem.** A judge's visible reasoning and retention decisions can reward optimizing the judge rather
than useful work. The cited literature motivates an independent outcome measure.

**Proposal.**
1. *Outcome of a deliverable.* For each operator-valued cycle with `delivered_via`, the supervisor looks at the next
   48 h of the operator's own actions on that item, from records it already has: he replied or asked a follow-up
   on it, edited the shared doc, sent the drafted mail, moved/accepted the event, or nothing. Score 0–3:
   ignored / seen / engaged / acted on. No thumbs and no "acceptance" rating are used (the 10-28 review's warning:
   training on acceptance breeds sycophancy). "Acted on" is a behaviour, not a vote.
2. *Hidden part of the grade.* The value grade becomes judge-value blended with the outcome score; the instance
   sees its judge verdicts as today but **not** the outcome scores per cycle, only a weekly aggregate in stats.
   Probation keeps judged success but adds the outcome aggregate as a tie-breaker, never the sole criterion.
3. *Kill test.* Over 200 judged cycles: delivered share ≥ 45% and "acted on" share rises, while claim support does
   not regress (unsupported ≤ 6%) and pushes stay within budget. If acted-on does not move, revert.

**Open decisions for the operator:** the 48 h window; whether "seen" (opened a doc) counts at all; whether the outcome
enters probation or only stats.

## 4. Implementation findings (2026-10-31)

- `gate.run_tests` returns `gate.Output`, a `str` carrying `cause`, `why`, `failing`, and `recheck`.
  `runner._settle` holds baseline-failed code through `supervisor/gate_held.py` under
  `.supervisor/gate-held/<name>/`, notifies the instance in its inbox and journal, and records every gate in
  `.supervisor/gate-runs/<name>.jsonl`. `record(kind="gate")` exposes only that instance's rows.
  After a fresh shared pass, the scheduler starts `runner.regate_held`. Touched files must still match their
  saved base bytes; no fuzzy patching is used. Three baseline-failed re-gates drop the change. Config lines
  are undone and are not held.
- `tools.coerce_args` exists because providers can return typed arguments as strings, such as
  `reopens: "null"`. `corrections.record` refused these values; recovery discarded the refusal log and could
  save tool-call text as the journal entry. Schema coercion, field-specific refusals, retained tool results,
  and a final reply without tools address that failure.
- Count an unfinished run or a deadline wrap-up as budget exhaustion; a finished step wrap-up does not count.
- Keep `SHAFT_JOB_RUN_CONFINED` off until scheduled plugins use declared `publishes` paths.
- S4b awaits the operator's decisions.
