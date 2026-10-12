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
- An OpenAI-compatible chat endpoint with tool calling on localhost (llama.cpp, Ollama,
  LM Studio, Atomic Chat…), set in each instance's `config.env`.
- Optional, in `.env` at the root (supervisor-only, never visible to instances):
  `SCRAPEBADGER_API_KEY` (web search), `ANTHROPIC_API_KEY` (the judge).
- Optional for fine-tuning (M6): `mlx-lm` and an MLX base model (`SHAFT_FT_BASE_MODEL`).

## Quick start

```bash
git clone git@github.com:dgrassi1984/shaft.git && cd shaft
bin/shaft setup          # check out the template and instance branches as worktrees
bin/shaft status
bin/shaft talk           # chat with the default instance
bin/shaft autonomy on    # let them live: a cycle every 2 h, sleep at 04:00
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

shaft talk [name]               # chat (with its tools); /exit or Ctrl-D to end; --show-thinking
shaft live|reflect|sleep [name|--all]   # one autonomous cycle / reflection / consolidation
shaft autonomy on|off|status    # launchd: `shaft tick` every 2 h, `shaft sleep --all` at 04:00
shaft status [name]             # how each instance is doing
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
shaft dataset|finetune [name]   # M6: export experiences / train a judged LoRA adapter (MLX)

shaft pause [name] · shaft resume [name] · shaft check [name] · shaft setup · shaft list
```

## Rules the supervisor enforces

- **Sandbox** (macOS `sandbox-exec`): an instance writes only inside `instances/<name>/`,
  reads nothing else in your home, and connects only to the LLM port(s) (`SHAFT_LLM_PORTS`,
  default 1337) and its run's broker. It inherits no secrets from the environment.
- **Broker**: the only door to the outside — web search (ScrapeBadger), page fetching
  (public addresses only, re-checked on every redirect), messages between instances and to
  you (max 3 per instance per day, with a macOS notification). Per-run token, call budget
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
- **Budgets**: one run per instance at a time; a cycle is killed after
  `SHAFT_CYCLE_TIMEOUT_S` (2400 s).

## Status

See [PLAN.md](PLAN.md) for milestones M0–M7 and what each one's acceptance check showed.
