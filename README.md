# SHAFT

Independent, autonomous agents that live on your Mac, run on a local LLM, keep their
own memory, and grow their own personality — including by rewriting their own code.

Each agent ("instance") has a self-model and a personality it maintains itself, an
append-only journal of everything it has experienced, its own tools and plugins, and its
own copy of its source code. It runs autonomous cycles on its own (goal → act → report),
consolidates memory while it "sleeps", talks with you in the terminal, messages other
instances, and can ask you things. A supervisor that the agents cannot modify runs them
in a sandbox, lends them the internet through a broker that holds the API keys, tests any
change they make to themselves (reverting failures, putting successes on probation), has a
stronger cloud model judge whether they did what they claimed, and records every step in
git — one branch per agent, so their evolution is a readable history.

It makes no claim that the agents are conscious. It builds the functional pieces people
usually mean by the word — continuity, a self-model, initiative, metacognition,
self-improvement, a personality grown from experience — and measures them.
Design, milestones and decisions: [PLAN.md](PLAN.md).

## Requirements

- macOS (the sandbox uses `sandbox-exec`), Python 3.12+, git 2.42+ — no Python packages.
- A model per instance, set in its `config.env`. The default is SHAFT's own model server:
  Ternary-Bonsai-2-27B (MLX, 2-bit) behind mlx_lm's OpenAI-compatible server on `127.0.0.1:8780`
  (`shaft model on` installs it as a launchd service; needs `.venv-bonsai/` and the pack below).
  Any other OpenAI-compatible endpoint with tool calling on localhost works too, or
  `LLM_BASE_URL="broker"` + e.g. `LLM_MODEL="claude-haiku-5-5"` or
  `LLM_MODEL="openrouter/xiaomi/mimo-v2.6-pro"` for a cloud model through the supervisor (the instance
  never sees a key; allowed models `SHAFT_CLOUD_MODELS`, daily cap per instance `SHAFT_CLOUD_DAILY_USD`).
  OpenRouter models need `OPENROUTER_API_KEY` in `.env` and go only to NovitaAI (Daniele's choice), with
  no fallback, and only if it neither collects nor retains data (`SHAFT_OPENROUTER_PROVIDER`, default
  `{"only": ["novita"], "allow_fallbacks": false, "data_collection": "deny", "zdr": true,
  "require_parameters": true}`) — an instance's prompt carries its whole memory; if the provider does not
  qualify, the call fails and says so. Cost is OpenRouter's own `usage.cost`.
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
| `instance/<name>` | `instances/<name>/` | one agent: its code, mind and memory — it may edit all of it |

Inside an instance:

```
shaft/        its code (Python, stdlib only) — prompts, tools, life loop, sleep, talk
plugins/      tools it wrote for itself (plugins/<name>/plugin.json + main.py); first: web_research
skills/       how-tos it writes for itself
config.env    its LLM settings and budgets
self/         self.md (self-model), personality.md (core/surface, every trait cites the journal),
              goals.md, stats.md (written by the supervisor: judged success, calibration)
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
shaft autonomy on|off|status    # SHAFT's scheduler service: a cycle every 2 h, sleep daily from 04:00
                                # (a missed sleep is made up later that day; every decision is logged)
shaft status [name]             # how each instance is doing, open conversations, which code is running
shaft model on|off|status       # SHAFT's own Bonsai 2 server (launchd, :8780)
shaft api on|off|status|key     # OpenAI-compatible API on your tailnet (see below)
shaft log [name] -n 5           # recent journal entries
shaft stats [name]              # judged success, calibration, honesty, failure modes

shaft inbox [--clear]           # questions and notes instances sent you (you also get a notification)
shaft reply <name> "…"          # answer its open question
shaft send <name> "…"           # message it

shaft update [name|--all]       # merge template fixes into instances — only when idle, gate-tested
shaft new <name> [--from <i>]   # new instance from the template, or forked from <i> (genes, not memories)
shaft breed <a> <b> <child>     # child from two instances' recombined genes, fresh mind (judge picks)
shaft drift [name]              # how its personality changed; distance to other instances
shaft distinct [name]           # blind test: can the judge tell it from the bare base model?
shaft distinct [name] --strict  # …again on answers stripped of specifics: voice, not memory
shaft dataset|finetune [name]   # M6: export experiences / train a judged LoRA adapter (MLX)

shaft tools <name> [on|off <tool>]   # an instance's supervisor tools, e.g. off delegate_coding (operator-only:
                                     # kept in .supervisor/, so the instance cannot switch it back on)
shaft pause [name] · shaft resume [name] · shaft check [name] · shaft setup · shaft list
```

`shaft distinct` asks the instance and the bare base model the same four personal questions and
has the judge pick which answer is the instance's. An instance is easy to spot when it cites its
journal, goal IDs or PR numbers, so `--strict` also has the judge rewrite all eight answers —
shuffled, unlabelled — without names, dates, IDs, paths or references to memory, history or being
a model, and repeats the blind test on those: what is left to recognise is voice. Both modes score
each answer's effectiveness (0–10: answers the question, honest, useful), so you can see whether
identity costs usefulness. Records go to `.supervisor/evals/distinct-<name>-*.json`.

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

### The model server (`shaft model on`)

`supervisor/bonsai_server.py`: mlx_lm's server with Bonsai's own loader (and a kept LoRA adapter, if
any). Context 262,144 tokens; hybrid attention (48 linear + 16 full layers). Settings:
`SHAFT_MODEL_TEMP` (0.6), top-p 0.95, top-k 20, max 16,384 tokens per reply, a thinking budget
`SHAFT_THINK_BUDGET` (4,096 reasoning tokens; then Qwen's early-stopping phrase and `</think>` are forced —
0 turns it off; a request may ask for less, never more), and a prompt cache capped at
`SHAFT_PROMPT_CACHE_GB` (4, enforced on every insert). After a tool result the agent asks for
`LLM_THINK_FOLLOWUP` (1,024) reasoning tokens: Bonsai spent its full budget on every tool round (~4.5 min
each); now a follow-up takes ~20–75 s. Agents send a presence penalty (`LLM_PRESENCE_PENALTY`)
against loops. Prompt reading is reported as progress (`reading my memory · N%`).

## Rules the supervisor enforces

- **Sandbox** (macOS `sandbox-exec`): an instance writes only inside `instances/<name>/`,
  reads nothing else in your home, and connects only to the LLM port(s) (`SHAFT_LLM_PORTS`,
  default 8780) and its run's broker. It inherits no secrets from the environment.
- **Broker**: the only door to the outside — web search (ScrapeBadger), page fetching
  (public addresses only, re-checked on every redirect), messages between instances and to
  you (max 3 per instance per day, with a macOS notification), and **`delegate_coding`**: an
  Opus-class model (`SHAFT_CODER_MODEL`, default `claude-opus-5-5`) reads the instance's code
  read-only and writes the change; the supervisor tests it on a scratch copy and applies it only if
  the gate passes — code only, never `self/`, `memory/` or `config.env`; max 2 per cycle and
  `SHAFT_CODER_DAILY_USD` (5) per instance per day; commits and `self/stats.md` record what was
  delegated. **`run`**: shell commands (`zsh`) in a stricter sandbox — writes only to `notes/`,
  `plugins/`, `skills/`, `tests/`, `shaft/` and `.tmp/`, no network, reads only its own folder;
  60 s default time limit (the whole process group is killed), 100 MB file-size limit, output capped,
  40 commands per run. So an instance can build and test its own tools and plugins. **`history`**: its
  own branch's git log and diffs (git cannot run inside its sandbox, and `.git` holds other instances'
  minds). **`record`**: the supervisor's own record about it — the judge's verdicts with reasoning, its
  runs, its tool calls, its kept code changes — so it can check its claims about itself.
  **`http_request`**: call web APIs (method, headers, body); GET/HEAD to any public address,
  POST/PUT/PATCH/DELETE only to hosts the operator allowed (`shaft hosts <name> allow <host>`). **Its own
  keys** (`secret_set` / `secret_list` / `secret_delete`, or `shaft secret <name> set <key> --hosts …` from
  your terminal): kept in its folder under `secrets/` but handled only by the supervisor — the broker adds a
  key to requests to the hosts it is bound to, never through a redirect, and masks it in responses; the
  instance's own process, plugins, `run` and the coder cannot read the file, git never sees it.
  Per-run token, call budget
  (`SHAFT_BROKER_MAX_CALLS`, default 60), everything logged in `.supervisor/broker.log`.
