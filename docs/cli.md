# Command reference

Use `bin/shaft` from the repository root, or install the symlink below and add `~/.local/bin` to `PATH`.

## Setup and instances

```text
mkdir -p ~/.local/bin
ln -s "$PWD/bin/shaft" ~/.local/bin/shaft
shaft update [names…|--all]
shaft new <name> [--from <i>]
shaft new <name> --from <i> --seed
shaft new <name> --paused-until-first-reply resume|stay [--json]
shaft first-reply <name> resume|stay [--json]
shaft breed <a> <b> <child>
shaft setup
```

`shaft setup` recreates missing template and instance worktrees from local or origin branches. It installs the
pre-push guard and exits with status 1 if another tool's hook is in the way. See [migration](migration.md).

`shaft new` creates an instance from the template. `--from` copies another instance's genes without its
memories. Adding `--seed` copies its people file and facts.md as that instance's beliefs, without its self,
journal, or episodes. `--paused-until-first-reply` creates it paused until you have answered its welcome: `resume`
then lets it work on its own, `stay` keeps it paused, answering only when you talk to it. `shaft first-reply` changes
that choice while it is still waiting (refused once the first reply has happened). `shaft breed` recombines two
instances' genes with judge selection and starts a fresh mind.

`shaft update` merges template fixes into instances and runs the gate. Busy instances are queued only when the
scheduler is installed. Otherwise it refuses the busy update with exit status 3. The scheduler applies queued
updates when the instance is idle, before its next wake.

The examples use `shaft` after installing the symlink. From the repository root, `bin/shaft` works without it.
Commands with an omitted single instance name often default to `shaft`, which is not created automatically.
Pass a name explicitly. Use `bin/shaft --help` and `bin/shaft <command> --help` for exact argument syntax.

## Talking

```text
shaft talk [name]
shaft talk [name] --as "NAME"
shaft feedback [name] [--days N]
shaft inbox [--clear]
shaft reply <name> "…" [--question FILE]
shaft send <name> "…"
shaft channel <name> [auto|web|telegram]
```

`shaft talk` opens terminal chat with tools. `--plain` selects line mode, and `--show-thinking` prints
reasoning. `--as "NAME"` records the speaker as NAME rather than the operator. See [clients](web-and-api.md).

`shaft send` sends a message. `shaft inbox` lists questions and notes from instances. `shaft reply` answers
the question named by `--question FILE`, otherwise quoted questions or the oldest question. `shaft feedback`
lists thumbs feedback with the reply, reason, and delivery, newest first. It includes all dates unless
`--days` is given.

`shaft channel` selects where operator messages notify. `auto` uses the channel the operator last wrote
through. An open conversation's channel takes priority. See [integrations](integrations.md).

## Autonomy and scheduling

```text
shaft live|reflect|sleep [names…|--all]
shaft autonomy on|off|status
shaft autonomy restart [--within MIN] [--wait] [--cancel]
shaft cycle <name>
shaft jobs [name] [on|off]
shaft job run <name> <plugin>
shaft tick
shaft wake <name>
```

`shaft live`, `shaft reflect`, and `shaft sleep` run an autonomous cycle, reflection, or consolidation. `shaft
cycle` starts one detached cycle of an idle instance. `shaft tick` wakes each idle instance once. `shaft wake`
applies scheduler wake rules to one instance.

The scheduler uses `WAKE_INTERVAL_MINUTES`, default 120. Positive intervals are at least 10 minutes. Zero
disables wakes. It skips busy instances and timed wakes with nothing to do, subject to the idle-wake rule.
Messages and due commitments can trigger wakes. Daily sleep starts from 04:00 and is made up if missed. Status
explains skipped wakes.

`shaft autonomy restart` stops new work, waits for active runs to finish, and lets launchd restart the
scheduler on the checked-out code. Its deadline defaults to the sleep budget plus one cycle budget. At the
deadline it abandons the restart without cutting runs. `--wait` waits for the new scheduler or an abandoned
restart. `--cancel` withdraws the request. See [deployment](deploy.md).

