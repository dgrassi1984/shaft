"""SHAFT supervisor — the part of the system the agents cannot edit.

It runs any number of SHAFT instances, each inside its own macOS sandbox;
enforces the time budget; gates changes an instance makes to its own code
(tests pass or the change is reverted); commits each instance's state to its
own branch; and owns the pause switch. It never imports agent code.

Git layout: `main` holds the supervisor; the `template` branch holds the
pristine agent (worktree: template/); each instance lives on its own branch
`instance/<name>`, forked from `template` (worktree: instances/<name>/), so
instances evolve independently and can later be compared and recombined.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from . import gate, sandbox

PROJECT = Path(__file__).resolve().parent.parent
INSTANCES = PROJECT / "instances"
TEMPLATE = PROJECT / "template"
SUP_DIR = PROJECT / ".supervisor"
HOME = Path.home()
STATE_PATHS = ("self", "memory", "notes", "skills")
DEFAULT_INSTANCE = "shaft"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def _knob(name: str, default: str, parse) -> tuple[object, str]:
    raw = os.environ.get(name)
    if raw is None:
        return parse(default), "default"
    try:
        return parse(raw), "env"
    except ValueError:
        print(f"warning: {name}={raw!r} is invalid; using {default} [default]", file=sys.stderr)
        return parse(default), "default"


CYCLE_TIMEOUT_S, _cycle_src = _knob("SHAFT_CYCLE_TIMEOUT_S", "1800", int)
LLM_PORTS, _ports_src = _knob("SHAFT_LLM_PORTS", "1337", lambda s: [int(p) for p in s.split(",")])


# -- helpers ------------------------------------------------------------------
def log(msg: str) -> None:
    SUP_DIR.mkdir(exist_ok=True)
    with (SUP_DIR / "supervisor.log").open("a") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT), *args], capture_output=True, text=True, check=check)


def git_wt(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in an instance worktree without trusting anything inside it.

    The worktree's `.git` file is writable by the instance, which could point it at
    a fake repository whose config runs commands (hooks, fsmonitor). So the git dir
    is taken from the main repository's own worktree registry, and hooks are off.
    """
    admin = _worktree_admin_dir(root)
    return subprocess.run(
        ["git", f"--git-dir={admin}", f"--work-tree={root}", "-c", "core.hooksPath=/dev/null",
         "-c", "core.fsmonitor=false", *args],
        cwd=root, capture_output=True, text=True, check=check)


def _worktree_admin_dir(root: Path) -> Path:
    for admin in (PROJECT / ".git" / "worktrees").glob("*"):
        gitdir = admin / "gitdir"
        if gitdir.exists() and Path(gitdir.read_text().strip()).resolve() == (root / ".git").resolve():
            return admin
    raise SystemExit(f"{root} is not a registered worktree of {PROJECT} (try: shaft setup)")


def instance_root(name: str) -> Path:
    root = INSTANCES / name
    if not (root / "shaft").is_dir() or not (root / ".git").exists():
        known = ", ".join(instance_names()) or "none"
        raise SystemExit(f"no instance named {name!r} (instances: {known})")
    return root


def instance_names() -> list[str]:
    return sorted(p.name for p in INSTANCES.iterdir() if (p / "shaft").is_dir()) if INSTANCES.exists() else []


def paused(name: str) -> bool:
    return (SUP_DIR / "paused").exists() or (SUP_DIR / f"paused-{name}").exists()


def journal_entries(root: Path) -> list[str]:
    journal = root / "memory" / "journal.md"
    if not journal.exists():
        return []
    return [b.strip() for b in journal.read_text().split("\n---\n") if b.strip().startswith("## ")]


def journal_note(root: Path, title: str, body: str) -> None:
    """Supervisor's own minimal journal append (deliberately not the agent's code)."""
    with (root / "memory" / "journal.md").open("a") as f:
        f.write(f"\n---\n## {datetime.now():%Y-%m-%d %H:%M} — {title}\n\n{body.strip()}\n")


