"""SHAFT supervisor — the part of the system the agents cannot edit.

It runs any number of SHAFT instances, each inside its own macOS sandbox, lends each
run a broker (web, messages) that holds the secrets the instances never see, enforces
budgets and invariants, gates changes an instance makes to its own code (tests pass or
the change is reverted; kept changes go on probation), judges autonomous cycles,
commits each instance's state to its own branch, and owns the schedule and the pause
switch. It never imports agent code.

Git layout: `main` holds the supervisor; the `template` branch holds the pristine agent
(worktree: template/); each instance lives on its own branch `instance/<name>`, forked
from `template` (worktree: instances/<name>/).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime

from . import autonomy, evolution, finetune, gate, messages, metrics
from .core import (DEFAULT_INSTANCE, HOME, INSTANCES, NAME_RE, PROJECT, SUP_DIR, TEMPLATE, git, git_wt,
                   instance_names, instance_root, journal_entries, journal_note, log, paused,
                   safe_mkdir, safe_write)
from .runner import Busy, busy_with, is_busy, request_yield, run_agent, run_lock, spawn_recover


# -- instances -------------------------------------------------------------------
def cmd_new(name: str, source: str | None) -> int:
    """Create instance/<name> forked from `template` (or from another instance's branch).

    A fork inherits code, skills and plugins (its "genes"), never the parent's mind:
    self-model, personality, goals, journal, episodes and notes start fresh. The lineage
    stays in git history.
    """
    if not NAME_RE.match(name) or name == "operator":
        print("instance names: lowercase letters, digits and '-', starting with a letter (not 'operator')",
              file=sys.stderr)
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
    for d in ("self", "memory", "notes", "inbox"):
        shutil.rmtree(root / d, ignore_errors=True)
    for d in ("self", "memory", "notes", "skills"):
        safe_mkdir(root, d)
    safe_write(root, "self/self.md", 
        f"# Self-Model\n\nI am {name}. I have just been created and have no experiences yet; "
        "this self-model is for me to write.\n")
    safe_write(root, "self/goals.md", "# Goals & Open Questions\n\n(none yet — mine to set)\n")
    origin = f"forked from {source} (code, skills and plugins only, not its memories)" if source \
        else "from the SHAFT template"
    safe_write(root, "memory/journal.md", f"# {name} — journal\n")
    journal_note(root, "Created", f"I was created by the supervisor {origin}. My memory starts here.")
    git_wt(root, "add", "-A", ".")
    git_wt(root, "commit", "-q", "-m", f"{name}: created {origin}")
    log(f"created instance {name} on instance/{name} {origin}")
    print(f"created {name} (branch instance/{name}, worktree {root.relative_to(PROJECT)})")
    return 0


def cmd_update(names: list[str]) -> int:
    """Merge `template` into instances — only while holding each instance's run lock, so an
    operator update is never mistaken for a change the instance made to itself mid-run."""
    worst = 0
    for name in names:
        root = instance_root(name)
        try:
            with run_lock(name, wait=False, kind="update"):
                r = git_wt(root, "-c", "user.name=SHAFT supervisor", "-c", "user.email=supervisor@shaft.localhost",
                           "merge", "--no-edit", "-m", f"{name}: merge template (operator update)", "template",
                           check=False)
                if r.returncode != 0:
                    git_wt(root, "merge", "--abort", check=False)
                    print(f"{name}: merge conflict with template, nothing changed:\n{(r.stdout + r.stderr)[-800:]}")
                    worst = 1
                    continue
                if "Already up to date" in r.stdout:
                    print(f"{name}: already up to date")
                    continue
                ok, test_log = gate.run_tests(root, HOME)
                if not ok:
                    git_wt(root, "reset", "-q", "--hard", "ORIG_HEAD")
                    print(f"{name}: merged template failed the gate; rolled back:\n{test_log[-800:]}")
                    worst = 1
                    continue
                log(f"{name}: updated from template")
                print(f"{name}: updated from template ({git_wt(root, 'rev-parse', '--short', 'HEAD').stdout.strip()})")
        except Busy:
            print(f"{name} is busy ({busy_with(name)}); update it when it is idle.")
            worst = max(worst, 3)
    return worst


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
    pending = messages.pending()
    questions = [m for m in pending if m["kind"] == "question"]
    print(f"SHAFT supervisor · {len(names)} instance(s) · "
          f"{'ALL PAUSED' if (SUP_DIR / 'paused').exists() else 'running'} · "
          f"operator inbox: {len(questions)} question(s), {len(pending) - len(questions)} note(s)")
    today = date.today().isoformat()
    for name in names:
        root = INSTANCES / name
        calls = errors = tokens = 0
        usage = root / "logs" / "llm_usage.jsonl"
        for line in usage.read_text().splitlines() if usage.exists() else []:
            rec = json.loads(line)
            if rec["ts"].startswith(today):
                calls, errors = calls + 1, errors + bool(rec.get("error"))
                tokens += rec.get("completion_tokens") or 0
        entries = journal_entries(root)
        js = metrics.judgments(name)
        rate = (sum(metrics.SCORE[j["criterion_met"]] for j in js) / len(js)) if js else None
        last_self = git_wt(root, "log", "-1", "--format=%cr", "--", "self/self.md", check=False).stdout.strip()
        flags = [f for f, on in (("paused", paused(name)), ("busy", is_busy(name))) if on]
        print(f"\n■ {name}{' (' + ', '.join(flags) + ')' if flags else ''}  ·  branch instance/{name}")
        print(f"  journal     {len(entries)} entries · self-model last changed {last_self or 'never'}")
        print(f"  judged      {len(js)} cycles" + (f" · success {rate:.0%}" if rate is not None else ""))
        print(f"  LLM today   {calls} calls · {tokens} completion tokens · {errors} errors")
        if entries:
            lines = entries[-1].splitlines()
            print("  latest:     " + lines[0].lstrip("# "))
            for line in lines[1:8]:
                print("    " + line[:160])
            if len(lines) > 8:
                print("    …")
    return 0


def run_many(kind: str, names: list[str], every: bool) -> int:
    names = instance_names() if every else (names or [DEFAULT_INSTANCE])
    worst = 0
    for name in names:
        if kind not in ("talk", "recover") and paused(name):   # writing journal entries is housekeeping
            print(f"{name} is paused (shaft resume {name}).", file=sys.stderr)
            worst = max(worst, 3)
            continue
        worst = max(worst, run_agent(name, kind, [kind], interactive=False))
    return worst


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="shaft", description="SHAFT supervisor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("talk", help="chat with an instance in the terminal")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p.add_argument("--show-thinking", action="store_true")
    p.add_argument("--plain", action="store_true", help="plain line-by-line mode instead of the terminal client")
    for verb, text in (("live", "one autonomous cycle with tools"), ("reflect", "one reflection (no tools)"),
                       ("sleep", "consolidate memory, personality, goals"),
                       ("recover", "write journal entries for conversations that have none yet")):
        p = sub.add_parser(verb, help=text)
        p.add_argument("names", nargs="*")
        p.add_argument("--all", action="store_true")
    sub.add_parser("tick", help="one autonomous round over all awake, idle instances (run by launchd)")
    p = sub.add_parser("api", help="OpenAI-compatible API for the instances, on your tailnet")
    p.add_argument("action", choices=["on", "off", "status", "key", "serve"])
    p = sub.add_parser("model", help="SHAFT's own Bonsai 2 model server (launchd service)")
    p.add_argument("action", choices=["on", "off", "status"])
    p = sub.add_parser("autonomy", help="install/remove the launchd schedule")
    p.add_argument("action", choices=["on", "off", "status"])
    p = sub.add_parser("status", help="how the instances are doing")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("log", help="recent journal entries")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p.add_argument("-n", type=int, default=3)
    p = sub.add_parser("stats", help="an instance's measured performance (judge + calibration)")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p = sub.add_parser("inbox", help="messages instances sent you")
    p.add_argument("--clear", action="store_true", help="archive notes (questions stay until answered)")
    p = sub.add_parser("reply", help="answer an instance's open question (or message it)")
    p.add_argument("name")
    p.add_argument("text")
    p = sub.add_parser("send", help="message an instance")
    p.add_argument("name")
    p.add_argument("text")
    for verb, text in (("drift", "how an instance's personality changed, and distance to others"),
                       ("distinct", "blind test: can the judge tell the instance from the bare base model?"),
                       ("dataset", "export an instance's experiences for fine-tuning"),
                       ("finetune", "train + judge a LoRA adapter on an instance's experiences (MLX)")):
        p = sub.add_parser(verb, help=text)
        p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
        if verb == "finetune":
            p.add_argument("--iters", type=int)
            p.add_argument("--evaluate", metavar="ADAPTER", help="only (re-)judge an existing adapter")
            p.add_argument("--min-examples", type=int, default=finetune.MIN_EXAMPLES,
                           help="lower only for a pipeline check; the result is labelled as such")
    p = sub.add_parser("breed", help="a child from two instances' recombined genes, with a fresh mind")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("child")
    sub.add_parser("list", help="list instances")
    sub.add_parser("setup", help="create missing worktrees for template and instance branches")
    p = sub.add_parser("update", help="merge the template into instances (only when idle; gate-tested)")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("new", help="create an instance")
    p.add_argument("name")
    p.add_argument("--from", dest="source", help="fork code, skills and plugins (not memory) from this instance")
    for verb in ("pause", "resume"):
        p = sub.add_parser(verb, help=f"{verb} autonomous runs (all, or one instance)")
        p.add_argument("name", nargs="?")
    p = sub.add_parser("check", help="run the test gate against an instance's code")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    args = parser.parse_args(argv)

    if args.cmd == "talk":
        extra = ["--show-thinking"] if args.show_thinking else []
        if (mine := busy_with(args.name)):
            print(f"{args.name} is busy ({mine}); your conversation starts when it finishes…", file=sys.stderr)
        others = {n: b for n in instance_names() if n != args.name and (b := busy_with(n))}
        background = [n for n, b in others.items() if not b.startswith("talk")]
        if background:
            request_yield(background)
            print(f"note: {', '.join(f'{n} ({others[n]})' for n in background)} is using the model; asked to "
                  f"wrap up at its next turn so you get it — the first reply may still wait for that turn.",
                  file=sys.stderr)
        ui_python = PROJECT / ".venv-ui" / "bin" / "python"
        if not args.plain and sys.stdin.isatty() and sys.stdout.isatty() and ui_python.exists():
            os.execv(ui_python, [str(ui_python), "-E", "-s", "-m", "supervisor.talk_ui", args.name, *extra])
        if not args.plain and sys.stdin.isatty() and not ui_python.exists():
            print(f"(plain mode: the terminal client needs {ui_python} — python3 -m venv .venv-ui && "
                  f".venv-ui/bin/pip install prompt_toolkit rich)", file=sys.stderr)
        code = run_agent(args.name, "talk", ["talk", *extra], interactive=True)
        spawn_recover(args.name)   # the journal entry is written in the background — leaving is instant
        return code
    if args.cmd in ("live", "reflect", "sleep", "recover"):
        return run_many(args.cmd, args.names, args.all)
    if args.cmd == "tick":
        return autonomy.tick()
    if args.cmd == "api":
        from . import api
        return api.command(args.action)
    if args.cmd == "model":
        return autonomy.service("model", args.action)
    if args.cmd == "autonomy":
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
    if args.cmd == "inbox":
        return messages.clear_notes() if args.clear else messages.show_inbox()
    if args.cmd == "reply":
        return messages.reply(args.name, args.text)
    if args.cmd == "send":
        return messages.send(args.name, args.text)
    if args.cmd == "drift":
        return evolution.drift(args.name)
    if args.cmd == "distinct":
        return evolution.distinct(args.name)
    if args.cmd == "breed":
        return evolution.breed(args.a, args.b, args.child)
    if args.cmd == "dataset":
        return finetune.cmd_dataset(args.name)
    if args.cmd == "finetune":
        if args.evaluate:
            return finetune.cmd_evaluate(args.name, args.evaluate)
        return finetune.cmd_finetune(args.name, args.iters, args.min_examples)
    if args.cmd == "list":
        print("\n".join(instance_names()))
        return 0
    if args.cmd == "setup":
        return cmd_setup()
    if args.cmd == "update":
        return cmd_update(instance_names() if args.all else (args.names or [DEFAULT_INSTANCE]))
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
