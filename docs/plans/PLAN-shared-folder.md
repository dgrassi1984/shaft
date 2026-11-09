# PLAN — a read-only shared folder for an instance

Goal: the operator can let one instance read a folder outside its own (first: shaft →
`/Users/example/Documents/AgentsShared`), so it can work on the operator's real material. Read-only: the
instance still writes only inside `instances/<name>/`; what it produces for the operator goes in its `notes/`.

## Decisions

- **The grant lives on the supervisor side**, in `supervisor/tools/<name>.json` (key `shared`), next to `off`
  and `write_hosts` — never in the instance's `config.env`, which the instance can edit. Same reason as
  `disabled_tools`: an instance must not widen its own access.
- **One folder per instance, read-only.** No write grant in this plan.
- **The instance sees it as `shared/`.** Its file tools accept `shared/...` and map it to the granted path
  (passed in as `SHAFT_SHARED`). No symlink inside the instance folder: `_path()` resolves symlinks and
  would reject it, and a symlink is something the instance could repoint.
- **Not indexed by `search_memory`** in this plan. The folder is the operator's material, not its memory;
  it can `list_files shared` and `read_file`. Revisit only if it asks for it with a concrete need.
- **`secrets/` stays denied** in both profiles; the deny rule stays last (last-match-wins).

## M0 — can a sandboxed agent read `~/Documents` at all? — DONE 2026-10-13

Result: yes. A one-shot launchd job running `sandbox-exec` with the agent profile plus a read rule listed and
read the folder; a write into it was refused ("Operation not permitted").

`~/Documents` is TCC-protected on macOS. The agent runs as `sandbox-exec` under the launchd scheduler
(`com.shaft.scheduler`), and TCC may refuse it whatever the sandbox profile says.
- Test, before any code: from the scheduler's context, `sandbox-exec` with a profile allowing the folder,
  then `ls` the folder.
- Accept: the listing is returned. If TCC refuses, stop and decide with the operator: grant the
  Python binary Documents access, or move the folder outside `~/Documents`.

## M1 — supervisor: grant, profile, CLI — DONE 2026-10-13 (branches shared-folder, template-shared-folder)

- `supervisor/core.py`: `shared_folder(name) -> Path | None`; `set_shared_folder(name, path | None)`.
  Fix `_save_tools` so it **keeps keys it does not own**: today it rewrites the file with only `off` and
  `write_hosts`, so allowing a host would silently wipe the grant.
- `supervisor/sandbox.py`: `profile()` and `run_profile()` take `shared: Path | None`. When set:
  `(allow file-read* (subpath <shared>))` after the home deny, plus metadata on its ancestors. No write rule.
- `supervisor/runner.py::_launch` and `supervisor/broker.py` (`run`): pass the grant; set `SHAFT_SHARED`.
- CLI: `shaft share <name> <path>` / `shaft share <name> off` / `shaft share <name>` (shows it). The path must
  exist, be a directory, be absolute after resolving, and not contain or sit inside `instances/` or the
  supervisor's folder.
- Accept: `shaft share shaft /Users/example/Documents/AgentsShared` and the generated `shaft.sb` contain the
  read rule; with no grant, the profiles are byte-identical to today's.

## M2 — template: the instance can use it — DONE 2026-10-13 (branches shared-folder, template-shared-folder)

- `template/shaft/tools.py`: `_path()` maps `shared/...` to `SHAFT_SHARED` (resolved, must stay inside
  it); writes there refused with a clear reason ("read-only: the operator's folder — write to notes/").
  `read_file` / `list_files` descriptions mention `shared/` when a folder is granted.
- `template/shaft/prompts.py`: one line, only when granted: what `shared/` is and that it is read-only.
- Ship to shaft with `shaft update shaft` (its journal note names the commits).

## M3 — tests (regression, named for what they guard) — DONE 2026-10-13 (branches shared-folder, template-shared-folder)

- Profiles: with a grant, read allowed and no write rule for it; `secrets/` deny still last; without a
  grant, output unchanged.
- `_save_tools` round-trip keeps `shared` when a host or tool is toggled.
- Template `_path()`: `shared/x` resolves inside the grant; `shared/../..` refused; write to `shared/`
  refused; no grant → `shared/` refused as outside the folder.
- CLI refuses a path inside `instances/` or the supervisor folder.
- Full suite green.

## M4 — verify in the running system

- Put a real file in `AgentsShared`, grant it, and talk to shaft through the normal talk path: ask it to
  list and summarise the file. Accept: it names real content from the file.
- Ask it to write into `shared/`. Accept: refused, and the folder is unchanged on disk.
- `shaft share shaft off`, talk again. Accept: `shared/` is refused.
