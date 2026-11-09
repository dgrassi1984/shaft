# PLAN — restart the scheduler without cutting a run (drain)

Branch `claude/scheduler-drain`, off `main` 7a16e850. Status: **signed off 2026-11-03 (recommended answers to the open questions); built, M1–M4 done.**

## The problem, from the code

- `com.shaft.scheduler` runs `autonomy.serve()` (supervisor/autonomy.py:431). Every 30 s it reaps its children and
  starts new ones: queued updates (:463), the nightly sleep (:471), wakes (:485), commitment and event wakes, plugin
  jobs (`jobs.tick`, :488), batteries (`_batteries`, :470), the gate warm-up and regate (:496), the trial worker
  (:500), and the mail and calendar watch (a thread, `watch.tick`, :493).
- Every child shares the scheduler's process group (`_start`, :638). On SIGTERM, `stop_children_on_signal` (:507)
  sends SIGTERM to every child and exits within `CHILD_STOP_S` = 15 s. A live wake stops and its cycle is lost
  ("live stopped with the supervisor (signal 15)"). That is why `docs/deploy.md` step 3 says "restart only when no
  instance is awake".
- The scheduler keeps starting children, so overlapping runs may prevent an idle moment indefinitely.
- Both restart paths go through SIGTERM and cut runs: `launchctl kickstart -k`, and `shaft autonomy on` (bootout +
  bootstrap). The guard (`actions.service_guard`/`cli_guard`, `web_actions.service_buttons`) can only refuse or
  force. There is no "restart once they end".

## Design

**Drain**: once a restart is requested, the scheduler starts nothing new, lets what is running finish, and then
restarts itself on the code now on disk. It has a deadline. If the deadline passes, it gives up the restart and goes
back to normal scheduling. It never cuts a run.

### 1. The request (a file, not a signal)

The verb writes `SUP_DIR/scheduler-restart.json` = `{requested, by, deadline}`. The scheduler reads it on each tick.

- **Why a file and not SIGUSR1/2:** today's scheduler (11-01 code) has no handler. A user signal sent to it takes
  the default action and kills it, and launchd then kills its process group, which cuts every run. An old scheduler
  ignores the file, so the wrong target can do no harm.
- A request older than this scheduler's own start is stale (left over by an earlier process). The scheduler deletes
  it and does not act on it, so a restarted scheduler never drains again in a loop.
- When the scheduler starts, its service record says that it can drain (`record_service("scheduler",
  {"drain": 1})`). The verb refuses to ask a scheduler whose record lacks this flag; see "The first cutover" below.

### 2. The loop while draining (`serve`)

The reap step (:447–461) is unchanged: exit codes, `update_ended`, `warm_ended`, stderr tails. After it, when a
fresh request is present:

| What | While draining |
|---|---|
| wake, sleep, update, job, gate warm/regate already running | **waited for**. Each has its own cap: a cycle has `SHAFT_CYCLE_TIMEOUT_S` (2400), a sleep `SLEEP_TIMEOUT_S` (5400), a long job `SHAFT_JOB_LONG_TIMEOUT_S` (900), and the gate has stage timeouts |
| new wakes, sleep, queued updates, jobs, event and commitment wakes, warm-ups, trials, batteries | **not started**. Their state is persisted (`schedule.json`, the update-queued flags, `jobs.pending_wakes`), so the new scheduler finds a due wake or sleep still due and a queued update still queued. Nothing is skipped or recorded as a skip |
| a running battery | **finishes the arm it is running and starts no new one**: `battery.run` asks `parent_draining()` between arms. *Changed after review:* the plan's SIGTERM "after the current arm" was wrong. The exam checks the stop flag every second and is killed mid-arm (battery.py:270), as Codex found |
| a running trial worker | **finishes the pairs it is running and submits no new one**: `trial.work` (trial.py:1399) checks the request before it submits. Without this check it keeps going until no pair is left |
| a running watch pass (a thread) | waited for (`watch._thread`) |
| `calendar_proposals.expire()` | still runs (it is not a child) |

- When nothing is left, the scheduler logs `scheduler: drained after N min — restarting on <commit>`, deletes the
  request and restarts (see 3).
- If the deadline passes first, it logs an alert (it must match `panel.ALERT_LINE`): `scheduler: restart
  FAILED to drain within N min — still running <children>; scheduling resumed on the old code (<commit>)`. It
  deletes the request and resumes the normal loop. It cuts nothing.
- It logs once when draining starts (`draining for a restart: waiting for wake <name>; by HH:MM at the
  latest`). While draining, it writes the list of children it still waits for into the request file, so the verb,
  `shaft status` and the web page can show it.
