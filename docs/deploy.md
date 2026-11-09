# Maintainer release-deploy runbook

This is the maintainer's release-deploy runbook, not an installation guide.

The release workflow tags `main`; this file is what comes after the tag, when the operator has asked for a deploy.
It is not a bump or tag procedure (agent-overlay.yaml `ship.procedure` points here for deploy and verify only).

SHAFT runs from the primary checkout (`~/Development/shaft`, on `main`) and its `template` worktree:

- every wake, sleep, job and replay is a fresh `python -m supervisor` started by the scheduler in that checkout,
  so the deployed release reaches the instances at their next wake;
- the long-running launchd services (`com.shaft.scheduler`, `com.shaft.web`, `com.shaft.api`, `com.shaft.telegram`,
  `com.shaft.model`, `com.shaft.embed`)
  keep the code they started with until restarted;
- template fixes reach an instance only through `shaft update`, under its run lock.

## Steps

1. **Fast-forward the primary checkout and the template worktree** to the tagged commits. Only a fast-forward:
   the primary checkout holds other sessions' untracked files, which a fast-forward leaves alone.

   ```bash
   # Set RELEASE_TAG to the exact release tag and TEMPLATE_COMMIT to its matching template commit.
   cd ~/Development/shaft
   git fetch origin --tags
   git status -sb                       # on main; no tracked changes
   git merge --ff-only "$RELEASE_TAG"
   git -C template status -sb           # on template; no tracked changes
   git -C template merge --ff-only "$TEMPLATE_COMMIT"
   ```

   Stop if either is not on its branch, has tracked changes, or cannot fast-forward: that is someone's work.
   Use the release's recorded template commit, not `origin/template` or another branch tip.
   Check that each resulting HEAD equals its selected commit. A fast-forward can succeed without moving
   an already newer checkout, so a successful merge alone does not prove the release is deployed.
2. **Update the instances** from the template: `bin/shaft update --all`. It is gate-tested per instance; a busy
   instance is queued and updated by the scheduler as soon as it is idle. A refused update (a merge conflict, a red
   gate) is a stop: report it, do not force it.
3. **Restart the long-running services** so they load the new code:
   - `scheduler`: use a drain-restart so active work finishes before the process restarts.

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

     **Once, from a scheduler older than the drain** (one without drain support): `autonomy restart`
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

   - `model` and `embed`: restart after active work using them finishes when their code or loaded settings
     change. Use `bin/shaft model on` and `bin/shaft embed on`; do not force a busy service.
     `supervisor/version.py` maps model code to `supervisor/bonsai_server.py` and `supervisor/bonsai_lora.py`,
     and embedding code to `supervisor/embed_server.py`. `supervisor/actions.py` guards CLI service changes; `supervisor/autonomy.py` restarts the service.

   Restart only loaded services (`launchctl list | grep com.shaft`).

## Verify

- `git -C ~/Development/shaft rev-parse HEAD` equals the selected release tag's commit; `template` equals the selected matching template commit.
- `launchctl list | grep com.shaft`: each restarted service has a new PID. Its exit status is 0, or -15 for the
  scheduler: its drain ends with its own `kickstart -k`, which stops it with SIGTERM once no run is left.
- The scheduler's log has `scheduler: drained after N min — restarting on <commit>`, and no run of it "stopped with
  the supervisor (signal 15)".
- `bin/shaft status`: "which code is running" names the new commit for each instance that has woken or updated,
  and `shaft update` reported each instance updated or queued.
- `bin/shaft alerts`: no new alert from the restart.
