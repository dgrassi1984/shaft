# Rules the supervisor enforces

## Operator action classes

`SHAFT_ACTION_INSTANCES` selects the instances using this model. It is empty by default.
Other instances retain their existing broker behavior. Grants still apply on every call.
The classification table lives in `supervisor/actions.py`.

| Class | Existing tools and arguments | Initial behavior |
|---|---|---|
| reversible | Calendar creates without supplied attendees; moves whose current event tells nobody; `mail_draft` and `mail_draft_update` (a draft in your mailbox, never sent) | Direct |
| staged | Calendar creates/moves with supplied attendees; moves with another attendee or organiser, or an incomplete attendee list; every `calendar_respond` | Proposal |
| irreversible | Sending as the operator or deleting. No existing operator broker tool does either | Always a proposal |

Mail and calendar reads keep their current grant checks and do not change class state.
`send_message` is outside the class model for every recipient. It sends as the instance, never as the operator,
and class demotions do not change message delivery.
Instance-owned keys and API calls are outside this operator-account model. There are no operator
mail-send, Notion-write, or Docs-write broker tools today. Operator mail drafts are written to your mailbox and
never sent; you send them from Gmail.
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

## Sandbox

The macOS `sandbox-exec` profile ensures that an instance writes inside its own folder,
except `config.env`, `drive.md` and `shaft-docs/`. It cannot read other files in your home or
protected operator locations such as per-user temp/cache directories,
`/private/tmp`, `/Users/Shared`, and `/Volumes`. Narrow temporary-file exceptions
let `sips` convert images. If `getconf DARWIN_USER_TEMP_DIR` returns no usable path,
the sandbox fails closed and the run refuses to start. It connects only to allowed model ports
(`SHAFT_LLM_PORTS`, default 8780), the embedding service, its broker, and its
egress proxy. Its environment is an allow-list (`sandbox.ENV_ALLOW`: `PATH`, `HOME`, `USER`, `LANG`, `TERM`,
`COLUMNS`, `LINES`, `TZ`, `LC_*`) plus what the supervisor sets by name; every other inherited variable,
`LLM_API_KEY` and any other key or token included, is dropped.

## Internet