- **The deadline is derived, not tuned**: `request + SLEEP_TIMEOUT_S + CYCLE_TIMEOUT_S` (130 min with today's
  knobs). That is the longest any one waited child may run on its own budget, plus one cycle's budget to settle.
  It follows those knobs if they change. `--within MIN` overrides it per request. A normal drain ends when the
  current cycles end, so in about 40 min at most.

### 3. How it comes back

When the scheduler runs under launchd (its PID is the PID that `launchctl print` reports for the label), it does
not just exit. It runs `launchctl kickstart -k gui/<uid>/com.shaft.scheduler` in a new session (outside its own
process group). launchd sends it SIGTERM. `stop_children_on_signal` finds no live child and exits at once, and
launchd starts the new process **explicitly**.

- **Why not just exit and rely on KeepAlive:** on 2026-10-13, launchd's on-demand-only mode stopped starting jobs it
  was meant to start on its own ("pending spawn"). An explicit kickstart still works in that mode (autonomy.py:110).
- When it was started by hand from a terminal (`shaft autonomy serve`), it just returns 0.

### 4. The verb: `shaft autonomy restart [--within MIN] [--wait] [--cancel]`

- Scheduler not running: it says so (`shaft autonomy on` starts it), exit 1.
- Running scheduler predates the drain (its record has no `drain` flag): refused, exit 2. The message names the
  scheduler's commit and start time, and gives the first-cutover recipe below.
- Otherwise it writes the request and prints what the drain waits for and its deadline. It returns at once.
  `--wait` blocks until the service record shows a new PID (and prints its commit), or until the drain is abandoned
  (exit 1). `--cancel` deletes a pending request.
- It needs no guard, because it never cuts a run. `restart` joins the `autonomy` action choices
  (supervisor/__main__.py:1124).

### 5. Wired end to end

- **CLI guard** (`actions.cli_guard`): the refusal of `shaft autonomy on` while runs go on now ends with "or
  `shaft autonomy restart`: it restarts as soon as they end, without cutting them".
- **Web** (`web_actions.service_buttons`, the scheduler row only): when the guard holds the restart back, the row
  offers **Restart when idle** (it runs `autonomy restart`) next to *Force restart*. While a drain is pending, the
  row shows "restarting: waiting for …, by HH:MM" and a **Cancel restart** button. `web_actions.run` gains
  `verb == "autonomy"`, `op == "restart"` and `op == "cancel-restart"`.
- **`shaft status` / `shaft autonomy status`**: one line about the pending drain (what it waits for, its deadline).
- **`docs/deploy.md` step 3**: the scheduler is restarted with `bin/shaft autonomy restart --wait` (run in the
  background, or checked later). Remove the "only when no instance is awake" wait and the `ps | grep` recipe. Keep
  `kickstart -k` for api, web and telegram only. Update "Verify": the scheduler's new PID comes from the drain. Its
  last exit is SIGTERM, from the kickstart in step 3 of the design, not 0.
- **`docs/code-map`/CONTRIBUTING**, if they describe the scheduler loop: brought into line.

### The first cutover (honest limit)

The scheduler that runs today has none of this. Its first restart onto drain-capable code cannot drain. It can only
use SIGTERM, which cuts runs. With the old code there is still a way that cuts nothing:

1. `bin/shaft pause`. The old loop then starts no wake, sleep, job, battery, warm-up or trial (autonomy.py:468).
   Queued updates still start (:463).
2. Wait until `panel.scheduled_runs()` is empty and no `update` child is running. This takes one cycle, at most
   about 40 min.
3. `launchctl kickstart -k gui/$(id -u)/com.shaft.scheduler`, then `bin/shaft resume`.

Cost: each instance gets a journal note "paused by the operator" and later "resumed" (`core.set_paused`). Event
wakes for messages that arrive during the pause wait for the resume, and none is lost (the unread inbox wakes the
instance, `wake_gate`). The verb's refusal prints this recipe. The operator owns this cutover, and I will not run it.

### Changes after the Codex review (gpt-6.1-sol)

- **Battery:** it is never signalled; see the table above. A real two-arm exam test pins the behaviour: the arm
  under way is scored and the next one never starts.
- **One lock** (`scheduler-restart.lock`) covers writing, cancelling and stale-dropping the request, and the
  scheduler's done or abandoned decision. A stale drop can no longer delete a newer request, and a cancel that comes
  after the drain has committed says "too late" (exit 1) instead of "scheduling resumes".
- **No regate while draining:** a warm-up that ends green during a drain starts no `gate regate`. The held changes
  wait for the next scheduler's first green warm-up.
- **First cutover:** a queued update still starts while paused, so the check is now "nothing left in the
  scheduler's process group" (`pgrep -g`), not the status line.

## Alternatives rejected