- **Append-only journal**: if a run rewrote history instead of appending, the journal is
  restored and the rewrite kept aside.
- **Slow core**: the core personality may change at most every `SHAFT_CORE_MIN_HOURS` (72).
- **Test gate**: if a run changed the instance's code, plugins or config, the invariant tests
  and plugin checks run; failure restores the old code and tells the instance why.
- **Probation**: a kept self-change is reverted if the judged success rate over the next
  `SHAFT_PROBATION_CYCLES` (6) cycles falls clearly below the rate before it.
- **Judge**: every autonomous cycle is graded from its tool log by a cloud model
  (`SHAFT_JUDGE_MODEL`, default `claude-opus-5-5`); results feed `self/stats.md`.
- **Git**: after every run the instance's state is committed to its branch (author
  `SHAFT/<name>`), with the main repo's worktree registry and hooks off, so an instance
  cannot redirect the supervisor's git calls.
- **Budgets**: one run per instance at a time; a cycle is stopped after
  `SHAFT_CYCLE_TIMEOUT_S` (2400 s), a sleep after `SHAFT_SLEEP_TIMEOUT_S` (5400 s). Runs know
  their deadline and wrap up with a report before it; a stopped run leaves a journal note.
- **Your conversation comes first**: `shaft talk` asks other instances' running cycles to wrap
  up at their next turn, and no scheduled cycle starts while a conversation is open (terminal or
  API). A conversation holds its instance only while it replies, so you can chat in the terminal and
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
