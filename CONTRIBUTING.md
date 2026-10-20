# Contributing

SHAFT is Daniele Grassi's personal research project. Issues are welcome; pull requests are considered. Explain the problem, the change and how you checked it. Report vulnerabilities through a private GitHub security advisory, as described in [SECURITY.md](SECURITY.md).

Run the supervisor tests from the repository root:

```sh
bin/shaft setup
python3 -m unittest discover -s supervisor/tests -t .
```

The tests need the `template` worktree (`supervisor/tests/test_template_client.py:11`). Setup recreates worktrees from local branches or their `origin` counterparts, so the clone must include the `template` branch (`supervisor/__main__.py:236`).

Keep the supervisor on the Python standard library. The optional terminal clients are the exception; install their dependencies in a separate environment (`supervisor/web.py:2`, `supervisor/webpush.py:14`, `supervisor/talk_ui.py:29`):

```sh
python3 -m venv .venv-ui
.venv-ui/bin/pip install -r requirements-ui.txt
```

Follow [.claude/CLAUDE.md](.claude/CLAUDE.md). Preserve the path for instances to build and test their own plugins before adding supervisor tools. Judges should grade usefulness and correctness first. Keep agent code out of supervisor imports, credentials out of instances, and the API on the tailnet. Hold an instance only while it replies, and do not write up an ongoing conversation. Trial replays must not commit, deliver messages, send writes or compete with instances for the local model. Enforce operator-mail label grants on every returned thread.