- **Hand-over** (children outlive the old scheduler, the new one adopts them through the run locks): the restart
  would be instant, but the reap bookkeeping would be lost (`update_ended`, `warm_ended`, the sleep crash record,
  stderr tails). The new scheduler could not stop a battery it did not start, and it would start a second warm-up
  or trial worker, because its `children` map would be empty. Too many invariants rest on `children`.
- **Reuse the global pause as the drain**: it writes journal notes to every instance, and it suppresses event-wake
  requests. A restart is not an operator pause.
- **SIGUSR1/2 as the trigger**: see 1. Sent to an old scheduler, it kills that scheduler.

## Milestones

**M0 — worktree** (done): `claude/scheduler-drain`, `template/` worktree, `.supervisor/` (docs/worktrees.md).

**M1 — drain in the loop** (supervisor/autonomy.py, supervisor/trial.py)
- `restart_request()` / `request_restart()` / `cancel_restart()` / `drain_step(children, started_at, now)`. The last
  one is pure: it decides between waiting, done and abandoned, and which batteries to stop. The service record gets
  `"drain": 1`. `trial.work` stops submitting while a request is pending.
- Acceptance (supervisor/tests/test_autonomy.py, new class `SchedulerDrain`):
  - **Regression `test_a_drain_restart_cuts_no_live_run`**: `serve(poll_s≈0.05)` runs in a thread with
    `SHAFT_SUPERVISOR_DIR` set to a temp folder. `_start` spawns **real** child processes: a fake `wake a` that
    sleeps about 1 s, writes `done` and exits 0, and writes `cut` if it gets SIGTERM. `decide_wakes` asks for a wake
    of `b` on every tick. After the request: `serve` returns, the wake child's file says `done` and never `cut`, its
    return code is 0, no `wake b` started after the request, and `kickstart` was not called (not under launchd).
    The test fails loudly on today's code, where `serve` never returns and a SIGTERM restart writes `cut`.
  - deadline: a child that outlives `--within` is neither signalled nor killed; scheduling resumes; the alert line
    matches `panel.ALERT_LINE`; the request file is gone.
  - a stale request (older than the scheduler's start) is deleted and starts no drain.
  - a battery child gets exactly one SIGTERM; the trial worker with a request pending submits no pair and returns
    once its in-flight pair ends.
  - under launchd (stubbed `launchctl print` with our PID): kickstart -k is issued in a new session, and nothing is
    issued otherwise.
  - the existing SIGTERM tests (cut on purpose) still pass unchanged: the drain does not change what a real stop
    does.

**M2 — verb, guard text, web, status** (supervisor/__main__.py, supervisor/actions.py, supervisor/web_actions.py,
supervisor/panel.py or dashboard.py for the status line)
- Acceptance: tests for the verb's refusal on a record without `drain` (the message names the commit and the
  recipe), for request, `--cancel` and the `--wait` outcomes, for the guard text, and for the web row's
  *Restart when idle* / *Cancel restart* buttons and the form handling. Checked in a browser against a local web
  server in this worktree, per the PWA local-verification practice: the row renders the button and, with a request
  pending, the waiting line.

**M3 — real launchd check, without touching the live scheduler**
- A throwaway LaunchAgent (`com.shaft.drain-check`, removed afterwards) runs **this worktree's**
  `python -m supervisor autonomy serve`. `SHAFT_SUPERVISOR_DIR` points to a scratch folder, there are no instances,
  and there is no `.env`. Its first tick starts a real `gate warm` child (no warm record for this commit), which is
  the run to drain. A small harness points `SERVICES["scheduler"]["label"]` at the throwaway label, then runs
  `restart --wait`.
- Acceptance: the `gate warm` child ends with exit 0 and is not cut, the label gets a new PID, the new process
  records itself, and `launchctl list` shows the job loaded. Then the agent is booted out and its plist deleted.
  The live `com.shaft.scheduler` is never touched.

**M4 — docs + PR**: `docs/deploy.md` step 3 and Verify, plus the first-cutover recipe as a one-time note. Full suite
on python@3.12 per the release-ritual practice. Commit, push, open the PR, and bind it. No merge, no restart.

## Open questions for sign-off

1. **At the deadline: resume (my pick) or cut?** I propose that a drain never cuts: at the deadline it gives up and
   scheduling goes on. The operator can still use `--force` the old way. The other option is to stop the remaining
   children at the deadline, which bounds the restart but cuts a run, which is what this is meant to avoid.
2. **Deadline default `SLEEP_TIMEOUT_S + CYCLE_TIMEOUT_S` (130 min)**, derived from existing knobs, or a flat value?
3. **Self-kickstart at the end** (3) rather than a plain exit. It is protected against on-demand-only mode. The cost
   is that `launchctl list` shows the last exit as SIGTERM, not 0.
4. **Plan file location**: here at the root for review. On merge, move it to `docs/plans/PLAN-scheduler-drain.md`?
