# Deploying a release

The release workflow tags `main`; this file is what comes after the tag, when the operator has asked for a deploy.
It is not a bump or tag procedure (agent-overlay.yaml `ship.procedure` points here for deploy and verify only).

SHAFT runs from the primary checkout (`~/Development/shaft`, on `main`) and its `template` worktree:

- every wake, sleep, job and replay is a fresh `python -m supervisor` started by the scheduler in that checkout,
  so a fast-forward of `main` there reaches the instances at their next wake;
- the long-running launchd services (`com.shaft.scheduler`, `com.shaft.web`, `com.shaft.api`, `com.shaft.telegram`)
  keep the code they started with until restarted;
- template fixes reach an instance only through `shaft update`, under its run lock.

## Steps

1. **Fast-forward the primary checkout and the template worktree** to the tagged commits. Only a fast-forward:
   the primary checkout holds other sessions' untracked files, which a fast-forward leaves alone.

   ```bash
   cd ~/Development/shaft
   git fetch origin
   git status -sb                       # on main; no tracked changes
   git merge --ff-only origin/main
   git -C template status -sb           # on template; no tracked changes
   git -C template merge --ff-only origin/template
   ```

   Stop if either is not on its branch, has tracked changes, or cannot fast-forward: that is someone's work.
   A gate warm-up that starts between the two fast-forwards tests the new `main` against the old template and can fail
   once (2026-10-30 09:49, a release whose tests and template changed together); the next warm-up, on the new pair,
   records its pass. A test in the gate's instance part must never pin a template change: an instance not yet updated
   runs it against its own code (that 09:59, a pinned call count reverted a good thelos change).
2. **Update the instances** from the template: `bin/shaft update --all`. It is gate-tested per instance; a busy
   instance is queued and updated by the scheduler as soon as it is idle. A refused update (a merge conflict, a red
   gate) is a stop: report it, do not force it.
3. **Restart the long-running services** so they load the new code:
   - `scheduler`: with a drain-restart, never `kickstart -k`. On SIGTERM it stops its wakes on purpose ("live
     stopped with the supervisor (signal 15)"), so a SIGTERM restart mid-cycle throws that cycle away (2026-10-28:
     v0.17.0 cut three live cycles short). Waiting for a moment with no instance awake did not work either: on
     2026-11-03 thelos and wren covered each other's cycles for 90 min without one.

     ```bash
     bin/shaft autonomy restart --wait
     ```

     From its next tick (≤ 30 s) the scheduler starts nothing new: no wake, sleep, update, job, trial or warm-up.
     What is due stays due for the next scheduler, and a queued update stays queued. A battery ends after the arm it
     is running, and the trial worker after the pairs it is running. Every other run ends on its own. Nothing is
     signalled. Then the scheduler has launchd restart it (`launchctl kickstart -k` on its own
     label), and `--wait` returns with the new PID and commit. That is usually within one cycle (≤ 40 min). It does
     not have to block the deploy: drop `--wait`, then check `bin/shaft autonomy status`. It shows what the drain
     waits for, and the line disappears once the scheduler has restarted. If the runs have not ended by the deadline
     (default `SHAFT_SLEEP_TIMEOUT_S + SHAFT_CYCLE_TIMEOUT_S`, `--within MIN` to change it), the drain gives up:
     scheduling resumes on the old code, nothing is cut, and the log raises an alert ("restart FAILED to drain").
     Ask again, or force it with `bin/shaft autonomy on --force` (that one cuts the runs). `--cancel` withdraws a
     pending restart. The web Services page offers the same: *Restart when idle*, then *Cancel restart*.

     **Once, from a scheduler older than the drain** (one that started before this release): `autonomy restart`
     refuses it, because such a scheduler cannot drain. Instead: `bin/shaft pause`. It then starts no wake, sleep,
     job, battery, warm-up or trial, but a queued update still starts. Wait until nothing is left in the scheduler's
     process group but the scheduler itself; this command must print nothing:

     ```bash
     pgrep -lf -g "$(ps -o pgid= -p "$(pgrep -f 'supervisor autonomy serve')" | tr -d ' ')" | grep -v 'autonomy serve'
     ```

     Then run `launchctl kickstart -k gui/$(id -u)/com.shaft.scheduler` and `bin/shaft resume`. Each instance gets a
     pause and a resume note.
   - `api`, `web`, `telegram`: only when no conversation is open on them (`runner.open_conversations(name)` is
     empty for every instance, or `bin/shaft status`).

     ```bash
     launchctl kickstart -k gui/$(id -u)/com.shaft.<service>
     ```

   Only services that are loaded (`launchctl list | grep com.shaft`) are restarted; `model` and `embed` do not
   run supervisor code that a release changes.

## Verify

- `git -C ~/Development/shaft rev-parse HEAD` equals the tag's commit; the same for `template`.
- `launchctl list | grep com.shaft`: each restarted service has a new PID. Its exit status is 0, or -15 for the
  scheduler: its drain ends with its own `kickstart -k`, which stops it with SIGTERM once no run is left.
- The scheduler's log has `scheduler: drained after N min — restarting on <commit>`, and no run of it "stopped with
  the supervisor (signal 15)".
- `bin/shaft status`: "which code is running" names the new commit for each instance that has woken or updated,
  and `shaft update` reported each instance updated or queued.
- `bin/shaft alerts`: no new alert from the restart.
