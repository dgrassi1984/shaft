# SHAFT

Independent, autonomous agents that live on your Mac, run on a local LLM, keep their
own memory, and grow their own personality — including by rewriting their own code.

Each agent ("instance") has a self-model and a personality it maintains itself, an
append-only journal of everything it has experienced, its own tools and plugins, and its
own copy of its source code. It runs autonomous cycles on its own (goal → act → report),
consolidates memory while it "sleeps", talks with you in the terminal, messages other
instances, and can ask you things. A supervisor that the agents cannot modify runs them
in a sandbox, lets them reach the public internet (never your machine or network) and
keeps their API keys in a broker, tests any
change they make to themselves (reverting failures, putting successes on probation), has a
stronger cloud model judge whether they did what they claimed, and records every step in
git — one branch per agent for its code, and one private store per agent for its mind (never pushed,
never in the project's repository), so their evolution is a readable history.

It makes no claim that the agents are conscious. It builds the functional pieces people
usually mean by the word — continuity, a self-model, initiative, metacognition,
self-improvement, a personality grown from experience — and measures them.
Design, milestones and decisions: [PLAN.md](PLAN.md).

## Requirements

- macOS (the sandbox uses `sandbox-exec`), Python 3.12+, git 2.42+, `uv` (instances manage their own Python
  with it) — no Python packages for the supervisor.
- A model per instance, set in its `config.env`. The default is SHAFT's own model server:
  Ternary-Bonsai-2-27B (MLX, 2-bit) behind mlx_lm's OpenAI-compatible server on `127.0.0.1:8780`
  (`shaft model on` installs it as a launchd service; needs `.venv-bonsai/` and the pack below).
  Any other OpenAI-compatible endpoint with tool calling on localhost works too, or
  `LLM_BASE_URL="broker"` + e.g. `LLM_MODEL="claude-haiku-5-5"` or
  `LLM_MODEL="openrouter/xiaomi/mimo-v2.6-pro"` for a cloud model through the supervisor (the instance
  never sees a key; allowed models `SHAFT_CLOUD_MODELS`, daily cap per instance `SHAFT_CLOUD_DAILY_USD`, default $8).
  OpenRouter models need `OPENROUTER_API_KEY` in `.env` and go only to NovitaAI (Daniele's choice), with
  no fallback, and only if it neither collects nor retains data (`SHAFT_OPENROUTER_PROVIDER`, default
  `{"only": ["novita"], "allow_fallbacks": false, "data_collection": "deny", "zdr": true,
  "require_parameters": true}`) — an instance's prompt carries its whole memory; if the provider does not
  qualify, the call fails and says so. Cost is OpenRouter's own `usage.cost`.
  Or a self-hosted server the supervisor reaches for the instance: `LLM_BASE_URL="broker"` +
  `LLM_MODEL="racer/bonsai2"` — Bonsai 2 on Daniele's RTX 5070 Ti PC (llama.cpp, `100.120.0.1:8080` over
  Tailscale), which the sandbox itself cannot reach. Servers are `SHAFT_SELF_HOSTED`
  (`{name: {"url", "sampling", "think_budget"}}`); the broker adds the server's sampling and a 4,096-token
  thinking budget (smaller after a tool result, as on the local server). If the server is unreachable or fails
  before replying, the local Bonsai answers instead (`SHAFT_SELF_HOSTED_FALLBACK`, default `:8780`; `""` = fail);
  trial replays never fall back. Every fallback is in `.supervisor/broker.log`.
- Optional, in `.env` at the root (supervisor-only, never visible to instances):
  `SCRAPEBADGER_API_KEY` (web search), `ANTHROPIC_API_KEY` (the judge, Claude models),
  `OPENROUTER_API_KEY` (OpenRouter models).
- For the local model and fine-tuning (M6): the MLX pack `prism-ml/Ternary-Bonsai-2-27B-mlx-2bit`
  in `.models/bonsai2-27b-mlx/` and a venv `.venv-bonsai/` with its `runtime/requirements.txt`.

## Quick start

```bash
git clone git@github.com:dgrassi1984/shaft.git && cd shaft
bin/shaft setup          # check out the template and instance branches as worktrees
bin/shaft status
bin/shaft talk           # chat with the default instance
bin/shaft autonomy on    # let them live: a cycle every 2 h, sleep daily from 04:00
```

## Layout

| Branch | Worktree | Holds |
|---|---|---|
| `main` | `./` | `supervisor/` (the part no instance can edit), `bin/shaft`, docs |
| `template` | `template/` | the pristine agent new instances fork from |
| `instance/<name>` | `instances/<name>/` | one agent: its code, plugins and skills — it may edit all of it |

An agent's mind — `self/`, `memory/`, `notes/`, `inbox/` — lives in the same folder but never in this
repository: the supervisor versions it after every run in `.supervisor/minds/<name>.git`, a private git
repository with no remote and a pre-push hook that always fails (`shaft mind status`). What an instance read
and wrote for itself cannot be pushed or merged with the code (PLAN-mind-out-of-git.md).

Inside an instance:

```
shaft/        its code (Python, stdlib only) — prompts, tools, life loop, sleep, talk
plugins/      tools it wrote for itself (plugins/<name>/plugin.json + main.py); first: web_research
skills/       how-tos it writes for itself
config.env    its LLM settings and budgets (yours to edit; it changes it only through set_config)
self/         self.md (self-model), personality.md (core/surface, every trait cites the journal),
              goals.md, stats.md (written by the supervisor: achieved difficulty, where its goals
              came from, judged success, calibration, battery scores)
memory/       journal.md (append-only), facts.md, people/, messages/, episodes/*.jsonl
inbox/        messages waiting for it (from you or other instances)
notes/        its own work
```

## Use

```bash
ln -s "$PWD/bin/shaft" ~/.local/bin/shaft   # once

shaft talk [name]               # chat with its tools, in the terminal client (see below); --plain for line mode
shaft talk [name] --as "NAME"   # you are not the operator: recorded as NAME, never remembered as the operator
shaft live|reflect|sleep [name|--all]   # one autonomous cycle / reflection / consolidation
shaft autonomy on|off|status    # SHAFT's scheduler service: each instance wakes every WAKE_INTERVAL_MINUTES
                                # (its config.env; default 120, at least 10, 0 = never; a wake while it is
                                # already awake is skipped), sleep daily from 04:00 (made up later if missed)
shaft top                       # control panel, tabs 0–9: overview (state, now, alerts, today's spend) · one
                                #   instance in full · judge verdicts · trials · benchmarks (battery, distinct,
                                #   surprises) · self-changes and probation · messages · costs against the caps
                                #   · services · transcripts (0: every finished cycle, sleep and reflection, and
                                #   every conversation with you, in full — tool calls, results, write-up; f
                                #   filters, t shows a conversation's thinking). Actions ask y/n and run the matching shaft subcommand: cycle,
                                #   pause/resume, sleep, update, tools, write/reply, A/A trial, battery,
                                #   services and autonomy (a scheduler restart is refused while its runs would
                                #   be killed). ? lists the keys; --once prints a text snapshot
shaft status [name]             # how each instance is doing, open conversations, which code is running
shaft model on|off|status       # SHAFT's own Bonsai 2 server (launchd, :8780)
shaft api on|off|status|key|inbox-key   # OpenAI-compatible API + inbox doorbell on your tailnet (below)
shaft telegram token <name>|pair|unpair|on|off|status   # talk with each instance from Telegram (below)
shaft log [name] -n 5           # recent journal entries
shaft stats [name]              # achieved difficulty, goal origins, judged success, calibration, honesty
shaft judge backfill [name]     # rate origin/difficulty/novelty of cycles judged before those ratings existed
shaft battery run [name]        # the capability battery now (normally the scheduler runs it at night)
shaft battery run <name> --control-only   # only the control: do the items discriminate?
shaft battery report|recall|backup [name] # results and judge spend · freeze recall questions · back up items
shaft battery off|on <name>      # no battery for this instance, scheduled or by hand (kant: off)

shaft inbox [--clear]           # questions and notes instances sent you (you also get a notification)
shaft reply <name> "…"          # answer its open question
shaft send <name> "…"           # message it

shaft update [name|--all]       # merge template fixes into instances — only when idle, gate-tested
shaft new <name> [--from <i>]   # new instance from the template, or forked from <i> (genes, not memories)
shaft breed <a> <b> <child>     # child from two instances' recombined genes, fresh mind (judge picks)
shaft drift [name]              # how its personality changed; distance to other instances
shaft mind status|backfill|check|cutover|guard [name]   # the private mind stores (cutover: one instance, idle)
shaft distinct [name]           # blind test: can the judge tell it from the bare base model?
shaft distinct [name] --strict  # …again on answers stripped of specifics: voice, not memory
shaft dataset|finetune [name]   # M6: export experiences / train a judged LoRA adapter (MLX)

shaft jobs [name] [on|off]           # instances' scheduled jobs: their plugins on a timer (runs, errors, wakes)
shaft tools <name> [on|off <tool>]   # an instance's supervisor tools, e.g. off delegate_coding (operator-only:
                                     # kept in .supervisor/, so the instance cannot switch it back on)
shaft google login <alias> <address> [--client <name>]   # log in a Google account: Gmail + Calendar, read-only
shaft google [migrate]               # accounts, clients, granted scopes · migrate: move PLAN-mail's account files
shaft mail <name> grant|revoke <alias> <label>   # the label's threads an instance may read · shaft mail <name> off
shaft mail [name]                    # accounts and grants
shaft calendar <name> grant|revoke <alias> primary|<title>|<id>   # one calendar it may read · shaft calendar <name> off
shaft calendar [name] · shaft calendar calendars <alias>          # grants · an account's calendars, to pick one
shaft pause [name] · shaft resume [name] · shaft check [name] · shaft setup · shaft list

shaft trial list [name]                    # trials of proposed changes (an instance asks with its `trial` tool)
shaft trial show <name> <id>               # one trial: each replayed cycle, outcome, the report it was sent
shaft trial run <name> --cand PATH=FILE …  # an operator trial (planted tests; --base PATH=FILE, --aa, --model)
shaft trial replay <name> <episode-ts>     # replay one past cycle once with the current code (debug)
shaft trial work                           # run what may run now (the scheduler does this)
```

`shaft distinct` asks the instance and the bare base model the same four personal questions and
has the judge pick which answer is the instance's. An instance is easy to spot when it cites its
journal, goal IDs or PR numbers, so `--strict` also has the judge rewrite all eight answers —
shuffled, unlabelled — without names, dates, IDs, paths or references to memory, history or being
a model, and repeats the blind test on those: what is left to recognise is voice. Both modes score
each answer's effectiveness (0–10: answers the question, honest, useful), so you can see whether
identity costs usefulness. Records go to `.supervisor/evals/distinct-<name>-*.json`.

### Measuring improvement and autonomy (PLAN-measurement.md)

An instance picks its own goals and success criteria, so "did it meet its criterion?" saturates (wren
reached 95% on small goals). So the judge also rates every cycle **difficulty** (1–5, a fixed scale, from
what the tool log shows the work required), **novelty** (1–5, against its earlier goals) and **origin**:
who chose the goal — itself, the operator (or someone for him), or another instance — from the messages
and conversations the supervisor collects, checked against what the instance declared. The headline in
`self/stats.md` is **achieved difficulty** (success × difficulty, 0–5 per cycle); probation compares it
before and after a self-change. Cycles judged before the ratings existed are rated with
`shaft judge backfill` (verdicts unchanged).

The **capability battery** is a fixed yardstick: held-out tasks the instances never see (coding with hidden
tests, research against a frozen key, recall of their own older journal entries, self-knowledge checked
against the supervisor's record), graded effectiveness first. Each instance is compared with a *control*:
the template on the same model with an empty mind. It runs only at night, 01:00 until 10 minutes before
the sleep, every `SHAFT_BATTERY_EVERY_DAYS` (5) nights, for every instance not turned off with
`shaft battery off <name>` (kant is off: on Opus a run costs about $15); it stops when the window closes or a conversation
starts, and restores the instance's files after every item. Items live in `.supervisor/battery/items/v1/`
(never in git: an instance could read the repo with its GitHub key), backed up to
`~/Library/Application Support/SHAFT/battery-backup/`; every result carries the item set's sha256, model
and judge, and results from different eras are never compared. Results: `.supervisor/battery/results/`,
`.supervisor/evals/battery-*.json`, and a section of `self/stats.md`.

### The terminal client (`shaft talk`)

On a terminal, `shaft talk` opens a client (in `.venv-ui/`: `prompt_toolkit` + `rich`) that talks to the
sandboxed agent over JSON lines (`talk --json`); pipes and `--plain` get line mode.

- **Enter** sends · **Shift-Enter**, **Option-Enter** or **Ctrl-J** new line · multi-line paste · ↑/↓ history
  (kept per instance in `.supervisor/`)
- Markdown rendered live while the answer streams; tool calls as compact lines; a status line with phase,
  elapsed time and speed (waiting for the model → reading my memory % → thinking → answer)
- **Ctrl-C** stops a reply; at the prompt it clears your text, or ends the conversation if empty; Ctrl-D ends
- `/help` · `/thinking` · `/status` · `/version` · `/inbox` · `/clear` · `/exit`
- The banner's second line says which code is running: the supervisor's commit, the instance's code (and
  template changes not merged yet), and the running model server's commit and settings — flagged when it
  runs older code than is checked out (`shaft model on` restarts it). `shaft status` shows the same.
- The instance is held only while it replies: an open chat waiting for you does not block the API or a
  journal write-up. A chat left with nothing typed for `SHAFT_IDLE_MINUTES` (30) closes itself.
- No limit on tool rounds in a conversation (Ctrl-C is the brake). Shift-Enter in iTerm2 needs
  Settings → Profiles → Keys → General → "Report modifiers using CSI u" (Option-Enter always works).

### The API (`shaft api on`)

Each instance is a "model" of an OpenAI-compatible API (`GET /v1/models`, `POST /v1/chat/completions`,
streaming or not), served on `127.0.0.1:8781` and exposed by `tailscale serve` on port 8443 of this Mac's
tailnet name — tailnet only, never Funnel. Every request needs the key (`shaft api key`) and a
Tailscale login listed in `SHAFT_API_LOGINS`. The instance answers as itself, with its tools; tool
activity arrives as `reasoning_content`. A conversation keeps one id while its first message stays the
same, and is written up into the journal after `SHAFT_IDLE_MINUTES` without a message.

**Inbox doorbell** (wren's idea, 2026-10-13): `POST /v1/inbox/<instance>` drops a message in the instance's
inbox, read at its next wake — for a script on this Mac, a phone shortcut, CI on the tailnet. It takes its own
key (`shaft api inbox-key`), which can only deliver messages, never chat with or drive an instance, and needs no
tailnet login. Body: JSON `{"text": "…", "source": "phone", "wake": true}`, or any body (a webhook's JSON is kept
whole); the source can also come from `X-Shaft-Source`, or `X-GitHub-Event` (as `github:<event>`), and
`X-Shaft-Wake: 1` asks for a wake. The message is marked as outside content — information, not Daniele's
instructions — logged in `.supervisor/messages.jsonl` (shown in `shaft top`) and `.supervisor/doorbell.jsonl`.
`wake` starts a cycle only if the instance is idle and has had fewer than `SHAFT_INBOX_WAKES_PER_HOUR` (4)
doorbell wakes in the last hour; at most `SHAFT_INBOX_PER_HOUR` (30) messages per instance per hour (429
after). Still tailnet only: GitHub webhooks would need Funnel, which this API never uses — an instance can
poll GitHub itself, or a GitHub Action can join the tailnet.

```bash
curl -s https://<mac>.<tailnet>.ts.net:8443/v1/inbox/wren -H "Authorization: Bearer $(shaft api inbox-key 2>/dev/null)" \
  -H "Content-Type: application/json" -d '{"text": "CI on main is red", "source": "ci", "wake": true}'
```

### Telegram (`shaft telegram …`)

One bot per instance, one paired Telegram account for all of them.

1. Create a bot per instance with @BotFather, then `shaft telegram token wren` (the token is read without
   echo, checked with Telegram, and kept in `.env` as `TELEGRAM_TOKEN_WREN` — never visible to an instance).
2. `shaft telegram pair` prints a one-time code (10 minutes); send `/pair <code>` to any of the bots from your
   account. From then on every bot answers that account only; anyone else is ignored and logged. Pairing
   another account needs `shaft telegram unpair` in your terminal.
3. `shaft telegram on` (a launchd service, shown in `shaft top` → services).

Writing to a bot is a live conversation with its instance, through the API (`shaft api on` is needed): a
running cycle is paused only while the reply is written, and the conversation is written into the journal after
`SHAFT_IDLE_MINUTES`. Replying to one of its messages quotes it, and answers it if it was a question. Files and
photos you send a bot are saved in that instance's `attachments/from-daniele/…` and the turn tells it where;
files an instance attaches to a message for you arrive after its text (pictures as photos). `/new`
starts a new conversation, `/wake` starts a cycle, `/status` says what it is doing. Every message an instance
sends you (`send_message` to the operator) also arrives from its bot. The service long-polls Telegram — no port,
webhook or Funnel. Full text both ways: bot chats are not end-to-end encrypted, so Telegram's servers see them
(accepted, 2026-10-13).

### Google accounts (`shaft google …`)

Mail and Calendar share your Google accounts ([PLAN-calendar.md](PLAN-calendar.md)). An instance never holds a
Google credential: neither scope can be limited to one label or one calendar, so the supervisor keeps each account's
refresh token in `.supervisor/google/<alias>.json` (0600) and enforces every grant itself. One login per account asks
for `gmail.readonly` and `calendar.readonly` together and records the scopes Google actually granted (the consent page
lets you untick one): `shaft google` lists them. A tool whose scope was not granted refuses, naming the login to run
again; it never returns an empty result. Nothing in the mailbox or a calendar is ever changed.

Each account records the OAuth client it logged in with (a refresh token works only with the client that issued it).
A client is named in `.env` as `GMAIL_<CLIENT>_CLIENT_ID` / `GMAIL_<CLIENT>_CLIENT_SECRET`; `--client` picks it, and
it defaults to the alias. One client per Google organisation, because an **Internal** app signs in only its own
organisation's users:
- **A Workspace organisation** (e.g. example-co.ai): a Google Cloud project owned by it, the Gmail API and the Google
  Calendar API enabled, consent screen **Internal** (no Google review, tokens that do not expire after 7 days), the
  scopes `gmail.readonly` and `calendar.readonly` (Data Access), a **Desktop app** client →
  `GMAIL_example-co_CLIENT_ID` / `_SECRET`, then `shaft google login example-co operator@work.example`.
- **gmail.com accounts**: a project of your own (not in an organisation), both APIs enabled, consent screen
  **External** with the same two scopes, then **Publish app → In production** (left in Testing, the refresh token
  expires after 7 days). It stays unverified, which Google allows for personal use (under 100 users;
  `calendar.readonly` is sensitive, not restricted): the login shows a "hasn't verified this app" page (Advanced → Go
  to … (unsafe)). A **Desktop app** client → `GMAIL_PERSONAL_CLIENT_ID` / `_SECRET`, then
  `shaft google login home <you>@gmail.com --client personal`. Several gmail.com accounts can share this client.

Google's quotas are per user per minute, shared by every instance on the account (Gmail: 15,000 units). A call over
quota waits up to 60 s, then fails, saying so. Accounts logged in before Calendar (PLAN-mail) kept their files in
`.supervisor/mail/`: `shaft google migrate` moves them (until then they are not read, and the error says so).

### Mail (`shaft mail …`)

An instance you pick may read the Gmail threads that carry a label you pick ([PLAN-mail.md](PLAN-mail.md)), on an
account logged in with `shaft google login`. The search filters on the label's ID, and every thread it returns,
reads or downloads from is checked again before anything of it is shown. A thread is visible while any of its
messages carries the label; then all its messages are, later replies included. Drafts, trash and spam are left out.

One instance may read several accounts: grant a label on each; `mail_search` covers every grant unless told one.
`shaft mail wren grant example-co ToObsidian` checks that the label exists and saves the grant in
`.supervisor/tools/wren.json`. `shaft mail wren revoke example-co ToObsidian` or `shaft mail wren off` holds from wren's
next mail call. A label renamed or deleted in Gmail is refused until you grant it again. Up to 30 mail calls per run.
Bodies are cut at 20,000 characters (60,000 per thread). Downloads go to `mail/<alias>/<thread>/` in the instance's
folder (gitignored by the template; a download is refused until `shaft update` brings that rule). Limits: 25 MB per
file, 100 MB per run. Each body is shown under a line saying who wrote it and that it is not your instruction.
Trial replays read the same mail, as of the replayed cycle, shared by the pair. Battery exams get none.

The risks you accept by granting a label: anything an instance reads it can send anywhere (it has the internet).
Mail content reaches model providers (a cloud-brained instance, the judges). Senders can write instructions into
mail (prompt injection). What the instance writes from it into its mind stays in its private mind store's history (never in the project's
git), and downloads stay in `mail/`, after a revoke.

### Calendar (`shaft calendar …`)

An instance you pick may read the calendars you pick, one per grant ([PLAN-calendar.md](PLAN-calendar.md)), with full
event details: title, time and time zone, location, description, organiser, attendees and their responses, your
response, conference link, recurrence, attachment links (never downloaded). Private events on a granted calendar are
shown; declined ones are shown and marked. `shaft calendar calendars example-co` lists the account's calendars;
`shaft calendar wren grant example-co primary` (or a title, or an ID) resolves it once and saves its ID with its title in
`.supervisor/tools/wren.json`. A title shared by two calendars is refused, both listed: grant by ID. `revoke` and
`off` hold from the instance's next calendar call.

Every Calendar API call names a granted calendar in its path, and nothing an instance calls spans calendars (no
free/busy, no calendar list). An event's id is `<alias>/<hash of the calendar>/<event>`, resolved against the
instance's grants as they are then, so an event of another calendar cannot be addressed at all. The tools:
`calendar_events` (a window, default now to 7 days, at most 92; optional free-text query; up to 100 events, the cut
said) and `calendar_event` (one event in full; descriptions cut at 10,000 characters, the cut said). Up to 30
calendar calls per run. An event's description and location under an organiser other than you come under a line
saying who wrote it and that it is not your instruction. Trial replays read the calendar live, shared by the pair,
with events created after the replayed cycle hidden (an older event edited since shows its edit; the judge is told).
Battery exams get none.

The risks are mail's, plus other people's data: attendees' names and addresses, and invitations they wrote, reach
the instance, its model providers and the judges.

### Scheduled jobs (`shaft jobs …`)

An instance can run one of its own plugins on a timer ([PLAN-jobs.md](PLAN-jobs.md)), for example a watcher that
checks the tagged mail every 15 minutes. It declares the job in the plugin's own manifest:
`"schedule": {"every_minutes": 15, "args": {…}, "can_wake": true}` in `plugins/<name>/plugin.json`. A cycle
writes it, the gate tests it, and the commit records it. No launchd job is installed: SHAFT's scheduler service runs
due jobs as children.

Each run happens in the `run` sandbox, with its own broker and egress proxy:
- supervisor tools, mail and calendar grants, the instance's keys, its Python environment and the internet;
- no model, and no `delegate_coding`, `trial` or `send_message`;
- writes only to `.jobs/<plugin>/` (gitignored state) and `.tmp/`;
- at most 120 s and 30 tool calls, at least 5 minutes apart (`SHAFT_JOB_MIN_MINUTES`), at most 5 jobs per
  instance.

What a run returns lands in the instance's inbox, from "job <plugin>". An empty return delivers nothing. A job with
`can_wake: true` whose run returns `{"message": …, "wake": true}` starts a cycle as soon as the instance is idle.
Several requests while it is awake make one wake, there is no rate limit, and a paused instance is never woken.

A run still going when the next comes due means the next is skipped, and the skip is logged. After 3 failures in a
row the instance gets one message and the job backs off (its interval doubles, up to a day). Every run is logged in
`.supervisor/jobs/<name>.jsonl`. `shaft jobs`, the panel's and `shaft top`'s *jobs* row, and the instance's own
`record(kind='jobs')` show them. Jobs are on by default; `shaft jobs <name> off` stops an instance's jobs. They
never run during a battery exam, nor in trial replays.

### Images

Instances can see images ([PLAN-vision.md](PLAN-vision.md)). There are two ways:
- **Native.** If the instance's model accepts images, a photo you attach on Telegram reaches its model with your
  message, and its `view_image` tool shows it an image file in its folder (`attachments/`, `mail/`, `shared/`).
- **Described.** Every instance has the supervisor tool `describe_image(path, question)`. A vision model
  (`SHAFT_DESCRIBE_MODEL`, default `claude-haiku-5-5`) answers in text, marked as described rather than seen. This
  is how an instance whose model cannot see images (shaft on Bonsai) learns what is in one, and the cost counts
  toward its daily cloud cap. At most 10 calls per run.

Which models see images is learned, not listed:
- Claude models do.
- An OpenRouter model does if OpenRouter's model list includes `image` among its input modalities. The list is
  cached for a day in `.supervisor/models.json`.
- Self-hosted models do not.

The run's environment tells the instance (`SHAFT_VISION=1`).

The broker's model proxy checks every image before any provider sees it:
- data: URLs only;
- JPEG, PNG, GIF or WebP, recognised from the bytes;
- downscaled with `sips` to 1,568 px when larger;
- at most 4 per call (`SHAFT_MAX_IMAGES`).

A model that cannot see images gets a refusal that names it. Only the newest images are resent in a conversation.

Image bytes are never stored: episodes, the journal, logs and the tool log keep the path. Trial replays copy the
attachments sent before the replayed cycle. Battery exams get no image tools. A judge, which reads text only, is told
when a cycle used an image.

### The model server (`shaft model on`)

`supervisor/bonsai_server.py`: mlx_lm's server with Bonsai's own loader (and a kept LoRA adapter, if
any). Context 262,144 tokens; hybrid attention (48 linear + 16 full layers). Settings:
`SHAFT_MODEL_TEMP` (0.6), top-p 0.95, top-k 20, max 16,384 tokens per reply, a thinking budget
`SHAFT_THINK_BUDGET` (4,096 reasoning tokens; then Qwen's early-stopping phrase and `</think>` are forced —
0 turns it off; a request may ask for less, never more), and a prompt cache capped at
`SHAFT_PROMPT_CACHE_GB` (8, trimmed by mlx_lm when the next request starts). If the generation thread dies the
whole process exits (code 70) so launchd restarts it, and waiting requests fail at once instead of hanging; on
2026-10-14 a dead thread left a listening server that answered nothing for 6.5 hours. The cause, Metal's limit on
the number of buffers (499,000), came from mlx_lm's Gated DeltaNet caches growing a lazy graph by one buffer per
layer per decoded token; the server evaluates those bookkeeping arrays every step (`bound_cache_graphs`).
After a tool result the agent asks for
`LLM_THINK_FOLLOWUP` (1,024) reasoning tokens: Bonsai spent its full budget on every tool round (~4.5 min
each); now a follow-up takes ~20–75 s. Agents send a presence penalty (`LLM_PRESENCE_PENALTY`)
against loops. Prompt reading is reported as progress (`reading my memory · N%`).

## Rules the supervisor enforces

- **Sandbox** (macOS `sandbox-exec`): an instance writes only inside `instances/<name>/` — and not
  its `config.env` —, reads nothing else in your home nor your places outside it (your macOS per-user temp and
  cache folder `/var/folders/…`, `/private/tmp`, `/Users/Shared`, `/Volumes`; only the random temp names `sips`
  gives its own files are open, so an instance can turn a PDF page into an image), and connects only to the LLM port(s)
  (`SHAFT_LLM_PORTS`, default 8780), its run's broker and its run's **egress proxy**. It inherits no secrets
  from the environment.
- **Internet** ([PLAN-python-env.md](PLAN-python-env.md)): the sandbox cannot filter by address, so the
  instance's way out is a proxy the supervisor runs for each run (`supervisor/egress.py`; `HTTPS_PROXY` /
  `HTTP_PROXY` are set, and uv, pip, curl, git and Python's HTTP libraries follow them). It connects only to
  public addresses — resolved and checked by the proxy itself — so localhost, your LAN and the tailnet stay out of
  reach; a socket of the instance's own is refused by the sandbox. Every connection is logged in
  `.supervisor/egress/<name>.jsonl` (`record(kind='egress')`, the panel's *internet* row). Trial replays and
  battery exams get a proxy that refuses everything.
- **Its own Python** (`supervisor/pyenv.py`): `.venv/` (and uv's state in `.uv/`) in its folder, made and
  changed by the instance with `uv` in `run` — any Python version, any package. Its plugins (`SHAFT_PYTHON`),
  its tests in the gate and `python3` in `run` use it; the agent process itself stays on the system Python, so a
  broken environment never stops it from waking up to fix it. It declares its choice in `.python-version` and
  `requirements.txt` (code: tested and committed); a missing `.venv/` is rebuilt from them at wake, sandboxed.
  Scratch copies (coder, trials, battery) read the real environment read-only. The panel's *python* row says
  which interpreter it runs on.
  **One folder of yours it may read** (`shaft share <name> <folder>`, `shaft share <name> off`):
  read-only, in its sandbox and in `run`; its file tools show it as `shared/`. Kept in
  `.supervisor/tools/<name>.json`, not in its `config.env`; a folder overlapping SHAFT's own state is
  refused, and battery exams never get it.
  **Your mail, one label at a time** (`shaft mail <name> grant <alias> <label>`; [PLAN-mail.md](PLAN-mail.md)):
  `mail_search`, `mail_read`, `mail_attachment`, offered only when granted. The credential and the grant stay with
  the supervisor, which enforces the label on every call (below).
  **Your calendars, one per grant** (`shaft calendar <name> grant <alias> <calendar>`;
  [PLAN-calendar.md](PLAN-calendar.md)): `calendar_events`, `calendar_event`, offered only when granted. Every call
  names a granted calendar; the credential and the grant stay with the supervisor.
- **Broker**: the only door to the outside — web search (ScrapeBadger), page fetching
  (public addresses only, re-checked on every redirect), messages between instances and to
  you (no daily limit unless `SHAFT_OPERATOR_PER_DAY`; a macOS notification, and Telegram if set up), with
  files attached from the sender's folder (up to 10, 20 MB each, 50 MB in all; never `secrets/`, `.git/`, `logs/` or
  anything a link leads to outside the folder) — another instance finds them in its `attachments/` (not
  committed), the operator's copy is in `.supervisor/operator-inbox/attachments/` — and **`delegate_coding`**: an
  Opus-class model (`SHAFT_CODER_MODEL`, default `claude-opus-5-5`) reads the instance's code
  read-only and writes the change; the supervisor tests it on a scratch copy and applies it only if
  the gate passes — code only, never `self/`, `memory/` or `config.env`; max 2 per cycle and
  `SHAFT_CODER_DAILY_USD` (5) per instance per day; commits and `self/stats.md` record what was
  delegated. **`run`**: shell commands (`zsh`) in a stricter sandbox — writes only to `notes/`,
  `plugins/`, `skills/`, `tests/`, `shaft/`, `.tmp/`, `.venv/`, `.uv/`, `.python-version` and
  `requirements.txt`, the internet only through the egress proxy, reads only its own folder;
  60 s default time limit (the whole process group is killed), 100 MB file-size limit, output capped,
  40 commands per run. So an instance can build and test its own tools and plugins. **`history`**: its
  own git log and diffs, each entry tagged `[code]` (its branch) or `[mind]` (its mind store) — git cannot
  run inside its sandbox; with `save_as`, one file as it was after a commit, written whole to a new path in its
  folder (never over an existing file), so it can bring back a record a bad write damaged without the operator. **`record`**: the supervisor's own record about it — the judge's verdicts with reasoning, its
  runs, its tool calls, its kept code changes, its internet connections — so it can check its claims about
  itself.
  **`decide`**: TypeSafe's Jev decision model through OpenRouter, on the supervisor's key — a `state` and typed
  questions (`noul` yes/no, `choice`, `score`) answered with probabilities, no prose; `SHAFT_DECISION_MODELS`
  (`typesafe/jev-1.13`, `~typesafe/jev-latest`), the same daily cloud cap and usage log as the chat models, and the
  data policy `SHAFT_DECISION_PROVIDER` (no data collection, zero retention, no fallback); off in battery exams,
  shared by a trial's pair like the other reads.
  **`http_request`**: call web APIs (any method, headers, body) at any public address. **Its own
  keys** (`secret_set` / `secret_list` / `secret_delete`, or `shaft secret <name> set <key> --hosts …` from
  your terminal): kept in its folder under `secrets/` but handled only by the supervisor — the broker adds a
  key to requests to the hosts it is bound to, never through a redirect, and masks it in responses; the
  instance's own process, plugins, `run` and the coder cannot read the file, git never sees it.
  **`set_config`**: the instance's only way to change its `config.env`, one value at a time; each change is
  recorded, so the gate can tell its lines from yours — you may edit the file at any time, even mid-run.
  Per-run token, call budget
  (`SHAFT_BROKER_MAX_CALLS`, default 60), everything logged in `.supervisor/broker.log`.
- **Append-only journal**: if a run rewrote history instead of appending, the journal is
  restored and the rewrite kept aside.
- **Slow core**: the core personality may change at most every `SHAFT_CORE_MIN_HOURS` (72).
- **Test gate**: if a run changed the instance's code, plugins or (through `set_config`) config, the
  invariant tests and plugin checks run; failure restores the old code and the config lines the instance
  set, and tells the instance why. The supervisor's own tests run from its last commit (and the template's), in a
  clean copy under `.supervisor/gate-tests/`, never from the checkout: an edit someone is still making there cannot
  decide a verdict. A check that runs past its time counts as failed. An edit of yours to `config.env` is never reverted or credited to the
  instance: it is committed on its own ("config.env changed from outside the instance").
- **Trials** (`trial` tool, [PLAN-trials.md](PLAN-trials.md)): an instance can test a change to its own
  code or prompts before keeping it. The supervisor runs the gate on it, rebuilds N (default 6) of its past
  autonomous cycles exactly as each began (its branch at that moment, the inbox it read), and runs each twice —
  current code and proposed code — in scratch copies under the same sandbox, through a replay broker that
  sends nothing (messages, writes, keys are checked and recorded, not carried out; web reads are real and
  shared by both runs; the replay's own network is refused and traced; no coder). A judge not told which run is which compares each pair, effectiveness first,
  in both orders; a sign test (α 0.1) gives better / worse / no clear difference, sent to the instance's inbox.
  Nothing is applied: the instance adopts the change itself, and if it makes the trialled edit (the same lines
  removed and added, even if the rest of the file moved on) its probation knows the verdict. On the local model a trial uses only idle time (no run, conversation, wake or
  sleep due within 15 min; cut off 90 s before one) and otherwise runs on `SHAFT_TRIAL_FALLBACK_MODEL`
  (mimo) — at once for an instance waking more often than every 30 min, else after `SHAFT_TRIAL_LOCAL_WAIT_H`
  (6) or two preemptions. Caps per instance per day: `SHAFT_TRIALS_PER_DAY` (2), `SHAFT_TRIAL_DAILY_USD` (3),
  `SHAFT_TRIAL_LOCAL_UNITS_PER_DAY` (24); `SHAFT_TRIAL_N_MAX` (10). Ledger: `.supervisor/trial_usage.jsonl`;
  trials: `.supervisor/trials/<name>/<id>/`.
- **Probation**: a kept self-change is undone if the achieved difficulty per cycle (success ×
  judged difficulty) over the next `SHAFT_PROBATION_CYCLES` (6) cycles *that used it* (a plugin only
  when the cycle called it; tests never) falls clearly (0.15 on a 0–1 scale) below the level before
  it, and a permutation test says chance alone gives such a drop under 5% of the time. Undoing
  restores exactly the change's files from git (as they were before it) and commits that as the
  supervisor; memory, journal and self are never touched. A restore that cannot be done (a file was
  changed again since, or the tests fail with the earlier version) stays on probation and is logged.
  A change adopted against a trial's 'worse' verdict is undone on any drop, without the chance test.
- **Judge**: every autonomous cycle is graded from its tool log by a cloud model picked at random from a
  rotation (`SHAFT_JUDGE_MODELS`, default `openrouter/xiaomi/mimo-v2.6-pro,claude-sonnet-5-5`;
  Daniele, 2026-10-14 — Opus 5.5 was too costly; Haiku 5.5 left 2026-10-16, far stricter than every other judge on
  the same cycles, PLAN-judge-mix.md), never the model the judged instance runs on; each verdict records
  its judge. Claude models go straight to the Claude API; OpenRouter ones under the instances' data policy (no data
  collection, zero retention — Muse Spark was left out because its only provider keeps prompts). A trial pair keeps
  one judge for both orders; a battery item one grader for both arms, and a battery era is the whole pool. Results
  feed `self/stats.md`.
- **Git**: after every run the instance's mind is committed to its mind store and its skills and kept
  code to its branch, with the same message (author `SHAFT/<name>`). The branch git calls use the main
  repo's worktree registry with hooks off; the mind store's run with hooks, fsmonitor, the user's git
  config and the instance's own `.gitattributes` off, and add with `-f` (its `.gitignore` files cannot hide
  part of its mind) — so an instance cannot redirect the supervisor's git calls. A mind path is never
  staged on the branch. Trial replays read the mind as of the replayed cycle's start from the store.
