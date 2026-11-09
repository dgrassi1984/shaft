# SHAFT coding guide

SHAFT is a macOS supervisor for persistent AI agent instances. It runs their tools,
conversations, autonomous cycles, evaluation, and access grants. Read README.md first.

## Layout

- `supervisor/`: supervisor code and `supervisor/tests/`. Python standard library.
- `apps/`: client apps.
- `docs/`: operator reference, architecture, and plans.
- `template` branch: agent code, checked out in `template/` by setup.
- `bin/shaft`: CLI entry point.

`main` holds the supervisor. Template changes belong on a branch based on `template`.
Instance branches contain private code and history. Never push `instance/*`.

## Setup and checks

Clone with the `template` branch available, then run from the repository root:

```sh
bin/shaft setup
python3 -m venv .venv-ui
.venv-ui/bin/pip install -r requirements-ui.txt
python3 -m unittest discover -s supervisor/tests -t .
```

For separate worktrees, follow `docs/worktrees.md`. Do not copy runtime state.
Before a merge, commit and run `bin/shaft gate budget`. It checks committed code,
not uncommitted edits. It rejects failed parts, parts over half the gate timeout,
tests over 10 seconds, and duplicate tests. The default gate timeout is 300 seconds.

## Invariants

- Instances build their own plugins with `plugin.json` and `main.py`, testable
  through broker `run`. Preserve this path before adding supervisor tools.
- Judges grade effectiveness first. Useful and correct beats resemblance.
- The supervisor never imports agent code. Instances never see secrets.
- The API stays tailnet-only.
- Conversations hold the instance only while it replies. Keep open conversations
  registered. Never write them up while they are still going on.
- Trial replays never settle, commit, deliver, or send. Record simulated writes.
  Trials never take an instance's time on the local model.
- Operator mail and Drive go only through broker tools. Instances hold no account
  credentials. Check label grants on every returned, read, or downloaded mail thread.
  Check Drive grants and reach on every file.

## Hard boundaries

Never read `.env`, `instances/`, `.supervisor/`, or `secrets/`.
Never include runtime data, credentials, or real mail in issues, PRs, or fixtures.
Use `.env.example` for configuration examples. Never bypass the instance push guard.
Security reports go through private advisories described in `SECURITY.md`.

## Documentation

`supervisor/runner.py` `DOCS_SOURCES` copies README.md and every top-level `docs/*.md`
into each instance's `shaft-docs/`. Do not rename or move README.md or the referenced
`docs/architecture.md`, `docs/cli.md`, `docs/configuration.md`, `docs/integrations.md`,
`docs/local-model.md`, `docs/safety.md`, and `docs/web-and-api.md`. The template's
`skills/helping-the-operator-configure.md` names them.
Keep the README heading that produces `#quickstart-cloud-only`.
Outside README and `docs/plans/`, name plans as `docs/plans/PLAN-…`.
Run `python3 docs/plans/check_references.py`. Keep relative links and GitHub anchors valid.
`docs/CODEMAP.md` and `agent-overlay.yaml` belong to maintainer tooling and need not
be edited by hand.

Use plain declarative English, short sentences, and honest statements of limits.
Say "the operator". Examples use `<name>` or `example`. Use live instance names only
where the code uses them as defaults. In docs, cite modules and symbols, not line numbers.
