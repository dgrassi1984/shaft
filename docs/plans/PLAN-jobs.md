# PLAN — instances schedule their own periodic jobs

Goal: an instance can have a script it wrote run every N minutes without a separate installation.
Synthetic example: check a public sample feed every 5–15 minutes and leave changes in the instance inbox.
The instance can write the watcher, but installing a launchd job would require access outside its sandbox.

**Status (2026-10-16): live.** Merged (main b776ef7, template 78fdc97), with the scheduler and API restarted, every instance updated, and M4 checked on the real system (below). Approved with the operator's corrections and answers (below).

## Why the supervisor runs them, not launchd

- An instance cannot install a launchd job, and should not: a plist runs outside every sandbox, as the operator.
- SHAFT's scheduler service (`supervisor/autonomy.py serve`) already starts each wake as a child process every
  30 s. Jobs become one more kind of child, and they run where everything an instance runs does: in its sandbox,
  through a broker.
- What the instance builds stays its own: **a job is one of its plugins**, run on a timer. Plugins are already the
  instance's own tools (`plugins/<name>/plugin.json` + `main.py`, `run(**args) -> str`), checked by the gate,
  testable with `run`, and able to call supervisor tools through `broker.call`. Nothing new has to be learned.

## Decisions

- **Declared in the plugin's own manifest:**
  `"schedule": {"every_minutes": 15, "args": {…}, "can_wake": true}` in `plugins/<name>/plugin.json`.
  - The instance creates, changes or removes a job by editing its plugin, in a cycle. The gate tests it and the
    commit records it, like any code change.
  - The scheduler reads the manifests on every tick. A manifest that does not parse, or a schedule out of
    bounds, is skipped, and the reason goes to the instance's inbox once (never silently).
- **Bounds:**
  - at least `SHAFT_JOB_MIN_MINUTES` between runs (default 5; operator: OK);
  - at most `SHAFT_JOBS_MAX` jobs per instance (default 12); beyond it, the rest by name are not run;
  - 120 s per run;
  - 30 supervisor tool calls per run;
  - output cut at 4,000 characters, with the cut stated (since 2026-10-31 the whole text is kept beside the message,
    and the cut names the file: see *Hand-off, publish and long jobs* below);
  - a long job (`"long": true`, 2026-10-31) gets 900 s and 200 calls per run instead, and is resumed from its
    checkpoint.
- **Where a job runs:** the job sandbox (`sandbox.job_profile`), with its own broker and egress proxy. Its own
  Python environment, the public internet, the mail and calendar grants, and the instance's own keys
  (`http_request auth=…`) all behave as in a cycle.
  - **Writes only to `.jobs/<plugin>/`** (state such as "last seen id"), `.tmp/` and its browser profile `.browser/`.
    A direct write elsewhere (e.g. `notes/`) fails with `[Errno 1] Operation not permitted`, so it cannot directly
    change the code, notes or mind that a cycle running at the same time is working on. The schedule's `publishes`
    is the route to `notes/`; see *Publish* under *Hand-off, publish and long jobs* below, and *Confinement* for
    the broker's `run` exception. Direct reads and writes to `secrets/` are denied.
  - `.jobs/` is gitignored and not in the mind store. It is a cache; what matters goes to the inbox.
- **No model.** The job's broker refuses `/llm`. A job is a script, so it never takes the local model and costs no
  tokens. Supervisor tools that cost money (`describe_image`, `decide`) count toward the instance's daily cap, as
  in a cycle. `delegate_coding`, `trial` and `send_message` are off in jobs (operator: "it would be the woken
  instance to do that").
- **What a job returns goes to the instance's inbox**, as a message "From: job <plugin>", marked as the
  instance's own job's output. An empty return delivers nothing, so a quiet check is silent. The next cycle (or
  conversation) reads it like any message.
- **Waking is the instance's choice, per job** (operator: "up to the instance … whether it is something that CAN
  wake it or not (no frequency limits); a job may have an exit outcome that DOES NOT wake the instance even if the
  possibility is set").
  - `"can_wake": true` in the schedule allows it; the default is false.
  - Each run decides. A plain string, or `{"message": …}`, is delivered without waking. `{"message": …,
    "wake": true}` from a job with `can_wake` starts a cycle as soon as the instance is idle, with no rate limit.
  - A wake request from a job without `can_wake` is not a wake: the message is still delivered, with a note that
    the job may not wake the instance.
  - If the instance is awake (a cycle, a conversation, its sleep), the request is kept, once, and starts a cycle
    when it is next idle. Several requests in the meantime make one wake, not a queue. A paused instance is
    never woken; its request is dropped and logged.
  - A wake from a job counts as the instance's wake: the next scheduled wake is a full interval later.
  - Timed wakes count delivered or published results, and job inbox messages, only for jobs whose current
    declaration has `can_wake: true`. Removed jobs do not count. If declarations cannot be read, mail still counts.
    Other output stays for the next cycle, including the 30-minute idle wake, or a conversation.
- **Failures are visible.**
  - Each run is logged in `.supervisor/jobs/<instance>.jsonl`: start, end, exit, characters out, delivered or
    not.
  - After 3 failures in a row, the instance gets one inbox message with the last error, and the job backs off
    (the interval doubles, up to 24 h) until it succeeds or its manifest changes.
- **No overlap.** If a job's previous run is still going when it comes due (e.g. one that overran its 120 s
  before the timeout killed it), the new run is skipped, not stacked, and the skip is logged in
  `.supervisor/jobs/<instance>.jsonl` and `supervisor.log`. The next run is a full interval later.
