# A worktree of SHAFT that the tests pass in

A fresh `git worktree add` of this repository lacks two things that the supervisor tests need. Without them the
suite fails in ways that look like real breakage.

1. **The `template` worktree** at `template/`. The invariants test template code by default, and
   `test_template_client` needs it as well (CONTRIBUTING.md).

   ```bash
   git worktree add --detach template template
   ```

   For a change to the template itself, branch it: `git -C template switch -c <branch> origin/template`.
2. **A `.supervisor/` directory**, which is gitignored. Sandbox tests create their temp folders under it (for example
   `test_shared_folder`, `test_browser`, `test_pyenv`, `test_keys_and_http` and `test_egress`). Without it they error
   with `FileNotFoundError: …/.supervisor/shaft-…` (seen 2026-10-20, #45).

   ```bash
   mkdir -p .supervisor
   ```

Nothing else is needed. Never copy or link `.env`, `instances/` or the real `.supervisor/` into a worktree to run the
tests: they need none of these, and a test that reaches real data is a bug.