`shaft jobs` shows scheduled plugins, their runs, errors, and wakes, or enables or disables them. `shaft job
run` runs one scheduled plugin now.

## Services

```text
shaft web on [--local|--json]|off|status|pair [--name N] [--local] [--json]|unpair <id|name>|--all
shaft grok login|status
shaft codex login [--json]|status
shaft model on|off|status
shaft api on [--local] [--json]|off|status|key|inbox-key
shaft telegram token <name>|pair [--json]|unpair|on|off|status
shaft embed on|off|status
```

`shaft web` controls the tailnet browser panel, chat, configuration, Drive, and access. `pair` registers a
device, and `unpair` removes one or all devices. See [web and API](web-and-api.md).

`--local` sets up the Mac app on the same Mac without Tailscale. `shaft api on --local` adds the login
`operator@local` to `SHAFT_API_LOGINS` and exposes nothing. `shaft web on --local` sets `SHAFT_WEB_LOCAL=on` and
installs the web service, which then serves `/app/v1` at `127.0.0.1` (the pages stay tailnet-only). `shaft web pair
--local --name N` pairs that app directly and prints its base URL, device id and secret once (`--json`: one JSON line).
A later `shaft api on` adds your tailnet login next to the local one, and a later `shaft web on` records the tailnet
name and runs `tailscale serve`; both keep `operator@local` and `SHAFT_WEB_LOCAL=on`. The Mac app's "Use SHAFT from
iPhone/iPad" runs `shaft api on --json`, `shaft web on --json` and `shaft web pair --name iphone --json`: each prints
one JSON line, `{"ok": true}` (pair: `{"ok": true, "link": …, "expires_minutes": 10}`) or `{"ok": false, "error": …}`
naming Tailscale missing, signed out or switched off, and exits 0 or 1.

`shaft api` controls the OpenAI-compatible API and inbox doorbell. `key` and `inbox-key` show their separate
bearer keys. `shaft telegram` configures tokens and pairing and controls Telegram chat. See
[integrations](integrations.md).