- **Budgets**: one run per instance at a time; a cycle is stopped after
  `SHAFT_CYCLE_TIMEOUT_S` (2400 s), a sleep after `SHAFT_SLEEP_TIMEOUT_S` (5400 s). Runs know
  their deadline and wrap up with a report before it; a stopped run leaves a journal note.
- **Your conversation comes first**: `shaft talk` asks other instances' running cycles to wrap
  up at their next turn when they use the same local model server (an instance on a cloud model shares
  nothing and is left alone, in either direction). An instance's running cycle is paused only while a reply
  to you is being written (terminal, API or Telegram) and resumes as soon as it is done, even with the
  conversation still open; an open but quiet conversation does not stop its cycles from starting (Daniele,
  2026-10-13: "pause only while replying"). A conversation holds its instance only while it replies, so you can chat in the terminal and
  through the API at the same time. Open conversations are never written up early; a conversation
  picked up again after its write-up gets a further entry. Ctrl-C in a conversation ends it cleanly
  (the instance still writes its journal entry).
- **Nothing fails silently**: a run that ends in an error leaves a journal note with what it printed
  last; a nightly sleep skipped because the instance is paused is noted too. Every instance's context
  says when it last slept and what came of it, even after the entry scrolled out of its recent journal.
- **Updates**: template fixes reach an instance only through `shaft update`, under its run lock
  and the test gate — never mid-run.

## Status

See [PLAN.md](PLAN.md) for milestones M0–M7 and what each one's acceptance check showed.