def commit_state(name: str, kind: str, kept_code: list[str]) -> None:
    """Commit the instance's mind (always) and its code changes that passed the gate."""
    root = INSTANCES / name
    paths = [p for p in STATE_PATHS if (root / p).exists()] + kept_code
    if not paths:
        return
    git_wt(root, "add", "-A", "--", *paths)
    if git_wt(root, "diff", "--cached", "--quiet", "--", *paths, check=False).returncode == 0:
        return
    msg = f"{name}: {kind} {datetime.now():%Y-%m-%d %H:%M}"
    if kept_code:
        msg += "\n\nCode changes made by the instance (tests passed):\n" + "\n".join(f"- {p}" for p in kept_code)
    git_wt(root, "-c", f"user.name=SHAFT/{name}", "-c", f"user.email={name}@shaft.localhost",
           "commit", "-q", "-m", msg, "--", *paths)
    log(f"{name}: committed {kind} on instance/{name}")


# -- running an instance -------------------------------------------------------
def run_agent(name: str, kind: str, agent_args: list[str], interactive: bool) -> int:
    if not (PROJECT / ".git").exists():
        print(f"{PROJECT} is not a git repository; the supervisor needs one to record changes.", file=sys.stderr)
        return 2
    root = instance_root(name)
    log(f"{name}: start {kind} cycle_timeout_s={CYCLE_TIMEOUT_S} [{_cycle_src}] "
        f"llm_ports={LLM_PORTS} [{_ports_src}] python={sys.executable}")
    before = gate.snapshot(root)
    SUP_DIR.mkdir(exist_ok=True)
    prof = SUP_DIR / f"{name}.sb"
    prof.write_text(sandbox.profile(root, HOME, LLM_PORTS))
    (root / ".tmp").mkdir(exist_ok=True)
    cmd = sandbox.wrap(prof, [sys.executable, "-E", "-s", "-m", "shaft", *agent_args])
    env = sandbox.agent_env(os.environ, SHAFT_ROOT=str(root), TMPDIR=str(root / ".tmp"))
    try:
        code = subprocess.run(cmd, cwd=root, env=env,
                              timeout=None if interactive else CYCLE_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        log(f"{name}: {kind} killed after {CYCLE_TIMEOUT_S}s budget")
        print(f"{name}: {kind} exceeded its {CYCLE_TIMEOUT_S}s budget and was stopped.", file=sys.stderr)
        code = 124
    except KeyboardInterrupt:
        code = 130
    log(f"{name}: end {kind} exit={code}")

    touched = gate.changed(before, gate.snapshot(root))
    kept: list[str] = []
    if touched:
        ok, output = gate.run_tests(root, HOME)
        if ok:
            kept = touched
            log(f"{name}: code change passed tests: {touched}")
        else:
            gate.restore(root, before, touched)
            log(f"{name}: code change FAILED tests, reverted: {touched}")
            journal_note(root, "Supervisor: code change reverted",
                         "My changes to " + ", ".join(touched) + " were reverted because the tests "
                         "failed. Last lines of the test output:\n\n```\n" + output[-1500:] + "\n```")
    commit_state(name, kind, kept)
    return code


# -- commands -------------------------------------------------------------------
def cmd_new(name: str, source: str | None) -> int:
    """Create instance/<name> forked from `template` (or from another instance's branch).

    A fork inherits code and skills (its "genes"), never the parent's mind: self-model,
    goals, journal, episodes and notes start fresh. The lineage stays in git history.
    """
    if not NAME_RE.match(name):
        print("instance names: lowercase letters, digits and '-', starting with a letter", file=sys.stderr)
        return 2
    root = INSTANCES / name
    if root.exists() or git("rev-parse", "--verify", "-q", f"refs/heads/instance/{name}", check=False).stdout:
        print(f"instance {name!r} already exists", file=sys.stderr)
        return 2
    if source:
        instance_root(source)
    base = f"instance/{source}" if source else "template"
    INSTANCES.mkdir(exist_ok=True)
    git("worktree", "add", "-q", "-b", f"instance/{name}", str(root), base)
    for d in ("self", "memory", "notes"):
        shutil.rmtree(root / d, ignore_errors=True)
        (root / d).mkdir()
    (root / "skills").mkdir(exist_ok=True)
    (root / "self" / "self.md").write_text(
        f"# Self-Model\n\nI am {name}. I have just been created and have no experiences yet; "
        "this self-model is for me to write.\n")
    (root / "self" / "goals.md").write_text("# Goals & Open Questions\n\n(none yet — mine to set)\n")
    origin = f"forked from {source} (code and skills only, not its memories)" if source \
        else "from the SHAFT template"
    (root / "memory" / "journal.md").write_text(f"# {name} — journal\n")
    journal_note(root, "Created", f"I was created by the supervisor {origin}. My memory starts here.")
    git_wt(root, "add", "-A", ".")
    git_wt(root, "commit", "-q", "-m", f"{name}: created {origin}")
    log(f"created instance {name} on instance/{name} {origin}")
    print(f"created {name} (branch instance/{name}, worktree {root.relative_to(PROJECT)})")
    return 0


def cmd_setup() -> int:
    """Recreate missing worktrees from branches — e.g. after a fresh clone."""
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
    return 0


def cmd_status(names: list[str]) -> int:
    usage_today = {}
    for name in names:
        root = INSTANCES / name
        calls = errors = tokens = 0
        usage = root / "logs" / "llm_usage.jsonl"
        if usage.exists():
            today = date.today().isoformat()
            for line in usage.read_text().splitlines():
                rec = json.loads(line)
                if rec["ts"].startswith(today):
                    calls, errors = calls + 1, errors + bool(rec.get("error"))
                    tokens += rec.get("completion_tokens") or 0
        usage_today[name] = (calls, tokens, errors)

    print(f"SHAFT supervisor · {len(names)} instance(s) · "
          f"{'ALL PAUSED' if (SUP_DIR / 'paused').exists() else 'running'} · autonomous schedule: not yet (M1)")
    for name in names:
        root = INSTANCES / name
        entries = journal_entries(root)
        last_self = git_wt(root, "log", "-1", "--format=%cr", "--", "self/self.md", check=False).stdout.strip()
        calls, tokens, errors = usage_today[name]
        print(f"\n■ {name}{' (paused)' if paused(name) else ''}  ·  branch instance/{name}")
        print(f"  journal     {len(entries)} entries")
        print(f"  self-model  last changed {last_self or 'never'}")
        print(f"  LLM today   {calls} calls · {tokens} completion tokens · {errors} errors")
        if entries:
            lines = entries[-1].splitlines()
            print("  latest:     " + lines[0].lstrip("# "))
            for line in lines[1:9]:
                print("    " + line)
            if len(lines) > 9:
                print("    …")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="shaft", description="SHAFT supervisor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("talk", help="chat with an instance in the terminal")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p.add_argument("--show-thinking", action="store_true")
    p = sub.add_parser("reflect", help="run one reflection cycle")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("status", help="how the instances are doing")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("log", help="recent journal entries")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p.add_argument("-n", type=int, default=3)
    sub.add_parser("list", help="list instances")
    sub.add_parser("setup", help="create missing worktrees for template and instance branches")
    p = sub.add_parser("new", help="create an instance")
    p.add_argument("name")
    p.add_argument("--from", dest="source", help="fork code and skills (not memory) from this instance")
    for verb in ("pause", "resume"):
        p = sub.add_parser(verb, help=f"{verb} autonomous runs (all, or one instance)")
        p.add_argument("name", nargs="?")
    p = sub.add_parser("check", help="run the test gate against an instance's code")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    args = parser.parse_args(argv)

    if args.cmd == "talk":
        extra = ["--show-thinking"] if args.show_thinking else []
        return run_agent(args.name, "talk", ["talk", *extra], interactive=True)
    if args.cmd == "reflect":
        names = instance_names() if args.all else (args.names or [DEFAULT_INSTANCE])
        worst = 0
        for name in names:
            if paused(name):
                print(f"{name} is paused (shaft resume).", file=sys.stderr)
                worst = max(worst, 3)
                continue
            worst = max(worst, run_agent(name, "reflect", ["reflect"], interactive=False))
        return worst
    if args.cmd == "status":
        return cmd_status(args.names or instance_names())
    if args.cmd == "log":
        print("\n\n---\n\n".join(journal_entries(instance_root(args.name))[-args.n:]))
        return 0
    if args.cmd == "list":
        print("\n".join(instance_names()))
        return 0
    if args.cmd == "setup":
        return cmd_setup()
    if args.cmd == "new":
        return cmd_new(args.name, args.source)
    if args.cmd in ("pause", "resume"):
        if args.name:
            instance_root(args.name)
        flag = SUP_DIR / (f"paused-{args.name}" if args.name else "paused")
        SUP_DIR.mkdir(exist_ok=True)
        if args.cmd == "pause":
            flag.write_text(datetime.now().isoformat())
        else:
            flag.unlink(missing_ok=True)
        log(f"{args.cmd} {args.name or 'all'} by operator")
        print(f"{args.cmd}d {args.name or 'all instances'}.")
        return 0
    if args.cmd == "check":
        ok, output = gate.run_tests(instance_root(args.name), HOME)
        print(output)
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
