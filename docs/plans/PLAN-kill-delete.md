# PLAN — kill and delete an instance

Status: **built** on branch `claude/kill-delete` (2026-10-20). Decisions (operator, 2026-10-20): verbs `kill` and
`delete`, kill = pause + stop; archive by default, no `undelete`; a paused instance's trial replays wait; delete
**purges everything** of it, its rows in the shared ledgers and logs included (they go into the archive's `purged/`).

Code: `supervisor/kill.py`, `supervisor/delete.py`, the verbs in `supervisor/__main__.py`, the panels in
`web_actions.py` / `top_views.py`, tests in `supervisor/tests/test_kill_delete.py` (+ panel tests).

## The problem

There is `shaft new`, `pause`, `resume`, but no way to (1) stop what an instance is doing *now* and
(2) remove an instance. `pause` only asks a running cycle to wrap up at its next turn and blocks new
wakes; a reply, a job, a battery item or a trial replay already running goes on. Removing an instance by
hand means finding ~25 places its state lives (worktree, branch, mind store, `.supervisor/` files and
entries inside shared files, a Telegram token in `.env`) — easy to leave half of it behind, and
`shaft new <same name>` then refuses ("a mind store … already exists").

## Verbs

```text
shaft kill <name>                 # pause it, and stop everything it is running now
shaft delete <name> [--yes] [--no-archive]   # archive it, then remove every trace of it
```

`kill` is reversible (`shaft resume <name>`); `delete` is not, except from its archive.

## M1 — `shaft kill <name>`

**Meaning: pause + stop now.** Writes `paused-<name>` first (nothing new starts: wakes, the nightly
sleep, jobs, battery, the job-asked wakes all already skip a paused instance), then stops what runs.
A kill that did not pause would be undone by the next scheduler tick.

**How each run is stopped — always the instance's own processes, never the supervisor's**, so the
supervisor-side runner does what it already does for a run "stopped from outside": journal note,
append-only/core checks, the **gate on its code changes**, commit, lock released (`runner._run`,
`code < 0` branch). Killing the supervisor process that holds the run (e.g. `supervisor wake shaft`)
instead would skip the gate and the commit: its unchecked code would become the next run's baseline.

| What it runs now | Holder | Kill does |
|---|---|---|
| cycle, sleep, reflection (`wake`, `cycle`, `live`…) | `supervisor wake <name>`, holds the run lock | SIGTERM the agent (`python -m shaft …`, cwd = its folder) |
| a reply (web chat, API, Telegram, `shaft talk`) | API / Telegram service / talk client, holds the lock per reply | same; the reply ends "stopped", the conversation stays open and is written up as usual later |
| a scheduled job | `supervisor job run <name> <plugin>` | SIGTERM its plugin process (`shaft.plugin_runner <plugin>`) |
| a broker `run` command, its Chromium | the run's broker | SIGTERM (cwd in its folder); `browser.sweep` already clears orphans |
| the battery | `supervisor battery run <name>` | SIGTERM *that* process — its designed stop (it cuts the exam and deletes the copy of the instance it ran in; the real folder is never written) |
| a trial replay (scratch copy) | trial worker | the replay's `should_stop` also returns true when the instance is paused → ends like a preemption; the trial stays queued and resumes after `resume` |
| a conversation write-up (`recover`) | `supervisor recover <name>` | SIGTERM its agent; the talk stays unwritten and is written up by the next `recover` |

**Finding the processes** (no new bookkeeping): processes whose cwd is inside `instances/<name>/`
*and* that descend from a SHAFT supervisor process (`python -m supervisor …`). The ancestor rule keeps
the operator's own shells and editors that happen to sit in that folder safe. **Excluded: the gate's
stages** (`shaft.plugin_runner --check`, the test runner) — they are the supervisor checking the run;
killing them would fail the gate and revert the instance's code. Kill waits for them instead.

**Then:** SIGTERM → wait ≤ 15 s → SIGKILL what is left of the targets → wait ≤ `--wait` (default
120 s) for the run lock to be free (settle + gate + commit). Prints what it stopped and whether the
instance is now free, e.g. `killed example-a: stopped live (agent 44815), job news (pid 51200); paused —
shaft resume example-a`. Nothing running → it just pauses and says so. Exit 0 when the instance ends free,
1 when something still holds it after the wait (with what).

Changes: `supervisor/kill.py` (new, ~120 lines: find, signal, wait, report), `__main__.py` (parser +
dispatch), `trial.py` (`should_stop` / `runnable` honour pause — **decision 3**).

## M2 — `shaft delete <name>`

**Refusals** (each says what to do):
- no trace of `<name>` anywhere → "no instance named …";
- anything running (run lock held, a reply waiting/writing, a terminal `shaft talk` open, a job,
  battery or trial replay of it) → "example-a is busy (live since 14:04): shaft kill example-a first";
- no confirmation: on a terminal it asks you to **type the name**; without a terminal it needs `--yes`.

**Order** (each step idempotent, so a delete interrupted half-way is finished by running it again —
it also cleans the leftovers of an instance whose folder is already gone):
1. `paused-<name>` and hold its run lock for the whole delete (no reply or wake can start meanwhile).
2. **Archive** (default; `--no-archive` skips) → `.supervisor/deleted/<name>-<YYYYmmdd-HHMMSS>.tar.gz`:
   the mind store (`minds/<name>.git`, full history), a `git bundle` of `instance/<name>` (its code
   and lineage), its folder without the rebuildable parts (`.venv/ .uv/ .tmp/ __pycache__`; the
   browser profile **is** kept — it holds the sessions of its own accounts), and every per-instance
   file under `.supervisor/`. Sizes today: example-a ≈ 52 MB mind + ~190 MB folder before compression.
   Restoring is manual and documented (untar, `git fetch` the bundle, `git worktree add`, mind store
   back in place) — **decision 2**: a `shaft undelete` is not built now.
3. Stop the Telegram bot: remove `TELEGRAM_TOKEN_<NAME>` from `.env` (line removed, value never read
   out, never archived), its entries in `telegram.json`. The running Telegram service is **not** restarted:
   its bot loop stops by itself once the instance is gone (`telegram.poll`) — a restart from a delete run
   in another checkout would point the launchd service at that checkout.
4. Remove the per-instance `.supervisor/` state and its entries inside shared files (inventory below).
5. `git worktree remove --force instances/<name>`, `git branch -D instance/<name>`, `git worktree prune`.
6. The mind store `minds/<name>.git` last (after the archive is verified readable: `tar -tzf`).
7. Log `deleted instance <name> (archive …)`; print what was removed.

`shaft new <name>` works again afterwards (its mind-store refusal no longer fires).

**Purged too** (decision 4: everything): its rows in `cloud_usage.jsonl`, `coder_usage.jsonl`,
`trial_usage.jsonl`, `doorbell.jsonl` (`instance` is it or `it@trial:…`), `judge_usage.jsonl` (the purpose's last
word is it), `messages.jsonl` (from or to it), the lines of `supervisor.log`, `broker.log`, `trials/broker.log`,
`battery/broker.log` and the panels' action logs whose subject — the word after the timestamp — is it, its
operator-inbox items, its evals, datasets and adapters, its battery items' backup. A line of another subject that
only mentions it is kept, and so is the delete's own record in `supervisor.log`. The spend caps are per instance,
so no other instance's cap moves. Rewriting a ledger other processes append to: read, filter, write a temp file,
and replace only if the file did not change meanwhile (else again), so no append is lost.

**Robustness for a missing instance** (fixed at the source, each with a regression test): a consumer that
finds a name in a shared record must skip it once it is no longer an instance, and **nothing may write
into `instances/<name>/` for a name that is not an instance** (that recreates a half folder which then
blocks `shaft new <name>`):
- `trial.deliver` and `metrics.write_stats` (the detached second judge) — today both recreate the folder;
- the trial worker — a trial of a missing instance is closed as `rejected: instance deleted`, not retried
  forever (today a `testing` one spins the scheduler every 30 s);
- `jobs.tick` pending wakes — dropped (today it recreates `locks/<name>.lock` and spawns a doomed wake);
- the API — open talks of a missing instance are dropped, never written up (today `recover` fails on them);
- Telegram — a running service stops polling a bot whose instance is gone.
Web, top, status, `send_message` and the panels already go through `instance_names()` (checked).

### Per-instance state — what delete does with each

From a sweep of `supervisor/*.py` and `.supervisor/` (SUP = `.supervisor/`). Enumeration is a directory
listing (`core.instance_names`: `instances/*` with a `shaft/`), so once the folder is gone nothing lists
it — the danger is code that *writes into* `instances/<name>/` by name and recreates it (below).

| Where | Kind | Delete |
|---|---|---|
| `refs/heads/instance/<name>`, worktree `instances/<name>`, `.git/worktrees/<admin>` | git | bundled, then `worktree remove --force`, `branch -D`, `worktree prune` (a leftover branch would be resurrected by `shaft setup`) |
| `SUP/minds/<name>.git` | mind store | archived, removed last |
| folder: `self/ memory/ notes/ inbox/ logs/ mail/ attachments/ .jobs/ .browser/ secrets/ config.env drive.md` | files | archived (minus `.venv .uv .tmp __pycache__`), removed with the worktree; Chromium on `.browser/` swept first |
| `SUP/tools/<name>.json` (tool switches, shared-folder path, mail/calendar grants, limits, jobs_off) | file | archived, removed — **the shared folder itself is the operator's and is never touched** |
| `SUP/<name>.sb`, `paused-<name>`, `core-changed-<name>`, `manual-wake-<name>.log`, `talk-history-<name>`, `update-<name>.log`, `backfill-value-<name>.log` | files | archived, removed |
| `SUP/locks/<name>.lock`, `locks/recap-<name>.lock`, `conversations/<name>/`, `replying/<name>/` | files | removed (after the busy check; the lock is released last) |
| `SUP/judgments/<name>.jsonl`, `<name>-second.jsonl`, `<name>-traits.json`, `second-cases/<name>@*` | files | archived, removed (a leftover second case would recreate the folder) |
| `SUP/code_changes/<name>.jsonl`, `probation/<name>.json`, `egress/<name>*.jsonl`, `trials/egress-<name>.jsonl`, `gate-logs/<name>-*.log` | files | archived, removed |
| `SUP/trials/<name>/` (the trials *are* the queue) | dir | archived, removed (a leftover `testing` trial makes the worker retry forever; a queued one recreates the folder on delivery) |
| `SUP/jobs/<name>.jsonl`; `jobs/state.json` keys `jobs.<name>`, `wake.<name>` | file + entries | archived; entries removed under `state.lock` |
| `SUP/schedule.json` `wakes.<name>`, `battery/schedule.json` `<name>`, `recaps.json` `<name>`, `evals/surprise-baseline.json` `instances.<name>` | entries | removed |
| `SUP/battery/off-<name>`, `results/<name>.jsonl`, `items/*/recall-<name>.json`, `<name>.sb`, `<name>-control.sb`, `control/<name>/`, `exam/<name>/` | files | archived, removed |
| `SUP/web/comments/<name>.json`, `web/chats/*.json` with `instance == <name>` | file + files | archived, removed |
| `SUP/telegram.json` `bots.<name>`, `offsets.<name>`, `questions["<name>:…"]`; `.env` `TELEGRAM_TOKEN_<NAME>` | entries | removed (`.env` by line prefix, never read out); Telegram restarted if it served that bot |
| `SUP/api_talks.json` talks with `model == <name>` | entries | left to the API, which owns the file and drops talks of a missing instance at its next idle check (writing it from outside races the running API) |
| `SUP/operator-inbox/*-from-<name>.md`, `read/…`, `attachments/…-from-<name>/` | your inbox | archived, removed |
| `cloud_usage.jsonl`, `coder_usage.jsonl`, `judge_usage.jsonl`, `trial_usage.jsonl`, `messages.jsonl`, `doorbell.jsonl`, the logs | ledgers | its rows archived (`purged/`), removed |
| `SUP/evals/{battery,distinct}-<name>-*`, `breed-*` naming it (read from the record: names hold `-`), `datasets/<name>-*`, `adapters/<name>-*` | records | archived, removed |
| `~/Library/Application Support/SHAFT/battery-backup/*/recall-<name>.json` | backup, outside the repo | archived, removed |
| other instances' `inbox/*-from-<name>.md`, their minds | theirs | never touched |
| `SUP/locks/config-<hash>.lock` | empty, hashed by path | left (harmless) |

Not found anywhere: per-instance launchd jobs, tags, `refs/shaft/*`, remote `instance/*` refs.

Name matching is exact everywhere: `example-a`'s files, rows and lines never take `example-a-2`'s.

## M3 — panels

Both panels already run the CLI subcommand for an action (`actions.py`), so they inherit every
refusal; the web server additionally checks the typed name itself, never trusting the page's JS.

- **Web UI** (`web_actions.instance_buttons`): **Kill** with the other actions (overview cards, the ⋯ menu,
  Summary): runs `shaft kill <name> --wait 20` (the page waits 30 s; "still settling" after that is said, not
  refused). **Delete…** on the Summary page only, off with the reason while it is not idle or paused ("kill it
  first"): you type the name (`data-confirm-type`, a `window.prompt`); the server refuses unless the posted
  `confirm` equals the name and nothing runs for it, then runs `shaft delete <name> --yes` detached (archiving
  takes seconds) and the page goes to the overview (`data-after="home"`).
- **`shaft top`**: `K` kill (y/n, detached: the panel never blocks), `D` delete on the instance tab (an input
  overlay: "Type example-a to delete it:"). Capitals so a stray key never does it.

## M4 — docs and tests

- `docs/cli.md` (both verbs), `docs/web-and-api.md` (the buttons), README command list, code map.
- Tests (`supervisor/tests/test_kill_delete.py`): target selection (ancestor + cwd rule, gate stages
  excluded, the operator's shell excluded); kill on a real sandbox-less dummy process tree; delete
  refusals (busy, reply, talk, no confirmation); delete on a throwaway project in a temp dir —
  afterwards no path under `.supervisor/` and no git ref mentions the name, `shaft new` of the same
  name succeeds; interrupted delete resumed; the missing-instance consumers.

## Verification (real system, throwaway instance only)

`shaft new kdtest` → start a cycle → `shaft kill kdtest` while it runs: journal note "stopped from
outside", gate ran, commit, `paused`; `shaft resume` → `shaft delete kdtest` (typed name) → archive
exists and lists, `git worktree list` / `git branch` / `ls .supervisor` show no `kdtest`, `shaft new
kdtest` works. Same through the web UI in the browser (Kill, then Delete… with the typed name) and
`shaft top`. Never on example-b, example-a, example-c or shaft. Nothing pushed or merged without your go-ahead.

Note: the archive holds the instance's own `secrets/secrets.json` (e.g. Google refresh tokens of *its own*
accounts). Delete prints a reminder to revoke them at the provider if the accounts should die too; it never
calls out to revoke anything itself.

## Decisions (operator, 2026-10-20)

1. Verbs `kill` and `delete`; kill = pause + stop (resume with `shaft resume`).
2. Archive by default to `.supervisor/deleted/`, `--no-archive` to skip; no `undelete` (manual restore in the
   archive's RESTORE.md).
3. A paused instance's trial replays wait, for plain `pause` too; a replay stopped by the pause does not count as a
   preemption (no step towards the fallback model).
4. Purge everything, the shared ledgers and logs included.
