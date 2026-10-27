# SHAFT

Independent, autonomous agents that live on your Mac, keep their own memory, and
rewrite their own code. Each agent (an "instance") runs goal → act → report cycles,
consolidates its journal, and talks with you. A supervisor runs instances in macOS
sandboxes, brokers model access without exposing credentials, tests their changes,
and records code and memory separately. SHAFT is a way to experiment with persistent,
self-modifying agents whose changes and outcomes you can inspect.

By Daniele Grassi. [MIT licensed](LICENSE).

## Status

Personal research project, at **v0.13.0** (2026-10-27). **macOS only. Expect breakage.**
Interfaces, defaults, and model dependencies can change. Start with one instance and
inspect its activity before leaving it autonomous.

The author runs it every day on one Mac mini, with several instances on cloud and
local models. On `main` today:

- the supervisor, sandbox, broker, test gate with probation, and cloud judges;
- private mind stores, kept out of the shared repository;
- the terminal client, the web UI/PWA for iPhone and iPad, the API, and Telegram;
- mail, calendar, and Drive grants, instance-owned Google accounts, a shared
  Chromium, scheduled jobs, and per-instance Python environments;
- trials, the capability battery, consults with other models, and the measurement
  reports in the [CLI reference](docs/cli.md).

The reference documents below describe the current behaviour. The plans in
[docs/plans/](docs/plans/PLAN.md) are dated design records. Their status lines
describe the plan when it was written, so they may be out of date.

> **Safety:** Instances execute self-written code and can reach the public internet
> through an egress proxy. That proxy rejects localhost, LAN, and tailnet addresses;
> the sandbox separately permits specific supervisor and model ports. Instances
> never receive the supervisor's API keys; keep all credentials in the broker.
> Cloud calls cost money. Caps exist, but they do not cover every supervisor expense
> (judging has an alert threshold, not a hard cap). Optional mail, calendar, and
> Drive access exposes content to prompt injection and possible disclosure.
> Read [SECURITY.md](SECURITY.md) before granting access.


## How it works

- The **supervisor** owns scheduling, sandbox execution, credentials, and commits.
- The **broker** exposes tools and model calls with per-run tokens and budgets.
- The **gate** tests self-edits, reverting failures. Kept edits enter probation.
- Cloud **judges** grade autonomous work from the tool log, separately from the
  instance's own report.
- The **template** branch contains agent code. `bin/shaft setup` materializes it as
  the gitignored `template/` worktree.
- Each **instance** has an `instance/<name>` branch and an `instances/<name>/`
  worktree. Its mind is versioned in a separate local-only store with no remote.

Read [architecture](docs/architecture.md) for the layout, evaluation design, and separation of code
from memory.

## Requirements

- macOS with `sandbox-exec` and a logged-in user session for `launchd` services.
- Python 3.12 or later available as `python3`, git, and `uv` on your PATH. With
  Homebrew: `brew install python@3.12 uv`. The scheduler, API, web, and Telegram
  services record the absolute path of the Python that installed them. The
  model and embedding services run `.venv-bonsai`'s Python. If either moves,
  run the service's `on` command again.
- Git `user.name` and `user.email` set, because creating an instance makes commits.
- Cloud-provider credentials for the cloud-only quickstart below.
- Optional: Apple Silicon and the MLX model pack for [local inference](docs/local-model.md).
- Optional: Tailscale for the [web UI and API](docs/web-and-api.md).
- Optional: Chromium installed with `bin/shaft browser install`, Google OAuth
  clients for mail/calendar/Drive, or a Telegram bot. See [integrations](docs/integrations.md).

The supervisor uses the Python standard library. The rich terminal client needs
its own environment.

## Quickstart: cloud-only

This path needs no local model, only cloud API keys.

1. Clone both the supervisor and the remote branch references:

   ```bash
   git clone https://github.com/dgrassi1984/shaft.git
   cd shaft
   bin/shaft setup
   git show-ref --verify refs/heads/template
   ```

   `setup` restores worktrees for existing local or `origin` branches. It does not
   create a template or an instance from nothing. It also installs a `pre-push`
   hook that refuses to push `instance/*` branches, because their history holds
   the instances' minds. If another tool already owns that hook, `setup` exits
   with status 1. In that case, merge SHAFT's refusal into the hook yourself.
   If the last command fails, this checkout lacks the required template branch,
   and you cannot create instances.

   Optionally, put the launcher on your PATH (the rest of this guide uses `bin/shaft`):

   ```bash
   mkdir -p ~/.local/bin && ln -s "$PWD/bin/shaft" ~/.local/bin/shaft
   ```