- **On by default** for every instance (operator).
- **Pausing:** a paused instance's jobs are paused, and so is everything when the scheduler is paused
  (`.supervisor/paused`). `shaft jobs <name> off` switches one instance's jobs off. That setting lives in
  `supervisor/tools/<name>.json`, so the instance cannot switch them back on.
- **Never in trial replays or battery exams.** A replay that edits a schedule only records the edit.
- **The instance can see its jobs:** `record(kind='jobs')` shows its jobs, their last runs and errors. The prompt
  gets one line on how to schedule a plugin, and `skills/writing-plugins.md` gets a section on it.

## Milestones

- **M1 — supervisor: `supervisor/jobs.py`.**
  - `declared(name)` reads and validates the manifests.
  - `due(name, now)` uses the last runs and the backoff.
  - `run_job(name, plugin)` runs the job: a broker with the job's tool set, no `/llm` and 30 calls; the run
    sandbox with writes limited to `.jobs/<plugin>/` and `.tmp/`; `plugin_runner` with the declared arguments
    and a 120 s timeout; then logging and inbox delivery.
  - The scheduler starts due jobs as children (`shaft job run <name> <plugin>`), a different kind of child
    from a wake, so a job never skips or delays a wake.
- **M2 — operator and instance surfaces.**
  - `shaft jobs [name]` (list, last runs, errors), and `shaft jobs <name> off|on`.
  - A `jobs` row in the panel and in `shaft top`.
  - `record(kind='jobs')`.
  - The template: the prompt line, the skill section, and `.jobs/` in `.gitignore`.
- **M3 — tests (regression, named for what they guard).**
  - `test_job_runs_on_its_interval_and_not_faster_than_the_floor`
  - `test_overrunning_job_is_skipped_not_stacked_and_logged`
  - `test_wake_only_when_declared_and_requested_coalesced_while_awake`
  - `test_bad_manifest_skipped_and_reported_once`
  - `test_job_writes_only_its_state_dir`: a write to `notes/`, `plugins/`, `memory/` or `config.env` is refused.
  - `test_job_has_no_model`
  - `test_job_output_lands_in_inbox_and_empty_output_delivers_nothing`
  - `test_job_mail_grant_applies`
  - `test_failures_back_off_and_are_reported_once`
  - `test_paused_instance_runs_no_jobs`
  - `test_operator_switch_off_holds`
  - `test_replay_never_runs_jobs`
  - Full suite green.
- **M4 — verify in the running system.** Ask example-a, through the talk path, to turn its watcher into a scheduled
  plugin. Then:
  - after its next cycle commits the plugin, watch the scheduler start the job;
  - tag a mail AssistantAccess, and see the next job run deliver a message to example-a's inbox;
  - see example-a's following cycle read it;
  - `shaft jobs example-a` shows the runs.

## Operator's answers (2026-10-16)

1. Waking: each job declares whether it can wake the instance, with no frequency limit, and each run decides
   whether it does (see Decisions).
2. `send_message` from jobs: off; the woken instance does that.
3. On by default.
4. A minimum interval of 5 minutes: OK.

Corrections: the inbox waits for the instance's own wake interval, which has no default floor, not "a
2-hour cycle". Added: the no-overlap rule (skip, and log it).

## As built (M1–M3)

- `supervisor/jobs.py` holds the manifests, due times, back-off, the run, inbox delivery, wake requests and
  `describe`. `sandbox.job_profile` shares its rules with `run_profile` (`_command_profile`).
- `Broker.unavailable` and `Broker.model_allowed` give the job's broker its limits.
- `autonomy.serve` calls `jobs.tick`. `shaft jobs` and `shaft job run` are the CLI. `record(kind='jobs')`, and a
  *jobs* row in the panel and `shaft top`.
- Template: one clause in the abilities line, a section in `skills/writing-plugins.md`, and `.jobs/` in
  `.gitignore`.
