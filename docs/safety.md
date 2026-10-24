# Rules the supervisor enforces

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
- **Broker**: privileged tools and credentials, including web search (ScrapeBadger), page fetching
  (public addresses only, re-checked on every redirect; `fetch_page` and `http_request` connect to the very
  address they checked, with TLS still verifying the name asked for, so a name that answers public for the
  check and private for the connection reaches nothing), messages between instances and to
  you (no daily limit unless `SHAFT_OPERATOR_PER_DAY`; a macOS notification, and a push on the web UI or Telegram,
  whichever reaches you for that instance: integrations.md), with
  files attached from the sender's folder (up to 10, 20 MB each, 50 MB in all; never `secrets/`, `.git/`, `logs/` or
  anything a link leads to outside the folder); another instance finds them in its `attachments/` (not
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
- **Append-only journal**: if a run rewrote history instead of appending, the journal is
  restored and the rewrite kept aside.
- **Slow core**: the core personality may change at most every `SHAFT_CORE_MIN_HOURS` (72).
- **Test gate**: if a run changed the instance's code, plugins or (through `set_config`) config, the
  invariant tests and plugin checks run; failure restores the old code and the config lines the instance
  set, and tells the instance why. The supervisor's own tests run from its last commit (and the template's), in a
  clean copy under `.supervisor/gate-tests/`, never from the checkout: an edit someone is still making there cannot
  decide a verdict. The plugin check and the instance's own tests run in its sandbox with no network at
  all, like `run` without its proxy. A check that runs past its time counts as failed, and so does any error of the
  gate's own (it never lets a change through unjudged). Each run is settled whatever happens to it: the agent
  and everything it started run in a process group of their own, ended (SIGTERM, then SIGKILL) before the gate
  looks, on every way out — a normal end, a failure, a timeout, Ctrl-C or the supervisor being stopped; the same
  holds for each `run` command, so nothing it puts in the background outlives it. A process that left the group
  (`setsid`) is found by its working folder: once its parent is gone, anything still working in the instance's
  folder is ended too (your own shells there keep their parent and are spared). One that also left the folder is
  not found; the sandbox is the boundary then (it can write only the instance's folder, and what it writes there
  later is caught as below). Stopping a service that starts runs (the API, the scheduler, a terminal
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
  default MiMo, GLM 5.3 Flash and DeepSeek V4.1 Flash), never the model the judged instance runs on; each verdict
  records its judge, and a cycle left out has no verdict rather than a failing one. A cycle the second judge
  (`SHAFT_JUDGE2_MODEL`) also grades is never given that model as its main judge. Claude models go straight to the Claude API; OpenRouter ones under the instances' data policy (no data
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
  their deadline and wrap up with a report before it; a stopped run leaves a journal note.
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