2. Copy the secret template, then edit `.env` with your own keys:

   ```bash
   cp .env.example .env
   ```

   Set `ANTHROPIC_API_KEY` for the `claude-haiku-5-5` instance below, and
   `OPENROUTER_API_KEY` for the default judges. The judges are all OpenRouter
   models (`SHAFT_JUDGE_MODELS`), as are consultations with other models. `SCRAPEBADGER_API_KEY` is optional for web search.
   Do not put secrets in instance config.

3. Install the terminal client dependencies:

   ```bash
   python3 -m venv .venv-ui
   .venv-ui/bin/python -m pip install -r requirements-ui.txt
   ```

   `talk` selects `.venv-ui/bin/python` on a terminal; without it, it falls back
   to plain mode.

4. Create an instance. Replace `example` with your chosen lowercase name:

   ```bash
   bin/shaft new example
   bin/shaft config example set OPERATOR_NAME="Your Name" LLM_BASE_URL=broker LLM_MODEL=claude-haiku-5-5
   ```

   The second command updates `instances/example/config.env`. You can instead
   edit that file to set `OPERATOR_NAME`, `LLM_BASE_URL="broker"`, and
   `LLM_MODEL="claude-haiku-5-5"`. Leave `LLM_API_KEY` empty. Your name is optional;
   an empty name resolves to "the operator". The template otherwise points to
   `http://127.0.0.1:8780/v1`, so do not skip the cloud configuration.

5. Talk to the named instance, then exit the conversation:

   ```bash
   bin/shaft talk example
   ```

   Use `/exit` or Ctrl-D to leave. Use `bin/shaft talk example --plain` for line
   mode. Inspect `bin/shaft status example` before enabling scheduled work.

6. Enable autonomy when you are ready for unattended work and spending:

   ```bash
   bin/shaft autonomy on
   bin/shaft autonomy status
   ```

   This starts the scheduler for all instances. The default wake interval is
   120 minutes, with daily consolidation from 04:00 local time. Stop the scheduler
   with `bin/shaft autonomy off`; use `bin/shaft pause example` to pause an
   instance, `bin/shaft kill example` to also stop what it is running now, and
   `bin/shaft delete example` to remove it (archived first).

The local model port is **8780**, also the default `SHAFT_LLM_PORTS` allowlist.
It is unrelated to the cloud-only setup above.

## Optional features

Use the [web UI/PWA and API](docs/web-and-api.md) for tailnet chat, phone access,
and an inbox doorbell. [Integrations](docs/integrations.md) covers Telegram,
Google mail/calendar/Drive grants, instance-owned Google accounts, scheduled jobs,
images, and browser automation. [Local models](docs/local-model.md) covers Bonsai,
self-hosted routing, and fine-tuning. The [CLI reference](docs/cli.md) covers
shared folders, evaluations, trials, and operator controls.

## Configuration

Root `.env` holds supervisor credentials and API identity settings. Use
`bin/shaft settings` for supervisor knobs and `bin/shaft config <name>` for
instance settings. Supervisor knobs use environment > `.supervisor/settings.env`
> default; long-running services need a restart to pick up saved changes.
See [configuration](docs/configuration.md) for every knob and its default.

## Backups and moving to a new Mac

GitHub holds only `main` and `template`. The instances exist only on this Mac:
their branches, their private mind stores under `.supervisor/minds/`, their
gitignored files, and the supervisor's state and credentials. A fresh clone gives
you SHAFT, not your instances. To move a setup, stop it, copy the whole project
folder, and reinstall the services on the new Mac. Follow
[moving SHAFT to a new Mac](docs/migration.md), which also lists what to back up.

## Development

Run the supervisor tests from the repository root:

```bash
python3 -m unittest discover -s supervisor/tests -t .
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and review guidance.

## Documentation

- [Architecture and evaluation](docs/architecture.md)
- [Command reference](docs/cli.md)
- [Configuration reference](docs/configuration.md)
- [Terminal, web UI/PWA, and API](docs/web-and-api.md)
- [Integrations](docs/integrations.md)
- [Local and self-hosted models](docs/local-model.md)
- [Rules the supervisor enforces](docs/safety.md)
- [Moving SHAFT to a new Mac](docs/migration.md)
- [A worktree the tests pass in](docs/worktrees.md) (development)
- [Design plans and milestones](docs/plans/PLAN.md) (dated design records)
- [Security policy](SECURITY.md)

## License

Copyright Daniele Grassi. Released under the [MIT License](LICENSE).