- **Beyond the plan:**
  - Jobs are skipped while a battery exam runs on that instance. An exam undoes every file created in the real
    folder, so a message delivered during one would be lost.
  - `secret_set`, `secret_delete` and `set_config` are also off in jobs: keys and config are changed in a cycle.
- **Tests:** `supervisor/tests/test_jobs.py`, 15 tests, 9 of them real runs under `sandbox-exec`. 17 deliberate
  breaks of the safeguards were each caught. Full suite: 638, with only the known checkout-name failure.

## M4 design findings (2026-10-16)

The scheduler must not run a plugin while a conversation is still writing it.
A scheduled plugin waits until its folder matches gate-tested committed code (`jobs.uncommitted`).
Regression test: `test_an_uncommitted_plugin_waits_for_its_commit`.
Verify inbox delivery and the resulting wake separately with a synthetic watcher.

## Hand-off, publish and long jobs (2026-10-31, [PLAN-thelos-review-2.md](PLAN-thelos-review-2.md) M1)

A job cannot write notes directly. Large output needs a whole-file hand-off rather than a truncated inbox message.
The broker's `run` must use the job's write boundary too; otherwise it bypasses that boundary.
Long jobs also need explicit time and call budgets, checkpoints and bounded resumes.


- **Hand-off.** A run's JSON output may carry `"files": [path, …]` — up to `MAX_FILES` (5) files of at most
  `MAX_FILE_BYTES` (256 KB), relative to `.jobs/<plugin>/` (or starting with it), read without following a link. They
  are copied into `inbox/<stamp>-from-job-<plugin>.files/`; the message lists each with its size and names each
  refused one with why (outside the folder, a link, too big, missing, more than five). A runaway list is read to its
  twentieth entry.
- **Cut output.** Past `MAX_OUTPUT` the whole text (up to 1 MB) is kept in that folder as `output.txt`, and the cut
  marker names it: `[… cut at 4000 of N characters; the whole text: inbox/….files/output.txt]`.
- **Beyond the plan:** the template archives only a message's `.md`, so each hand-off folder would stay in the inbox
  (and the mind store) for good. When a job delivers, the folders of its earlier messages that have left the inbox
  are removed (through descriptors: a link is removed itself, never followed); the message says so.
- **Publish.** `"publishes": {"notes/briefing.md": "briefing.md"}` in the schedule: at most 5 targets, each under
  `notes/` (never code, plugins, skills, tests, shaft, self or memory), each source a plain path in the state
  folder; `_invalid` refuses anything else, so the schedule is not run and the reason reaches the inbox. After a
  successful run only, each source whose content changed since it was last published (a sha per target in the job's
  state) replaces its target in one rename through the folder's descriptor. An unchanged source never overwrites the
  instance's own edit of the target. A publish problem (source over 256 KB, a link on the way) reaches the inbox once
  until it changes. notes/ is mind: the instance's next notes commit records it.
- **Confinement.** With `SHAFT_JOB_RUN_CONFINED=1` the job's broker runs `run` in `sandbox.run_profile(…,
  job_state=…)`: the job's write set, not `RUN_WRITABLE`. Default `0` this release: the operator turns it on once
  every instance's scheduled plugin uses `publishes` (PLAN-thelos-review-2 §4 records when).
- **`run`'s stream cut** (S8) says how long each stream was; since review C8 the broker saves the whole output to
  `.tmp/outputs/run-*.txt` and each cut names the `read_file` offset where it starts (a job's write set includes
  `.tmp/`).
- **Long jobs.** `"long": true` → `SHAFT_JOB_LONG_TIMEOUT_S` (900) and `SHAFT_JOB_LONG_CALLS` (200); one long job at a
  time per instance (the tick starts no second one; a lock holds it for `shaft job run` too, exit 3); env
  `SHAFT_JOB_CHECKPOINT=.jobs/<plugin>/checkpoint.json` and `SHAFT_JOB_RESUME=0|1`. Stopped by its timeout with a
  checkpoint written since it started (a plain file whose mtime is after the start): not a failure; resumed at the
  next tick, not its interval, up to `SHAFT_JOB_LONG_RESUMES` (4) times. After that — or a resumed run that wrote no
  new checkpoint — it gives up: one message, a failure counted, and the next run a full interval later and afresh.
  Each long run's row carries `resumed: n` (and `resuming` or `gave_up`). `record(kind='jobs')` shows each job's
  args, what it publishes and its status (running; for a long job done / resuming n of N / gave up), live and in a
  trial replay (rebuilt from the run log).
- **Tests:** `supervisor/tests/test_jobs_handoff.py`, one per acceptance line, each naming the bug it guards.
- **Left for the template lane:** `skills/writing-plugins.md` has no section yet on `files`, `publishes` and
  `long`.

