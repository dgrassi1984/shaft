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
2. **Update the instances** from the template: `bin/shaft update --all`. It is gate-tested per instance; a busy
   instance is queued and updated by the scheduler as soon as it is idle. A refused update (a merge conflict, a red
   gate) is a stop: report it, do not force it.
3. **Restart the long-running services** so they load the new code. Wakes are started with `start_new_session`,
   so restarting the scheduler does not end a running cycle. A conversation does end with its service, so first
   check `bin/shaft status` for open conversations and restart `api`, `web` or `telegram` only when none is open
   on it (wait, or leave that one for the operator).

   ```bash
   for s in scheduler web api telegram; do launchctl kickstart -k gui/$(id -u)/com.shaft.$s; done
   ```

   Only services that are loaded (`launchctl list | grep com.shaft`) are restarted; `model` and `embed` do not
   run supervisor code that a release changes.

## Verify

- `git -C ~/Development/shaft rev-parse HEAD` equals the tag's commit; the same for `template`.
- `launchctl list | grep com.shaft`: each restarted service has a new PID and exit status 0.
- `bin/shaft status`: "which code is running" names the new commit for each instance that has woken or updated,
  and `shaft update` reported each instance updated or queued.
- `bin/shaft alerts`: no new alert from the restart.
