# SHAFT

Independent, autonomous agents that live on your Mac, run on a local LLM, keep their
own memory, and grow their own personality — including by rewriting their own code.

Each agent ("instance") has a self-model it maintains itself, an append-only journal of
everything it has experienced, and its own copy of its source code. A supervisor that
the agents cannot modify runs them in a sandbox, tests any change they make to
themselves (reverting it if the tests fail), and records every step in git — one branch
per agent, so their evolution is a readable history.

It makes no claim that the agents are conscious. It builds the functional pieces people
usually mean by the word — continuity, a self-model, initiative, metacognition,
self-improvement — and measures them. Design, milestones and decisions: [PLAN.md](PLAN.md).

## Requirements

- macOS (the sandbox uses `sandbox-exec`), Python 3.12+, git 2.42+ — no Python packages.
- An OpenAI-compatible chat endpoint on localhost (llama.cpp, Ollama, LM Studio,
  Atomic Chat…), set in each instance's `config.env`.

## Quick start

```bash
git clone git@github.com:dgrassi1984/shaft.git && cd shaft
bin/shaft setup        # check out the template and instance branches as worktrees
bin/shaft status
bin/shaft talk         # chat with the default instance
```

## Layout

Each instance lives on its own git branch, so instances evolve independently and can
later be compared and recombined.

| Branch | Worktree | Holds |
|---|---|---|
| `main` | `./` | `supervisor/` (the part no instance can edit), `bin/shaft`, docs |
| `template` | `template/` | the pristine agent new instances fork from |
| `instance/<name>` | `instances/<name>/` | one agent: its code, mind and memory — it may edit all of it |

Inside an instance:

```
shaft/        its code (Python, stdlib only)
config.env    its LLM settings
self/         self.md (self-model), goals.md
memory/       journal.md (append-only), episodes/*.jsonl (raw transcripts)
notes/ skills/ its own work
```

`.env` at the root holds supervisor-only secrets (gitignored, unreadable by instances).
After a fresh clone: `bin/shaft setup` recreates the worktrees from the branches.

## Use

```bash
ln -s "$PWD/bin/shaft" ~/.local/bin/shaft   # once

shaft talk                 # chat with the default instance (shaft); /exit or Ctrl-D to end
shaft talk --show-thinking # also stream its hidden reasoning, dimmed
shaft reflect [name|--all] # one reflection cycle
shaft status               # how each instance is doing
shaft log [name] -n 5      # recent journal entries
shaft new <name> [--from <instance>]   # new branch from template, or fork code+skills (not memory)
shaft setup                # recreate worktrees after a clone
shaft pause [name] / shaft resume [name]
shaft check [name]         # run the test gate against an instance's code
```

## Rules the supervisor enforces

- Each instance runs in a macOS sandbox: writes only inside `instances/<name>/`, no reads of
  your home outside it, network only to the LLM port(s) (`SHAFT_LLM_PORTS`, default 1337),
  no inherited secrets.
- If a run changed the instance's code (`shaft/`, `tests/`, `config.env`), the invariant tests
  run; failure restores the old code and notes it in the instance's journal.
- After every run the instance's state is committed to its branch (author `SHAFT/<name>`).
  Git runs with the main repo's worktree registry and hooks off, so an instance cannot
  redirect the supervisor's git calls.
- Reflection cycles have a time budget (`SHAFT_CYCLE_TIMEOUT_S`, default 1800 s).

## Status

**M0** (2026-10-12): supervisor, sandbox, test gate, branch-per-instance, terminal chat,
reflection cycles. Instances have no tools yet — they can only think and write.
**Next (M1):** an autonomous life loop under launchd, tools (notes, files, web reading),
messages between instances, and the supervisor owning its own model server (today it
uses the one Atomic Chat serves on :1337).