The sandbox cannot filter by address, so the
instance's way out is a proxy the supervisor runs for each run (`supervisor/egress.py`; `HTTPS_PROXY` /
`HTTP_PROXY` are set, and uv, pip, curl, git and Python's HTTP libraries follow them). It connects only to
public addresses, resolved and checked by the proxy itself, so arbitrary localhost services, your LAN and the tailnet stay out of
reach through that proxy; a socket of the instance's own is refused by the sandbox. Every connection is logged in
`.supervisor/egress/<name>.jsonl` (`record(kind='egress')`, the panel's *internet* row). Trial replays and
battery exams get a proxy that refuses everything.

`SHAFT_TAILNET_GRANTS` is a separate exception for the broker's `http_request`.
It permits only the granted host:port pairs for that instance, with one of the instance's
host-bound keys supplied through `auth`. The egress proxy and `fetch_page` still refuse
private destinations. Redirects do not carry this grant or forward the key.
See `supervisor/broker.py`, `Broker._http_request` and `Broker._send`.

## Its own Python

`supervisor/pyenv.py` manages `.venv/` (and uv's state in `.uv/`) in its folder, made and
changed by the instance with `uv` in `run`; any Python version, any package. Its plugins (`SHAFT_PYTHON`),
its tests in the gate and `python3` in `run` use it; the agent process itself stays on the system Python, so a
broken environment never stops it from waking up to fix it. It declares its choice in `.python-version` and
`requirements.txt` (code: tested and committed); a missing `.venv/` is rebuilt from them at wake, sandboxed.
Scratch copies (coder, trials, battery) read the real environment read-only. The panel's *python* row says
which interpreter it runs on.

### Browser

The shared Chromium (`.browsers/`) is read-only and available to `run` and jobs.
The instance writes its profile under `.browser/`, hidden from coder and trial copies.

### Shared folder

`shaft share <name> <folder>` grants a read-only folder in the sandbox and in `run`.
`shaft share <name> off` removes it. File tools show the folder as `shared/`. The grant is kept in
`.supervisor/tools/<name>.json`, not in its `config.env`; a folder overlapping SHAFT's own state is
refused, and battery exams never get it.

### Mail grants

`shaft mail <name> grant <alias> <label>` enables `mail_search`, `mail_read`, and
`mail_attachment` for a label. The credential and the grant stay with
the supervisor, which enforces the label on every call (see [integrations](integrations.md)).
`shaft mail <name> grant-draft <alias> <label>` adds `mail_draft` and `mail_draft_update`: the instance picks which
drafted account a draft is written from, and replies in a thread only on a label with the draft flag. Nothing is sent.
A draft can carry labels, only ones granted to the instance for drafts on that account.

### Drive grants

`shaft gdrive <name> grant <alias> [--all]` enables `drive_search`, `drive_read`, and
`drive_download`. The supervisor holds the whole-Drive read credential and checks the grant and reach
on each file. `own` includes owned files, member Shared Drives, and same-domain files only when
Drive's `about` reports `canCreateDrives=true` for the account. A Workspace that blocks Shared Drive creation
shows no same-domain files under `own` unless they qualify as owned files or files in a member Shared Drive;
`--all` includes external shares. Both exclude trash. Revoke deletes the account's downloaded copies under
`gdrive/` and reports any deletion failure. Memories remain. See [integrations](integrations.md),
`supervisor/broker.py`, `Broker.gdrive_grants` and `Broker._drive_read`, `supervisor/gdrive.py`, `visible`, and `supervisor/__main__.py`, `cmd_gdrive`.

### Google Chat grants

`shaft gchat <name> grant <alias>` enables `gchat_search`, `gchat_read`, and `gchat_download`
(plan). Access covers every conversation on the account, read-only,
with no hide list. The supervisor holds the credential and checks the grant on every call. Pages and watch
messages mark content as written by its senders, not as the operator's instructions. This marking cannot prevent
prompt injection. Revoke deletes the account's downloaded copies under `gchat/` and reports deletion failures;
memories remain. Content reaches the instance's model and may reach judges. See
[integrations](integrations.md#the-operators-google-chat-shaft-gchat), `supervisor/broker.py`,
`Broker.gchat_grants`, `Broker._gchat_read`, `supervisor/gchat.py`, `marking`, and
`supervisor/__main__.py`, `cmd_gchat`.

### Calendar grants

`shaft calendar <name> grant <alias> <calendar>` enables `calendar_events` and
`calendar_event`. Every call
names a granted calendar; the credential and the grant stay with the supervisor.

### Calendar writes

`shaft calendar <name> grant-write <alias> <calendar>` grants writes to one calendar
(calendar write plan); write implies read, and `revoke-write` keeps read access.
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

## Broker

### Tools and delivery

The broker holds privileged tools and credentials, including web search (ScrapeBadger), page fetching
(public destinations are re-checked on every redirect; `fetch_page` and public `http_request` connect to the very
address they checked, with TLS still verifying the name asked for, so a name that answers public for the
check and private for the connection reaches nothing), messages between instances and to
you (no daily limit unless `SHAFT_OPERATOR_PER_DAY`; a macOS notification, and a push on the web UI or Telegram,
whichever reaches you for that instance: integrations.md), with
files attached from the sender's folder (up to 10, 20 MB each, 50 MB in all; never `secrets/`, `.git/`, `logs/` or
a file reached through a link); another instance finds them in its `attachments/` (not
committed), the operator's copy is in `.supervisor/operator-inbox/attachments/`.
`http_request` also supports the explicit [tailnet grant exception](#sandbox).

### Coding delegation

`delegate_coding`: an
Opus-class model (`SHAFT_CODER_MODEL`, default `claude-opus-5-5`; any provider the supervisor calls:
claude-…, openrouter/…, a priced custom/… or a codex/… plan model) reads the instance's code
read-only and writes the change; the supervisor tests it on a scratch copy and applies it only if
the gate passes; code only, never `self/`, `memory/` or `config.env`; max 2 per cycle and
`SHAFT_CODER_DAILY_USD` (5) per instance per day. Admission blocks new tasks once
recorded daily spend reaches the allowance. An OpenRouter reply without a cost is booked at the dearest
qualifying provider's catalogue price; with no price to be had, at the rest of the day's allowance. An admitted task can make up to 16 model calls
and can exceed that allowance (`supervisor/coder.py`, `MAX_TURNS`, `write`).
Commits and `self/stats.md` record what was delegated.

### Shell commands

`run`: shell commands (`zsh`) in a stricter sandbox; writes only to `notes/`,
`plugins/`, `skills/`, `tests/`, `shaft/`, `.tmp/`, `.venv/`, `.uv/`, `.browser/`, `.python-version` and
`requirements.txt`, the internet only through the egress proxy, reads its own folder, the shared Chromium, and any granted shared folder. System runtime reads
are also allowed by the sandbox profile.
The default time limit is 60 s. The supervisor ends the process group after every command;
processes that escape it are subject to the [cleanup discovery limits](#process-cleanup-and-settlement). There is a 1 GiB file-size limit (a file past it ends the
command with SIGXFSZ, and `run` says so), output capped,
40 commands per run. So an instance can build and test its own tools and plugins.

### History and supervisor records

`history`: its
own git log and diffs, each entry tagged `[code]` (its branch) or `[mind]` (its mind store).
The broker reads branch and mind-store metadata on the supervisor side. Sandbox filesystem restrictions
limit direct access; the profile does not prohibit executing the git binary itself. With `save_as`, it restores one file as it was after a commit, written whole to a new path in its
folder (never over an existing file), so it can bring back a record a bad write damaged without the operator. **`record`**: the supervisor's own record about it; the judge's verdicts with reasoning, its
runs, its tool calls, its kept code changes, its internet connections; so it can check its claims about
itself. `broker.LEDGER_KINDS` also exposes `families` for judged goal chains, `habits` for repeated specific calls
and sequences (`habits.MIN_CYCLES=3`), `usage` for per-plugin calls and per-skill-file reads from live tool logs,
`facts` for current facts with checked sources and later restatements, `predictions` for reports and scores,
and `unrecorded_orders` for future orders found in ended conversations. Scheduled job runs are not cycle calls.
`facts` uses `offset` paging. These records are readings, not instructions or gates.

### Decision model

`decide`: TypeSafe's Jev decision model through OpenRouter, on the supervisor's key; a `state` and typed
questions (`noul` yes/no, `choice`, `score`) answered with probabilities, no prose; `SHAFT_DECISION_MODELS`
(`typesafe/jev-1.13`, `~typesafe/jev-latest`), the same daily cloud cap and usage log as the chat models, and the
data policy `SHAFT_DECISION_PROVIDER` (no data collection, zero retention, no fallback); off in battery exams,
shared by a trial's pair like the other reads.

### HTTP requests and instance keys

`http_request`: call web APIs (any method, headers, body) at public addresses, or granted tailnet
host:port pairs with host-bound authentication; `body_file` sends a file
of its folder as the body byte for byte and `save_to` writes the answer to a new file of its folder (2xx only,
whole or not at all; 50 MB each way), for files that are not text. **Its own
keys** (`secret_set` / `secret_list` / `secret_delete`, or `shaft secret <name> set <key> --hosts …` from
your terminal): kept in its folder under `secrets/` but handled only by the supervisor; the broker adds a
key to requests to the hosts it is bound to, never through a redirect, and masks it in responses; the
instance's own process, plugins, `run` and the coder cannot read the file, git never sees it. A change is made
under a lock and written whole (a new 0600 file renamed over the old one); a store that cannot be read is never
written over, the change is refused, naming the file and the error, and the keys go unused until you repair it
(`KEY STORE UNREADABLE` in `.supervisor/supervisor.log`). A **Google key**
(`shaft secret <name> google`, the instance's own Google account) is refreshed by the supervisor per run.

### Configuration and call budget

`set_config`: the instance's only way to change its `config.env`, one value at a time; each change is
recorded, so the gate can tell its lines from yours; you may edit the file at any time, even mid-run.
Per-run token, call budget
(`SHAFT_BROKER_MAX_CALLS`, default 60), everything logged in `.supervisor/broker.log`.

### Tool quotas

`supervisor/broker.py`, `Broker.call`, checks these limits in addition to the total
broker call budget. Mail, Drive, and Google Chat continuation pages do not consume another tool-specific
call allowance.

| Resource | Enforced limit |
|---|---|
| Messages | 10 per run, plus `SHAFT_OPERATOR_PER_DAY` for operator messages when nonzero. |
| Mail | 30 calls and 100 MiB of attachments per run; at most 5 of the calls are drafts. |
| Drive | 30 calls and 100 MiB of downloads per run. |
| Google Chat | 30 calls and 100 MiB of downloads per run; 25,000,000 bytes per uploaded file. |
| Calendar | 30 calls per run; `SHAFT_CALENDAR_WRITES_PER_DAY`, default 20, counts write attempts and proposals per day. |
| Image descriptions | 10 calls per run; each image file at most 30 MiB. |
| Coding delegation | 2 tasks per cycle; the daily allowance checks admission, not a running task's final cost. |
| Shell commands | 40 per run. |

### Standing-order checks and notifications

`send_message` to the operator or another instance, `mail_draft`, `mail_draft_update`, `calendar_create`,
`calendar_move` and `calendar_respond` are checked against standing orders and held corrections.
`http_request` and `run` are not checked: they have no single text to check and no bound on their number
(`supervisor/orders.py`). Violations are refused before delivery, draft submission,
calendar writing or proposal creation, quoting the rule and the judge's reason. The instance may rewrite
and retry. A judge error lets the call proceed with a NOT checked note. No rules or held corrections
means no judge call (`supervisor/broker.py`, `Broker._check_orders`).

An instance may record an order heard from the operator or set its own rule with `record_order(own=true)`,
citing an exact past live-cycle timestamp or a unique minute prefix. Self rules bind it in the same way.
At most 10 self rules may be in force. Operator additions count only non-self orders against the limit of 40;
instance additions count every order. The instance drops only its own rules with
`record_order(drop=ID)`. Only the operator drops his orders; he can also drop self rules.
Refusals are counted per rule. Only passing messages to the operator advance a held correction's pass
count, and only a refused operator message restarts it. Drafts, calendar changes and other messages do neither.

With `SHAFT_PUSH_HOLD=1`, the default, device notifications for non-urgent messages
wait through quiet hours and after the daily push budget is reached. The inbox still receives the message. Urgent messages and open
conversations bypass both holds. A reply within two hours bypasses quiet hours but keeps
the budget (`supervisor/pushes.py`, `hold_until`).

## Filesystem reads and copies

The supervisor runs outside the sandbox, and an instance can put a
link anywhere in its own folder. Its safe filesystem helpers open files one folder at a time through descriptors, none followed if it is a link (`supervisor/core.py`:
`open_beneath`, `safe_read`, `safe_write`, `walk_beneath`, `copy_beneath`); a pipe in a file's place is refused,
never waited on. The policies below describe those helpers; they do not establish that every caller uses them.

- **What goes out** (an attachment, an image for `describe_image`, a file the coder reads, a judge's evidence):
  a link anywhere on the way is refused, even one that stays inside the folder. To that reader a link is no file.
- **A copy** (a battery exam's, a gate's scratch copy): a link that stays inside the copy, relative, its `..`
  first and no more of them than its folder is deep, is copied as the link it is, so the copy runs the code the
  instance runs; any other link is left out. The supervisor reads no link while copying; the copy's folders are
  made and its files created through descriptors (`O_EXCL`, `O_NOFOLLOW`), so a folder of the copy swapped for a
  link is refused, never written through.
- **Code** (`shaft/`, `tests/`, `plugins/`, `.python-version`, `requirements.txt`, as the gate sees them): a link
  is an entry of the gate's snapshot (its target, never what it points to), and so is anything else that is not a
  plain file of one name, a file with more than one name (a hard link), a pipe, a socket, a device. One added,
  removed or changed is a change, and a change with any of them in a code path fails the gate untested (the
  template has none). The gate's restore puts back each file as a new file renamed over whatever stands there, a
  link, a pipe, one name of a hard-linked file, never writing through it, makes a folder replaced by a link again
  through descriptors, and puts an approved file back where a link stood; every path it put back is read again, and
  if any is not as approved the run is refused ("run refused", an alert on the panel), so nothing runs on it.

Safe outgoing-file reads exclude keys, git and logs (`secrets/`, `.git`, `logs/`) by folder identity and name.
The helpers compare both folder identity (device and inode) and case-folded names, covering
alternate spellings on case-insensitive volumes. The sandbox also denies reads of `secrets/` (`supervisor/sandbox.py`). Reads are bounded: one file at `SHAFT_FILE_READ_MAX_MB`, the gate's code snapshot
at `SHAFT_SNAPSHOT_MAX_MB`, a copy at `SHAFT_COPY_MAX_GB`, any walk at `SHAFT_TREE_MAX_FILES` files and folders and
`SHAFT_TREE_MAX_DEPTH` deep. Past a bound the operation fails instead of returning a partial snapshot or copy. If a run's code grows
beyond the snapshot bound, settlement restores its starting files and config writes and writes a journal note.
New files remain uncommitted until the code is small enough to check. Pre-launch
code that is too large to compare refuses the run and can remain in place (`runner._too_big_to_check`,
`runner._settle`).

## Append-only journal

`supervisor/runner.py`, `enforce_journal`, checks for rewritten history and attempts to
restore the original journal while keeping the rewrite aside. A link or other non-file
entry is moved aside unread. An unsafe path or OS error is reported and can prevent
checking or restoration. Restoration is not guaranteed after such an error.

## Slow core

A core personality change, including removing the whole `## Core` section, sooner than
`SHAFT_CORE_MIN_HOURS` (default 72) after the last core change is reverted: the previous core is put back, with its
heading restored if it was renamed and a duplicate section removed. A first core, or a change from an empty core, is
kept and starts the clock. If the personality file cannot be written safely (a link in its place), the change stays
and the instance is told why. See `supervisor/runner.py`, `enforce_core`.

## Test gate

### Scope and test sources

`supervisor/gate.py`, `CODE_PATHS`, defines the gated code paths: `shaft/`, `tests/`, `plugins/`,
`.python-version`, and `requirements.txt`, plus config lines changed through `set_config`.
Memory and other non-code writes are not gated. If a run changes these code paths or config lines, the
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
day on its hourly check. A warm-up test failure is one panel alert and is not retried on those commits during that scheduler run.
A timeout retries one hour after it ends (`supervisor/autonomy.py`, `warm_ended`).

### Failure classification and held changes

If sandbox infrastructure is unavailable, `gate.Output.refused` causes `runner._settle` to refuse settlement.
Code and config changes remain unsettled, without revert, holding, or probation, and must be checked before
a later launch. This is distinct from a failed check.

A failed gate is classified (`gate.Output`), and either way the change is
reverted: the classification never lets a change through.

| Failure class | Conditions |
|---|---|
| Baseline | Shared tests fail or time out; the gate itself errors; a protected copy fails for a reason other than size or invalid entries; or the supervisor's instance-part tests are the only failing check and fail again against approved code. |
| Candidate | Other failures, including instance-part tests that fail only with the change, plugin checks, instance tests, their timeouts, or a link in place of `.tmp`. |

A candidate failure wins over a baseline one. On a baseline failure the change's code files are
also **held** under `.supervisor/gate-held/<name>/<id>/` (the bytes it wrote, each file's base digest, the
instance's commit, a patch to read; the instance cannot write there), and the instance is told in its inbox and
journal; config lines it set are undone and not held. After a green warm-up the scheduler starts `shaft gate regate`:
for each instance not busy or paused, under its run lock (never waiting), each held change is written back only if
every file it touched is still as it was when held, else dropped with a message carrying its patch, and settled
like any run's change, through the same gate: kept and committed, reverted and dropped if a check of it fails, or
held again if the baseline is red again, up to three tries. Every outcome is an inbox message. Every settled gate for an
instance's change is a row of `.supervisor/gate-runs/<name>.jsonl` (outcome, failing tests, the last
3,000 characters of a failed gate's output), which `record(kind="gate")` shows to that instance only, with its held
changes.

### Sandboxed checks

The plugin check and the instance's own tests run in its sandbox with no network at
all, like `run` without its proxy. A check that runs past its time counts as failed, and so do gate errors other than the infrastructure refusal described above. No unchecked change is approved.

### Process cleanup and settlement

The supervisor attempts settlement after a normal end, failure, timeout, Ctrl-C, or handled stop.
Sandbox infrastructure refusal can defer settlement as described above. The agent and its children run
in a process group of their own, ended with SIGTERM then SIGKILL before the gate looks; the same
holds for each `run` command, subject to the orphan discovery limit below. A process that left the group
(`setsid`) is found by its working folder: once its parent is gone, anything still working in the instance's
folder is ended too (your own shells there keep their parent and are spared). A battery exam's copy, a random
folder nobody else works in, is swept harder: every process working in it, everything below each, and each one's
ancestors while they share its session or process group (a parent that left the copy while its child stayed). One
that left the instance's folder is not found; the sandbox is the boundary then (it can write only the instance's folder, and what it writes there
later is caught as below). A kept change is committed with exactly the bytes the gate checked, written into
git's object store and a separate index, never read again from the working tree, and if the file on disk differs
from them by then, it is put back to them (logged as FAILED, with a journal note); the approved-code record is
made from the same bytes.

### Protected code

The supervisor materializes the approved start-of-run
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
folder back to it (`protected._link_data`), never a code path. Code that locates data relative to its own file can still read
that data through these links. The copy leads the import path, so an entry that could supply an
import, a module file (`unittest.py` would replace the gate's runner) or a folder with an `__init__`, is never
linked; a plain folder can only be a namespace portion, which never shadows a module. The gate's tests run in the
instance's own sandbox, as a launch does: they read and write its folder as in a launch, never its code
paths in either place nor its Python environment. The commit and approved-code record use the same snapshot bytes.
This closes both check-to-use windows: a working file changed after reconciliation cannot supply a launched
import, and a file swapped after the gate's snapshot cannot supply its tests. Copies are removed after the
run or gate, including on exceptions; copies left by dead supervisor processes are swept before making the
next copy for that instance. Quiet conversations retain their launch copy; trial replays and battery exams
retain their own copies. Code snapshot reads use the descriptor-based helpers in `supervisor/core.py`.
Stopping a service that starts runs (the API, the scheduler, a terminal
conversation) ends its runs' processes first.

### Approved baseline

After every settlement the supervisor records
the code it approved (`.supervisor/baselines/<name>.json`: the last commit, plus what the gate passed that git
does not hold, an ignored or untracked file in a code path), where the instance cannot write. Before any run,
whatever is not the approved code (an earlier run that ended before it was checked, a file edited from outside)
is gated first: kept, or put back as approved, the rejected files copied to `.supervisor/rejected/<name>-<time>/`
and named in the instance's journal and the log. A merge, rebase, cherry-pick or revert in progress in the
instance's worktree, or code that cannot be compared, refuses the run instead ("run refused" in
`.supervisor/supervisor.log`, an alert on the panel): finish or abort the merge and the next run checks it. Until an
instance's first settlement under this scheme, its last commit is the approved code. An edit of yours to
`config.env` is never reverted or credited to the
instance: it is committed on its own ("config.env changed from outside the instance"); so is an edit of yours to
`drive.md` ("drive.md changed by the operator"), with a note in the instance's journal that its drive changed.

## Trials

With the `trial` tool, an instance can test a change to its own
code or prompts before keeping it. The supervisor runs the gate on it, rebuilds N (default 6) of its past
autonomous cycles from saved state (its branch at that moment, the inbox it read), and runs each twice, once with
current code and once with proposed code, in scratch copies under the same sandbox, through a replay broker that
sends nothing (messages, writes, keys are checked and recorded, not carried out; web reads are real and
shared by both runs; the replay's own network is refused and traced; no coder). A judge not told which run is which compares each pair, effectiveness first,
in both orders; a sign test (α 0.1) gives better / worse / no clear difference, sent to the instance's inbox.
Nothing is applied: the instance adopts the change itself, and if it makes the trialled edit (the same lines
removed and added, even if the rest of the file moved on) its probation knows the verdict. On the local model a trial uses only idle time (no run, conversation, wake or
sleep due within 15 min; cut off 90 s before one) and otherwise runs on `SHAFT_TRIAL_FALLBACK_MODEL`
(mimo; `none` keeps it waiting for the local model); at once for an instance waking more often than every 30 min, else after `SHAFT_TRIAL_LOCAL_WAIT_H`
(6) or two preemptions. Caps per instance per day: `SHAFT_TRIALS_PER_DAY` (2), `SHAFT_TRIAL_DAILY_USD` (3),
`SHAFT_TRIAL_LOCAL_UNITS_PER_DAY` (24); `SHAFT_TRIAL_N_MAX` (10). Ledger: `.supervisor/trial_usage.jsonl`;
trials: `.supervisor/trials/<name>/<id>/`.

### Replay records

`ReplayBroker` in `supervisor/broker.py` validates and answers `gauges(choose=...)`, `predict`, `record_order`
and own-rule drops as live calls. Gauge choices, prediction events, recorded rules, drops and outgoing-check
results remain in that replay; later calls see them. Nothing is written to their live supervisor ledgers.
Prediction events use the replay clock and are not scored. A replayed `nothing_to_do` answers as a live call
but keeps no idle-exit row and makes no schedule change. Its simulated call remains in the replay trace.

## Probation

A kept self-change is undone if the achieved difficulty per cycle (success ×
judged difficulty) over the next `SHAFT_PROBATION_CYCLES` (6) cycles *that used it* (a plugin only
when the cycle called it; tests never) falls clearly (0.15 on a 0 to 1 scale) below the level before
it, and a permutation test says chance alone gives such a drop under 5% of the time. Undoing
restores exactly the change's files from git (as they were before it) and commits that as the
supervisor; memory, journal and self are never touched. A restore that cannot be done (a file was
changed again since, or the tests fail with the earlier version) stays on probation and is logged.
A change adopted against a trial's 'worse' verdict is undone on any drop, without the chance test.

## Judge

One autonomous cycle in `SHAFT_JUDGE_EVERY` (default 2, drawn after the run, so the instance cannot
tell which) is graded from its tool log by a cloud model picked at random from a rotation (`SHAFT_JUDGE_MODELS`,
default MiMo and GLM 5.3 Flash), never the model the judged instance runs on; each verdict
records its judge, and a cycle left out has no verdict rather than a failing one. A cycle that sends the operator
a message, mail or document (or claims to, in finish_cycle's `delivered_via`) is always graded; one the draw left out
is marked `operator_selected` and kept out of the sampled grade aggregates, so they stay a random sample. The second judge's model
(`SHAFT_JUDGE2_MODEL`, DeepSeek V4.1 Flash) stays out of the main rotation, so its cross-check is independent; a
pool that still lists it uses it only when no other judge may grade, and that cycle gets no second judgment. Every
judge call goes through `supervisor/modelcall.py`: Claude models straight to the Claude API; OpenRouter ones under the
instances' data policy (no data collection, zero retention); `custom/` ones to the operator's endpoint at their declared
prices; `codex/` ones to a ChatGPT plan, through a short-lived Codex app-server of the supervisor's own. A pool of one
model that is the instance's own (an operator whose one provider offers one model) grades it with that model, and the
verdict records `"independence": "same-model"`; a pool of several models never does. A trial pair keeps
one judge for both orders; a battery item one grader for both arms, and a battery era is the whole pool. Results
feed `self/stats.md`.

### Conversation order checks

`supervisor/unrecorded.py` sends the judge the last `TRANSCRIPT_CHARS=24000` characters of an ended,
written-up conversation, including agent and operator turns, plus that conversation's recorded commitment
promises and due times and standing-order texts sourced to it. Open conversations are excluded.
Returned quotes must match visible operator turns, outside their quote-reply lines (words found only there are the
instance's own, quoted back, and are not counted as orders). The check runs in `metrics.after_cycle`, with
`PER_RUN=3` judge calls at most. A start marker is written even when the judge is down or nothing is pending.
Write-ups more than `FIRST_RUN_HOURS=24` hours before that marker are skipped.
It never writes commitments or standing orders: an unrecorded order reaches the instance as an inbox note from
"supervisor (unrecorded orders)", which it records or ignores. Trial replays never run it.

## Git

After every run the instance's mind is committed to its mind store and its skills and kept
code to its branch, with the same message (author `SHAFT/<name>`). The branch git calls use the main
repo's worktree registry with hooks off; the mind store's run with hooks, fsmonitor, the user's git
config and the instance's own `.gitattributes` off, and add with `-f` (its `.gitignore` files cannot hide
part of its mind); so an instance cannot redirect the supervisor's git calls. A mind path is never
staged on the branch. Trial replays read the mind as of the replayed cycle's start from the store.

## Budgets

Only one run per instance runs at a time. A cycle is stopped after
`SHAFT_CYCLE_TIMEOUT_S` (2400 s), a sleep after `SHAFT_SLEEP_TIMEOUT_S` (5400 s). Runs receive
their deadline and a request to wrap up before it; a stopped run leaves a journal note. A cycle and a sleep
also see their *operational envelope* before they hit a limit: calls left this run, minutes left, today's
cloud, coder and trial allowances, any probation window (cycles used / needed) and the next sleep, computed by
the supervisor (the broker's `GET /envelope`, not a tool and not counted), shown in the first prompt and, calls
and minutes, after every step. It carries no keys and no host paths. Live and replay envelopes use `calls_used`, `calls_max`, `run_usd`, `cloud`, `consult`,
`coder`, `trials`, `probation`, `next_sleep`, `next_wake`, and `pushes`, with no replay flag.
Unavailable fields can be null. Consultations are unavailable in replays. Spend, trial starts and probation counts stop before the replayed cycle's start; the schedule is computed
from that start. Closed probation entries cannot be reconstructed.

## Your conversation comes first

`shaft talk` asks other instances' running cycles to wrap
up at their next turn when they use the same local model server (an instance on a cloud model shares
nothing and is left alone, in either direction). An instance's running cycle is paused only while a reply
to you is being written (terminal, API or Telegram) and resumes as soon as it is done, even with the
conversation still open; an open but quiet conversation does not stop its cycles from starting. A conversation holds its instance only while it replies, so you can chat in the terminal and
through the API at the same time. Open conversations are never written up early; a conversation
picked up again after its write-up gets a further entry. Ctrl-C in a conversation ends it cleanly
(the instance still writes its journal entry).

## Failure reporting

A run that ends in an error leaves a journal note with what it printed
last; a nightly sleep skipped because the instance is paused is noted too. Every instance's context
says when it last slept and what came of it, even after the entry scrolled out of its recent journal.

## Updates

The supported path for template fixes is `shaft update`, under its run lock
and the test gate; never mid-run.

## Scope of these controls

These are code-enforced restrictions, not a claim that arbitrary self-written code is
safe. The sandbox has explicit local service exceptions; the public egress proxy
itself rejects all non-public destination addresses. Data an instance can read may
be sent to public services. Provider routing requests are not an independent audit
of a provider's retention practices. See [SECURITY.md](../SECURITY.md).

The instance cloud cap does not cover all supervisor spending. Coding, consultations, and trials have separate allowances. Cloud and trial admission
checks use recorded usage; an admitted request can finish above its remaining allowance.
Consultations reserve an estimated worst-case cost, but a provider can report a higher cost.
Judging has an alert threshold, not a hard cap.
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
| Gate and config restoration | [supervisor/gate.py](../supervisor/gate.py) |
| Trial replay and comparison | [supervisor/trial.py](../supervisor/trial.py) |
| Probation | [supervisor/metrics.py](../supervisor/metrics.py) |
| Judge rotation | [supervisor/judge.py](../supervisor/judge.py) |
| Private mind commits and guards | [supervisor/mind.py](../supervisor/mind.py) |
| Run deadlines and conversation priority | [supervisor/runner.py](../supervisor/runner.py) |
| Template updates | [supervisor/__main__.py](../supervisor/__main__.py) |
