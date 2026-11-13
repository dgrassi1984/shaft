# Contributing

SHAFT is a research project maintained by one person. Issues are welcome, and pull requests are considered. Bug reports, documentation corrections, tests, and focused improvements are useful contributions. Explain the problem, the proposed change, and how you checked it. There is no response-time promise.

Report vulnerabilities through a private GitHub security advisory. See [SECURITY.md](SECURITY.md).

## Development setup

SHAFT runs on macOS. Clone both the supervisor and template branches. A normal clone fetches both. If you use a single-branch clone, fetch `template` before setup.

```sh
git clone https://github.com/dgrassi1984/shaft.git
cd shaft
bin/shaft setup
python3 -m venv .venv-ui
.venv-ui/bin/pip install -r requirements-ui.txt
```

`setup` recreates missing worktrees from local or origin branches and installs the pre-push guard. Tests need the `template/` worktree. For a separate development worktree, follow [worktree setup](docs/worktrees.md). Never copy runtime state into a test worktree.

The supervisor uses the Python standard library. Optional UI dependencies belong in `.venv-ui`.

## Branches

`main` holds the supervisor, apps, and documentation. `template` holds the agent code used to create instances. Make template changes on a branch based on `template` in its worktree.

Each `instance/*` branch contains an instance's code and private history. Never push these branches or bypass the pre-push guard.

## Tests

Run the supervisor tests from the repository root:

```sh
python3 -m unittest discover -s supervisor/tests -t .
```

Before proposing a merge, commit your change and run:

```sh
bin/shaft gate budget
```

This check uses committed supervisor and template code, so it does not test uncommitted edits. It runs both test parts as the instance gate does. It fails if a part fails, exceeds half the gate timeout, contains a test over 10 seconds, or runs a test twice. The default gate timeout is 300 seconds. Tests that wait must wait for an event rather than a fixed sleep. See the gate budget plan.

## Architectural invariants

### Instances build their own plugins

Preserve the path for an instance to build, test, and run `plugins/<name>/plugin.json` and `main.py` through the broker. Consider that path before adding a supervisor tool.

### Judges grade effectiveness first

Useful and correct results take priority over whether an answer sounds like the instance.

### The supervisor and instances stay separate

The supervisor never imports agent code. Instances never see secrets. The API is reachable on the tailnet only, or on
loopback for the native app on the same Mac with a paired device's bearer (`/app/v1` only, never the web pages).

### Conversations hold an instance only while it replies

Keep open conversations registered. Never write up a conversation while it is still going on.

### Trial replays have no live effects

Replays never settle, commit, deliver, or send. The replay broker records simulated writes. Trials never take an instance's time on the local model.

### Mail and Drive access goes through broker tools

Instances never hold credentials for the operator's mail or Drive. Check label grants on every mail thread returned, read, or downloaded from. Check the grant and reach on every Drive file.

## Documentation and private data

Keep documentation with behavior changes. Use plain English and generic instance names in examples. Never include `.env`, instance data, credentials, or real mail in an issue or pull request, including logs and attachments.

`docs/CODEMAP.md` is generated. Contributors need not edit it by hand.