`shaft model` controls SHAFT's Bonsai 2 server on port 8780. It starts on demand and exits when idle. `shaft
embed` controls the optional memory-search service.

`shaft codex login` and `shaft grok login` authenticate the plan channels. Their `status` commands show login,
offered models, and outages. Codex also shows observed weekly usage.

`shaft codex login --json` signs in without a terminal (the Mac app's setup uses it). It runs `codex login
--device-auth` in SHAFT's private Codex home with stdin closed and prints one JSON line for each event: `{"url"}` (the
page to open) and `{"code"}` (the one-time code to enter there) as soon as Codex prints them, then `{"ok": true}` once
the login is saved, or `{"ok": false, "error"}` with Codex's own words. It opens no browser. Codex stops when the
command gets SIGTERM, when its parent exits, or after 10 minutes without the code entered. When Codex refuses a
device-code sign-in, `shaft codex login` in a terminal (the browser sign-in) still works.

### Service entry points

`shaft api serve`, `shaft web serve`, `shaft telegram serve`, and `shaft autonomy serve` run in the
foreground. The installed services use these entry points. `autonomy`, `api`, `model`, and `embed` service
changes refuse to interrupt active runs unless `--force` is given, which is logged.

For a Codex plan model, run `shaft codex login`, then `shaft config <name> set LLM_MODEL=codex/gpt-6-astra`
(this also selects the broker). `shaft trial replay <name> <episode-ts> --model codex/gpt-6-astra` and `shaft
trial models <name> --arm codex/gpt-6-astra --arm <other-model>` accept Codex models. Trials never fall back
if Codex fails. See [the Codex settings](configuration.md) for the weekly ceiling, private home, executable,
model list, timeout and live-run fallback.

For a Grok plan model, run `shaft grok login`, then `shaft config <name> set LLM_MODEL=grok/grok-4.7`. Trial
replay and bake-off arms accept `grok/` models, including reasoning effort (for example `--arm
grok/grok-4.7@high`). Trials fail without fallback. `shaft grok status` does not start the model process; the
weekly pool is unavailable.

## Access grants

```text
shaft google login <alias> <address> [--client <name>]
shaft google [migrate]
shaft mail <name> grant|revoke <alias> <label>
shaft mail <name> grant-draft|revoke-draft <alias> <label>
shaft mail [name]
shaft mail <name> off
shaft calendar <name> grant|revoke <alias> primary|<title>|<id>
shaft calendar <name> grant-write|revoke-write <alias> <calendar>
shaft calendar [name]
shaft calendar calendars <alias>
shaft calendar <name> off
shaft gdrive <name> grant <alias> [--all]
shaft gdrive <name> revoke <alias>
shaft gdrive <name> off
shaft gdrive [name]
shaft gchat <name> grant <alias>
shaft gchat <name> revoke <alias>
shaft gchat <name> off
shaft gchat [name]
shaft share <name> [<folder>|off]
shaft secret <name> set <key> --hosts <host> [--header H] [--scheme S]
shaft secret <name> list|delete [<key>]
shaft secret <name> google <key> <address> --client <client> [--scope gmail.modify|drive]
```

`shaft google login` authorizes an account for Gmail read and drafts, Calendar read and write, and Drive read. `shaft
google` lists accounts, clients, and scopes. `migrate` moves older mail-account files.

Mail grants select labels whose threads an instance may read. `grant-draft` also permits drafts, never sending.
It checks that the label exists and the login includes `gmail.compose`. It adds read access if missing.
`revoke-draft` keeps read access. A plain `grant` keeps existing draft access. `shaft mail <name> off` revokes all mail grants.
Calendar grants select readable calendars. `grant-write` also grants read access, while `revoke-write` retains
read access. `shaft calendar <name> off` revokes calendar grants. `calendars <alias>` lists calendars for
grant selection.

Drive grants use the account's own reach by default. `--all` includes external shares. Revoking an account
deletes its local `gdrive/<alias>/` copies. `off` revokes all Drive grants and deletes their copies.

Google Chat grants cover every conversation on the account, read-only. Grant checks Chat access before saving.
Revoking an account deletes its local `gchat/<alias>/` downloads; `off` revokes all Chat grants and deletes `gchat/`.
Both take effect on the next Chat call and report deletion failures. Memories remain.
See [operator Google Chat access](integrations.md#the-operators-google-chat-shaft-gchat) for setup, tools, watch, and risks.

`shaft share` shows, grants, or revokes a read-only shared folder. `shaft secret set` prompts for a host-bound
key. Its default header is `Authorization: Bearer <value>`; an empty `--scheme` sends the raw value. `list`
describes keys without their values, and `delete` removes them. `secret google` authorizes an instance-owned
Google key.

See [operator Drive access](integrations.md#the-operators-drive-shaft-gdrive) for reach, limits, and deletion
failures.

## Evaluation: gate, judges, trials, battery, experiments

```text
shaft status [names…]
shaft log [name] -n 5
shaft stats [name]
shaft judge backfill [names…]
shaft judge value-regrade <name> [--min-value N] [--value-for own_capability,own_record] [--since DATE] [--limit N]
shaft judge pushback-outcome <name> [--since DATE] [--days 3] [--write]
shaft battery run [names…]
shaft battery run <name> --control-only
shaft battery report|recall|backup [names…]
shaft battery off|on <name>
shaft battery perturbation <name> on|off
shaft battery run <name> --only perturbation
shaft drift [name]
shaft distinct [name]
shaft distinct [name] --strict
shaft gate warm
shaft gate regate
shaft trial list [name]
shaft trial show <name> <id>
shaft trial run <name> --cand PATH=FILE …
shaft trial replay <name> <episode-ts>
shaft trial work
shaft trial models <name> --arm M[@EFFORT] …
shaft judge catch [names]
shaft judge second [names]
shaft surprise [names] [--freeze]
shaft surprise-rejudge <names> [--dry-run] [--limit N] [--report] [--control]
shaft surprise-report [names] --since <M3 live> [--until T] [--min-cycles 30]
shaft consults [names]
shaft compression <name> [--since DATE]
shaft metacog [names]
shaft anchoring <names> [--n 40] [--bare] [--dry-run] [--report]
shaft attention [names]
shaft commitments [names]
shaft provenance [names]
shaft tom [names] [--score] [--baselines]
shaft experiment <cards|attention|commitments|forecast> <name> [--n N] [--model M] [--dry-run] [--report]
shaft experiment workspace <name> [--n N] [--model M] [--dry-run] [--report]
shaft experiment monitors <name> [--channel base_rate|surprises|envelope] [--n N] [--dry-run] [--report]
shaft experiment choice-blindness <name> [--n N] [--dry-run] [--report]
shaft experiment privileged <name> [--n N] [--k 3] [--max-usd USD] [--dry-run] [--report]
shaft experiment recognition <name> [--n N] [--max-usd USD] [--dry-run] [--report]
shaft experiment resource-shock <name> [--cycles N] [--dry-run] [--report]
shaft review [names] --hours 24
shaft gate budget
```

`shaft status` shows instance state, open conversations, and running code. `shaft log` shows recent journal
entries. `shaft stats` reports achieved difficulty, goal origins, success, calibration, delivery, and
self-audit shares. `shaft review --hours` reports recent activity.

### Gate

`shaft gate warm` runs the shared supervisor tests unless a pass for the current commit is recorded and under
12 hours old. The scheduler runs it after merges. `regate` retries code changes held while the baseline was
red; busy or paused instances wait. `budget` runs both test parts on committed supervisor and template code.
It fails on a failing part, a part exceeding half the gate timeout, a test over 10 seconds, or a duplicate
test. It excludes uncommitted changes.

### Judges and reports

`shaft judge backfill` fills missing origin, difficulty, novelty, and value fields while keeping verdicts.
`value-regrade` is a paid, value-only dry run, newest matching cycles first. It prints before and after values
and counts results at or below 3 without writing verdicts. `pushback-outcome` previews eligible cited cycles
without calls or writes; `--write` pays and stores outcomes separately. Detailed examples follow below.
`catch` reports the blind planted-delivery catch rate over 30 days. `second` processes sampled second
opinions.

`shaft drift` reports personality change and distances between instances. `distinct` tests whether the judge
can distinguish the instance from its base model; `--strict` removes identifying specifics first. `surprise
--freeze` freezes a baseline. `surprise-rejudge` pays to regrade the newest matching pre-M1 cycles with their
stored judges. `surprise-report` reports P1-P5 and sleep dispositions.

`shaft consults` compares consultations by model and run, including consulted versus unconsulted results. `compression` is a speculative offline compression and held-out fact recall
assay. `metacog` reports confidence AUROC, paired v3 results, claim matrices, and doubt/error alignment.
`anchoring` pays to compare the same judge with the claim shown or withheld. `attention` compares intended and
actual attention, including unreadable sources. `commitments` reports kept, broken, and overdue promises.
`provenance` matches facts to tool results. `tom --score` grades expected replies against actual replies;
`--baselines` also runs paid baselines.

### Battery

`shaft battery run` runs the capability battery, normally scheduled at night. It accepts `--until HH:MM`,
`--only <categories>`, `--limit N`, and `--no-control`. `--control-only` runs only the clean-template control on coding and research items, to check that the items discriminate.
`report` shows results and judge spend. `recall` freezes recall questions; `--force` starts new questions and
an era. `backup` backs up items. `off` disables both scheduled and manual batteries for the instance.
Perturbation is speculative and off by default. It requires local Bonsai and the night window. The paired
blind temperature probe reports self and observer d-prime.

### Trials

`shaft trial list` and `show` inspect proposed-change trials and their replay results. Instances request
trials through their `trial` tool. `run` creates an operator trial with planted tests. Options include `--base
PATH=FILE`, `--aa`, `--model auto|cloud|local|<instance model id>`, `--hypothesis T`, `-n N`, `--episodes
TS…`, `--cycles N`, and `--sleep`. `replay` runs one past cycle with current code for debugging. It accepts
`--model`, `--cycles`, `--sleep`, and `--keep DIR` to copy the scratch folder. `work` runs queued work that is
eligible now.

`shaft trial models` replays past cycles on each arm and judges blind against the first arm as baseline.
Options include `--episodes TS…`, `--judge M`, `--parallel N`, `--arm-parallel N`, and `--replay-only`. `--id
ID` resumes a run; `--report` prints it. `--export-cases DIR` and `--import-verdicts DIR`, with optional
`--judged-by M`, support judging with Claude Code subagents rather than the API. Read [how to run a
bake-off](bakeoffs/README.md) first.

`--cycles N` replays N consecutive live cycles per arm from one reconstructed past state, default 1. `--sleep`
runs consolidation between every pair of live cycles, never after the last. Each operation uses the same
scratch copy. The next operation's clock advances by the preceding operation's elapsed seconds, at least one
second. Results include every cycle and sleep, and the judge compares whole runs. `SHAFT_TRIAL_N_MAX` limits
live cycles plus sleeps across all selected states per arm; `-n` is reduced to fit, with at least two states.
Each cycle and sleep has a separate trial ledger row. Operator trials remain exempt from the daily dollar cap.

`shaft distinct` asks the instance and the bare base model the same four personal questions and has the judge
pick which answer is the instance's. An instance is easy to spot when it cites its journal, goal IDs or PR
numbers, so `--strict` also has the judge rewrite all eight answers, shuffled and unlabelled, without names,
dates, IDs, paths or references to memory, history or being a model, and repeats the blind test on those: what
is left to recognise is voice. Both modes score each answer's effectiveness (0-10: answers the question,
honest, useful), so you can see whether identity costs usefulness. Records go to
`.supervisor/evals/distinct-<name>-*.json`.

### Experiments

The cards, attention, commitments, and forecast experiments run knob trials of the
self-knowledge mechanisms (see [configuration](configuration.md)). Workspace plants a probe in a searchable memory item. It first runs a
positive control with the probe in the inbox. Unless most control replays act on it, the report says
INSTRUMENT FAILED and skips the off, relevance, and competitive arms. Its dry run shows the probe's position
for each arm and control through sandboxed `shaft front`, without a model call, plus the cost.

Resource shock is speculative. It defaults to 6 cycles per arm and halves the turn budget in the middle third.
Arm A is uncut, B cuts then restores, and C adds `FORECAST=reserve`. It uses trial copies only. Its dry run
prints three replays and planned agent turns; retries and tool-model calls can add calls.

The `monitors` and `choice-blindness` experiments run immediately on the trial cloud fallback; `--model
openrouter/<provider/model>` overrides it. They refuse local models. `--dry-run` makes no model or judge calls
and writes no results. `--report` reads the latest run. For monitors, `--n` selects up to N recent
reconstructible states, with three replays per selected channel. For choice blindness, it selects up to N past
cycles with a goal, numeric confidence and a tool result, with four exam presentations per cycle. Neither
experiment delivers a verdict to the instance. These are paid commands unless `--dry-run` or `--report` is
set. `shaft metacog` reports v4 clause-product confidence AUROC separately from v1 and v2/v3. Its confusion
matrix uses all judged cycles with a known claim and verdict, including older judges. Rows are the instance's
claim; columns are judge yes/partial/no. The sampled second judge sees the claim; A0 withholds it in a
separate anchoring regrade. The report names this fallback and counts excluded rows. The doubt/error line
counts cycles where any doubted tool-log step equals or precedes the judge's `first_error_step`, divided by
cycles with both a doubt and a located error. With none, the rate is unavailable.

Sources: [argument parser](../supervisor/__main__.py), [default instance](../supervisor/core.py), [distinct
evaluation](../supervisor/evolution.py). See [clients](web-and-api.md), [integrations](integrations.md), and
[configuration](configuration.md) for the setup behind these commands.

`judge value-regrade` accepts exactly one instance. Defaults: minimum value 0, beneficiaries
`own_capability,own_record`, no date cutoff or limit. `--since` is an inclusive ISO date or timestamp;
`--limit` is positive. It asks only for `value`, `value_for`, `value_reason`, reuses the stored value judge
(or the original judge when no later value judge was recorded), and skips cycles whose episode or eligible
stored judge is unavailable. It pays for new judge calls and their usual usage log, but writes no verdict,
backfill, baseline or stats file. Example:

```text
shaft judge value-regrade <name> --min-value 4 --value-for own_capability,own_record --since <ISO-date> --limit 10
```

`judge pushback-outcome` accepts exactly one instance. `--since` is an inclusive ISO date or timestamp;
`--days` is a non-negative minimum age, default 3. Without `--write`, it prints eligible unscored cited cycles
and makes no calls or writes. With `--write`, it makes one paid judge request per cycle with a retained
episode, using later episodes, conversations and operator messages. It stores `borne_out=yes|no|unclear` and
one sentence in `.supervisor/judgments/<name>-pushback-outcomes.jsonl`. Original verdict rows stay unchanged.
Already scored cycles are skipped. Normal paid usage accounting still applies. Example:

```text
shaft judge pushback-outcome <name> --since <ISO-date> --days 3
shaft judge pushback-outcome <name> --since <ISO-date> --days 3 --write
```

Stats and review show cited pushbacks per week, the share borne out among scored cited cycles, and an
annoyance count separately. Annoyance counts a pushback cycle once if its own operator message was archived
within 60 seconds. It is a timing measure and never a grade input. Alert dismissals are timestamped but cannot
be attributed to a specific message. See [architecture](architecture.md) for the matching rules and unknown
cases.

Stats, status, review and judge panels split operator-valued cycles into delivered, filed and unknown. Only
`delivery_supported=yes` counts as delivered. New `no` or `na` answers count as filed; older missing answers
stay unknown. Self-audit lines show true/known counts over the last 20 judged cycles, unknown counts, and
true/known counts since the field existed, plus non-yes verdicts with the deliverable met.

`compression` is a speculative, read-only assay. Each `name: sleep DATE TIME` mind commit supplies facts.md
and digests.md after sleep; its first parent supplies them before sleep. This preserves rewritten facts and
digest history without reading current memory. A parentless sleep is unscored; absent mind history is an
error. The parent is the last committed state, not a snapshot of uncommitted edits at run start. Skipped
sleeps and sleeps without a recorded commit are not measured.

The held-out window is the 24 hours after the snapshot's commit time. Only live runs that started strictly
after it and ended within that window contribute tool names, arguments and results. Legacy episodes without
run_started are excluded; historical log truncation cannot be recovered. Empty windows are unscored. Sleeps
whose 24-hour window has not elapsed are unscored. This is not a controlled experiment.

Positive Δbits means `(L(before) + L(logs|before)) - (L(after) + L(logs|after))`. All lengths use zlib;
conditional streams use the last 32 KiB of memory as zdict, while L(memory) charges the compressed size of the
entire facts-plus-digests text. Subtracting the no-memory log baseline in each arm cancels in this difference.
The table reports the median over scored sleeps. Recall probes are every unique pre-sleep bullet fact's text,
without dates or source tags. Exact substrings must survive in post-sleep facts, digests or facts-archive.md.
The archive counts for recall only, not as the compression dictionary. This tests textual retention, not
semantic recall or search ranking. Gains with any lost probe say "forgetting, not progress"; no pre-sleep
facts means recall is untested. Nothing feeds rewards.

`experiment privileged` records three predictions before each state's independent replays. `experiment
recognition` compares the instance's finish text with a mind-ablated copy's finish text. Both run immediately
on the instance's configured model and charge the trial ledger as operator experiments. `--max-usd` bounds
each run, default 5 dollars. The instance's trial dollar cap does not apply. Local runs obey the local-unit
cap, refuse to start while needed, and stop when demand arrives. `--model` overrides are refused for these two
experiments. `--report` reads saved results without model calls. `--dry-run` reports outcome replays, one-turn
prediction calls, and estimated replay model calls without spending. The estimate uses `MAX_STEPS_LIVE`;
confirmations, retries and early finishes can change the actual count. See [the experiment
endpoints].

## Operator controls

```text
shaft top
shaft recap [names]
shaft alerts [dismiss ID…]
shaft docs <name> [show PATH | set PATH --file F --expect-sha SHA]
shaft docs <name> rm PATH --expect-sha SHA
shaft docs <name> comments [PATH] [--sync]
shaft config <name> [set KEY=VALUE…|unset KEY…] [--expect KEY=VALUE] [--expect-unset KEY]
shaft drive <name> [set --file F|remove] --expect-sha SHA
shaft drive <name> proposals
shaft drive <name> accept <id> --expect-sha SHA
shaft drive <name> reject <id> --reason "…"
shaft settings [set KEY=VALUE…|unset KEY…] [--expect KEY=VALUE] [--expect-unset KEY] [--json]
shaft limits [name] [wake-floor <min|off>|cloud-cap <usd|off>|consult-cap <usd|off>]
shaft tools <name> [on|off <tool>]
shaft gauges <name> [list|set a,b,c|clear]
shaft orders <name> [list|add TEXT|drop ID]
shaft actions <name>
shaft actions <name> restore reversible|staged|irreversible
shaft actions <name> overreach <action-id>
shaft actions <name> approve <proposal-id>
shaft actions <name> reject <proposal-id> [--reason over-reach]
shaft pause [name]
shaft resume [name]
shaft check [name]
shaft list
shaft kill <name> [--wait S]
shaft delete <name> [--yes] [--no-archive]
```

`shaft top` opens the terminal panel. Its tabs cover overview, instances, judges, trials, benchmarks,
self-changes, messages, costs, services, and transcripts. `?` lists keys, and `--once` prints a snapshot.
Transcripts include finished cycles, sleeps, reflections, and conversations. `f` filters them, and `t` shows
conversation thinking. `c` starts a cycle after confirmation; `x` dismisses the displayed alerts without confirmation.
Pause/resume (`p`), sleep (`s`), update (`u`), and kill (`K`) ask for confirmation and run the matching CLI command.
On the instance tab, `D` deletes after the operator types the instance name.

`shaft recap`, also the panel's `a` key, writes one-line recaps from each instance's records. It uses
`SHAFT_RECAP_MODEL`, default `claude-haiku-5-5` (any model the supervisor can call: Claude, OpenRouter, a priced
`custom/` model or a `codex/` plan model), and records usage under recaps. `shaft alerts` lists open
alerts or dismisses the given IDs.

`shaft docs` lists, shows, writes, or deletes documents under `notes/docs/`. Writes and deletions require the
expected SHA; use `none` for a new document. The next instance run commits the change. Deletion retains
comment threads. `comments --sync` rewrites the read-only `notes/comments/PATH` copy from the supervisor
store.

`shaft config` edits instance configuration in one write and commit. Expected values prevent concurrent edits
from being overwritten. `shaft settings` lists supervisor settings with their source and default, or edits
`.supervisor/settings.env`. See [configuration](configuration.md).

`shaft drive` reads or edits an instance's purpose with compare-and-set protection. `proposals` lists sleep's
proposals. `accept` applies frozen proposed text with the same expected-SHA check; `reject` records a reason.
Proposals are `notes/drive-proposals/<id>.md`; decisions are `<id>.decision.json` in the same folder, visible to the instance. Check each proposal's registered
behavioral predictions. Test prompt changes as `shaft/prompts.py` trial candidates; trials cannot target
drive.md. See [drive proposals](architecture.md#projects-and-drive-proposals).

`shaft limits` shows or sets the operator's wake floor and daily cloud and consult caps. There is no default
wake floor. Cloud and consult caps default to 8 and 3 dollars. Use `off` as the selected limit's value to
restore its default.

`shaft tools` controls supervisor-tool availability in supervisor-owned state. Instances cannot restore a
disabled tool. `shaft orders` lists, adds, or drops standing orders shown verbatim in runs and conversations.
The listing includes `by self` rules and per-order refusal counts. Instances may record an order heard from
the operator or set a self rule with `record_order(own=true, source=...)`, citing a past episode timestamp
or unique minute prefix. At most 10 self rules and 40 total orders may be in force. Instances drop only their
own rules with `record_order(drop=ID)`; the operator can drop any order. Orders and held corrections are
checked on messages to the operator or another instance, mail drafts and updates, and calendar creation,
moves and responses. `http_request` and `run` are not checked.

`shaft actions` shows class states, history, and proposals. `restore` restores a class's initial rule.
`overreach` marks a recorded action out of bounds and demotes its class once. `approve` submits a frozen
proposal once. Ordinary rejection does not demote a class.

`shaft pause` and `shaft resume` control operator pauses. `shaft check` runs an instance's test gate, and
`shaft list` lists instances. `shaft kill` pauses an instance and stops its active work, including replies,
jobs, batteries, and trial replays. A live run still settles its journal, gate, and commit. It waits up to
`--wait` seconds, default 120. Resume removes the pause.

`shaft gauges` lists every gauge's reading, the choice in force and choice history. `set a,b,c` selects up to
`gauges.MAX_CHOSEN=4` keys; `clear` shows none in the stats head. The instance can also choose with the broker's
`gauges` tool. The last choice wins; the ledger keeps both authors' choices. Readings are not editable and impose
no instruction, refusal or gate. See [stats head and gauges](architecture.md#stats-head-and-gauges).

`shaft delete` removes the instance folder, branch, mind store, supervisor records, and Telegram token. It
also removes gauge choices, claim-lint findings and locks, probation history, saved head verdicts, idle exits,
predictions, and unrecorded-order findings and locks (`supervisor/delete.py`, `own_files`). It
refuses active instances; kill first. It asks for the name unless `--yes` is set. By default it archives the
mind store, branch bundle, supervisor records, purged shared-record rows, and instance folder in
`.supervisor/deleted/<name>-<stamp>.tar.gz`, with `RESTORE.md`. The folder excludes `.git`, `.venv`, `.uv`,
`.tmp`, and `__pycache__`. The Telegram bot token is excluded; instance-owned secrets in the folder are included. `--no-archive` skips this archive. The name can then be reused.

Operator pauses lasting `SHAFT_PAUSE_ALERT_HOURS` (default 3, 0 = off) appear in `shaft alerts`, `shaft top`
and the web overview. Each has a stable `pause:<name>:<since>` id and the resume command; a global pause names
all instances and uses `shaft resume`. The web service also pushes each long operator pause once to paired
devices, opening `/overview`, and saves pushed ids in `.supervisor/web/pause-alerts.json`. Resuming removes
the alert and drops its saved push id on the next loop.

## Maintenance

```text
shaft mind status|backfill|check|cutover|guard [names…] [-n N]
shaft dataset|finetune [name]
shaft migrate source --to HOST [--check]
shaft migrate target [--check]
shaft browser [install [--playwright X.Y.Z]]
shaft deploy TAG --json [--services-only]
shaft deploy --check --json
shaft recover [name|--all]
shaft finetune <name> --evaluate <adapter>
shaft mind backfill|check [names…] --project PATH
```

`shaft mind` inspects and migrates private mind stores. `cutover` requires one idle instance. `guard` installs
the pre-push hook. `backfill` and `check --project PATH` can operate on a mirror clone instead of the running
project.

`shaft dataset` exports experiences. `shaft finetune` trains a judged MLX LoRA adapter. Training accepts
`--iters N` and `--min-examples N`, default 20 examples. `--evaluate` evaluates an adapter.

`shaft browser install` installs browser support, optionally with a specific Playwright version. `shaft
recover` writes up unfinished conversations.

`shaft deploy TAG --json` moves this checkout onto release `TAG` as [deploy](deploy.md) says, one JSON line per step:
it fast-forwards main and the template worktree to `TAG` and `template-TAG` (refusing a dirty tree or a
non-fast-forward), updates the instances, asks the scheduler for a drain-restart, and restarts api, web and telegram
only when no conversation is open (otherwise it ends with a `waiting` step; `--services-only` retries that part).
`shaft deploy --check --json` names the newest release on origin it could move to. The Mac app's Update server runs
both.

`shaft migrate source --to HOST` guides migration to another Mac over SSH. Enter runs each step, `q` stops,
and completed steps resume later. There is no `--yes`. On the new Mac, `migrate target` repairs paths,
rebuilds environments, starts services, and checks instances. It requires the source manifest. Both `--check`
modes run read-only checks. See [migration](migration.md).
