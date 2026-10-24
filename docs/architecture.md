# Architecture

The supervisor owns execution, credentials, scheduling, and git commits. An instance
owns its code and accumulated memory. Self-edits are proposals whose tests and later
results determine whether they survive. A judge's score is evidence about a run,
not proof that the agent's account is correct.

The broker gives each run a token and a limited set of tools. The gate tests code
changes before keeping them; the code it approved is recorded outside the instance's reach
([baseline](../supervisor/baseline.py)), anything else found at the next run's start is gated
then, and a run's processes (it leads its own process group) are all gone before the gate
looks. Cloud judges grade autonomous work from recorded tool
activity, and probation can revert a kept change after later outcomes deteriorate.
Sources: [run lifecycle](../supervisor/runner.py),
[gate](../supervisor/gate.py), [judging](../supervisor/metrics.py),
[probation](../supervisor/metrics.py).

## Layout

| Branch | Worktree | Holds |
|---|---|---|
| `main` | `./` | `supervisor/` (the part no instance can edit), `bin/shaft`, docs |
| `template` | `template/` | the pristine agent new instances fork from |
| `instance/<name>` | `instances/<name>/` | one agent: its code, plugins and skills; it may edit all of it |

An agent's mind consists of `self/`, `memory/`, `notes/`, and `inbox/`. These live in the
instance folder but are excluded from the project repository: the supervisor versions it after every run in `.supervisor/minds/<name>.git`, a private git
repository with no remote and a pre-push hook that always fails (`shaft mind status`). What an instance read
and wrote for itself cannot be pushed or merged with the code ([PLAN-mind-out-of-git.md](plans/PLAN-mind-out-of-git.md)).

Inside an instance:

```
shaft/        its code (Python, stdlib only); prompts, tools, life loop, sleep, talk
plugins/      tools (plugins/<name>/plugin.json + main.py), including web_research
skills/       how-tos it writes for itself
config.env    its LLM settings and budgets (yours to edit; it changes it only through set_config)
drive.md      what it is for; the drive paragraph of its system prompt (yours alone: its sandbox cannot
              write it; without one, the template's "improve yourself")
shaft-docs/   the supervisor's copy of README.md and docs/*.md (not docs/plans/), refreshed at every start;
              it reads it to tell you how to configure SHAFT (skills/helping-the-operator-configure.md),
              never writes it, and it is never committed
self/         self.md (self-model), personality.md (core/surface, every trait cites the journal),
              goals.md, stats.md (written by the supervisor: achieved difficulty, where its goals
              came from, judged success, calibration, battery scores)
memory/       journal.md (append-only), facts.md, people/, messages/, episodes/*.jsonl
inbox/        messages waiting for it (from you or other instances)
notes/        its own work
```

## Measuring improvement and autonomy

An instance can meet its own success criteria by choosing undemanding goals. The judge therefore also rates **difficulty** (1–5, a fixed scale, from
what the tool log shows the work required), **novelty** (1–5, against its earlier goals) and **origin**:
who chose the goal: itself, the operator, or another instance. The judge uses the messages
and conversations the supervisor collects, checked against what the instance declared. The headline in
`self/stats.md` is **achieved difficulty** (success × difficulty, 0–5 per cycle); probation compares it
before and after a self-change. Cycles judged before the ratings existed are rated with
`shaft judge backfill` (verdicts unchanged).

The **capability battery** is a fixed yardstick: held-out tasks the instances never see (coding with hidden
tests, research against a frozen key, recall of their own older journal entries, self-knowledge checked
against the supervisor's record), graded effectiveness first. Each instance is compared with a *control*:
the template on the same model with an empty mind. It runs only at night, 01:00 until 10 minutes before
the sleep, every `SHAFT_BATTERY_EVERY_DAYS` (5) nights, for every instance not turned off with
`shaft battery off <name>`. it stops when the window closes or a conversation
starts. Each item runs in a copy of the instance's folder, in a folder made for it and never used again
(`.supervisor/battery/exam/<name>/item-*/<name>/`: its code, plugins, skills, mind and config, copied without
following a link, links that stay inside kept as links; never `.git`, `secrets/`, its browser profile or mail).
Exams never get `trial`, which acts on the live instance. When an item ends, the exam's process group and any
process still working in its folder are killed, and the folder is deleted (as is the control, made afresh for
each item), so the real folder is never written — a message delivered meanwhile stays in its inbox, and a battery
killed mid-item leaves at most a stale copy, which the next run removes. Items live in
`.supervisor/battery/items/v1/` (never in git: an instance could read the repo with its GitHub key), backed up to
`~/Library/Application Support/SHAFT/battery-backup/`; every result carries the item set's sha256, model
and judge, and results from different eras are never compared. Results: `.supervisor/battery/results/`,
`.supervisor/evals/battery-*.json`, and a section of `self/stats.md`.

Sources: [instance creation](../supervisor/__main__.py),
[mind store and push guard](../supervisor/mind.py),
[metrics](../supervisor/metrics.py), [battery](../supervisor/battery.py).
The `template/` worktree is gitignored and materialized by `setup`; it is a branch,
not a directory shipped on `main` ([setup](../supervisor/__main__.py)).
See [enforced rules](safety.md) and [design milestones](plans/PLAN.md).
