# Deploying a release

How to move a running setup onto a new release tag. It is not an installation guide (see the README) and not a
bump or tag procedure.

SHAFT runs from the primary checkout (the clone you installed from, on `main`) and its `template` worktree:

- every wake, sleep, job and replay is a fresh `python -m supervisor` started by the scheduler in that checkout,
  so the deployed release reaches the instances at their next wake;
- the long-running launchd services (`com.shaft.scheduler`, `com.shaft.web`, `com.shaft.api`, `com.shaft.telegram`,
  `com.shaft.model`, `com.shaft.embed`)
  keep the code they started with until restarted;
- template fixes reach an instance only through `shaft update`, under its run lock.

`shaft deploy TAG --json` runs the steps and the checks below as one command, one JSON line per step (the Mac app's
Settings → Services → Update server runs it, after `shaft deploy --check --json` found a newer release). Each release
tag `vX.Y.Z` has a lightweight tag `template-vX.Y.Z` on the template commit shipped with it (see [Cutting a
release](#cutting-a-release)). A release without its `template-vX.Y.Z` cannot be installed or deployed by the app, nor by
`shaft deploy`, which refuses it. `shaft deploy vX.Y.Z` does steps 1 to 3 below, and the app's Update server runs
it; by hand, resolve the template commit with `git rev-parse template-vX.Y.Z^{commit}`. The command
stops where these steps say to stop, never forces a service, and restarts api, web and telegram only when no
conversation is open: otherwise its services step says who it waits for, and `shaft deploy TAG --json
--services-only` does that part later. `model` and `embed` stay manual (step 3). From its look at the open conversations until the old
processes are gone it holds `.supervisor/restarting.lock`: a conversation that tries to open meanwhile (API, web, Telegram
or a terminal talk) is refused with "the server is restarting, try again in a moment", so the restart never cuts one.
`shaft api on` and `shaft web on` (also run by the Mac app) restart a running service under the same lock, and restart
nothing while a conversation is open.
Each step's outcome is kept in `.supervisor/deploy-state.json`; `shaft deploy --check --json` names a deploy left
unfinished, and running the same command again finishes it from the step it stopped at: what it recorded done and the
checkout still shows (main and the template at the tags' commits) is not repeated, and origin is not asked
(`shaft deploy --check --local --json` names it without origin too). The services it restarted are kept with their old
PIDs, so a verify retried later checks those same services again: it passes only once each runs a new process. Run again after success, it changes
nothing. The Mac app installs the server as a clone detached at its release tag rather than on `main`; the command
accepts that checkout too (a detached HEAD at a `vX.Y.Z` tag) and fast-forwards it the same way.

## Cutting a release

Besides the annotated release tag `vX.Y.Z`, every release records its template commit as a lightweight tag:

```bash
git rev-parse template                  # the template commit: the tip of the template branch that passed the gate
git tag template-vX.Y.Z <template commit>
git push origin vX.Y.Z template-vX.Y.Z
```

The template commit is the tip of the `template` branch that passed the gate with that release (`bin/shaft gate budget`
tests committed supervisor and template code together). Tag it before or with the release tag and push both together:
the app's installer clones at `vX.Y.Z` and fetches `template-vX.Y.Z`, and `shaft deploy vX.Y.Z` fast-forwards the template
worktree to it, so a release pushed without it is not installable until the tag exists.

## Steps

1. **Fast-forward the primary checkout and the template worktree** to the tagged commits. Only a fast-forward,
   which leaves untracked files alone.

   ```bash
   # Set RELEASE_TAG to the exact release tag (vX.Y.Z).
   TEMPLATE_COMMIT=$(git rev-parse "template-$RELEASE_TAG^{commit}")   # the template commit shipped with it
   cd <primary checkout>
   git fetch origin --tags
   git status -sb                       # on main; no tracked changes
   git merge --ff-only "$RELEASE_TAG"
   git -C template status -sb           # on template; no tracked changes
   git -C template merge --ff-only "$TEMPLATE_COMMIT"
   ```

   Stop if either is not on its branch, has tracked changes, or cannot fast-forward: that is someone's work.
   Use the commit of `template-vX.Y.Z`, not `origin/template` or another branch tip.
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

- In the primary checkout, `git rev-parse HEAD` equals the selected release tag's commit; `template` equals the selected matching template commit.
- `launchctl list | grep com.shaft`: each restarted service has a new PID. Its exit status is 0, or -15 for the
  scheduler: its drain ends with its own `kickstart -k`, which stops it with SIGTERM once no run is left.
- The scheduler's log has `scheduler: drained after N min — restarting on <commit>`, and no run of it "stopped with
  the supervisor (signal 15)".
- `bin/shaft status`: "which code is running" names the new commit for each instance that has woken or updated,
  and `shaft update` reported each instance updated or queued.
- `bin/shaft alerts`: no new alert from the restart.
