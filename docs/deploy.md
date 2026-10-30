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
3. **Restart the long-running services** so they load the new code, each only when nothing it runs is going on:
   - `scheduler`: only when no instance is awake. On SIGTERM it stops its wakes on purpose ("live stopped with the
     supervisor (signal 15); its changes are settled at its next run"), so a restart mid-cycle throws that cycle
     away: on 2026-10-28 the v0.17.0 deploy cut three live cycles short this way. Check that no wake, sleep or
     update is running (`ps -ax | grep -- '-m supervisor \(wake\|sleep\|update\)'`, or "live since" in
     `bin/shaft update --all`'s answer); otherwise wait, or leave the scheduler for later. A queued update runs
     either way: the running scheduler starts it when the instance is idle.
   - `api`, `web`, `telegram`: only when no conversation is open on them (`runner.open_conversations(name)` is
     empty for every instance, or `bin/shaft status`).

   ```bash
   launchctl kickstart -k gui/$(id -u)/com.shaft.<service>
   ```

   Only services that are loaded (`launchctl list | grep com.shaft`) are restarted; `model` and `embed` do not
   run supervisor code that a release changes.

## Verify

- `git -C ~/Development/shaft rev-parse HEAD` equals the tag's commit; the same for `template`.
- `launchctl list | grep com.shaft`: each restarted service has a new PID and exit status 0.
- `bin/shaft status`: "which code is running" names the new commit for each instance that has woken or updated,
  and `shaft update` reported each instance updated or queued.
- `bin/shaft alerts`: no new alert from the restart.
