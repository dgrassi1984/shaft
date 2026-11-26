"""SHAFT supervisor — the part of the system the agents cannot edit.

It runs any number of SHAFT instances, each inside its own macOS sandbox, lends each
run a broker (web, messages) that holds the secrets the instances never see, enforces
budgets and invariants, gates changes an instance makes to its own code (tests pass or
the change is reverted; kept changes go on probation), judges autonomous cycles,
commits each instance's code and skills to its own branch and its mind to its private
mind store, and owns the schedule and the pause switch. It never imports agent code.

Git layout: `main` holds the supervisor; the `template` branch holds the pristine agent
(worktree: template/); each instance lives on its own branch `instance/<name>`, forked
from `template` (worktree: instances/<name>/). Its mind (self/, memory/, notes/, inbox/)
is never in this repository: it is versioned in .supervisor/minds/<name>.git (supervisor/mind.py).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from . import actions, autonomy, core, evolution, finetune, gate, messages, metrics, mind, runner, surprise, version
from .core import say as core_say
from .core import (jsonl_lines, DEFAULT_INSTANCE, HOME, INSTANCES, MIND_PATHS, add_all_but_mind, NAME_RE, PROJECT, SUP_DIR, TEMPLATE, commit_state,
                   git, git_wt, instance_config, instance_names, instance_root, journal_entries, journal_note, log, paused,
                   safe_mkdir, safe_read, safe_write, set_paused, set_update_queued, update_queued)
from .runner import (Busy, busy_with, is_busy, model_server, request_yield, run_agent, run_lock, sharing_model,
                     spawn_recover)


# -- instances -------------------------------------------------------------------
def _seed_paths(operator: str) -> tuple[str, str]:
    return f"memory/people/{operator.lower().replace(' ', '-')}.md", "memory/facts.md"   # as the agent's sleep names it


def _seed_files(source: str) -> tuple[str, dict[str, str]]:
    """What `new --seed` copies from the parent's mind (its store's HEAD, not its worktree, which a cycle may be
    writing): what it knows about the operator — memory/people/<operator>.md and memory/facts.md — and nothing of
    itself. Returns the operator's name and {path: text} of the files it has. Raises ValueError if it cannot."""
    operator = instance_config(source).get("OPERATOR_NAME", "")
    if not operator:
        raise ValueError(f"{source}'s config.env sets no OPERATOR_NAME: whose people file to seed is unknown")
    head = mind.head(source)
    if head is None:
        raise ValueError(f"{source} has no mind store at {mind.store(source)} to seed from")
    return operator, {p: text for p in _seed_paths(operator) if (text := mind.show(source, head, p))}


def _seeded(text: str, source: str, operator: str) -> str:
    """A seeded file with where it came from, under its heading: the parent wrote it, in its own voice ("me" in it
    is the parent), citing a journal the child cannot read."""
    note = (f"> Seeded {date.today():%Y-%m-%d} from {source}'s memory: {source}'s beliefs about {operator}, written in "
            f"{source}'s voice (\"I\" and \"me\" here mean {source}, not me). A [YYYY-MM-DD HH:MM] citation refers to "
            f"{source}'s journal, which I cannot read: re-check a claim before relying on it.\n")
    first, _, rest = text.partition("\n")
    return f"{first}\n\n{note}{rest}" if first.startswith("# ") else f"{note}\n{text}"


def _git_identity_problem() -> str | None:
    """None when git resolves an author and committer in PROJECT (env, repo-local or global config: the same test the
    commit makes), else the message. Checked first, so `new` never fails at its commit with a worktree half made."""
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        if git("var", var, check=False).returncode != 0:
            return core_say("git has no identity here, so the instance's first commit would fail: run `shaft install` "
                            "(it sets one for this project) or `git config user.name` and `git config user.email`, "
                            "then try again",
                            "SHAFT has no name to sign the instance's first commit with: go back to Install and run it "
                            "again (it sets one), then Try again.")
    return None


def _write_first_reply(name: str, then: str, created: str | None = None) -> None:
    """`.supervisor/first-reply-<name>`: what to do once the operator has answered the welcome (atomic, 0600)."""
    SUP_DIR.mkdir(exist_ok=True)
    path = SUP_DIR / f"first-reply-{name}"
    fd, tmp = tempfile.mkstemp(prefix=f".first-reply-{name}.", dir=SUP_DIR)     # exclusive, 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"then": then,
                                "created": created or datetime.now().astimezone().isoformat(timespec="seconds")}))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cmd_first_reply(name: str, then: str, as_json: bool = False) -> int:
    """`shaft first-reply NAME resume|stay`: change what the operator's first answered reply does to an instance made
    with `new --paused-until-first-reply` (the Mac app's "How it works alone" step). Under `core.first_reply_lock`,
    which the web service's first reply holds too while it reads, acts on and removes the marker: the marker is read
    and replaced in place (temporary file, then os.replace: never a moment without it), keeping when it was made.
    Refused (exit 1) when there is no marker: the first reply has happened already, or the instance was not created
    waiting for one — never a marker after the fact, which would resume the instance at some later reply."""
    from .core import first_reply_lock

    def say(ok: bool, message: str, code: int) -> int:
        if as_json:
            print(json.dumps({"ok": True, "name": name, "then": then} if ok else {"ok": False, "error": message}))
        else:
            print(message, file=sys.stdout if ok else sys.stderr)
        return code

    if not NAME_RE.match(name) or name == "operator":
        return say(False, f"no instance named {name!r}", 2)
    path = SUP_DIR / f"first-reply-{name}"
    try:
        with first_reply_lock(name):
            try:
                created = json.loads(path.read_text()).get("created")
            except FileNotFoundError:
                return say(False, core_say(
                    f"{name} is not waiting for its first reply (it was answered already, or {name} was not created "
                    f"waiting for one): use shaft pause {name} or shaft resume {name} instead",
                    f"{name} is not waiting for its first reply any more (it was answered already): pause or resume it "
                    f"from its settings instead."), 1)
            except (ValueError, AttributeError):
                created = None                 # unreadable: replaced whole, as the consumer would have left it
            _write_first_reply(name, then, created if isinstance(created, str) else None)
    except OSError as e:
        return say(False, f"could not change {name}'s first-reply marker: {e}", 1)
    return say(True, f"{name}: after your first reply it will "
                     + ("work on its own" if then == "resume" else "stay paused, answering only when you talk to it"), 0)


def cmd_new(name: str, source: str | None, seed: bool = False, as_json: bool = False,
            paused_until_first_reply: str | None = None) -> int:
    """Create instance/<name> forked from `template` (or from another instance's branch).

    A fork inherits code, skills and plugins (its "genes"), never the parent's mind:
    self-model, personality, goals, journal, episodes and notes start fresh. The lineage
    stays in git history. With `seed`, it also starts knowing what the parent knows about the operator
    (its people file and facts, marked as the parent's), never what the parent is.
    With `paused_until_first_reply` ("resume" or "stay") it is also paused, with a marker the app reads once the
    operator has answered its welcome. With `as_json` stdout is one JSON object; refusals exit 2, failures 1.
    """
    def refuse(message: str, code: int = 2) -> int:
        if as_json:
            print(json.dumps({"ok": False, "error": message}))
        else:
            print(message, file=sys.stderr)
        return code

    if not NAME_RE.match(name) or name == "operator":
        return refuse("instance names: lowercase letters, digits and '-', starting with a letter (not 'operator')")
    root = INSTANCES / name
    if root.exists() or git("rev-parse", "--verify", "-q", f"refs/heads/instance/{name}", check=False).stdout:
        return refuse(f"instance {name!r} already exists")
    if mind.exists(name):
        return refuse(f"a mind store for {name!r} already exists at {mind.store(name)} (an earlier instance of that "
                      "name); move it away first")
    if seed and not source:
        return refuse("--seed copies from a parent instance: name it with --from")
    if source:
        try:
            instance_root(source)
        except SystemExit as e:        # an unknown parent is a usage refusal, not an exit from inside the verb
            return refuse(str(e.code))
    if seed:
        try:
            operator, seeds = _seed_files(source)
        except ValueError as e:
            return refuse(f"cannot seed from {source}: {e}")
    if (problem := _git_identity_problem()):
        return refuse(problem)
    base = f"instance/{source}" if source else "template"
    paused = bool(paused_until_first_reply)
    made = []        # the pause files this call created, removed again if the creation fails before any worktree exists
    try:
        if paused:     # before the worktree exists: the scheduler finds an instance by its `shaft` folder
            made = [f for f in (SUP_DIR / f"paused-{name}", SUP_DIR / f"first-reply-{name}") if not f.exists()]
            set_paused(name, True, reason="waiting for the operator's first reply", note=False)
            _write_first_reply(name, paused_until_first_reply)
        INSTANCES.mkdir(exist_ok=True)
        git("worktree", "add", "-q", "-b", f"instance/{name}", str(root), base)
        git_wt(root, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *MIND_PATHS)   # a parent's, pre-cutover
        for d in MIND_PATHS:
            shutil.rmtree(root / d, ignore_errors=True)
        for d in ("self", "memory", "notes", "skills"):
            safe_mkdir(root, d)
        safe_write(root, "self/self.md", 
            f"# Self-Model\n\nI am {name}. I have just been created and have no experiences yet; "
            "this self-model is for me to write.\n")
        safe_write(root, "self/goals.md", "# Goals & Open Questions\n\n(none yet — mine to set)\n")
        origin = f"forked from {source} (code, skills and plugins only, not its memories)" if source \
            else "from the SHAFT template"
        if seed:
            for path, text in seeds.items():
                safe_write(root, path, _seeded(text, source, operator))
            origin = (f"forked from {source} (code, skills and plugins; of its memories only what it knows about "
                      f"{operator}: {', '.join(seeds) or 'none, it had neither file'})")
        safe_write(root, "memory/journal.md", f"# {name} — journal\n")
        journal_note(root, "Created", f"I was created by the supervisor {origin}. My memory starts here."
                     + (f" The seeded files are {source}'s beliefs, in {source}'s voice; my own sleeps will make them mine."
                        if seed and seeds else ""))
        message = f"{name}: created {origin}"
        add_all_but_mind(root)
        git_wt(root, "commit", "-q", "--allow-empty", "-m", message)   # from the template: no code change, the lineage
        mind.init(name)
        mind.commit(name, message, root)
        log(f"created instance {name} on instance/{name} {origin}, its mind in {mind.store(name)}")
    except (subprocess.CalledProcessError, OSError, mind.MindError) as e:
        detail = (getattr(e, "stderr", None) or "").strip() or str(e)
        if root.exists():     # by the filesystem, not git's return: a hook can fail after the worktree was made
            return refuse(core_say(f"{name} is half-created{' and paused' if paused else ''} ({detail}); remove it "
                                   f"with `shaft delete {name}`",
                                   f"{name} was only partly created ({detail}). Choose another name: {name} stays "
                                   f"paused and never runs."), 1)
        for f in made:
            try:
                f.unlink(missing_ok=True)
            except OSError as err:
                detail += f"; could not remove {f}: {err}"
        return refuse(f"could not create {name}: {detail}", 1)
    if paused:     # the instance exists now: the note the pause owes its journal (best effort, like every pause)
        set_paused(name, True, reason="waiting for the operator's first reply", write_flag=False)
    if as_json:
        print(json.dumps({"ok": True, "name": name, "branch": f"instance/{name}", "worktree": str(root),
                          "paused": paused}))
        return 0
    print(f"created {name} (branch instance/{name}, worktree {root.relative_to(PROJECT)})")
    if seed:
        missing = [p for p in _seed_paths(operator) if p not in seeds]
        print(f"seeded from {source}: {', '.join(seeds) or 'nothing'}"
              + (f" ({source} has no {', '.join(missing)})" if missing else ""))
    return 0


def cmd_delete(name: str, yes: bool, keep_archive: bool) -> int:
    """`shaft delete`: the confirmation, then delete.delete."""
    from . import delete
    if not yes:
        if not sys.stdin.isatty():
            print(f"deleting {name} needs a confirmation: run it in a terminal, or add --yes", file=sys.stderr)
            return 2
        found = delete.find(name)
        if found.anything():
            print(f"This removes {name}: " + ", ".join(found.describe()) + ".\n"
                  + (f"An archive is kept in .supervisor/{delete.ARCHIVES}/." if keep_archive
                     else "NO archive is kept (--no-archive): its mind is gone for good."))
        try:
            typed = input(f"Type {name} to delete it: ").strip()
        except (EOFError, KeyboardInterrupt):   # Ctrl-D, Ctrl-C: a no
            typed = ""
        if typed != name:
            print(f"\nnot deleted (you typed {typed!r}, not {name})", file=sys.stderr)
            return 1
    try:
        return delete.delete(name, keep_archive=keep_archive)
    except delete.Refused as e:
        print(f"not deleted: {e}", file=sys.stderr)
        return 2


