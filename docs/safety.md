# Rules the supervisor enforces

## Operator action classes (#142)

`SHAFT_ACTION_INSTANCES` selects the instances using this model, initially `thelos` only.
Other instances retain their existing broker behavior. Grants still apply on every call.
The classification table lives in `supervisor/actions.py`.

| Class | Existing tools and arguments | Initial behavior |
|---|---|---|
| reversible | Calendar creates without supplied attendees; moves whose current event tells nobody | Direct |
| staged | Calendar creates/moves with supplied attendees; moves with another attendee or organiser, or an incomplete attendee list; every `calendar_respond` | Proposal |
| irreversible | Sending as the operator or deleting. No existing operator broker tool does either | Always a proposal |

Mail and calendar reads keep their current grant checks and do not change class state.
`send_message` is outside the class model for every recipient. It sends as the instance, never as the operator,
and class demotions do not change message delivery.
Instance-owned keys and API calls are outside this operator-account model. There are no operator
mail-send, Notion-write or Docs-write broker tools today. Local notes and drafts stay in the instance's sandbox.
Supplied attendees require a proposal even if the only supplied address is the operator's, preserving the
existing calendar rule. A move's classification uses the current Google event, never just the caller's arguments.

Only the operator decides an over-reach: a recorded action marked with `shaft actions <name> overreach <action-id>`,
or a proposal rejected with the exact reason `over-reach`. A refused call is recorded with mode `refused` and its
reason, without changing class state. This includes disabled tools, absent calendar write grants and refused
class entitlement. Invalid arguments, budgets, stale ETags, Google errors, expiry and ordinary declines also do not
demote a class. One operator-marked incident counts once, including after restore.
An over-reach changes reversible to staged, or staged/irreversible to refused, for that instance's entire class.
`shaft actions <name> restore <class>` restores its initial behavior; irreversible can never become direct.
State and history live in the supervisor's locked, append-only `actions/<name>.jsonl` under `SUP_DIR`.
Class changes apply on the next dispatch; an already claimed dispatch may finish.

All proposals use the existing calendar proposal journal and `/calendar/proposals` approval page.
Approve or reject there or with `shaft actions <name> approve|reject <proposal-id>`.
`--reason over-reach` on CLI rejection, or the page's **Reject as over-reach** button, demotes the class.
Proposals expire after `SHAFT_CALENDAR_PROPOSAL_HOURS` (24). Calendar writes and proposals retain their existing
calendar cap. Approval rechecks class entitlement and write grants. A durable claim prevents duplicate submission.
Demoted reversible calendar actions approved later still use `sendUpdates=none`.
The broker call log includes the class, action id and disposition for classified calls.

## Other enforced rules

- **Sandbox** (macOS `sandbox-exec`): an instance writes inside its own folder,
  except `config.env`, `drive.md` and `shaft-docs/`. It cannot read other files in your home or
  protected operator locations such as per-user temp/cache directories,
  `/private/tmp`, `/Users/Shared`, and `/Volumes`. Narrow temporary-file exceptions
  let `sips` convert images. It connects only to allowed model ports
  (`SHAFT_LLM_PORTS`, default 8780), the embedding service, its broker, and its
  egress proxy. Its environment is an allow-list (`sandbox.ENV_ALLOW`: `PATH`, `HOME`, `USER`, `LANG`, `TERM`,
  `COLUMNS`, `LINES`, `TZ`, `LC_*`) plus what the supervisor sets by name; every other inherited variable,
  `LLM_API_KEY` and any other key or token included, is dropped.