def _env_values(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v
    return out


def _resolve_config_conflict(root) -> bool:
    """A conflict only in config.env: take the template's file, keep each value the instance changed.
    (wisp's Haiku model conflicted with every template change near those lines.)"""
    conflicted = git_wt(root, "diff", "--name-only", "--diff-filter=U").stdout.split()
    if conflicted != ["config.env"]:
        return False
    show = lambda rev: git_wt(root, "show", f"{rev}:config.env", check=False).stdout  # noqa: E731
    base_rev = git_wt(root, "merge-base", "HEAD", "template").stdout.strip()
    base, mine, theirs = _env_values(show(base_rev)), _env_values(show("HEAD")), show("template")
    own = {k: v for k, v in mine.items() if base.get(k) != v}          # what the instance customised
    lines = []
    for line in theirs.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        lines.append(f"{key}={own.pop(key)}" if key in own else line)
    lines += [f"{k}={v}" for k, v in own.items()]                        # customised keys the template dropped
    safe_write(root, "config.env", "\n".join(lines) + "\n")
    git_wt(root, "add", "config.env")
    return True


def _fails_without_merge(name: str, root: Path, pre_merge: dict | None, test_log) -> list[str]:
    """A template merge whose only failing check is the instance's own tests (gate.Output.own_only): its failing tests
    when each one fails the same way (gate.failure_signatures) on its code before the merge as on the merged code — a
    test of its live data (an undated row in a notes file rolled back six merges in a week, #243), not of the merged
    code — else []. Either run unreadable: [] (the gate's verdict stands)."""
    from . import protected
    failing = set(getattr(test_log, "failing", ()))
    if pre_merge is None or not getattr(test_log, "own_only", False) or not failing:
        return []
    try:
        after = gate.own_tests_failures(root, HOME, env_root=root)
        with protected.copy(pre_merge, SUP_DIR / "protected" / name, name,
                            runner.safe_read_bytes(root, "config.env", b""), instance=root) as tree:
            before = gate.own_tests_failures(tree, HOME, env_root=root, data_root=root)
            before = None if before is None else {k: v.replace(str(tree), str(root)) for k, v in before.items()}
    except Exception as e:   # no answer: the gate's verdict on the merge stands
        log(f"{name}: its own tests could not be compared with its pre-merge code: {type(e).__name__}: {e}")
        return []
    if not after or not before or set(after) != failing:
        return []
    return sorted(failing) if all(before.get(n) == after[n] for n in failing) else []


def _note_update(name: str, root, before: str) -> None:
    """Tell the instance its code changed: a journal note naming the template commits just merged (their
    subjects), committed right after the merge. Without it an instance never learned of an update, and its
    self-model kept describing code it no longer ran (shaft, 2026-10-13: "no embeddings", "every 2 hours")."""
    subjects = git_wt(root, "log", "--no-merges", "--format=%h %s", f"{before}..HEAD").stdout.splitlines()
    if not subjects:
        return
    journal_note(root, "My code was updated",
                 "The operator merged these template changes into my code (they passed the gate):\n\n"
                 + "\n".join(f"- {s}" for s in subjects)
                 + "\n\nWhat I believe about my own code (self-model, notes) may now be out of date.")
    commit_state(name, "update", [], root=root)


def cmd_update(names: list[str]) -> int:
    """Merge `template` into instances — only while holding each instance's run lock, so an
    operator update is never mistaken for a change the instance made to itself mid-run.

    Never on uncommitted changes to tracked files: the gate would test code that is not committed, and
    the rollback of a failed merge must undo only the merge (wisp and kiln both had an uncommitted
    config.env while updates ran, 2026-10-13, and the old `reset --hard` would have discarded it).
    An operator's config.env edit is first committed on its own, as a run would (between runs every
    config.env change is the operator's); anything else uncommitted stops the update for the operator.
    Untracked files are safe: the merge refuses to overwrite one and the rollback never touches one.

    A busy instance's update is queued, not refused: the scheduler runs it the moment the instance is idle,
    before its next wake (autonomy.update_jobs). Retrying by hand lost the race to the next cycle, which starts
    as soon as the last one ends (2026-10-25). Every outcome is also logged, since a queued run has no terminal."""
    worst = 0
    for name in names:
        root = instance_root(name)
        try:
            with run_lock(name, wait=False, kind="update"):
                set_update_queued(name, False)   # it runs now, whatever comes of it: never retried in a loop
                runner._commit_outside_config(name, root, [])
                runner._commit_outside_drive(name, root)
                dirty = git_wt(root, "status", "--porcelain", "--untracked-files=no").stdout.rstrip()
                if dirty:
                    _said(f"{name}: uncommitted changes in its worktree, not updated — commit or discard them "
                          f"first (git -C {root} diff):\n{dirty}")
                    worst = max(worst, 1)
                    continue
                before = git_wt(root, "rev-parse", "HEAD").stdout.strip()
                try:   # its own tests run on it again if only they fail the merge (#243)
                    pre_merge = gate.snapshot(root)
                except core.TreeTooBig as e:   # no comparison then: the gate's verdict on the merge stands alone
                    log(f"{name}: no copy of its code before the merge ({e}): a failed gate rolls the merge back")
                    pre_merge = None
                r = git_wt(root, "-c", "user.name=SHAFT supervisor", "-c", "user.email=supervisor@shaft.localhost",
                           "merge", "--no-edit", "-m", f"{name}: merge template (operator update)", "template",
                           check=False)
                if r.returncode != 0 and not _resolve_config_conflict(root):
                    git_wt(root, "merge", "--abort", check=False)
                    _said(f"{name}: merge conflict with template, nothing changed:\n{(r.stdout + r.stderr)[-800:]}")
                    worst = 1
                    continue
                if r.returncode != 0:
                    git_wt(root, "-c", "user.name=SHAFT supervisor", "-c", "user.email=supervisor@shaft.localhost",
                           "commit", "-q", "--no-edit", "-m",
                           f"{name}: merge template (config.env merged key by key, its own settings kept)")
                if "Already up to date" in r.stdout:
                    print(f"{name}: already up to date")
                    continue
                runner.end_warm(name)   # a quiet voice session's process runs the old code: ended (it holds no lock)
                ok, test_log = gate.run_tests(root, HOME)
                if getattr(test_log, "refused", False):
                    from .runner import RunRefused, _refused
                    # HEAD is approved code at the next launch. Uncommit the untested merge, keeping its files.
                    undo = git_wt(root, "reset", "-q", "--mixed", before, check=False)
                    why = test_log.why
                    if undo.returncode:
                        why += f"; could not uncommit the untested merge: {undo.stderr.strip()}"
                    worst = _refused(name, RunRefused(why))
                    continue
                if not ok and (unrelated := _fails_without_merge(name, root, pre_merge, test_log)):
                    log(f"{name}: merged template kept: its own tests {', '.join(unrelated)} fail on its code before "
                        "the merge too, so not because of it")
                    journal_note(root, "Supervisor: template merged with my own tests failing",
                                 "The operator's template update was merged although my own tests fail, because they "
                                 "fail the same way on my code before the merge (they read my live data, not the "
                                 "merged code):\n" + "\n".join(f"- {n}" for n in unrelated))
                    ok = True
                if not ok:
                    # --keep, not --hard: it undoes only the merge and refuses rather than discard a file
                    # that something changed while the gate ran.
                    undo = git_wt(root, "reset", "-q", "--keep", before, check=False)
                    if undo.returncode != 0:
                        _said(f"{name}: merged template failed the gate and could not roll back — a merged file "
                              f"changed while the gate ran, so it is still at the merge "
                              f"({git_wt(root, 'rev-parse', '--short', 'HEAD').stdout.strip()}); undo it with "
                              f"`git -C {root} reset --keep {before[:12]}` once the change is saved.\n"
                              f"git said: {undo.stderr.strip()}\n{test_log[-800:]}")
                    else:
                        _said(f"{name}: merged template failed the gate; rolled back:\n{test_log[-800:]}")
                    worst = 1
                    continue
                _note_update(name, root, before)
                log(f"{name}: updated from template")
                print(f"{name}: updated from template ({git_wt(root, 'rev-parse', '--short', 'HEAD').stdout.strip()})")
        except Busy:
            if not autonomy.scheduler_installed():
                print(f"{name} is busy ({busy_with(name)}); update it when it is idle.")
                worst = max(worst, 3)
                continue
            set_update_queued(name, True)
            log(f"{name}: update queued — busy ({busy_with(name)})")
            print(f"{name} is busy ({busy_with(name)}): update queued. The scheduler runs it as soon as {name} is "
                  f"idle, before its next wake; the outcome is in the supervisor log and `shaft status`.")
    return worst


def _said(text: str) -> None:
    """An update's outcome, on the terminal and (its first line) in the supervisor log."""
    print(text)
    log(text.splitlines()[0])


def cmd_setup() -> int:
    """Recreate missing worktrees from branches — e.g. after a fresh clone — and install the push guard.
    A failing git (not a repository, no identity) says so and returns 1, never a traceback."""
    try:
        return _setup()
    except subprocess.CalledProcessError as e:
        print(f"setup failed: {(e.stderr or e.stdout or str(e)).strip()}", file=sys.stderr)
        return 1


def _setup() -> int:
    branches = git("for-each-ref", "--format=%(refname:short)", "refs/heads/template",
                   "refs/heads/instance/").stdout.split()
    remote = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/template",
                 "refs/remotes/origin/instance/").stdout.split()
    for ref in remote:
        local = ref.removeprefix("origin/")
        if local not in branches:
            git("branch", "-q", "--track", local, ref)
            branches.append(local)
    git("worktree", "prune")
    for branch in sorted(branches):
        path = TEMPLATE if branch == "template" else INSTANCES / branch.removeprefix("instance/")
        if (path / ".git").exists():
            print(f"ok      {branch:24} {path.relative_to(PROJECT)}")
            continue
        path.parent.mkdir(exist_ok=True)
        git("worktree", "add", "-q", str(path), branch)
        print(f"created {branch:24} {path.relative_to(PROJECT)}")
    # A fresh clone has no hooks: until `shaft mind guard` ran, nothing stopped `git push --all` from publishing the
    # instance branches, whose history holds the minds of the past.
    try:
        print(f"guarded {mind.guard_project()}: instance/* branches are refused on push")
    except mind.MindError as e:
        print(f"worktrees ready, but the push guard is not installed: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_status(names: list[str]) -> int:
    pending = messages.pending()
    questions = [m for m in pending if m["kind"] == "question"]
    print(f"SHAFT supervisor · {len(names)} instance(s) · "
          f"{'ALL PAUSED' if (SUP_DIR / 'paused').exists() else 'running'} · "
          f"operator inbox: {len(questions)} question(s), {len(pending) - len(questions)} note(s)")
    if (line := autonomy.drain_line()):
        print(line)
    today = date.today().isoformat()
    for name in names:
        root = INSTANCES / name
        calls = errors = tokens = 0
        for line in jsonl_lines(safe_read(root, "logs/llm_usage.jsonl", "", errors="replace")):   # never via a link
            rec = json.loads(line)
            if rec["ts"].startswith(today):
                calls, errors = calls + 1, errors + bool(rec.get("error"))
                tokens += rec.get("completion_tokens") or 0
        entries = journal_entries(root)
        js = metrics.judgments_for_stats(name)
        rate = (sum(metrics.SCORE[j["criterion_met"]] for j in js) / len(js)) if js else None
        rated = [v for v in map(metrics.cycle_value, js) if v is not None]
        worth = metrics.mean_value(metrics.valued(js)[-20:])
        try:
            last_self = mind.log(name, "-1", "--format=%cr", "--", "self/self.md").strip() or "never"
        except mind.MindError:
            last_self = f"unknown: no mind store (shaft mind cutover {name})"
        convs = runner.open_conversations(name)
        from .core import disabled_tools
        tools_off = disabled_tools(name)
        queued = update_queued(name)
        flags = [f for f, on in (("paused", paused(name)), ("busy", is_busy(name)),
                                 (f"update queued since {(queued or '')[11:16]}", queued),
                                 (f"{len(convs)} conversation(s) open", convs)) if on]
        print(f"\n■ {name}{' (' + ', '.join(flags) + ')' if flags else ''}  ·  branch instance/{name}")
        print(f"  journal     {len(entries)} entries · self-model last changed {last_self}")
        print(f"  judged      {len(js)} cycles" + (f" · success {rate:.0%}" if rate is not None else "")
              + (f" · achieved difficulty {5 * sum(rated[-20:]) / len(rated[-20:]):.1f}/5 (last {len(rated[-20:])})"
                 if rated else "")
              + (f" · value {worth:.1f}/5 (last {len(metrics.valued(js)[-20:])})" if worth is not None else ""))
        print("  " + metrics.delivery_line(metrics.valued(js)[-20:]))
        for line in metrics.self_audit_lines(js):
            print("  " + line)
        print(f"  LLM today   {calls} calls · {tokens} completion tokens · {errors} errors")
        print(f"  versions    {version.describe(name)}")
        if tools_off:
            print(f"  tools off   {', '.join(sorted(tools_off))} (shaft tools {name} on <tool>)")
        from .core import shared_folder
        if (folder := shared_folder(name)):
            print(f"  may read    {folder}{'' if folder.is_dir() else ' (MISSING: left out of its runs)'} "
                  f"(shaft share {name} off)")
        from .core import calendar_grants, gdrive_grants, gchat_grants, mail_grants
        if (grants := mail_grants(name)):
            labels = ", ".join(g["account"] + "/" + g["label"] + (" [drafts]" if g.get("draft") else "")
                               for g in grants)
            print(f"  mail        {labels} (shaft mail {name} off)")
        if (grants := gdrive_grants(name)):
            print(f"  gdrive      {', '.join(g['account'] + ' (' + g['reach'] + ')' for g in grants)} (shaft gdrive {name} off)")
        if (grants := gchat_grants(name)):
            print(f"  gchat       {', '.join(g['account'] for g in grants)} (shaft gchat {name} off)")
        if (grants := calendar_grants(name)):
            print(f"  calendar    {'; '.join(g['account'] + '/' + g['name'] for g in grants)} (shaft calendar {name} off)")
        if entries:
            lines = entries[-1].splitlines()
            print("  latest:     " + lines[0].lstrip("# "))
            for line in lines[1:8]:
                print("    " + line[:160])
            if len(lines) > 8:
                print("    …")
    return 0


def cmd_alerts(action: str | None, ids: list[str]) -> int:
    """The control panel's open alerts (panel.extras, as shaft top and the web UI show them), or dismiss some."""
    from . import dashboard, panel
    if action == "dismiss":
        if not ids:
            print("dismiss which? shaft alerts lists the ids")
            return 1
        panel.dismiss(ids)
        print(f"dismissed {len(ids)} alert(s)")
        return 0
    snap = panel.extras({"instances": [], "services": dashboard.services()})
    for a in snap.get("alerts") or []:
        print(f"{a['id']}  {a['text']}")
    if not snap.get("alerts"):
        print("no open alerts")
    return 0


def cmd_limits(name: str | None, limit: str | None, value: str | None) -> int:
    """The operator's own limits for an instance, kept in supervisor/tools/<name>.json (never its config.env, which
    it can change): how often it may wake (WAKE_INTERVAL_MINUTES in its config.env, at least the floor) and how much
    it may spend on cloud models a day."""
    from . import autonomy, cloudllm, consult
    from .core import _update_tool_settings
    if limit and not (name and value):
        print("usage: shaft limits <name> wake-floor <minutes>|off · shaft limits <name> cloud-cap <dollars>|off "
              "· shaft limits <name> consult-cap <dollars>|off",
              file=sys.stderr)
        return 2
    if limit:
        instance_root(name)
        key = {"wake-floor": "wake_floor_minutes", "cloud-cap": "cloud_daily_usd",
               "consult-cap": "consult_daily_usd"}[limit]
        from .core import limit_value
        try:
            new = limit_value(limit, value)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        _update_tool_settings(name, **{key: new})
        log(f"{name}: {limit} set to {value} by the operator")
    for n in ([name] if name else instance_names()):
        interval = autonomy.wake_interval(n)
        floor, default_floor = autonomy.wake_floor(n), autonomy.WAKE_FLOOR_MIN
        cap = cloudllm.daily_cap(n)
        print(f"{n}: wakes every {int(interval.total_seconds() // 60) if interval else 'never'} min · wake floor {floor} min"
              f"{' (yours)' if floor != default_floor else ' (default)'} · cloud cap ${cap:.2f}"
              f"{' (yours)' if cap != cloudllm.DAILY_USD else ' (default)'} · spent today ${cloudllm.spent_today(n):.2f}"
              f" · consult cap ${consult.daily_cap(n):.2f}"
              f"{' (yours)' if consult.daily_cap(n) != consult.DAILY_USD else ' (default)'}"
              f" · consult spent today ${consult.spent_today(n):.2f}")
    return 0


def cmd_jobs(name: str | None, action: str | None) -> int:
    """Scheduled jobs: an instance's own plugins, run on the timer their manifest declares."""
    from . import jobs
    if action and not name:
        print("usage: shaft jobs <name> on|off", file=sys.stderr)
        return 2
    if action:
        instance_root(name)
        jobs.set_jobs_off(name, action == "off")
        log(f"{name}: scheduled jobs switched {action} by the operator")
    for n in ([name] if name else instance_names()):
        print(jobs.describe(n))
    return 0


def cmd_tools(name: str, action: str | None, tool: str | None) -> int:
    from .broker import SPECS
    from .core import disabled_tools, set_tool
    instance_root(name)
    if action:
        if tool not in SPECS:
            print(f"unknown tool {tool!r}; supervisor tools: {', '.join(sorted(SPECS))}", file=sys.stderr)
            return 2
        set_tool(name, tool, action == "on")
        log(f"{name}: supervisor tool {tool} switched {action} by the operator")
    off = disabled_tools(name)
    for t in sorted(SPECS):
        print(f"{t:18} {'off' if t in off else 'on'}")
    if action:
        print(f"(takes effect from {name}'s next run or conversation)")
    return 0


def cmd_channel(name: str, value: str | None) -> int:
    """Where this instance's messages to you alert you: auto (where you last wrote
    to it), web or telegram. An open conversation's channel comes first either way."""
    from . import channel
    instance_root(name)
    if value:
        channel.set_preference(name, value)
        log(f"{name}: operator channel set to {value} by the operator")
    print(channel.describe(name))
    return 0


def cmd_orders(name: str, action: str | None, value: list[str], via: str = "cli") -> int:
    """List orders and refusal counts, add an operator order, or drop any order by id."""
    from . import orders
    root = instance_root(name)
    try:
        if action == "add":
            previous = orders.in_force(orders.listing(name), " ".join(value).strip())
            order, new = orders.add(name, " ".join(value), "operator", via)
            if new:
                log(f"{name}: standing order {order['id']} added by the operator ({via}): {order['text'][:200]}")
            action_text = "took over" if new and previous else "added" if new else "already in force:"
            print(f"{action_text} {orders.line(order)}")
            return 0
        if action == "drop":
            if len(value) != 1:
                print(f"which order? e.g. shaft orders {name} drop o3", file=sys.stderr)
                return 2
            order = orders.drop(name, value[0])
            log(f"{name}: standing order {order['id']} dropped by the operator ({via}): {order['text'][:200]}")
            print(f"dropped {order['id']}: {order['text']}")
            return 0
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    every = orders.listing(name, dropped=True)
    counts = orders.violations(name)
    for o in every:
        state = f"dropped {o['dropped_at'][:16].replace('T', ' ')}" if o.get("dropped_at") else "in force"
        print(f"{o['id']:5} {state:22} by {o['by']:8} {o['added_at'][:16].replace('T', ' ')}"
              + (f" ({o['source']})" if o.get("source") else "") + f": {o['text']}"
              + f" ({counts.get(o['id'], 0)} message(s)/action(s) refused)")
    for c in orders.corrections(name, root):
        print(f"{c['id']} still checked on its messages to you ({c['passes']} of {orders.CORRECTION_PASSES} passed): "
              f"{c['what_operator_said']}")
    if not every:
        print(f"{name} has no standing orders")
    return 0


def cmd_secret(args) -> int:
    """Give an instance a key without it passing through a conversation (and so through transcripts, journal
    write-ups and, for a cloud-brained instance, the provider's prompt)."""
    from .core import delete_secret, describe_key, read_secrets, store_secret
    root = instance_root(args.name)
    if args.action == "list":
        try:
            store = read_secrets(root, strict=True)       # an unreadable store is said, not shown as "no keys"
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        for k, e in sorted(store.items()):
            print(describe_key(k, e))
        if not store:
            print(f"{args.name} has no keys")
        return 0
    if not args.key:
        print("which key? e.g. shaft secret wisp set github --hosts api.github.com", file=sys.stderr)
        return 2
    if args.action == "delete":
        try:
            found = delete_secret(root, args.key)
        except ValueError as e:                           # KeyStoreUnreadable: nothing was changed
            print(e, file=sys.stderr)
            return 1
        print(f"deleted {args.name}'s key {args.key}" if found else f"{args.name} has no key {args.key}")
        return 0 if found else 1
    if args.action == "google":
        return _secret_google(args, root)
    import getpass
    value = getpass.getpass(f"value for {args.name}'s key {args.key} (not echoed): ") if sys.stdin.isatty() \
        else sys.stdin.readline()
    try:
        store_secret(root, args.key, value, args.hosts.split(","), args.header, args.scheme)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    log(f"{args.name}: operator gave it a key named {args.key} for {args.hosts}")
    print(f"stored: {args.name} can use it as http_request(auth={args.key!r}) to {args.hosts}; it never sees the value")
    return 0


def _secret_google(args, root) -> int:
    """`shaft secret <name> google <key> <address> --client <c> [--scope gmail.modify|drive]`: the instance's own
    Google account, signed in by the operator in the browser; the broker
    refreshes it per run."""
    from . import google
    from .core import secrets_env, store_google_key
    env = secrets_env()
    clients = sorted({k[len("GMAIL_"):-len("_CLIENT_ID")].lower().replace("_", "-") for k in env
                      if k.startswith("GMAIL_") and k.endswith("_CLIENT_ID")})
    if not (args.address and args.client):
        print(f"usage: shaft secret {args.name} google <key> <address> --client <name> [--scope "
              f"{'|'.join(google.KEY_SCOPES)}] (e.g. shaft secret tundra google drive assistant@example.com --client "
              f"personal --scope drive); the OAuth clients in .env: {', '.join(clients) or 'none'}", file=sys.stderr)
        return 2
    try:
        data = google.instance_key_login(args.address, args.client, args.scope, env)
        if args.hosts:
            data["hosts"] = args.hosts.split(",")
        store_google_key(root, args.key, data)
    except (google.AccountError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    hosts = ", ".join(sorted({h.strip().lower() for h in data["hosts"]}))
    log(f"{args.name}: operator gave it a Google key named {args.key} for {data['address']} ({args.scope}, client "
        f"{data['client']}) for {hosts}")
    print(f"stored: {args.name} can use {data['address']} ({args.scope}) as http_request(auth={args.key!r}) to "
          f"{hosts}; the supervisor refreshes it, and {args.name} never sees a token")
    return 0


def cmd_share(name: str, folder: str | None) -> int:
    """The operator's folder an instance may read (read-only), seen by it as shared/."""
    from .core import set_shared_folder, shared_folder
    instance_root(name)
    if folder:
        try:
            path = set_shared_folder(name, None if folder == "off" else folder)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        log(f"{name}: shared folder {'set to ' + str(path) if path else 'removed'} by the operator")
    current = shared_folder(name)
    print(f"{name} may read {current} as shared/ (read-only)" if current else f"{name} has no shared folder")
    if folder:
        print(f"(takes effect from {name}'s next run or conversation)")
    return 0


def cmd_google(action: str | None, alias: str | None, address: str | None, client: str = "",
               as_json: bool = False) -> int:
    """The operator's Google accounts, shared by mail, calendar, Drive and Chat: keep an OAuth client from the JSON
    Google Cloud downloads, log one in (one consent), list them with their clients and granted scopes, or move the
    account files out of the old mail folder."""
    from . import google
    from .core import secrets_env
    if as_json and action not in ("client", "login"):
        print("shaft google" + (f" {action}" if action else "") + " has no --json (only client and login)",
              file=sys.stderr)
        return 2
    if action == "client":
        return _google_client(alias, as_json)
    if action == "login" and as_json:
        return _google_login_json(alias, address, client)
    if action == "login":
        if not (alias and address):
            print("usage: shaft google login <alias> <address> [--client <name>]  (e.g. shaft google login work "
                  "operator@work.example; the client defaults to the alias: GMAIL_<CLIENT>_CLIENT_ID/_SECRET in .env)",
                  file=sys.stderr)
            return 2
        try:
            saved = google.login(alias, address, secrets_env(), client=client)
        except google.AccountError as e:
            print(e, file=sys.stderr)
            return 2
        got = google.scope_text(saved)
        log(f"Google account {alias} logged in as {saved['address']} (client {saved['client']}, granted {got}) by the "
            "operator")
        print(f"logged in: Google account {alias} is {saved['address']} (mail, calendar, Drive and Chat; client {saved['client']}). "
              f"Granted: {got}.")
        missing = [google.SCOPE_NAMES[s] for s in google.SCOPES
                   if s not in google.LATER_SCOPES and s not in google.scopes(saved)]
        for s, needs in google.LATER_SCOPES.items():
            if s not in google.scopes(saved):
                print(needs + google.missing_scope_message(alias, saved, s), file=sys.stderr)
        if missing:
            print(f"NOT granted: {', '.join(missing)} (unticked on Google's consent page, or not on the app's Data "
                  f"Access list): the tools that need it refuse, naming this login. To add it, run this login again "
                  "and tick it.", file=sys.stderr)
        print(f"Grant with shaft mail <instance> grant {alias} <label> · shaft calendar <instance> grant {alias} "
              f"<calendar> · shaft gdrive <instance> grant {alias} · shaft gchat <instance> grant {alias}")
        return 0
    if action == "migrate":
        try:
            lines = google.migrate()
        except google.AccountError as e:
            print(e, file=sys.stderr)
            return 2
        log("Google account files migrated by the operator: " + "; ".join(lines))
        print("\n".join(lines))
        return 0
    if action:
        print(f"unknown google action {action!r}: client, login or migrate (alone: list the accounts)", file=sys.stderr)
        return 2
    accts = google.accounts()
    for a, d in accts.items():
        print(f"{a:10} {d.get('address', '?')} · client {d.get('client') or 'NONE: log in again'} · granted "
              f"{google.scope_text(d)} · since {d.get('created', '?')[:10]}")
    if not accts:
        print("no Google accounts (shaft google login <alias> <address> [--client <name>])")
    if (left := google.unmigrated()):
        print(f"NOT READ: {', '.join(left)} still in {google.old_accounts_dir()} (shaft google migrate)", file=sys.stderr)
    return 0


def _google_client(name: str | None, as_json: bool) -> int:
    """`shaft google client NAME [--json]`: the Desktop app OAuth client from the JSON Google Cloud downloads, read on
    stdin, kept as GMAIL_<NAME>_CLIENT_ID / _CLIENT_SECRET in .env (core.set_secret_env). Neither value is printed or
    logged. One line: `{"ok": true, "client", "login_again": [aliases]}` or `{"ok": false, "error"}`."""
    from . import google

    def say(ok: bool, text: str, **extra) -> int:
        if as_json:
            print(json.dumps({"ok": ok, **({} if ok else {"error": text}), **extra}))
        else:
            print(text, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if not name:
        usage = "usage: shaft google client <name> [--json] < client.json  (the Desktop app client's JSON on stdin)"
        if as_json:
            print(json.dumps({"ok": False, "error": usage}))
        else:
            print(usage, file=sys.stderr)
        return 2
    try:
        text = sys.stdin.read(google.CLIENT_JSON_MAX + 1)
        again = google.save_client(name, text)
    except google.AccountError as e:
        return say(False, f"not saved: {e}")
    except (ValueError, OSError) as e:
        return say(False, f"not saved: {e}")
    id_key, secret_key = google.client_keys(name)
    log(f"Google OAuth client {name} saved from its JSON ({id_key}, {secret_key}) by the operator"
        + (f"; {', '.join(again)} must log in again" if again else ""))
    text = f"saved: Google client {name} ({id_key} and {secret_key} in .env)" + (
        f". {', '.join(again)} logged in with its earlier ID: run shaft google login for "
        f"{'it' if len(again) == 1 else 'each'} again" if again else "")
    return say(True, text, client=name, login_again=again)


def _google_login_json(alias: str | None, address: str | None, client: str) -> int:
    """`shaft google login ALIAS ADDRESS [--client NAME] --json`, for the Mac app: `{"url"}` (Google's consent page;
    the app opens it, nothing here opens a browser) as soon as the loopback listener waits, then
    `{"ok": true, "alias", "address", "client", "granted": [scope names], "missing": [scope names]}` or
    `{"ok": false, "error"}` with the real reason (300 s without an answer from the browser among them). SIGTERM (the
    app's Cancel) ends it with nothing saved."""
    from . import google
    from .core import secrets_env

    def emit(row: dict) -> None:
        print(json.dumps(row), flush=True)
    if not (alias and address):
        emit({"ok": False, "error": "usage: shaft google login <alias> <address> [--client <name>] --json"})
        return 2
    try:
        saved = google.login(alias, address, secrets_env(), client=client,
                             open_url=lambda url: emit({"url": url}), announce=False)
    except BrokenPipeError:
        return 1                                     # nobody reads the answer any more
    except google.AccountError as e:
        error = str(e)
    except (OSError, ValueError) as e:              # the token exchange could not reach Google
        error = f"the Google login failed: {e}"
    else:
        granted = google.scopes(saved)
        names = [google.SCOPE_NAMES[s] for s in google.SCOPES if s in granted]
        missing = [google.SCOPE_NAMES[s] for s in google.SCOPES if s not in granted]
        log(f"Google account {alias} logged in as {saved['address']} (client {saved['client']}, granted "
            f"{google.scope_text(saved)}) by the operator, from the app")
        try:
            emit({"ok": True, "alias": alias, "address": saved["address"], "client": saved["client"],
                  "granted": names, "missing": missing})
        except BrokenPipeError:
            pass
        return 0
    try:
        emit({"ok": False, "error": error})
    except BrokenPipeError:
        pass
    return 1


def cmd_mail(target: str | None, action: str | None, alias: str | None, label: str | None) -> int:
    """Grant or revoke label reads and optional drafts, never sending. The
    accounts are `shaft google`'s. The grant is read on every mail call, so a revoke takes effect on the instance's
    next call."""
    from . import google, mail
    from .core import mail_grants, secrets_env, set_mail_grants

    def show(name: str) -> None:
        grants = mail_grants(name)
        labels = ", ".join(f"{g['account']}/{g['label']}" + (" [drafts]" if g.get("draft") else "")
                           for g in grants)
        print(f"{name} may read: " + (labels or "no mail"))

    if not target:
        accts = google.accounts()
        print("accounts: " + (", ".join(f"{a} ({d.get('address', '?')})" for a, d in accts.items()) if accts
                              else "none") + " (shaft google)")
        for name in instance_names():
            show(name)
        return 0
    instance_root(target)
    if not action:
        show(target)
        return 0
    grants = mail_grants(target)
    if action == "off":
        set_mail_grants(target, [])
        log(f"{target}: all mail grants removed by the operator (were: "
            f"{', '.join(g['account'] + '/' + g['label'] for g in grants) or 'none'})")
    elif not (alias and label):
        print(f"usage: shaft mail {target} {action} <alias> <label>", file=sys.stderr)
        return 2
    elif action in ("grant", "grant-draft"):
        saved = google.account(alias)
        if not saved:
            known = ", ".join(google.accounts()) or "none"
            print(f"no Google account {alias!r} (logged in: {known}); first: shaft google login {alias} <address>",
                  file=sys.stderr)
            return 2
        try:   # the label must exist in that account now
            if action == "grant-draft" and google.GMAIL_COMPOSE not in google.scopes(saved):
                raise google.AccountError(google.missing_scope_message(alias, saved, google.GMAIL_COMPOSE))
            mail.label_id(google.Account(alias, secrets_env()).require(google.GMAIL), label)
        except (google.AccountError, google.ApiError) as e:
            print(f"not granted: {e}", file=sys.stderr)
            return 2
        prior = next((g for g in grants if (g["account"], g["label"]) == (alias, label)), {})
        grant = {"account": alias, "label": label}
        if action == "grant-draft" or prior.get("draft"):
            grant["draft"] = True
        set_mail_grants(target, grants + [grant])
        log(f"{target}: mail {action} {alias}/{label} added by the operator")
    else:   # revoke-draft leaves the read grant; neither revoke contacts Google.
        hits = [g for g in grants if (g["account"], g["label"]) == (alias, label)]
        if not hits or (action == "revoke-draft" and not any(g.get("draft") for g in hits)):
            print(f"{target} has no grant {alias}/{label}", file=sys.stderr)
            show(target)
            return 1
        rest = [g for g in grants if g not in hits]
        if action == "revoke-draft":
            rest += [{k: v for k, v in g.items() if k != "draft"} for g in hits]
        set_mail_grants(target, rest)
        log(f"{target}: mail {action} {alias}/{label} revoked by the operator")
    show(target)
    print(f"(from {target}'s next mail call; the prompt line changes from its next run)")
    return 0


def cmd_gdrive(target: str | None, action: str | None, alias: str | None, all_reach: bool = False) -> int:
    """Show or change the operator's read-only Drive grants."""
    from . import google
    from .core import gdrive_grants, gdrive_lock, remove_gdrive_copies, secrets_env, set_gdrive_grants, UnsafePath

    def show(name: str) -> None:
        grants = gdrive_grants(name)
        print(f"{name} may read: " + (", ".join(f"{g['account']} ({g['reach']})" for g in grants)
                                     if grants else "no Drive"))

    if not target:
        accts = google.accounts()
        print("accounts: " + (", ".join(f"{a} ({d.get('address', '?')})" for a, d in accts.items()) if accts
                              else "none") + " (shaft google)")
        for name in instance_names():
            show(name)
        return 0
    instance_root(target)
    if not action:
        show(target)
        return 0
    if action not in ("grant", "revoke", "off") or (action != "off" and not alias) or (all_reach and action != "grant"):
        print(f"usage: shaft gdrive {target} grant <alias> [--all] | revoke <alias> | off", file=sys.stderr)
        return 2
    if alias and not google.ALIAS_RE.fullmatch(alias):
        print(f"invalid Google account alias {alias!r}", file=sys.stderr)
        return 2
    if action == "grant":
        if not google.account(alias):
            known = ", ".join(google.accounts()) or "none"
            print(f"no Google account {alias!r} (logged in: {known}); first: shaft google login {alias} <address>",
                  file=sys.stderr)
            return 2
        try:
            google.Account(alias, secrets_env()).require(google.DRIVE_READONLY).get(google.DRIVE_ABOUT)
        except (google.AccountError, google.ApiError) as e:
            print(f"not granted: {e}; check Drive API access and run shaft google login {alias} <address> "
                  "with drive.readonly ticked", file=sys.stderr)
            return 2
    with gdrive_lock(target):
        grants = gdrive_grants(target)
        rest = [g for g in grants if g["account"] != alias]
        if action == "grant":
            reach = "all" if all_reach else "own"
            set_gdrive_grants(target, rest + [{"account": alias, "reach": reach}])
            log(f"{target}: gdrive grant {alias} ({reach}) added by the operator")
            if reach == "own" and any(g["account"] == alias and g["reach"] == "all" for g in grants):
                try:
                    remove_gdrive_copies(target, alias)
                except (OSError, UnsafePath) as e:
                    print(f"grant narrowed, but copies left at {instance_root(target) / 'gdrive' / alias}: {e}",
                          file=sys.stderr)
                    return 1
                print(f"{alias}: copies removed because the grant narrowed to own")
        else:
            if action == "revoke" and rest == grants:
                print(f"{target} has no gdrive grant {alias}", file=sys.stderr)
                return 1
            set_gdrive_grants(target, [] if action == "off" else rest)
            log(f"{target}: gdrive {action} {alias or 'all'} by the operator (were: {grants})")
            path = instance_root(target) / "gdrive"
            if action == "revoke":
                path /= alias
            try:
                remove_gdrive_copies(target, alias if action == "revoke" else "")
            except (OSError, UnsafePath) as e:
                print(f"grant revoked, but copies left at {path}: {e}", file=sys.stderr)
                return 1
    show(target)
    if all_reach:
        print("everything the account can reach, including files strangers share with it, reaches the instance")
    print(f"(from {target}'s next Drive call; the prompt line changes from its next run)")
    return 0


def cmd_gchat(target: str | None, action: str | None, alias: str | None) -> int:
    """Show or change the operator's read-only Google Chat grants."""
    from . import google
    from .core import gchat_grants, gchat_lock, remove_gchat_copies, secrets_env, set_gchat_grants, UnsafePath

    def show(name: str) -> None:
        grants = gchat_grants(name)
        print(f"{name} may read: " + (", ".join(g["account"] for g in grants)
                                     if grants else "no Google Chat"))

    if not target:
        accts = google.accounts()
        print("accounts: " + (", ".join(f"{a} ({d.get('address', '?')})" for a, d in accts.items()) if accts
                              else "none") + " (shaft google)")
        for name in instance_names():
            show(name)
        return 0
    instance_root(target)
    if not action:
        show(target)
        return 0
    if action not in ("grant", "revoke", "off") or (action != "off" and not alias):
        print(f"usage: shaft gchat {target} grant <alias> | revoke <alias> | off", file=sys.stderr)
        return 2
    if action == "off" and alias is not None:
        print("gchat off takes no account; use revoke <alias>", file=sys.stderr)
        return 2
    if alias and not google.ALIAS_RE.fullmatch(alias):
        print(f"invalid Google account alias {alias!r}", file=sys.stderr)
        return 2
    if action == "grant":
        if not google.account(alias):
            known = ", ".join(google.accounts()) or "none"
            print(f"no Google account {alias!r} (logged in: {known}); first: shaft google login {alias} <address>",
                  file=sys.stderr)
            return 2
        try:
            # Every scope the broker's Chat tools require: a grant they would refuse is not saved.
            google.Account(alias, secrets_env()).require(google.CHAT_MESSAGES).require(google.CHAT_SPACES).require(
                google.CHAT_MEMBERS).get("https://chat.googleapis.com/v1/spaces", pageSize=1)
        except (google.AccountError, google.ApiError) as e:
            print(f"not granted: {e}", file=sys.stderr)
            return 2
    with gchat_lock(target):
        grants = gchat_grants(target)
        rest = [g for g in grants if g["account"] != alias]
        if action == "grant":
            set_gchat_grants(target, rest + [{"account": alias}])
            log(f"{target}: gchat grant {alias} added by the operator")
        else:
            if action == "revoke" and rest == grants:
                print(f"{target} has no gchat grant {alias}", file=sys.stderr)
                return 1
            set_gchat_grants(target, [] if action == "off" else rest)
            log(f"{target}: gchat {action} {alias or 'all'} by the operator (were: {grants})")
            path = instance_root(target) / "gchat"
            if action == "revoke":
                path /= alias
            try:
                remove_gchat_copies(target, alias if action == "revoke" else "")
            except (OSError, UnsafePath) as e:
                print(f"grant revoked, but copies left at {path}: {e}", file=sys.stderr)
                return 1
    show(target)
    print(f"(from {target}'s next Google Chat call; the prompt line changes from its next run)")
    return 0


def cmd_calendar(target: str | None, action: str | None, alias: str | None, which: str | None) -> int:
    """The operator's calendars an instance may read, one calendar per grant: grant (by
    `primary`, title or ID, stored as the ID), revoke, off, show; `calendars <alias>` lists an account's calendars.
    The grant is read on every calendar call, so a revoke takes effect on the instance's next call."""
    from . import calendar as gcal, google
    from .core import calendar_grants, secrets_env, set_calendar_grants

    def show(name: str) -> None:
        grants = calendar_grants(name)
        print(f"{name} may read: " + ("; ".join(f"{g['account']}/{g['name']} ({g['calendar']})"
                                                   + (" [writable]" if g.get("write") else "") for g in grants)
                                      if grants else "no calendar"))

    def session(name: str):
        if not google.account(name):
            known = ", ".join(google.accounts()) or "none"
            raise google.AccountError(f"no Google account {name!r} (logged in: {known}); first: shaft google login "
                                      f"{name} <address>")
        return google.Account(name, secrets_env()).require(google.CALENDAR)

    if target == "calendars":
        if not action:
            print("usage: shaft calendar calendars <alias>", file=sys.stderr)
            return 2
        try:
            cals = gcal.calendars(session(action))
        except (google.AccountError, google.ApiError) as e:
            print(e, file=sys.stderr)
            return 2
        for c in cals:
            print(f"{'primary ' if c['primary'] else '        '}{c['title']}  ·  {c['id']}  ·  {c['access']}")
        print(f"(grant one: shaft calendar <instance> grant {action} primary|<title>|<id>)")
        return 0
    if not target:
        for name in instance_names():
            show(name)
        return 0
    instance_root(target)
    if not action:
        show(target)
        return 0
    grants = calendar_grants(target)
    if action == "off":
        set_calendar_grants(target, [])
        log(f"{target}: all calendar grants removed by the operator (were: "
            f"{'; '.join(g['account'] + '/' + g['calendar'] for g in grants) or 'none'})")
    elif not (alias and which):
        print(f"usage: shaft calendar {target} {action} <alias> primary|<title>|<id>", file=sys.stderr)
        return 2
    elif action in ("grant", "grant-write"):
        try:
            if action == "grant-write":
                saved = google.account(alias) or {}
                if google.CALENDAR_EVENTS not in google.scopes(saved):
                    raise google.AccountError(google.missing_scope_message(alias, saved, google.CALENDAR_EVENTS))
            cal = gcal.resolve(session(alias), which)
        except (google.AccountError, google.ApiError) as e:
            print(f"not granted: {e}", file=sys.stderr)
            return 2
        # Write implies read; upgrading replaces the same record, never a parallel grant.
        prior = next((g for g in grants if (g["account"], g["calendar"]) == (alias, cal["id"])), {})
        grant = {"account": alias, "calendar": cal["id"], "name": cal["title"]}
        if action == "grant-write" or prior.get("write"):
            grant["write"] = True
        rest = [g for g in grants if (g["account"], g["calendar"]) != (alias, cal["id"])]
        set_calendar_grants(target, rest + [grant])
        log(f"{target}: calendar {action} {alias}/{cal['id']} ({cal['title']}) added by the operator")
    else:   # revoke by the stored name/ID without contacting Google; revoke-write leaves its read grant.
        ids = {(google.account(alias) or {}).get("address", "").casefold()} if which == "primary" else set()
        hits = [g for g in grants if g["account"] == alias and (which in (g["calendar"], g["name"])
                                                             or g["calendar"].casefold() in ids)]
        if not hits or (action == "revoke-write" and not any(g.get("write") for g in hits)):
            print(f"{target} has no calendar {action} grant {alias}/{which}", file=sys.stderr)
            show(target)
            return 1
        rest = [g for g in grants if g not in hits]
        if action == "revoke-write":
            rest += [{k: v for k, v in g.items() if k != "write"} for g in hits]
        set_calendar_grants(target, rest)
        log(f"{target}: calendar {action} {alias}/{which} revoked by the operator")
    show(target)
    print(f"(from {target}'s next calendar call; the prompt line changes from its next run)")
    return 0


def run_many(kind: str, names: list[str], every: bool) -> int:
    names = instance_names() if every else (names or [DEFAULT_INSTANCE])
    if kind == "sleep" and every:   # the nightly sleep, by the scheduler or by hand: today's is done
        autonomy.record("sleep")
    worst = 0
    for name in names:
        if kind not in ("talk", "recover") and paused(name):   # writing journal entries is housekeeping
            print(f"{name} is paused (shaft resume {name}).", file=sys.stderr)
            if kind == "sleep" and every:   # the nightly run: tell it why it did not sleep
                with run_lock(name, wait=True, kind="sleep"):
                    journal_note(instance_root(name), "Supervisor: no sleep tonight",
                                 "My nightly sleep cycle did not run: the operator had paused me.")
                    commit_state(name, "sleep skipped", [])
            worst = max(worst, 3)
            continue
        worst = max(worst, run_agent(name, kind, [kind], interactive=False))
    return worst


def cmd_mind(args) -> int:
    """The mind stores (supervisor/mind.py). backfill and check only read the project
    repository; cutover changes the instance's branch (one untrack commit) and is the operator's step."""
    if args.action == "guard":
        print(f"installed {mind.guard_project(args.project)}: instance/* refs are refused on push")
        return 0
    names = args.names or instance_names()
    if args.action == "cutover" and len(args.names) != 1:
        print("cutover is one instance at a time: shaft mind cutover <name>", file=sys.stderr)
        return 2
    worst = 0
    for name in names:
        if args.action == "status":
            print(f"{name}: {mind.describe(name, args.project)}")
        elif args.action == "backfill":
            started = datetime.now()
            made = mind.backfill(name, args.project)
            print(f"{name}: {made} mind commits imported in {(datetime.now() - started).total_seconds():.1f} s — "
                  f"{mind.describe(name, args.project)}")
        elif args.action == "check":
            results = mind.check(name, args.n, args.project)
            same = sum(r["identical"] for r in results)
            for r in results:
                print(f"  {r['t0']}  {r['start_commit'][:10]} ↔ {(r['mind_commit'] or '(none)')[:10]}  "
                      f"{r['files']:4d} files  {'identical' if r['identical'] else 'DIFFER: ' + ', '.join(r['differ'])}")
            print(f"{name}: {same}/{len(results)} identical")
            worst = max(worst, 0 if same == len(results) else 1)
        else:
            worst = max(worst, mind.cutover(name, args.n))
    return worst


def cmd_battery(args) -> int:
    from . import battery
    if args.action == "perturbation":
        from .perturbation import enabled_path
        if len(args.names) != 2 or args.names[1] not in ("on", "off"):
            sys.exit("shaft battery perturbation <name> on|off")
        name, action = args.names
        instance_root(name)
        path = enabled_path(name)
        if action == "on":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(datetime.now().isoformat(timespec="seconds") + "\n")
        else:
            path.unlink(missing_ok=True)
        print(f"{name}: perturbation {action}")
        return 0
    names = args.names or instance_names()
    if args.action in ("off", "on"):
        for name in args.names or sys.exit("shaft battery off|on needs instance names"):
            instance_root(name)
            if args.action == "off":
                battery.off_path(name).parent.mkdir(parents=True, exist_ok=True)
                battery.off_path(name).write_text(datetime.now().isoformat(timespec="seconds") + "\n")
            else:
                battery.off_path(name).unlink(missing_ok=True)
            log(f"battery {args.action} for {name} by operator")
            print(f"{name}: battery {args.action}")
        return 0
    if args.action == "backup":
        print(f"item set backed up to {battery.backup()} · " + ", ".join(f"{n} {battery.checksum(n)[:12]}" for n in names))
        return 0
    worst = 0
    for name in names:
        instance_root(name)
        if args.action == "report":
            print(battery.report(name))
        elif args.action == "recall":
            path = battery.make_recall(name, force=args.force)
            print(f"{name}: {path or 'not enough old journal entries yet (see supervisor.log)'}")
        elif battery.is_off(name):
            print(f"{name}: battery off (`shaft battery on {name}` turns it on)")
        else:
            until = None
            if args.until:
                h, m = map(int, args.until.split(":"))
                end = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                until = (end if end > datetime.now() else end + timedelta(days=1)).timestamp()
            only = [c.strip() for c in args.only.split(",") if c.strip()] or None
            n = battery.run(name, until=until, only=only, control=not args.no_control, limit=args.limit,
                            control_only=args.control_only)
            print(f"{name}: {n} item result(s) scored")
            worst = max(worst, 0 if n else 1)
    return worst


def cmd_night_replay(args) -> int:
    import signal
    from . import nightreplay
    names = args.names or instance_names()
    for name in names:
        instance_root(name)
        if args.action == "report":
            print(f"{name}:\n" + (nightreplay.record_text(name) or "(no night replay yet)"))
            continue
        until = None
        if args.until:
            h, m = map(int, args.until.split(":"))
            end = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
            until = (end if end > datetime.now() else end + timedelta(days=1)).timestamp()
        # The scheduler stops a run with SIGTERM at the window's end: the replay in progress is stopped (its agent runs
        # in its own session, so it would outlive a killed parent), recorded, and nothing further starts.
        stopped = {}
        signal.signal(signal.SIGTERM, lambda signum, _frame: stopped.setdefault("signal", signum))
        try:
            nightreplay.run(name, until=until, dry_run=args.dry_run,
                            stop=lambda name=name: paused(name) or bool(stopped))
        except Exception as e:   # recorded, so the scheduler does not start it again tonight
            log(f"{name}: night replay failed: {type(e).__name__}: {e}")
            if not args.dry_run:
                nightreplay.record_failure(name, f"{type(e).__name__}: {e}")
            return 1
    return 0


def _pairs(specs: list[str]) -> dict:
    out = {}
    for spec in specs:
        rel, eq, src = spec.partition("=")
        if not eq:
            raise SystemExit(f"{spec!r}: expected PATH=FILE (or PATH= to delete)")
        out[rel] = Path(src).read_bytes() if src else None
    return out


def cmd_trial(args) -> int:
    from . import trial
    if args.action == "list":
        print("\n".join(trial.describe(t) for t in trial.all_trials(args.name)) or "no trials yet")
        return 0
    if args.action == "show":
        t = trial.load(args.name, args.id)
        print(trial.describe(t))
        for i, s in enumerate(t["states"], 1):
            print(f"  {i}. {s['ts']} from {s['start_commit'][:10]} · {s['chosen_by']} · {s['status']} · "
                  f"{s.get('outcome') or ''} {s.get('model') or ''} · {s.get('why') or ''}")
        if t["status"] in ("done", "rejected"):
            print("\n" + trial.report(t))
        return 0
    if args.action == "work":
        return trial.work()
    if args.action == "models":
        from . import bakeoff
        instance_root(args.name)
        try:
            drawn = bakeoff.draw(args.name, args.draw, args.since, args.until, kind=args.kind) if args.draw else None
            if drawn and not args.arm:   # the set only: to look at before spending
                print(f"{len(drawn['episodes'])} cycles of {args.name} ended after {drawn['since']} and by "
                      f"{drawn['until']}; eligible by kind: "
                      + ", ".join(f"{k} {c}" for k, c in drawn["population"].items()))
                print(" ".join(drawn["episodes"]))
                return 0
            b = bakeoff.create(args.name, args.arm, drawn["episodes"] if drawn else args.episodes, judge=args.judge,
                               bid=args.id, drawn=drawn)
        except bakeoff.BakeoffError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        if args.export_cases:
            todo = bakeoff.export_cases(b, Path(args.export_cases))
            print(f"bake-off {b['id']}: {len(todo)} cases without a verdict in {args.export_cases}/verdicts/")
            print("\n".join(todo))
            return 0
        if args.import_verdicts:
            try:
                bad = bakeoff.import_verdicts(b, Path(args.import_verdicts), judge=args.judged_by)
            except bakeoff.BakeoffError as e:
                print(f"refused: {e}", file=sys.stderr)
                return 1
            print(bakeoff.report(b))
            if bad:
                print(f"\ninvalid verdict files, judge these cases again: {' '.join(bad)}")
            return 0
        if not args.report:
            print(f"bake-off {b['id']}: {len(b['states'])} past cycles × {len(b['arms'])} models", flush=True)
            try:
                bakeoff.run(b, parallel=args.parallel, arm_parallel=args.arm_parallel, replay_only=args.replay_only)
            except bakeoff.BakeoffError as e:
                print(f"refused: {e}", file=sys.stderr)
                return 1
        print(bakeoff.report(b))
        return 0
    if args.action == "replay":
        from .core import instance_model
        try:
            if args.model != "local":
                args.model = instance_model(args.model)
        except ValueError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        instance_root(args.name)
        ep = next((e for e in trial.live_episodes(INSTANCES / args.name) if e["ts"] == args.ts), None)
        if not ep:
            print(f"no live episode of {args.name} ended at {args.ts}", file=sys.stderr)
            return 1
        st = trial.state_of(PROJECT, args.name, ep)
        if not st:
            print(f"cannot find when that cycle started", file=sys.stderr)
            return 1
        import threading
        rec = trial.replay_arm(PROJECT, args.name, st, trial.committed_code(PROJECT, args.name), model=args.model,
                               cache={}, cache_lock=threading.Lock(), billed=f"{args.name}@trial:replay",
                               keep=Path(args.keep).resolve() if args.keep else None,
                               cycles=args.cycles, sleep_between=args.sleep,
                               on_unit=lambda unit: trial._ledger({"instance": args.name, "trial": "replay",
                                   "by": "operator", "kind": "arm", "model": args.model,
                                   "usd": unit["usd"], "seconds": unit["seconds"], "ended": unit["ended"],
                                   "tokens_in": unit["usage"]["prompt_tokens"],
                                   "tokens_out": unit["usage"]["completion_tokens"]}))
        ep2 = rec["episode"] or {}
        print(f"state: {st['start_commit'][:10]} (cycle began {st['t0']}; inbox {st['inbox']})")
        print(f"exit {rec['exit']} · {rec['ended']} · {rec['seconds']} s · completed {rec['completed']} · "
              f"tokens {rec['usage']} · ${rec['usd']}")
        print(f"goal: {(ep2.get('intent') or {}).get('goal')}\nclaimed: {(ep2.get('finish') or {}).get('criterion_met')}")
        for c in rec["trace"]:
            print(f"  {c['tool']} {c['args'][:100]} -> {'ok' if c['ok'] else 'ERROR'} {c['tags']}")
        print(rec["diff"][:3000] or "(no file changes)")
        if not rec["completed"]:
            print(rec["output_tail"])
        return 0
    instance_root(args.name)
    change = _pairs(args.cand)
    try:
        t = trial.create(args.name, change, args.hypothesis, by="operator", episodes=args.episodes, n=args.n,
                         base=_pairs(args.base) or None, allow_empty=args.aa, model=args.model,
                         cycles=args.cycles, sleep_between=args.sleep)
    except trial.TrialError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"queued {t['id']} with {len(t['states'])} past cycles; `shaft trial work` runs it now, or the "
          f"scheduler does")
    return 0


def cmd_feedback(name: str | None, days: int | None) -> int:
    """Show current operator ratings without delivering them to an instance."""
    from . import feedback, web_chat
    if name is not None and name not in instance_names():
        print(f"no instance named {name!r}", file=sys.stderr)
        return 1
    rows = feedback.items(name)
    if days is not None:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = [r for r in rows if r["ts"] >= since]
    if not rows:
        print("No feedback.")
        return 0
    logged = None
    print("When | Instance | Thumb | Sent | Reply | Reason")
    for row in rows:
        target, text = row["target"], "(gone)"
        if target["kind"] == "chat":
            chat = web_chat.load(target["chat"])
            if chat and chat["instance"] == row["instance"] and target["k"] < len(chat["turns"]):
                text = chat["turns"][target["k"]].get("content") or "(gone)"
        else:
            if logged is None:
                logged = messages.history()
            text = next((m["text"] for m in reversed(logged)
                         if m["file"] == target["file"] and m["from"] == row["instance"]), "(gone)")
        sent = row["sent"]
        delivery = f"{sent['via']} {sent['ts'][:19].replace('T', ' ')}" if sent else "kept"
        thumb = "👍" if row["thumb"] == "up" else "👎"
        reason = " ".join(row["reason"].splitlines())
        first = text.splitlines()[0] if text else "(gone)"
        print(f"{row['ts'][:19].replace('T', ' ')} | {row['instance']} | {thumb} | {delivery} | {first} | {reason}")
    return 0


def cmd_codex(action: str, as_json: bool = False) -> int:
    from . import codexcli, planchannel
    if as_json and action not in ("login", "where"):
        print(f"shaft codex {action} has no --json", file=sys.stderr)
        return 2
    if action == "where":
        binary = codexcli.find_bin()
        if error := codexcli.bin_error(binary):
            if as_json:
                print(json.dumps({"path": None, "error": error}))
            else:
                print(error, file=sys.stderr)
            return 1
        print(json.dumps({"path": binary, "source": codexcli.bin_source()}) if as_json else binary)
        return 0
    home = Path(codexcli.HOME).expanduser().resolve()
    if action == "login":
        if as_json:
            return _codex_login_json(home)
        binary = codexcli.find_bin()
        if error := codexcli.bin_error(binary):
            print(error, file=sys.stderr)
            return 1
        try:
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            home.chmod(0o700)
            return subprocess.run([os.path.abspath(binary), "login"], env=codexcli.login_env(binary, home)).returncode
        except OSError as e:
            print(f"Codex login failed: {e}", file=sys.stderr)
            return 1
    print("Codex login: " + ("present" if (home / "auth.json").is_file() else "missing; run shaft codex login"))
    rate = planchannel.saved_rate("codex")
    print("Weekly allowance: " + (f"{rate['used_percent']}% used" if rate is not None else "not observed yet"))
    print("Models: " + ", ".join(codexcli.models()))
    if (fault := planchannel.outage("codex")):
        print(("Codex security event: " if fault.get("security") else "Codex unavailable: ") + fault["reason"])
    return 0


def _codex_login_json(home: Path) -> int:
    """`shaft codex login --json`, for the Mac app (no terminal): `codex login --device-auth` in SHAFT's private Codex
    home, stdin closed. Print JSON lines with `codex` (the absolute executable path before launch), `url` (the page
    to open), `code` (the one-time code), and `say` (other output, up to 200 lines of 300 characters, never repeating the page or the code). Finish with
    `{"ok": true}` once the login is saved, or `{"ok": false, "error": …}` with the failure reason. The app opens
    the browser. SIGTERM, the app quitting, or 10 minutes without the code entered stop Codex."""
    from . import codexcli

    def emit(row: dict) -> None:
        print(json.dumps(row), flush=True)
    try:
        error = codexcli.bin_error(codexcli.find_bin())
        if error:
            ok = False
        else:
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            home.chmod(0o700)
            ok, error = codexcli.device_login(home, emit)
    except OSError as e:
        ok, error = False, f"Codex login failed: {e}"
    except codexcli.LoginStopped as e:
        ok, error = False, f"Codex sign-in stopped ({e})"
    except BrokenPipeError:
        return 1                                     # nobody reads the answer any more
    try:
        emit({"ok": True} if ok else {"ok": False, "error": error})
    except BrokenPipeError:
        pass
    return 0 if ok else 1


def cmd_grok(action: str) -> int:
    from . import grokcli, planchannel
    home = Path(grokcli.HOME).expanduser().resolve()
    if action == "login":
        try:
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            home.chmod(0o700)
            env = grokcli.child_env(home, home)
            env["HOME"] = str(home)
            return subprocess.run([grokcli.BIN, "login"], env=env).returncode
        except OSError as e:
            print(f"Grok login failed: {e}", file=sys.stderr)
            return 1
    print("Grok login: " + ("present" if (home / "auth.json").is_file() else "missing; run shaft grok login"))
    print("Weekly allowance: not available programmatically")
    print("Models: " + ", ".join(grokcli.models()))
    if (fault := planchannel.outage("grok")):
        print(("Grok security event: " if fault.get("security") else "Grok unavailable: ") + fault["reason"])
    return 0


def cmd_claude(action: str) -> int:
    from . import claudecli, planchannel
    if action == "login":
        try:
            config_dir = claudecli.home()
            env = claudecli.child_env(config_dir, config_dir)
            return subprocess.run([str(Path(claudecli.BIN).expanduser()), "auth", "login"], env=env).returncode
        except OSError as e:
            print(f"Claude login failed: {e}", file=sys.stderr)
            return 1
    status = claudecli.login_status()
    print("Claude login: " + (f"present ({status.get('authMethod', 'unknown')})" if status.get("loggedIn")
                              else "missing; run shaft claude login"))
    rate = planchannel.saved_rate("claudecode")
    print("Weekly allowance: " + (f"{rate['used_percent']}% used" if rate is not None else "not observed yet"))
    print("Models: " + ", ".join(claudecli.models()))
    if (fault := planchannel.outage("claudecode")):
        print(("Claude security event: " if fault.get("security") else "Claude unavailable: ") + fault["reason"])
    return 0


def cmd_antigravity(action: str) -> int:
    from . import agycli
    if action == "login":
        try:
            home = agycli.home()
            # agy's own Google sign-in (the ACP terminal login), with the private home: the operator's ~/.gemini stays out.
            return subprocess.run([str(Path(agycli.BIN).expanduser()), "acp", "--login"],
                                  env=agycli.child_env(home, home)).returncode
        except OSError as e:
            print(f"Antigravity login failed: {e}", file=sys.stderr)
            return 1
    print("Antigravity login: " + ("present" if agycli.logged_in() else "missing; run shaft antigravity login"))
    print("Models: " + ", ".join(agycli.models()))
    if (fault := agycli.STATE.outage()):
        print(("Antigravity security event: " if fault.get("security") else "Antigravity unavailable: ") + fault["reason"])
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="shaft", description="SHAFT supervisor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("talk", help="chat with an instance in the terminal")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p.add_argument("--show-thinking", action="store_true")
    p.add_argument("--plain", action="store_true", help="plain line-by-line mode instead of the terminal client")
    p.add_argument("--as", dest="speaker", default="", metavar="NAME",
                   help="you are not the operator (e.g. --as \"Claude Code\"): recorded, so nothing you say is "
                        "remembered as the operator's")
    for verb, text in (("live", "one autonomous cycle with tools"), ("reflect", "one reflection (no tools)"),
                       ("sleep", "consolidate memory, personality, goals"),
                       ("recover", "write journal entries for conversations that have none yet")):
        p = sub.add_parser(verb, help=text)
        p.add_argument("names", nargs="*")
        p.add_argument("--all", action="store_true")
    sub.add_parser("tick", help="wake every instance once now (skipping any already awake)")
    p = sub.add_parser("wake", help="one scheduled wake of an instance (run by the scheduler; skipped if awake)")
    p.add_argument("name")
    p = sub.add_parser("api", help="OpenAI-compatible API for the instances, on your tailnet")
    p.add_argument("action", choices=["on", "off", "restart", "status", "key", "inbox-key", "serve"])
    p.add_argument("--force", action="store_true",
                   help="on/off/restart even if it would kill or cut off runs in progress")
    p.add_argument("--local", action="store_true", help="on: without Tailscale — allow the Mac app's local login, "
                                                        "expose nothing")
    p.add_argument("--json", action="store_true",
                   help="on, restart: one JSON object on stdout ({ok} or {ok: false, error})")
    p = sub.add_parser("secrets", help="the supervisor's .env secrets: set NAME (value on stdin), unset NAME, list "
                                       "(names only)")
    p.add_argument("action", choices=["set", "unset", "list"])
    p.add_argument("name", nargs="?")
    p.add_argument("--json", action="store_true", help="one JSON object on stdout")
    p = sub.add_parser("meetings", help="manage meeting recordings and name speakers")
    msub = p.add_subparsers(dest="action", required=True)
    for action in ("list", "show", "delete", "import", "reprocess", "name", "file", "grant", "revoke", "proposals"):
        q = msub.add_parser(action)
        q.add_argument("--json", action="store_true", help="one JSON object on stdout")
        if action in ("show", "delete", "reprocess", "name", "file"):
            q.add_argument("id")
        if action in ("grant", "revoke"):
            q.add_argument("name")
        if action in ("file", "grant", "revoke"):
            q.add_argument("alias")
        if action == "name":
            q.add_argument("label")
            q.add_argument("name")
        if action == "import":
            q.add_argument("file")
            q.add_argument("--title")
            q.add_argument("--mode", choices=("online", "inperson"), default="inperson")
    p = sub.add_parser("speakers", help="list, rename, or forget known meeting speakers")
    ssub = p.add_subparsers(dest="action", required=True)
    for action in ("list", "rename", "forget"):
        q = ssub.add_parser(action)
        q.add_argument("--json", action="store_true", help="one JSON object on stdout")
        if action != "list":
            q.add_argument("pid")
        if action == "rename":
            q.add_argument("name")
        if action == "forget":
            q.add_argument("--anonymise", action="store_true")
    p = sub.add_parser("provider", help="use PROVIDER: one consistent preset of every model the supervisor calls "
                                        "(openrouter, codex or custom); check PROVIDER: one live call per configured "
                                        "model of it. Never touches .env (shaft secrets sets the keys)")
    p.add_argument("action", choices=["use", "check"])
    p.add_argument("provider", choices=["openrouter", "codex", "custom"])
    p.add_argument("--model", help="codex: the instance model (default codex/gpt-6-astra) · custom: the model name")
    p.add_argument("--judge-model", help="custom: the judge's model (default: the same model)")
    p.add_argument("--base-url", help="custom: the OpenAI-compatible endpoint, e.g. https://host/v1")
    p.add_argument("--price-in", type=float, help="custom: dollars per million input tokens of --model")
    p.add_argument("--price-out", type=float, help="custom: dollars per million output tokens of --model")
    p.add_argument("--judge-price-in", type=float, help="custom: dollars per million input tokens of --judge-model")
    p.add_argument("--judge-price-out", type=float, help="custom: dollars per million output tokens of --judge-model")
    p.add_argument("--vision", action="store_true", help="custom: --model reads images (declares it in SHAFT_CUSTOM_VISION)")
    p.add_argument("--json", action="store_true", help="one JSON object per line on stdout")
    p = sub.add_parser("config", help="an instance's config.env as the operator: list it, set KEY=VALUE …, unset KEY …"
                                      " (one write, one commit; --expect refuses if a value changed since you looked)")
    p.add_argument("name")
    p.add_argument("action", nargs="?", choices=["set", "unset"])
    p.add_argument("items", nargs="*", help="set: KEY=VALUE … · unset: KEY …")
    p.add_argument("--expect", action="append", default=[], metavar="KEY=VALUE",
                   help="refuse unless KEY's value is still VALUE")
    p.add_argument("--expect-unset", action="append", default=[], metavar="KEY", help="refuse unless KEY is not set")
    p = sub.add_parser("settings", help="the supervisor's settings (knobs): list them with their value, source and "
                                        "default, set KEY=VALUE …, unset KEY … (.supervisor/settings.env; env > file > "
                                        "default; --expect refuses if a value changed since you looked)")
    p.add_argument("action", nargs="?", choices=["set", "unset"])
    p.add_argument("items", nargs="*", help="set: KEY=VALUE … · unset: KEY …")
    p.add_argument("--expect", action="append", default=[], metavar="KEY=VALUE",
                   help="refuse unless KEY's value in the file is still VALUE")
    p.add_argument("--expect-unset", action="append", default=[], metavar="KEY",
                   help="refuse unless KEY is not in the file")
    p.add_argument("--json", action="store_true", help="the listing as JSON (the web UI reads it)")
    p.add_argument("--from-web", action="store_true", help="refuse the settings only a terminal may set (ports, paths)")
    p = sub.add_parser("drive", help="an instance's drive.md (what it is for): show it, set --file FILE, or remove it "
                                     "(the default drive applies); --expect-sha from `shaft drive <name>`")
    p.add_argument("name")
    p.add_argument("action", nargs="?", choices=["set", "remove", "proposals", "accept", "reject"])
    p.add_argument("--file")
    p.add_argument("--expect-sha")
    p.add_argument("id", nargs="?")
    p.add_argument("--reason")
    p = sub.add_parser("docs", help="an instance's documents (notes/docs/*.md, shown beside the web chat): list them, "
                                    "show PATH, set PATH --file FILE --expect-sha SHA (none for a new one), or "
                                    "rm PATH --expect-sha SHA; its next "
                                    "run commits your edit with its notes")
    p.add_argument("name")
    p.add_argument("action", nargs="?", choices=["show", "set", "rm", "comments"])
    p.add_argument("path", nargs="?", help="inside notes/docs/, e.g. plan.md")
    p.add_argument("--file")
    p.add_argument("--expect-sha")
    p.add_argument("--sync", action="store_true")
    p = sub.add_parser("cycle", help="start one cycle of an idle instance now, detached (as shaft top's c and the web "
                                     "UI do); refused if it is paused or already awake")
    p.add_argument("name")
    p = sub.add_parser("recap", help="write each instance's one-line recap now (what it is doing, what it did in the "
                                     "last hour), by a small model; shown by shaft top and the web UI")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("alerts", help="the control panel's open alerts with their ids; 'dismiss ID…' hides them")
    p.add_argument("action", nargs="?", choices=["dismiss"])
    p.add_argument("ids", nargs="*")
    p = sub.add_parser("web", help="the web UI for managing the instances, on your tailnet: on [--local], off, "
                                   "status, pair [--name NAME] (a one-time link for a browser; --local: the Mac app on "
                                   "this Mac, directly), unpair DEVICE|--all")
    p.add_argument("action", choices=["on", "off", "restart", "status", "pair", "unpair", "serve"])
    p.add_argument("device", nargs="?", help="unpair: the browser's id or name (shaft web status lists them)")
    p.add_argument("--name", help="pair: what to call this browser (e.g. laptop, phone)")
    p.add_argument("--all", action="store_true", help="unpair: every browser")
    p.add_argument("--local", action="store_true", help="on: without Tailscale (SHAFT_WEB_LOCAL=on, /app/v1 at "
                                                        "127.0.0.1); pair: the Mac app on this Mac, no link")
    p.add_argument("--json", action="store_true", help="pair, on (tailnet), restart: one JSON object on stdout")
    p.add_argument("--force", action="store_true",
                   help="on, restart: even while an instance is replying (the reply is cut off)")
    p = sub.add_parser("telegram", help="talk with each instance from Telegram, one bot per instance, one paired account")
    p.add_argument("action", choices=["on", "off", "restart", "status", "token", "pair", "unpair", "serve"])
    p.add_argument("name", nargs="?", help="token: the instance the bot is for")
    p.add_argument("--json", action="store_true", help="pair: one JSON line with the code")
    p.add_argument("--force", action="store_true",
                   help="on, restart: even while an instance is replying (the reply is cut off)")
    p = sub.add_parser("grok", help="Grok plan login and status")
    p.add_argument("action", choices=["login", "status"])
    p = sub.add_parser("codex", help="Codex plan login, status and executable location")
    p.add_argument("action", choices=["login", "status", "where"])
    p.add_argument("--json", action="store_true",
                   help="login: device sign-in as JSON lines; where: executable path and source")
    p = sub.add_parser("claude", help="Claude plan (claude -p on your own subscription) login and status")
    p.add_argument("action", choices=["login", "status"])
    p = sub.add_parser("antigravity", help="Antigravity plan (Gemini through agy on your own subscription) login and status")
    p.add_argument("action", choices=["login", "status"])
    p = sub.add_parser("migrate", help="move a SHAFT setup to a new Mac (docs/migration.md): source --to HOST [--check], "
                                       "or target [--check]")
    p.add_argument("role", choices=["source", "target"])
    p.add_argument("--to", help="source: the new Mac (its .local or tailnet name)")
    p.add_argument("--check", action="store_true", help="run only the read-only checks; write nothing")
    p = sub.add_parser("model", help="SHAFT's own Bonsai 2 model server (launchd service)")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--force", action="store_true", help="on/off even if it would kill or cut off runs in progress")
    p = sub.add_parser("embed", help="SHAFT's embedding service for memory search (launchd service)")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--force", action="store_true", help="on/off even if it would kill or cut off runs in progress")
    p = sub.add_parser("listen", help="SHAFT's meeting transcription worker (launchd service)")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--force", action="store_true", help="on/off even if a meeting is processing")
    p = sub.add_parser("autonomy", help="the autonomous schedule (SHAFT's scheduler service): on, off, status; "
                                        "restart: once its runs end, cutting none (a deploy)")
    p.add_argument("action", choices=["on", "off", "status", "serve", "restart"])
    p.add_argument("--force", action="store_true", help="on/off even if it would kill or cut off runs in progress")
    p.add_argument("--within", type=int, metavar="MIN", help="restart: give up (cutting nothing) after MIN minutes "
                   "(default: the sleep's and a cycle's budgets, SHAFT_SLEEP_TIMEOUT_S + SHAFT_CYCLE_TIMEOUT_S)")
    p.add_argument("--wait", action="store_true", help="restart: return once the new scheduler runs (or it gave up)")
    p.add_argument("--cancel", action="store_true", help="restart: withdraw a pending restart")
    p = sub.add_parser("top", help="live view of every instance, the services and the messages between instances; "
                                   "starts a cycle for an idle one (q quits)")
    p.add_argument("--once", action="store_true", help="print one snapshot as text and exit")
    p = sub.add_parser("status", help="how the instances are doing")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("log", help="recent journal entries")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p.add_argument("-n", type=int, default=3)
    p = sub.add_parser("review", help="what the instances did in the last hours and what of it was worth anything "
                                      "(judged value, success, repeated goals, the cycles worth reading)")
    p.add_argument("names", nargs="*")
    p.add_argument("--hours", type=float, default=24)
    p = sub.add_parser("stats", help="an instance's measured performance (judge + calibration)")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p = sub.add_parser("judge", help="judging: 'backfill' asks cycles judged before for the ratings and value they lack; "
                                     "'catch' reports planted delivery failures; 'second' runs the second judge; 'value-regrade' re-asks only value (paid); "
                                     "'pushback-outcome' previews later outcomes, --write pays and stores apart")
    p.add_argument("action", choices=["backfill", "second", "value-regrade", "pushback-outcome", "catch"])
    p.add_argument("names", nargs="*")
    p.add_argument("--days", type=int, default=3, help="pushback-outcome: minimum cycle age in days")
    p.add_argument("--write", action="store_true", help="pushback-outcome: pay for judgments and store apart")
    p.add_argument("--min-value", type=int, default=0, help="value-regrade: minimum stored value")
    p.add_argument("--value-for", default="own_capability,own_record", help="value-regrade: comma-separated beneficiaries")
    p.add_argument("--since", default="", help="value-regrade/pushback-outcome: cycles since DATE (ISO format)")
    p.add_argument("--limit", type=int, default=None, help="value-regrade: newest N matching cycles")
    p = sub.add_parser("battery", help="the capability battery: run, report, recall (questions), backup, off|on per instance")
    p.add_argument("action", choices=["run", "report", "recall", "backup", "off", "on", "perturbation"])
    p.add_argument("names", nargs="*")
    p.add_argument("--until", default="", help="run: stop at this time (HH:MM), like the night window")
    p.add_argument("--only", default="", help="run: only these categories (comma-separated)")
    p.add_argument("--no-control", action="store_true", help="run: skip the control")
    p.add_argument("--control-only", action="store_true",
                   help="run: only the control, on coding and research (checks the items discriminate)")
    p.add_argument("--limit", type=int, default=None, help="run: at most this many items")
    p.add_argument("--force", action="store_true", help="recall: write new questions (starts a new era)")
    p = sub.add_parser("night-replay", help="replay the day's worst judged cycles with the judge's first error shown "
                       "(cloud-model instances, night window; #5)")
    p.add_argument("action", choices=["run", "report"])
    p.add_argument("names", nargs="*")
    p.add_argument("--until", default="", help="run: stop at this time (HH:MM), like the night window")
    p.add_argument("--dry-run", action="store_true", help="run: show eligibility, picks and the cap; no model call")
    p = sub.add_parser("surprise", help="unexpected tool results, their recurrence, live cost (the surprise baseline)")
    p.add_argument("names", nargs="*")
    p.add_argument("--freeze", action="store_true", help="also write the baseline the surprise loop is judged against")
    p = sub.add_parser("surprise-rejudge", help="re-judge the cycles before the surprise loop with its judge fields, "
                       "into judgments/<name>-surprise-baseline.jsonl (paid; --dry-run counts and prices it first)")
    p.add_argument("names", nargs="+")
    p.add_argument("--dry-run", action="store_true", help="count the cycles and estimate the cost; no judge call")
    p.add_argument("--limit", type=int, default=None, help="the newest this many cycles per instance (a re-run judges only "
                   "what they still lack)")
    p.add_argument("--report", action="store_true", help="only print the comparison with the stored verdicts")
    p.add_argument("--control", action="store_true", help="the M2 control: re-judge the baseline's cycles by the same "
                   "judge without the surprise fields, into judgments/<name>-surprise-control.jsonl, and compare")
    p = sub.add_parser("surprise-report", help="the surprise loop's acceptance window: P1-P5 against the frozen "
                       "baseline, and how each sleep handled the surprises")
    p.add_argument("names", nargs="*")
    p.add_argument("--since", required=True, help="when the window began (M3 live): YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    p.add_argument("--until", default="", help="when it ends (default: now)")
    p.add_argument("--min-cycles", type=int, default=30, help="judged cycles the window needs (§4.1: shaft 30)")
    # Measurements and kill tests (read the record; only anchoring pays a judge,
    # only experiment replays cycles)
    p = sub.add_parser("consults", help="what each instance spent consulting other models, per model and run, and how "
                       "its consulted cycles fared")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("compression", help="offline memory compression assay with held-out recall (#113)")
    p.add_argument("name")
    p.add_argument("--since", default="", help="inclusive sleep date: YYYY-MM-DD")
    p = sub.add_parser("metacog", help="how well each instance's confidence tells its met cycles from its missed ones: "
                       "AUROC (clauses apart), bias, resolution, meta-I, by domain, paired v3, claim confusion "
                       "matrix and doubt/first-error alignment (A1, #98)")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("anchoring", help="does the judge follow the instance's own claim? The same judge grades sampled "
                       "cycles sighted, re-asked and with the claim withheld (A0; paid: --dry-run prices it first)")
    p.add_argument("names", nargs="+")
    p.add_argument("--n", type=int, default=40, help="cycles per instance (default 40; a re-run asks only what is missing)")
    p.add_argument("--bare", action="store_true", help="also the arm with the whole report withheld")
    p.add_argument("--dry-run", action="store_true", help="count and price the judge calls; ask nothing")
    p.add_argument("--report", action="store_true", help="only print the comparison")
    p = sub.add_parser("attention", help="where each instance said it would look, and where it looked (B3)")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("commitments", help="each instance's promises: kept (checked against its record), broken, "
                       "overdue (C2)")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("provenance", help="where each instance's facts come from, checked against its tool results (C3)")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("experiment", help="kill tests of the self-knowledge mechanisms: cards, attention, commitments, forecast "
                       "(knob trials), monitors, choice-blindness, privileged, recognition and "
                       "resource-shock; --report shows results")
    p.add_argument("item", choices=["cards", "attention", "commitments", "forecast", "monitors",
                                   "choice-blindness", "privileged", "recognition", "resource-shock"])
    p.add_argument("name")
    p.add_argument("--n", type=int, default=6, help="past cycles (knob trials: per trial; choice-blindness: four "
                   "presentations per cycle; otherwise: states; default 6)")
    p.add_argument("--k", type=int, default=3, help="independent replays per privileged state (default 3)")
    p.add_argument("--max-usd", type=float, default=5.0, help="privileged/recognition: dollars this run may spend "
                   "(default 5; charged as the operator's, not to the instance's trial budget)")
    p.add_argument("--cycles", type=int, default=6, help="resource-shock: consecutive cycles per arm (minimum 3)")
    p.add_argument("--model", default="", help="replays' model (default: the trial default; "
                   "monitors/choice-blindness: cloud fallback)")
    p.add_argument("--dry-run", action="store_true", help="say what would run; replay nothing")
    p.add_argument("--report", action="store_true", help="only print the result so far")
    p.add_argument("--channel", choices=["base_rate", "surprises", "envelope"],
                   help="monitors only: intervene on just this channel (default: each separately)")
    p = sub.add_parser("feedback", help="current operator ratings, newest first")
    p.add_argument("name", nargs="?")
    p.add_argument("--days", type=int, help="only ratings from the last N days (default: all)")
    p = sub.add_parser("inbox", help="messages instances sent you")
    p.add_argument("--clear", action="store_true", help="archive notes (questions stay until answered)")
    p = sub.add_parser("reply", help="answer an explicit, quoted, or oldest open question (or message it)")
    p.add_argument("name")
    p.add_argument("text")
    p.add_argument("--question", metavar="FILE", help="which open question to answer, as shaft inbox names it; "
                   "default: the one the text quotes, else the oldest")
    p = sub.add_parser("send", help="message an instance")
    p.add_argument("name")
    p.add_argument("text")
    for verb, text in (("drift", "how an instance's personality changed, and distance to others"),
                       ("distinct", "blind test: can the judge tell the instance from the bare base model?"),
                       ("dataset", "export an instance's experiences for fine-tuning"),
                       ("finetune", "train + judge a LoRA adapter on an instance's experiences (MLX)")):
        p = sub.add_parser(verb, help=text)
        p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
        if verb == "distinct":
            p.add_argument("--strict", action="store_true",
                           help="also judge answers stripped of specifics (voice, not memory)")
        if verb == "finetune":
            p.add_argument("--iters", type=int)
            p.add_argument("--evaluate", metavar="ADAPTER", help="only (re-)judge an existing adapter")
            p.add_argument("--min-examples", type=int, default=finetune.MIN_EXAMPLES,
                           help="lower only for a pipeline check; the result is labelled as such")
    p = sub.add_parser("breed", help="a child from two instances' recombined genes, with a fresh mind")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("child")
    p = sub.add_parser("mind", help="an instance's private mind store: status, backfill and check (on a copy, or "
                                    "before a cutover), cutover (one instance, when idle), guard (the project's "
                                    "pre-push hook refusing instance/* branches)")
    p.add_argument("action", choices=["status", "backfill", "check", "cutover", "guard"])
    p.add_argument("names", nargs="*")
    p.add_argument("-n", type=int, default=20, help="check, cutover: past live cycles compared")
    p.add_argument("--project", type=Path, default=None,
                   help="backfill, check: a copy of the project repository (git clone --mirror) instead of the real one")
    sub.add_parser("list", help="list instances")
    sub.add_parser("setup", help="create missing worktrees for template and instance branches")
    p = sub.add_parser("install", help="the fresh-Mac install steps after the clone (git identity, setup, api and web "
                                       "on this Mac only, scheduler); one JSON line per step with --json")
    p.add_argument("--operator", required=True, help="your name (the git author when none is set)")
    p.add_argument("--email", help="the git author's email (default: user@host.local)")
    p.add_argument("--json", action="store_true", required=True, help="one JSON object per step on stdout")
    p = sub.add_parser("deploy", help="move this checkout onto release TAG as docs/deploy.md says (fast-forward main and "
                                      "the template, update the instances, drain-restart the scheduler, restart api, "
                                      "web and telegram when no conversation is open); one JSON line per step")
    p.add_argument("tag", nargs="?", help="the release tag, vX.Y.Z (its template commit is template-vX.Y.Z)")
    p.add_argument("--check", action="store_true", help="only say the newest release on origin this could move to")
    p.add_argument("--local", action="store_true",
                   help="with --check: ask nothing of origin, only say a deploy left unfinished here")
    p.add_argument("--services-only", action="store_true",
                   help="only the services and verify steps: the retry after a deploy that waited for conversations")
    p.add_argument("--json", action="store_true", required=True, help="one JSON object per step on stdout")
    p = sub.add_parser("update", help="merge the template into instances (only when idle; gate-tested)")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("new", help="create an instance")
    p.add_argument("name")
    p.add_argument("--from", dest="source", help="fork code, skills and plugins (not memory) from this instance")
    p.add_argument("--seed", action="store_true",
                   help="with --from: also start knowing what the parent knows about you (its people file and "
                        "facts, marked as its own), never what it is")
    p.add_argument("--json", action="store_true", help="one JSON object on stdout: ok, name, branch, worktree, paused")
    p.add_argument("--paused-until-first-reply", choices=["resume", "stay"], metavar="{resume,stay}",
                   help="create it paused, with a marker saying what to do once you have answered its welcome")
    p = sub.add_parser("first-reply", help="for an instance created with --paused-until-first-reply: what your first "
                                           "answered reply does (resume: it works on its own; stay: it stays paused)")
    p.add_argument("name")
    p.add_argument("then", choices=["resume", "stay"])
    p.add_argument("--json", action="store_true", help="one JSON object on stdout: ok, name, then (or error)")
    for verb in ("pause", "resume"):
        p = sub.add_parser(verb, help=f"{verb} autonomous runs (all, or one instance)")
        p.add_argument("name", nargs="?")
    p = sub.add_parser("kill", help="pause an instance and stop everything it is running now (cycle, reply, job, "
                                    "battery, trial replay); its runs settle as usual (gate, commit). shaft resume undoes it")
    p.add_argument("name")
    p.add_argument("--wait", type=float, default=120, help="seconds to wait for its runs to settle (default 120)")
    p = sub.add_parser("delete", help="remove an instance and every trace of it, after archiving it in "
                                      ".supervisor/deleted/ (refused while it runs: shaft kill first)")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="no question (otherwise you type its name to confirm)")
    p.add_argument("--no-archive", action="store_true", help="keep no archive: its mind is gone for good")
    p = sub.add_parser("limits", help="the operator's own limits for an instance: its wake floor (shortest wake interval "
                                      "it may set itself) and its daily cloud and consult caps; 'off' goes back to the default")
    p.add_argument("name", nargs="?")
    p.add_argument("limit", nargs="?", choices=["wake-floor", "cloud-cap", "consult-cap"])
    p.add_argument("value", nargs="?", help="minutes (wake-floor), dollars (cloud-cap or consult-cap), or off")
    p = sub.add_parser("jobs", help="instances' scheduled jobs (their own plugins on a timer): list, or switch an "
                                    "instance's jobs on/off")
    p.add_argument("name", nargs="?")
    p.add_argument("action", nargs="?", choices=["on", "off"])
    p = sub.add_parser("job", help="(the scheduler's) run one scheduled job now: job run <name> <plugin>")
    p.add_argument("action", choices=["run"])
    p.add_argument("name")
    p.add_argument("plugin")
    p = sub.add_parser("tools", help="an instance's supervisor tools: list them, or switch one on/off")
    p.add_argument("name")
    p.add_argument("action", nargs="?", choices=["on", "off"])
    p.add_argument("tool", nargs="?")
    p = sub.add_parser("channel", help="where an instance's messages alert you: auto (where you last wrote to it), web "
                                       "or telegram; alone shows it and where a message would go now")
    p.add_argument("name")
    p.add_argument("value", nargs="?", choices=["auto", "web", "telegram"])
    p = sub.add_parser("orders", help="an instance's standing orders (decisions that stay decided): list them, add "
                                      "TEXT, or drop ID; it is shown them every run and its messages to you are checked "
                                      "against them")
    p.add_argument("name")
    p.add_argument("action", nargs="?", choices=["list", "add", "drop"])
    p.add_argument("text", nargs="*", help="add: the order, verbatim; drop: its id (o3)")
    p.add_argument("--via", choices=["cli", "web"], default="cli", help=argparse.SUPPRESS)
    p = sub.add_parser("secret", help="an instance's own keys: give it one (value read from your terminal), list, delete")
    p.add_argument("name")
    p.add_argument("action", choices=["set", "google", "list", "delete"],
                   help="google: its own Google account, signed in in your browser (google <key> <address> --client)")
    p.add_argument("key", nargs="?")
    p.add_argument("address", nargs="?", help="google: the instance's own Google address (never one of yours)")
    p.add_argument("--hosts", default="", help="comma-separated hosts the key may be sent to (google: from the scope)")
    p.add_argument("--client", default="", help="google: the OAuth client in .env (GMAIL_<CLIENT>_CLIENT_ID/_SECRET)")
    p.add_argument("--scope", default="gmail.modify",
                   help="google: gmail.modify (read, send, label, trash its mail) or drive (its Drive, Sheets, Docs and "
                        "Slides)")
    p.add_argument("--header", default="Authorization")
    p.add_argument("--scheme", default="Bearer", help="e.g. Bearer, token; '' for the raw value")
    p = sub.add_parser("share", help="a folder of yours an instance may read (read-only, seen as shared/); 'off' removes it")
    p.add_argument("name")
    p.add_argument("folder", nargs="?", help="a directory, or 'off'")
    p = sub.add_parser("browser", help="the Chromium every instance browses with: 'install "
                                       "[--playwright X.Y.Z]' (re)installs it; alone shows it and each profile")
    p.add_argument("action", nargs="?", choices=["install"])
    p.add_argument("--playwright", default=None, help="install: the Playwright version (default: the supervisor's pin)")
    p = sub.add_parser("google", help="your Google accounts, for mail, calendar and Drive: 'client <name>' (a Desktop "
                                      "app OAuth client from its downloaded JSON, on stdin), 'login <alias> <address> "
                                      "[--client <name>]' (reads, calendar writes and mail drafts), 'migrate' (from .supervisor/mail/); "
                                      "alone lists them with their clients and granted scopes")
    p.add_argument("action", nargs="?", choices=["client", "login", "migrate"])
    p.add_argument("alias", nargs="?", help="with login: the account's alias (e.g. work); with client: the client's "
                                            "name (GMAIL_<NAME>_CLIENT_ID/_SECRET), its downloaded JSON on stdin")
    p.add_argument("address", nargs="?", help="with login: its address")
    p.add_argument("--client", default="", help="with login: the OAuth client (GMAIL_<CLIENT>_CLIENT_ID/_SECRET in .env); "
                                                "default: the alias. One per Google organisation: personal Google needs its own")
    p.add_argument("--json", action="store_true", help="client, login: one JSON object per line on stdout (login: "
                                                       "the consent URL first, opened by the caller)")
    p = sub.add_parser("mail", help="your Gmail an instance may read, one label at a time: "
                                    "'<name> grant|revoke|grant-draft|revoke-draft <alias> <label>', "
                                    "'<name> off', '<name>' shows; alone lists all "
                                    "(accounts: shaft google)")
    p.add_argument("target", nargs="?", help="an instance")
    p.add_argument("action", nargs="?", help="grant, revoke, grant-draft, revoke-draft, off")
    p.add_argument("alias", nargs="?", help="the account alias")
    p.add_argument("label", nargs="?", help="the Gmail label")
    p = sub.add_parser("gdrive", help="your Drive an instance may read: '<name> grant <alias> [--all]', "
                                      "'<name> revoke <alias>', '<name> off'; alone lists accounts and grants")
    p.add_argument("target", nargs="?", help="an instance")
    p.add_argument("action", nargs="?", choices=("grant", "revoke", "off"))
    p.add_argument("alias", nargs="?", help="the Google account alias")
    p.add_argument("--all", dest="all_reach", action="store_true", help="include everything shared from outside")
    p = sub.add_parser("gchat", help="your Google Chat an instance may read: '<name> grant|revoke <alias>', "
                                     "'<name> off'; alone lists accounts and grants")
    p.add_argument("target", nargs="?", help="an instance")
    p.add_argument("action", nargs="?", choices=("grant", "revoke", "off"))
    p.add_argument("alias", nargs="?", help="the Google account alias")
    p = sub.add_parser("calendar", help="your calendars an instance may read, one per grant: "
                                        "'<name> grant|revoke|grant-write|revoke-write <alias> primary|<title>|<id>', '<name> off', '<name>' "
                                        "shows; 'calendars <alias>' lists an account's; alone lists all grants")
    p.add_argument("target", nargs="?", help="an instance, or 'calendars'")
    p.add_argument("action", nargs="?", help="grant, revoke, grant-write, revoke-write, off — or, after calendars, the account alias")
    p.add_argument("alias", nargs="?", help="the account alias")
    p.add_argument("which", nargs="?", help="primary, a calendar's title, or its ID")
    p = sub.add_parser("actions", help="action classes, history and approvals (#142)")
    p.add_argument("name")
    p.add_argument("action", nargs="?", choices=["restore", "overreach", "approve", "reject"])
    p.add_argument("target", nargs="?", help="class to restore, or action/proposal id")
    p.add_argument("--reason", default="", help="reject reason; 'over-reach' demotes the class")
    p = sub.add_parser("trial", help="trials of proposed changes: list, show, run (operator), replay, models, work")
    tsub = p.add_subparsers(dest="action", required=True)
    q = tsub.add_parser("list", help="every trial, newest last")
    q.add_argument("name", nargs="?")
    q = tsub.add_parser("show", help="one trial in full, with its report")
    q.add_argument("name")
    q.add_argument("id")
    q = tsub.add_parser("run", help="queue a trial as the operator (its verdict is not sent to the instance)")
    q.add_argument("name")
    q.add_argument("--cand", action="append", default=[], metavar="PATH=FILE",
                   help="the change: PATH (in the instance) gets FILE's content; PATH= deletes it")
    q.add_argument("--base", action="append", default=[], metavar="PATH=FILE",
                   help="replace PATH in the current version (A) too — for planted tests")
    q.add_argument("--aa", action="store_true", help="A/A calibration: the 'change' is the current code itself")
    q.add_argument("--hypothesis", default="operator trial")
    q.add_argument("--cycles", type=int, default=1, help="consecutive live cycles per arm from one past state")
    q.add_argument("--sleep", action="store_true", help="sleep between each pair of replayed live cycles")
    q.add_argument("-n", type=int, default=0)
    q.add_argument("--episodes", nargs="*", default=[])
    q.add_argument("--model", default="auto", help="auto, cloud, local, or an instance model id")
    q = tsub.add_parser("replay", help="replay one past cycle once with the current code (debug); keeps a copy")
    q.add_argument("name")
    q.add_argument("ts", help="the episode's ts")
    q.add_argument("--model", default="local", help="'local' (its own configuration) or an instance model id")
    q.add_argument("--keep", help="copy the replayed folder here")
    q.add_argument("--cycles", type=int, default=1, help="consecutive live cycles from one past state")
    q.add_argument("--sleep", action="store_true", help="sleep between each pair of replayed live cycles")
    q = tsub.add_parser("models", help="bake-off: replay past cycles on each model and judge them against the first "
                                       "(supervisor/bakeoff.py); the same --id again resumes or reports")
    q.add_argument("name")
    q.add_argument("--arm", action="append", default=[], metavar="MODEL[@EFFORT]",
                   help="an instance model, optionally with LLM_REASONING_EFFORT; the first is the baseline")
    q.add_argument("--episodes", nargs="*", default=[], help="the past live cycles (episode ts)")
    q.add_argument("--draw", type=int, default=0, metavar="N",
                   help="instead of --episodes: N eligible live cycles drawn by kind (operator-asked, writing, "
                        "checking only) after --since; without --arm it prints the set and runs nothing")
    q.add_argument("--since", default="", help="with --draw: cycles that ended after this time (default: the "
                                               "instance's loop or configuration last changed)")
    q.add_argument("--until", default="", help="with --draw: cycles that ended by this time (default: now)")
    q.add_argument("--kind", default="", choices=["", "operator", "writes", "check"],
                   help="with --draw: that kind of cycle only (operator: those that set out to do what you asked)")
    q.add_argument("--judge", default="claude-sonnet-5-5")
    q.add_argument("--id", default="", help="a bake-off's id: resume it (its arms and cycles are kept)")
    q.add_argument("--parallel", type=int, default=3, help="past cycles replayed at once")
    q.add_argument("--arm-parallel", type=int, default=1, help="candidate models of one cycle replayed at once "
                                                                "(after its baseline)")
    q.add_argument("--replay-only", action="store_true", help="replay, do not judge yet (a later run judges)")
    q.add_argument("--report", action="store_true", help="print the saved report only; run nothing")
    q.add_argument("--export-cases", metavar="DIR", help="write the blind judge cases for subagents to DIR/cases/ "
                                                          "and list those still without a DIR/verdicts/<case>.json")
    q.add_argument("--import-verdicts", metavar="DIR", help="decide every pair from DIR/verdicts/ and print the report")
    q.add_argument("--judged-by", default="claude-opus-5-5", help="the model of the subagents whose verdicts are "
                                                                  "imported")
    tsub.add_parser("work", help="run the queued trials' tests and replays that may run now (the scheduler does this)")
    p = sub.add_parser("check", help="run the test gate against an instance's code")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p = sub.add_parser("gate", help="the test gate's upkeep: warm runs the supervisor's shared tests on the current "
                                    "commit unless their pass is recorded (the scheduler does it after each merge); "
                                    "budget runs both parts on the last commit and fails one over its time budget "
                                    "(the check before a merge); regate gates again the code changes held while the "
                                    "gate's baseline was red (the scheduler does it after a green warm-up)")
    p.add_argument("action", choices=["warm", "budget", "regate"])
    args = parser.parse_args(argv)
    from .core import app_wording
    # A --json verb answers the Mac app, and so does a verb an app's route runs (actions._run): the app shows its
    # errors as they are, so they name what to do in the app, never a terminal command (core.say).
    with app_wording(bool(getattr(args, "json", False)) or os.environ.get(actions.APP_WORDING) == "1"):
        return _dispatch(args, parser)


def _dispatch(args, parser) -> int:
    if args.cmd == "grok":
        return cmd_grok(args.action)
    if args.cmd == "codex":
        return cmd_codex(args.action, args.json)
    if args.cmd == "claude":
        return cmd_claude(args.action)
    if args.cmd == "antigravity":
        return cmd_antigravity(args.action)
    if args.cmd == "migrate":
        from . import migrate
        return migrate.command(args.role, args.to, args.check)

    if args.cmd == "talk":
        extra = (["--show-thinking"] if args.show_thinking else []) + (["--as", args.speaker] if args.speaker else [])
        if (mine := busy_with(args.name)):
            print(f"{args.name} is busy ({mine}); " + ("pausing it for your conversation…" if mine.startswith(
                ("live", "sleep", "reflect")) else "your conversation starts when it finishes…"), file=sys.stderr)
        others = {n: b for n in instance_names() if n != args.name and (b := busy_with(n))}
        background = sharing_model(args.name, [n for n, b in others.items() if not b.startswith("talk")])
        if background:
            request_yield(background)
            print(f"note: {', '.join(f'{n} ({others[n]})' for n in background)} is using the model {args.name} "
                  f"needs ({model_server(args.name)}); asked to wrap up at its next turn so you get it — the first "
                  f"reply may still wait for that turn.", file=sys.stderr)
        ui_python = PROJECT / ".venv-ui" / "bin" / "python"
        if not args.plain and sys.stdin.isatty() and sys.stdout.isatty() and ui_python.exists():
            os.execv(ui_python, [str(ui_python), "-E", "-s", "-m", "supervisor.talk_ui", args.name, *extra])
        if not args.plain and sys.stdin.isatty() and not ui_python.exists():
            print(f"(plain mode: the terminal client needs {ui_python} — python3 -m venv .venv-ui && "
                  f".venv-ui/bin/pip install -r requirements-ui.txt)", file=sys.stderr)
        plain_args = ["talk"] + (["--show-thinking"] if args.show_thinking else []) + \
            (["--speaker", args.speaker] if args.speaker else [])
        code = run_agent(args.name, "talk", plain_args, interactive=True)
        spawn_recover(args.name)   # the journal entry is written in the background — leaving is instant
        return code
    if args.cmd in ("live", "reflect", "sleep", "recover"):
        return run_many(args.cmd, args.names, args.all)
    if args.cmd == "wake":
        return autonomy.wake(args.name)
    if args.cmd == "tick":
        return autonomy.tick()
    if args.cmd in actions.GUARDED_CLI and (refused := actions.cli_guard(args.cmd, args.action, args.force)):
        if getattr(args, "json", False):    # the Mac app reads one JSON line, whatever refused
            # the app's own words: no --force or terminal verb in what it shows
            print(json.dumps({"ok": False, "error": refused.split(" Try again when")[0] + " Try again when they end."}))
        else:
            print(refused, file=sys.stderr)
        return 1
    if args.cmd == "api":
        from . import api
        return api.command(args.action, local=args.local, as_json=args.json, force=args.force)
    if args.cmd == "secrets":
        from . import opsecrets
        return opsecrets.command(args.action, args.name, args.json)
    if args.cmd == "meetings":
        from . import meetings
        return meetings.command(args)
    if args.cmd == "speakers":
        from . import speakers
        return speakers.command(args)
    if args.cmd == "provider":
        from . import provider
        return provider.command(args)
    if args.cmd == "config":
        from . import opconfig
        return opconfig.config_command(args.name, args.action, args.items, args.expect, args.expect_unset)
    if args.cmd == "settings":
        from . import settings
        return settings.command(args.action, args.items, args.expect, args.expect_unset, args.json, args.from_web)
    if args.cmd == "drive":
        if args.action in ("proposals", "accept", "reject"):
            from . import drive_proposals
            return drive_proposals.command(args.name, args.action, args.id, args.expect_sha, args.reason)
        from . import opconfig
        return opconfig.drive_command(args.name, args.action, args.file, args.expect_sha)
    if args.cmd == "docs":
        from . import docs
        return docs.command(args.name, args.action, args.path, args.file, args.expect_sha, sync=args.sync)
    if args.cmd == "cycle":
        instance_root(args.name)
        try:
            print(autonomy.start_wake(args.name))
        except RuntimeError as e:
            print(f"not started: {e}")
            return 1
        return 0
    if args.cmd == "recap":
        from . import recap
        return recap.command(args.names)
    if args.cmd == "alerts":
        return cmd_alerts(args.action, args.ids)
    if args.cmd == "web":
        from . import web
        return web.command(args.action, args.name if args.action == "pair" else args.device, args.all,
                           local=args.local, as_json=args.json, force=args.force)
    if args.cmd == "telegram":
        from . import telegram
        return telegram.command(args.action, args.name, args.json, force=args.force)
    if args.cmd == "model":
        return autonomy.service("model", args.action)
    if args.cmd in ("embed", "listen"):
        return autonomy.service(args.cmd, args.action)
    if args.cmd == "top":
        ui_python = PROJECT / ".venv-ui" / "bin" / "python"
        extra = ["--once"] if args.once else []
        if not ui_python.exists():
            print(f"shaft top needs {ui_python} (python3 -m venv .venv-ui && .venv-ui/bin/pip install "
                  f"-r requirements-ui.txt)", file=sys.stderr)
            return 2
        os.execv(ui_python, [str(ui_python), "-E", "-s", "-m", "supervisor.top", *extra])
    if args.cmd == "limits":
        return cmd_limits(args.name, args.limit, args.value)
    if args.cmd == "jobs":
        return cmd_jobs(args.name, args.action)
    if args.cmd == "job":
        from . import jobs
        instance_root(args.name)
        return jobs.run(args.name, args.plugin)
    if args.cmd == "tools":
        return cmd_tools(args.name, args.action, args.tool)
    if args.cmd == "channel":
        return cmd_channel(args.name, args.value)
    if args.cmd == "orders":
        return cmd_orders(args.name, args.action, args.text, args.via)
    if args.cmd == "secret":
        return cmd_secret(args)
    if args.cmd == "share":
        return cmd_share(args.name, args.folder)
    if args.cmd == "browser":
        from . import browser
        if args.action == "install":
            return browser.install(args.playwright or browser.PLAYWRIGHT)
        print(f"shared Chromium: {browser.describe_install()} ({browser.BROWSERS})")
        for name in instance_names():
            print(f"  {name}: {browser.describe(INSTANCES / name)}")
        return 0
    if args.cmd == "google":
        return cmd_google(args.action, args.alias, args.address, args.client, args.json)
    if args.cmd == "mail":
        if args.target == "login":   # one login for every Google service
            print("shaft mail login is gone: shaft google login <alias> <address> [--client <name>]", file=sys.stderr)
            return 2
        if args.target and args.action not in (None, "grant", "revoke", "grant-draft", "revoke-draft", "off"):
            print(f"unknown mail action {args.action!r}: grant, revoke, grant-draft, revoke-draft or off", file=sys.stderr)
            return 2
        return cmd_mail(args.target, args.action, args.alias, args.label)
    if args.cmd == "gchat":
        return cmd_gchat(args.target, args.action, args.alias)
    if args.cmd == "gdrive":
        return cmd_gdrive(args.target, args.action, args.alias, args.all_reach)
    if args.cmd == "calendar":
        if args.target not in (None, "calendars") and args.action not in (None, "grant", "revoke", "grant-write", "revoke-write", "off"):
            print(f"unknown calendar action {args.action!r}: grant, revoke, grant-write, revoke-write or off", file=sys.stderr)
            return 2
        return cmd_calendar(args.target, args.action, args.alias, args.which)
    if args.cmd == "actions":
        from . import calendar_proposals
        try:
            if args.action and not args.target:
                raise ValueError('an action needs a class or id')
            if args.action == 'restore':
                actions.restore(args.name, args.target)
            elif args.action == 'overreach':
                actions.overreach(args.name, args.target)
            elif args.action in ('approve', 'reject'):
                row = next((r for r in calendar_proposals.listing()
                            if r['id'] == args.target and r['instance'] == args.name), None)
                if row is None:
                    raise ValueError('no such proposal for this instance')
                row = (calendar_proposals.decide(args.target, True) if args.action == 'approve' else
                       calendar_proposals.reject(args.target, args.reason))
                print(json.dumps(row, ensure_ascii=False))
                return 1 if row['state'] == 'failed' else 0
            state = actions.status(args.name)
            state['proposals'] = [r for r in calendar_proposals.listing() if r['instance'] == args.name]
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    if args.cmd == "autonomy":
        if args.action == "restart":
            return autonomy.restart(args.within, args.wait, args.cancel)
        return autonomy.autonomy(args.action)
    if args.cmd == "status":
        return cmd_status(args.names or instance_names())
    if args.cmd == "log":
        print("\n\n---\n\n".join(journal_entries(instance_root(args.name))[-args.n:]))
        return 0
    if args.cmd == "stats":
        instance_root(args.name)
        metrics.judge_pending(args.name)
        print(metrics.stats_text(args.name))
        return 0
    if args.cmd == "review":                 # read-only: judges nothing, so it costs nothing and waits for no run
        for name in args.names:
            instance_root(name)
        print(metrics.review_text(args.names or instance_names(), args.hours), end="")
        return 0
    if args.cmd == "judge" and args.action == "pushback-outcome":
        if len(args.names) != 1:
            print("pushback-outcome needs exactly one instance", file=sys.stderr)
            return 2
        instance_root(args.names[0])
        from . import pushback
        try:
            print(pushback.outcome(args.names[0], args.since, args.days, args.write), end="")
        except (ValueError, metrics.judge.JudgeError) as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0
    if args.cmd == "judge" and args.action == "catch":
        from . import judge_probe
        for name in args.names or instance_names():
            instance_root(name)
            print(f"{name}: {judge_probe.line(name)}")
        return 0
    if args.cmd == "judge" and args.action == "second":
        print(f"second judge: {metrics.second_pending(args.names or None)} cycle(s) graded")
        return 0
    if args.cmd == "judge" and args.action == "value-regrade":
        if len(args.names) != 1:
            print("value-regrade needs exactly one instance", file=sys.stderr)
            return 2
        instance_root(args.names[0])
        try:
            print(metrics.value_regrade(args.names[0], args.min_value, args.value_for, args.since, args.limit), end="")
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0
    if args.cmd == "judge":
        for name in args.names or instance_names():
            instance_root(name)
            n = metrics.backfill(name)
            metrics.write_stats(name)
            print(f"{name}: backfilled {n} earlier cycle(s)")
        return 0
    if args.cmd == "battery":
        return cmd_battery(args)
    if args.cmd == "night-replay":
        return cmd_night_replay(args)
    if args.cmd == "surprise":
        for name in args.names:
            instance_root(name)
        return surprise.command(args.names or instance_names(), args.freeze)
    if args.cmd == "surprise-rejudge" and args.control:
        for name in args.names:
            instance_root(name)
            if not args.report:
                r = metrics.surprise_control(name, dry_run=args.dry_run)
                print(f"{name}: {r['todo']} cycle(s) for the control, {r['done']} done; about ${r['estimated_usd']:.2f} "
                      f"(${r['per_judgment_usd']:.3f} each: {r['basis']})")
        if args.report or not args.dry_run:
            print(metrics.surprise_control_report(args.names))
        return 0
    if args.cmd == "surprise-rejudge":
        for name in args.names:
            instance_root(name)
            if not args.report:
                r = metrics.surprise_baseline(name, args.limit, dry_run=args.dry_run)
                print(f"{name}: {r['todo']} cycle(s) to re-judge, {r['done']} done; about ${r['estimated_usd']:.2f} "
                      f"(${r['per_judgment_usd']:.3f} each: {r['basis']}); each by its stored verdict's judge; "
                      f"{r['cross_judge']} earlier re-judge(s) by another judge set aside, "
                      f"{r['no_stored_judge']} cycle(s) left out with no usable stored judge")
            if args.report or not args.dry_run:
                print(metrics.surprise_baseline_report(name))
        return 0
    if args.cmd == "surprise-report":
        names = args.names or instance_names()
        for name in names:
            instance_root(name)
        try:
            since = datetime.fromisoformat(args.since)
            until = datetime.fromisoformat(args.until) if args.until else None
        except ValueError as e:
            raise SystemExit(f"--since/--until: {e}")
        code = surprise.m5_command(names, since, until, min_cycles=args.min_cycles)
        for name in names:
            print(surprise.disposition_text(name) + "\n")
        return code
    if args.cmd == "compression":
        from . import compression
        instance_root(args.name)
        try:
            print(compression.report(args.name, args.since))
        except (ValueError, mind.MindError) as e:
            raise SystemExit(str(e))
        return 0
    if args.cmd in ("metacog", "attention", "commitments", "provenance", "consults"):
        import importlib
        names = args.names or instance_names()
        for name in names:
            instance_root(name)
        module = "consultstats" if args.cmd == "consults" else args.cmd
        print(importlib.import_module(f".{module}", __package__).report(names))
        return 0
    if args.cmd == "anchoring":
        from . import anchoring
        for name in args.names:
            instance_root(name)
            if not args.report:
                r = anchoring.run(name, args.n, bare=args.bare, dry_run=args.dry_run)
                print(f"{name}: {r['cycles']} cycle(s), {r['todo']} judge call(s) to make, {r['done']} done; about "
                      f"${r['estimated_usd']:.2f} (${r['per_judgment_usd']:.3f} each: {r['basis']})")
                if r.get("stopped"):
                    print(f"{name}: stopped — {r['stopped']}", file=sys.stderr)
        if args.report or not args.dry_run:
            print(anchoring.report(args.names))
        return 0
    if args.cmd == "experiment":
        from . import experiments, trial
        instance_root(args.name)
        try:
            if args.channel and args.item != "monitors":
                raise experiments.ExperimentError("--channel is only for monitors")
            if args.item in ("monitors", "choice-blindness"):
                run = experiments.run_monitors if args.item == "monitors" else experiments.run_choice_blindness
                report = experiments.monitors_report if args.item == "monitors" else experiments.choice_blindness_report
                kw = {"channel": args.channel} if args.item == "monitors" else {}
                if not args.report:
                    r = run(args.name, args.n, model=args.model, dry_run=args.dry_run, **kw)
                    print(f"{args.name}: {r['states']} state(s), {r['replays']} presentation(s) on {r['model']}"
                          + (" (dry run: no spend)" if args.dry_run else f", {r['done']} done"))
                if args.report or not args.dry_run:
                    print(report(args.name, **kw))
                return 0
            if args.item in ("privileged", "recognition"):
                from . import privileged
                if args.model:
                    raise trial.TrialError("self-knowledge experiments use the instance's own model")
                if not args.report:
                    print(json.dumps(privileged.run(args.name, args.item, args.n, args.k, args.dry_run, max_usd=args.max_usd)))
                if args.report or not args.dry_run:
                    print(privileged.report(args.name, args.item))
                return 0
            if args.item == "resource-shock":
                if args.report:
                    print(experiments.resource_shock_report(args.name))
                else:
                    r = experiments.resource_shock(args.name, args.cycles, dry_run=args.dry_run)
                    print(f"{r['replays']} replays, {r['replay_units']} cycles, {r['model_calls_planned']} planned "
                          f"agent model turns on {r['model']} (retries and tool-model calls extra)"
                          + (" (dry run: nothing spent)" if args.dry_run else ""))
                    if not args.dry_run:
                        print(experiments.resource_shock_report(args.name))
                return 0
            if not args.report:
                print("\n".join(experiments.queue(args.name, args.item, args.n, model=args.model or "auto",
                                                  dry_run=args.dry_run)))
                if not args.dry_run:
                    print("`shaft trial work` runs them now, or the scheduler does; then --report")
            if args.report:
                print(experiments.report(args.name, args.item))
        except (experiments.ExperimentError, trial.TrialError) as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "feedback":
        return cmd_feedback(args.name, args.days)
    if args.cmd == "inbox":
        return messages.clear_notes() if args.clear else messages.show_inbox()
    if args.cmd == "reply":
        return messages.reply(args.name, args.text, question=args.question)
    if args.cmd == "send":
        return messages.send(args.name, args.text)
    if args.cmd == "drift":
        return evolution.drift(args.name)
    if args.cmd == "distinct":
        return evolution.distinct(args.name, args.strict)
    if args.cmd == "breed":
        return evolution.breed(args.a, args.b, args.child)
    if args.cmd == "dataset":
        return finetune.cmd_dataset(args.name)
    if args.cmd == "finetune":
        if args.evaluate:
            return finetune.cmd_evaluate(args.name, args.evaluate)
        return finetune.cmd_finetune(args.name, args.iters, args.min_examples)
    if args.cmd == "mind":
        return cmd_mind(args)
    if args.cmd == "list":
        print("\n".join(instance_names()))
        return 0
    if args.cmd == "setup":
        return cmd_setup()
    if args.cmd == "install":
        from . import install
        return install.run(args.operator, args.email)
    if args.cmd == "deploy":
        from . import deploy
        if args.check == bool(args.tag) or (args.check and args.services_only) or (args.local and not args.check):
            parser.error("deploy takes TAG [--services-only], or --check [--local]")
        return deploy.check(args.local) if args.check else deploy.run(args.tag, args.services_only)
    if args.cmd == "update":
        return cmd_update(instance_names() if args.all else (args.names or [DEFAULT_INSTANCE]))
    if args.cmd == "new":
        return cmd_new(args.name, args.source, args.seed, args.json, args.paused_until_first_reply)
    if args.cmd == "first-reply":
        return cmd_first_reply(args.name, args.then, args.json)
    if args.cmd == "kill":
        from . import kill
        if not (INSTANCES / args.name / "shaft").is_dir():
            instance_root(args.name)   # says which instances there are
        return kill.kill(args.name, args.wait)
    if args.cmd == "delete":
        return cmd_delete(args.name, args.yes, not args.no_archive)
    if args.cmd in ("pause", "resume"):
        if args.name:
            instance_root(args.name)
        set_paused(args.name, args.cmd == "pause")
        log(f"{args.cmd} {args.name or 'all'} by operator")
        print(f"{args.cmd}d {args.name or 'all instances'}.")
        return 0
    if args.cmd == "trial":
        return cmd_trial(args)
    if args.cmd == "check":
        ok, output = gate.run_tests(instance_root(args.name), HOME)
        print(output)
        if getattr(output, "refused", False):
            from .runner import RunRefused, _refused
            return _refused(args.name, RunRefused(output.why))
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    if args.cmd == "gate":
        gate.stop_stages_on_signal()        # stopped with the scheduler or a ^C: its test run goes too
        if args.action == "budget":
            return gate.budget()            # not niced: it measures the time a gate takes
        if args.action == "regate":         # not niced: a gate's stages have their time limits
            from .runner import regate_all
            return regate_all()
        try:
            os.nice(10)                     # behind the instances' own work: nothing waits on it
        except OSError:
            pass
        return gate.warm()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