- **Internet** ([PLAN-python-env.md](plans/PLAN-python-env.md)): the sandbox cannot filter by address, so the
  instance's way out is a proxy the supervisor runs for each run (`supervisor/egress.py`; `HTTPS_PROXY` /
  `HTTP_PROXY` are set, and uv, pip, curl, git and Python's HTTP libraries follow them). It connects only to
  public addresses, resolved and checked by the proxy itself, so arbitrary localhost services, your LAN and the tailnet stay out of
  reach; a socket of the instance's own is refused by the sandbox. Every connection is logged in
  `.supervisor/egress/<name>.jsonl` (`record(kind='egress')`, the panel's *internet* row). Trial replays and
  battery exams get a proxy that refuses everything.
- **Its own Python** (`supervisor/pyenv.py`): `.venv/` (and uv's state in `.uv/`) in its folder, made and
  changed by the instance with `uv` in `run`; any Python version, any package. Its plugins (`SHAFT_PYTHON`),
  its tests in the gate and `python3` in `run` use it; the agent process itself stays on the system Python, so a
  broken environment never stops it from waking up to fix it. It declares its choice in `.python-version` and
  `requirements.txt` (code: tested and committed); a missing `.venv/` is rebuilt from them at wake, sandboxed.
  Scratch copies (coder, trials, battery) read the real environment read-only. The panel's *python* row says
  which interpreter it runs on.
  **A real browser** ([PLAN-browser.md](plans/PLAN-browser.md)): a shared Chromium (`.browsers/`, read-only) that only
  `run` and jobs can read, and its own profile `.browser/`, writable there, hidden from the coder and trial copies.
  **One folder of yours it may read** (`shaft share <name> <folder>`, `shaft share <name> off`):
  read-only, in its sandbox and in `run`; its file tools show it as `shared/`. Kept in
  `.supervisor/tools/<name>.json`, not in its `config.env`; a folder overlapping SHAFT's own state is
  refused, and battery exams never get it.
  **Your mail, one label at a time** (`shaft mail <name> grant <alias> <label>`; [PLAN-mail.md](plans/PLAN-mail.md)):
  `mail_search`, `mail_read`, `mail_attachment`, offered only when granted. The credential and the grant stay with
  the supervisor, which enforces the label on every call (see [integrations](integrations.md)).
  **Your calendars, one per grant** (`shaft calendar <name> grant <alias> <calendar>`;
  [PLAN-calendar.md](plans/PLAN-calendar.md)): `calendar_events`, `calendar_event`, offered only when granted. Every call
  names a granted calendar; the credential and the grant stay with the supervisor.
  **Calendar writes** ([calendar write plan](../docs/plans/PLAN-calendar-write.md)): `shaft calendar thelos grant-write
  <alias> <calendar>` grants one instance one calendar; write implies read, and `revoke-write` keeps read access.
  `calendar_create` and `calendar_move` run at once with `sendUpdates=none` when no attendees are supplied and,
  for a move, the event has no attendees other than the operator and no other organiser is identified.
  An incomplete attendee list also requires approval. Changes that would tell someone else, supplied attendees, invitations,
  and every `calendar_respond` RSVP become proposals. Only the operator approves or declines in the web UI at
  `/calendar/proposals`, through the tailnet and paired-browser checks with CSRF protection.
  Approval re-checks the write grant and sends the frozen request with `sendUpdates=all`; moves and RSVPs use the
  saved ETag, so a changed event fails without a fresh read and retry. A durable claim prevents double approval
  or a crash from submitting the proposal again. Decline and expiry send no write; `SHAFT_CALENDAR_PROPOSAL_HOURS`
  defaults to 24. Outcomes go to the instance's inbox; delivery failures are logged without undoing the decision.
  `SHAFT_CALENDAR_WRITES_PER_DAY` defaults to 20 per instance, counting submitted write attempts and proposals.
  Attempts and proposal outcomes are audited in `.supervisor/calendar/<instance>.jsonl`, available through
  `record(kind="calendar")`. Writes are refused in trial replays. No delete, cancel, or whole-series changes;
  only one occurrence at a time.
- **Broker**: privileged tools and credentials, including web search (ScrapeBadger), page fetching
  (public addresses only, re-checked on every redirect; `fetch_page` and `http_request` connect to the very
  address they checked, with TLS still verifying the name asked for, so a name that answers public for the
  check and private for the connection reaches nothing), messages between instances and to
  you (no daily limit unless `SHAFT_OPERATOR_PER_DAY`; a macOS notification, and a push on the web UI or Telegram,
  whichever reaches you for that instance: integrations.md), with
  files attached from the sender's folder (up to 10, 20 MB each, 50 MB in all; never `secrets/`, `.git/`, `logs/` or
  a file reached through a link); another instance finds them in its `attachments/` (not
  committed), the operator's copy is in `.supervisor/operator-inbox/attachments/`; and **`delegate_coding`**: an
  Opus-class model (`SHAFT_CODER_MODEL`, default `claude-opus-5-5`) reads the instance's code
  read-only and writes the change; the supervisor tests it on a scratch copy and applies it only if
  the gate passes; code only, never `self/`, `memory/` or `config.env`; max 2 per cycle and
  `SHAFT_CODER_DAILY_USD` (5) per instance per day; commits and `self/stats.md` record what was
  delegated. **`run`**: shell commands (`zsh`) in a stricter sandbox; writes only to `notes/`,
  `plugins/`, `skills/`, `tests/`, `shaft/`, `.tmp/`, `.venv/`, `.uv/`, `.browser/`, `.python-version` and
  `requirements.txt`, the internet only through the egress proxy, reads only its own folder (and the shared
  Chromium);
  60 s default time limit (the whole process group is killed; at the end of every command, whatever it left
  running too), 1 GiB file-size limit (a file past it ends the
  command with SIGXFSZ, and `run` says so), output capped,
  40 commands per run. So an instance can build and test its own tools and plugins. **`history`**: its
  own git log and diffs, each entry tagged `[code]` (its branch) or `[mind]` (its mind store); git cannot
  run inside its sandbox; with `save_as`, one file as it was after a commit, written whole to a new path in its
  folder (never over an existing file), so it can bring back a record a bad write damaged without the operator. **`record`**: the supervisor's own record about it; the judge's verdicts with reasoning, its
  runs, its tool calls, its kept code changes, its internet connections; so it can check its claims about
  itself.
  **`decide`**: TypeSafe's Jev decision model through OpenRouter, on the supervisor's key; a `state` and typed
  questions (`noul` yes/no, `choice`, `score`) answered with probabilities, no prose; `SHAFT_DECISION_MODELS`
  (`typesafe/jev-1.13`, `~typesafe/jev-latest`), the same daily cloud cap and usage log as the chat models, and the
  data policy `SHAFT_DECISION_PROVIDER` (no data collection, zero retention, no fallback); off in battery exams,
  shared by a trial's pair like the other reads.
  **`http_request`**: call web APIs (any method, headers, body) at any public address; `body_file` sends a file
  of its folder as the body byte for byte and `save_to` writes the answer to a new file of its folder (2xx only,
  whole or not at all; 50 MB each way), for files that are not text. **Its own
  keys** (`secret_set` / `secret_list` / `secret_delete`, or `shaft secret <name> set <key> --hosts …` from
  your terminal): kept in its folder under `secrets/` but handled only by the supervisor; the broker adds a
  key to requests to the hosts it is bound to, never through a redirect, and masks it in responses; the
  instance's own process, plugins, `run` and the coder cannot read the file, git never sees it. A change is made
  under a lock and written whole (a new 0600 file renamed over the old one); a store that cannot be read is never
  written over — the change is refused, naming the file and the error, and the keys go unused until you repair it
  (`KEY STORE UNREADABLE` in `.supervisor/supervisor.log`). A **Google key**
  (`shaft secret <name> google`, the instance's own Google account) is refreshed by the supervisor per run.
  **`set_config`**: the instance's only way to change its `config.env`, one value at a time; each change is
  recorded, so the gate can tell its lines from yours; you may edit the file at any time, even mid-run.
  Per-run token, call budget
  (`SHAFT_BROKER_MAX_CALLS`, default 60), everything logged in `.supervisor/broker.log`.
- **No link followed in an instance's folder**: the supervisor runs outside the sandbox, and an instance can put a
  link anywhere in its own folder. So every file the supervisor reads there — to check it, copy it, show it to you
  or send it to a judge, the coder or a vision model — and every file it writes, appends to or removes there is
  opened one folder at a time through descriptors, none followed if it is a link (`supervisor/core.py`:
  `open_beneath`, `safe_read`, `safe_write`, `walk_beneath`, `copy_beneath`); a pipe in a file's place is refused,
  never waited on. Two link policies:
  - **What goes out** (an attachment, an image for `describe_image`, a file the coder reads, a judge's evidence):
    a link anywhere on the way is refused, even one that stays inside the folder. To that reader a link is no file.
  - **A copy** (a battery exam's, a gate's scratch copy): a link that stays inside the copy — relative, its `..`
    first and no more of them than its folder is deep — is copied as the link it is, so the copy runs the code the
    instance runs; any other link is left out. The supervisor reads no link while copying; the copy's folders are
    made and its files created through descriptors (`O_EXCL`, `O_NOFOLLOW`), so a folder of the copy swapped for a
    link is refused, never written through.
  - **Code** (`shaft/`, `tests/`, `plugins/`, `.python-version`, `requirements.txt`, as the gate sees them): a link
    is an entry of the gate's snapshot (its target, never what it points to), and so is anything else that is not a
    plain file of one name — a file with more than one name (a hard link), a pipe, a socket, a device. One added,
    removed or changed is a change, and a change with any of them in a code path fails the gate untested (the
    template has none). The gate's restore puts back each file as a new file renamed over whatever stands there — a
    link, a pipe, one name of a hard-linked file — never writing through it, makes a folder replaced by a link again
    through descriptors, and puts an approved file back where a link stood; every path it put back is read again, and
    if any is not as approved the run is refused ("run refused", an alert on the panel), so nothing runs on it.

  Its keys, git and logs (`secrets/`, `.git`, `logs/`) are never sent out by any spelling: the volume is
  case-insensitive (`Secrets/` is `secrets/`), so the supervisor compares the folder itself (device and inode) and
  the name case-folded. The sandbox's own deny on `secrets/` holds for every spelling (checked: `cat Secrets/…` from
  inside the sandbox is refused). Reads are bounded: one file at `SHAFT_FILE_READ_MAX_MB`, the gate's code snapshot
  at `SHAFT_SNAPSHOT_MAX_MB`, a copy at `SHAFT_COPY_MAX_GB`, any walk at `SHAFT_TREE_MAX_FILES` files and folders and
  `SHAFT_TREE_MAX_DEPTH` deep. Past a bound the read fails, never returns part: code too big to snapshot fails the
  gate closed (nothing of the run's code is kept, the files it began with go back, a journal note says why).
- **Append-only journal**: if a run rewrote history instead of appending, the journal is
  restored and the rewrite kept aside. A journal replaced by a link (or anything that is not a file) is moved aside
  unread and the journal restored.
- **Slow core**: the core personality may change at most every `SHAFT_CORE_MIN_HOURS` (72).
- **Test gate**: if a run changed the instance's code, plugins or (through `set_config`) config, the
  invariant tests and plugin checks run; failure restores the old code and the config lines the instance
  set, and tells the instance why. The supervisor's own tests run from its last commit (and the template's), in a
  clean copy under `.supervisor/gate-tests/`, never from the checkout: an edit someone is still making there cannot
  decide a verdict. They run in two parts: modules that read the instance (`test_invariants`, modules naming
  `SHAFT_UNDER_TEST`, and their direct or indirect importers) at every gate; the rest ("shared") once per commit pair
  while a pass is fresh. A module the scan cannot parse also runs at every gate. The shared pass is recorded beside
  the tree under `.supervisor/gate-tests/<commits>/`, one file per key (commits, interpreter on disk, `SHAFT_*` settings
  except `SHAFT_UNDER_TEST`), reused for up to a day. Only passes with tests run are recorded, never failures; a shared
  run that fails or times out deletes that key's record. A lock allows one shared run per commit pair at a time; its wait
  is bounded by the stage timeout and fails closed. Each part passes only with exit 0 and its runner's successful
  report carrying the token given on stdin. The shared part sets `SHAFT_UNDER_TEST` to an unreadable folder and blocks
  instance-part modules from import, so reaching the instance through a route the split missed fails it. `load_tests`
  hooks fail their part. A test that hides the variable's name AND skips when the folder is unreadable can escape this;
  that residual case is accepted because supervisor tests are trusted committed code instances cannot edit. The
  unpaused scheduler warms the shared part after each merge (`shaft gate warm`), refreshing passes older than half a
  day on its hourly check. A warm-up failure is one panel alert, not retried on those commits during that scheduler run.
  A failed gate is classified (`gate.Output`, docs/plans/PLAN-thelos-review-2.md M2), and either way the change is
  reverted: the classification never lets a change through. **Baseline** — the supervisor's side: the shared part
  failing or running out of time (it never reads the instance), the gate's own error, or a failed protected copy (not
  one too big); and the supervisor's instance-part tests when they are the only check of the change that failed and
  fail again, run once more against a protected copy of the approved code. **Candidate** — anything else: the instance
  part failing only with the change, the plugin check, the instance's own tests, a stage of those out of time, a link
  where `.tmp` belongs. A candidate failure wins over a baseline one. On a baseline failure the change's code files are
  also **held** under `.supervisor/gate-held/<name>/<id>/` (the bytes it wrote, each file's base digest, the
  instance's commit, a patch to read; the instance cannot write there), and the instance is told in its inbox and
  journal; config lines it set are undone and not held. After a green warm-up the scheduler starts `shaft gate regate`:
  for each instance not busy or paused, under its run lock (never waiting), each held change is written back only if
  every file it touched is still as it was when held — else dropped with a message carrying its patch — and settled
  like any run's change, through the same gate: kept and committed, reverted and dropped if a check of it fails, or
  held again if the baseline is red again, up to three tries. Every outcome is an inbox message. Every gate an
  instance's change went through is a row of `.supervisor/gate-runs/<name>.jsonl` (outcome, failing tests, the last
  3,000 characters of a failed gate's output), which `record(kind="gate")` shows to that instance only, with its held
  changes.
  The plugin check and the instance's own tests run in its sandbox with no network at
  all, like `run` without its proxy. A check that runs past its time counts as failed, and so does any error of the
  gate's own (it never lets a change through unjudged). Each run is settled whatever happens to it: the agent
  and everything it started run in a process group of their own, ended (SIGTERM, then SIGKILL) before the gate
  looks, on every way out — a normal end, a failure, a timeout, Ctrl-C or the supervisor being stopped; the same
  holds for each `run` command, so nothing it puts in the background outlives it. A process that left the group
  (`setsid`) is found by its working folder: once its parent is gone, anything still working in the instance's
  folder is ended too (your own shells there keep their parent and are spared). A battery exam's copy, a random
  folder nobody else works in, is swept harder: every process working in it, everything below each, and each one's
  ancestors while they share its session or process group (a parent that left the copy while its child stayed). One
  that left the instance's folder is not found; the sandbox is the boundary then (it can write only the instance's folder, and what it writes there
  later is caught as below). A kept change is committed with exactly the bytes the gate checked — written into
  git's object store and a separate index, never read again from the working tree — and if the file on disk differs
  from them by then, it is put back to them (logged as FAILED, with a journal note); the approved-code record is
  made from the same bytes. **Protected code (#86)**: the supervisor materializes the approved start-of-run
  snapshot under `.supervisor/protected/<name>/<pid>-<random>/<name>/`. It copies exactly the code paths
  `gate.snapshot` covers, from the bytes already read, within the snapshot's tree bounds and `SHAFT_COPY_MAX_GB`.
  Code files are mode 444 and directories mode 555; the sandbox allows reading this copy and denies all writes
  to it, including chmod and renames. The launched agent uses isolated Python with this copy first on its import
  path, excluding the working directory. Its cwd and `SHAFT_ROOT` still point at its instance folder, where its
  mind, notes and working edits remain writable. Broker `run` and plugin execution still use the working folder,
  so a plugin written during a run can be tested before settlement. At settlement a separate protected copy of
  the gate's snapshot supplies both the supervisor invariants and the sandboxed plugin and instance tests;
  of the copy only its `.tmp/` is writable. The copy also holds `config.env` for the trusted tests that read it, and
  reads the instance's Python environment in place. Both copies link every other top-level entry of the instance
  folder back to it (`protected._link_data`), never a code path: code that finds its data from its own file
  (wren's `Path(__file__).parent.parent / "notes/…"`) found nothing in a copy of the code alone, and on 2026-10-29
  every change wren made was reverted for it. The copy leads the import path, so an entry that could supply an
  import — a module file (`unittest.py` would replace the gate's runner) or a folder with an `__init__` — is never
  linked; a plain folder can only be a namespace portion, which never shadows a module. The gate's tests run in the
  instance's own sandbox, as a launch does: they read and write its folder as they did before #86, never its code
  paths in either place nor its Python environment. The commit and approved-code record use the same snapshot bytes.
  This closes both check-to-use windows: a working file changed after reconciliation cannot supply a launched
  import, and a file swapped after the gate's snapshot cannot supply its tests. Copies are removed after the
  run or gate, including on exceptions; copies left by dead supervisor processes are swept before making the
  next copy for that instance. Quiet conversations retain their launch copy; trial replays and battery exams
  retain their own copies. Every check reads through descriptors, never through a link.
  Stopping a service that starts runs (the API, the scheduler, a terminal
  conversation) ends its runs' processes first. **Approved code**: after every settlement the supervisor records
  the code it approved (`.supervisor/baselines/<name>.json`: the last commit, plus what the gate passed that git
  does not hold — an ignored or untracked file in a code path), where the instance cannot write. Before any run,
  whatever is not the approved code (an earlier run that ended before it was checked, a file edited from outside)
  is gated first: kept, or put back as approved, the rejected files copied to `.supervisor/rejected/<name>-<time>/`
  and named in the instance's journal and the log. A merge, rebase, cherry-pick or revert in progress in the
  instance's worktree, or code that cannot be compared, refuses the run instead ("run refused" in
  `.supervisor/supervisor.log`, an alert on the panel): finish or abort the merge and the next run checks it. Until an
  instance's first settlement under this scheme, its last commit is the approved code. An edit of yours to
  `config.env` is never reverted or credited to the
  instance: it is committed on its own ("config.env changed from outside the instance"); so is an edit of yours to
  `drive.md` ("drive.md changed by the operator"), with a note in the instance's journal that its drive changed.
- **Trials** (`trial` tool, [PLAN-trials.md](plans/PLAN-trials.md)): an instance can test a change to its own
  code or prompts before keeping it. The supervisor runs the gate on it, rebuilds N (default 6) of its past
  autonomous cycles exactly as each began (its branch at that moment, the inbox it read), and runs each twice, once with
  current code and once with proposed code, in scratch copies under the same sandbox, through a replay broker that
  sends nothing (messages, writes, keys are checked and recorded, not carried out; web reads are real and
  shared by both runs; the replay's own network is refused and traced; no coder). A judge not told which run is which compares each pair, effectiveness first,
  in both orders; a sign test (α 0.1) gives better / worse / no clear difference, sent to the instance's inbox.
  Nothing is applied: the instance adopts the change itself, and if it makes the trialled edit (the same lines
  removed and added, even if the rest of the file moved on) its probation knows the verdict. On the local model a trial uses only idle time (no run, conversation, wake or
  sleep due within 15 min; cut off 90 s before one) and otherwise runs on `SHAFT_TRIAL_FALLBACK_MODEL`
  (mimo); at once for an instance waking more often than every 30 min, else after `SHAFT_TRIAL_LOCAL_WAIT_H`
  (6) or two preemptions. Caps per instance per day: `SHAFT_TRIALS_PER_DAY` (2), `SHAFT_TRIAL_DAILY_USD` (3),
  `SHAFT_TRIAL_LOCAL_UNITS_PER_DAY` (24); `SHAFT_TRIAL_N_MAX` (10). Ledger: `.supervisor/trial_usage.jsonl`;
  trials: `.supervisor/trials/<name>/<id>/`.
- **Probation**: a kept self-change is undone if the achieved difficulty per cycle (success ×
  judged difficulty) over the next `SHAFT_PROBATION_CYCLES` (6) cycles *that used it* (a plugin only
  when the cycle called it; tests never) falls clearly (0.15 on a 0 to 1 scale) below the level before
  it, and a permutation test says chance alone gives such a drop under 5% of the time. Undoing
  restores exactly the change's files from git (as they were before it) and commits that as the
  supervisor; memory, journal and self are never touched. A restore that cannot be done (a file was
  changed again since, or the tests fail with the earlier version) stays on probation and is logged.
  A change adopted against a trial's 'worse' verdict is undone on any drop, without the chance test.
- **Judge**: one autonomous cycle in `SHAFT_JUDGE_EVERY` (default 2, drawn after the run, so the instance cannot
  tell which) is graded from its tool log by a cloud model picked at random from a rotation (`SHAFT_JUDGE_MODELS`,
  default MiMo and GLM 5.3 Flash), never the model the judged instance runs on; each verdict
  records its judge, and a cycle left out has no verdict rather than a failing one. The second judge's model
  (`SHAFT_JUDGE2_MODEL`, DeepSeek V4.1 Flash) stays out of the main rotation, so its cross-check is independent; a
  pool that still lists it uses it only when no other judge may grade, and that cycle gets no second judgment. Claude models go straight to the Claude API; OpenRouter ones under the instances' data policy (no data
  collection, zero retention). A trial pair keeps
  one judge for both orders; a battery item one grader for both arms, and a battery era is the whole pool. Results
  feed `self/stats.md`.
- **Git**: after every run the instance's mind is committed to its mind store and its skills and kept
  code to its branch, with the same message (author `SHAFT/<name>`). The branch git calls use the main
  repo's worktree registry with hooks off; the mind store's run with hooks, fsmonitor, the user's git
  config and the instance's own `.gitattributes` off, and add with `-f` (its `.gitignore` files cannot hide
  part of its mind); so an instance cannot redirect the supervisor's git calls. A mind path is never
  staged on the branch. Trial replays read the mind as of the replayed cycle's start from the store.
- **Budgets**: one run per instance at a time; a cycle is stopped after
  `SHAFT_CYCLE_TIMEOUT_S` (2400 s), a sleep after `SHAFT_SLEEP_TIMEOUT_S` (5400 s). Runs know
  their deadline and wrap up with a report before it; a stopped run leaves a journal note. A cycle and a sleep
  also see their *operational envelope* before they hit a limit: calls left this run, minutes left, today's
  cloud, coder and trial allowances, any probation window (cycles used / needed) and the next sleep — computed by
  the supervisor (the broker's `GET /envelope`, not a tool and not counted), shown in the first prompt and, calls
  and minutes, after every step. It carries no keys and no host paths. Replays use the same eight keys
  (`calls_used`, `calls_max`, `cloud`, `coder`, `trials`, `probation`, `next_sleep`, `next_wake`), with no replay
  flag. Spend, trial starts and probation counts stop before the replayed cycle's start; the schedule is computed
  from that start. [Closed probation entries cannot be reconstructed](plans/PLAN-trials.md#8-known-fidelity-gaps-equal-for-both-arms-so-the-comparison-stays-fair).
- **Your conversation comes first**: `shaft talk` asks other instances' running cycles to wrap
  up at their next turn when they use the same local model server (an instance on a cloud model shares
  nothing and is left alone, in either direction). An instance's running cycle is paused only while a reply
  to you is being written (terminal, API or Telegram) and resumes as soon as it is done, even with the
  conversation still open; an open but quiet conversation does not stop its cycles from starting. A conversation holds its instance only while it replies, so you can chat in the terminal and
  through the API at the same time. Open conversations are never written up early; a conversation
  picked up again after its write-up gets a further entry. Ctrl-C in a conversation ends it cleanly
  (the instance still writes its journal entry).
- **Failure reporting**: a run that ends in an error leaves a journal note with what it printed
  last; a nightly sleep skipped because the instance is paused is noted too. Every instance's context
  says when it last slept and what came of it, even after the entry scrolled out of its recent journal.
- **Updates**: template fixes reach an instance only through `shaft update`, under its run lock
  and the test gate; never mid-run.

## Scope of these controls

These are code-enforced restrictions, not a claim that arbitrary self-written code is
safe. The sandbox has explicit local service exceptions; the public egress proxy
itself rejects all non-public destination addresses. Data an instance can read may
be sent to public services. Provider routing requests are not an independent audit
of a provider's retention practices. See [SECURITY.md](../SECURITY.md).

The instance cloud cap does not cover all supervisor spending. Coding and trials
have separate caps; judging has an alert threshold, not a hard cap.
Sources: [cloud cap](../supervisor/cloudllm.py),
[coder cap](../supervisor/coder.py), [trial cap](../supervisor/trial.py),
[judge alert](../supervisor/panel.py).

| Rule | Implementation |
|---|---|
| Filesystem, local ports, clean environment | [supervisor/sandbox.py](../supervisor/sandbox.py), [runner.py](../supervisor/runner.py) |
| Public egress addresses | [supervisor/egress.py](../supervisor/egress.py) |
| Instance Python | [supervisor/pyenv.py](../supervisor/pyenv.py) |
| Broker tools and quotas | [supervisor/broker.py](../supervisor/broker.py) |
| Journal and personality | [supervisor/runner.py](../supervisor/runner.py), [metrics.py](../supervisor/metrics.py) |
| Gate and config restoration | [supervisor/gate.py](../supervisor/gate.py), [gate.py](../supervisor/gate.py) |
| Trial replay and comparison | [supervisor/trial.py](../supervisor/trial.py) |
| Probation | [supervisor/metrics.py](../supervisor/metrics.py) |
| Judge rotation | [supervisor/judge.py](../supervisor/judge.py) |
| Private mind commits and guards | [supervisor/mind.py](../supervisor/mind.py) |
| Run deadlines and conversation priority | [supervisor/runner.py](../supervisor/runner.py) |
| Template updates | [supervisor/__main__.py](../supervisor/__main__.py) |
