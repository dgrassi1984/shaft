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
from datetime import date, datetime, timedelta

from . import autonomy, evolution, finetune, gate, messages, metrics, runner, version
from .core import (DEFAULT_INSTANCE, HOME, INSTANCES, NAME_RE, PROJECT, SUP_DIR, TEMPLATE, commit_state, git, git_wt,
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


def _env_values(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v
    return out


def _resolve_config_conflict(root) -> bool:
    """A conflict only in config.env: take the template's file, keep each value the instance changed.
    (wren's Haiku model conflicted with every template change near those lines.)"""
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
    the rollback of a failed merge must undo only the merge (wren and kant both had an uncommitted
    config.env while updates ran, 2026-10-13, and the old `reset --hard` would have discarded it).
    Untracked files are safe: the merge refuses to overwrite one and the rollback never touches one."""
    worst = 0
    for name in names:
        root = instance_root(name)
        try:
            with run_lock(name, wait=False, kind="update"):
                dirty = git_wt(root, "status", "--porcelain", "--untracked-files=no").stdout.rstrip()
                if dirty:
                    print(f"{name}: uncommitted changes in its worktree, not updated — commit or discard them "
                          f"first (git -C {root} diff):\n{dirty}")
                    worst = max(worst, 1)
                    continue
                before = git_wt(root, "rev-parse", "HEAD").stdout.strip()
                r = git_wt(root, "-c", "user.name=SHAFT supervisor", "-c", "user.email=supervisor@shaft.localhost",
                           "merge", "--no-edit", "-m", f"{name}: merge template (operator update)", "template",
                           check=False)
                if r.returncode != 0 and not _resolve_config_conflict(root):
                    git_wt(root, "merge", "--abort", check=False)
                    print(f"{name}: merge conflict with template, nothing changed:\n{(r.stdout + r.stderr)[-800:]}")
                    worst = 1
                    continue
                if r.returncode != 0:
                    git_wt(root, "-c", "user.name=SHAFT supervisor", "-c", "user.email=supervisor@shaft.localhost",
                           "commit", "-q", "--no-edit", "-m",
                           f"{name}: merge template (config.env merged key by key, its own settings kept)")
                if "Already up to date" in r.stdout:
                    print(f"{name}: already up to date")
                    continue
                ok, test_log = gate.run_tests(root, HOME)
                if not ok:
                    # --keep, not --hard: it undoes only the merge and refuses rather than discard a file
                    # that something changed while the gate ran.
                    undo = git_wt(root, "reset", "-q", "--keep", before, check=False)
                    if undo.returncode != 0:
                        print(f"{name}: merged template failed the gate and could not roll back — a merged file "
                              f"changed while the gate ran, so it is still at the merge "
                              f"({git_wt(root, 'rev-parse', '--short', 'HEAD').stdout.strip()}); undo it with "
                              f"`git -C {root} reset --keep {before[:12]}` once the change is saved.\n"
                              f"git said: {undo.stderr.strip()}\n{test_log[-800:]}")
                    else:
                        print(f"{name}: merged template failed the gate; rolled back:\n{test_log[-800:]}")
                    worst = 1
                    continue
                _note_update(name, root, before)
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
        rated = [v for v in map(metrics.cycle_value, js) if v is not None]
        last_self = git_wt(root, "log", "-1", "--format=%cr", "--", "self/self.md", check=False).stdout.strip()
        convs = runner.open_conversations(name)
        from .core import disabled_tools
        tools_off = disabled_tools(name)
        flags = [f for f, on in (("paused", paused(name)), ("busy", is_busy(name)),
                                 (f"{len(convs)} conversation(s) open", convs)) if on]
        print(f"\n■ {name}{' (' + ', '.join(flags) + ')' if flags else ''}  ·  branch instance/{name}")
        print(f"  journal     {len(entries)} entries · self-model last changed {last_self or 'never'}")
        print(f"  judged      {len(js)} cycles" + (f" · success {rate:.0%}" if rate is not None else "")
              + (f" · achieved difficulty {5 * sum(rated[-20:]) / len(rated[-20:]):.1f}/5 (last {len(rated[-20:])})"
                 if rated else ""))
        print(f"  LLM today   {calls} calls · {tokens} completion tokens · {errors} errors")
        print(f"  versions    {version.describe(name)}")
        if tools_off:
            print(f"  tools off   {', '.join(sorted(tools_off))} (shaft tools {name} on <tool>)")
        from .core import write_hosts
        if (hosts := write_hosts(name)):
            print(f"  may write   {', '.join(sorted(hosts))} (shaft hosts {name} deny <host>)")
        if entries:
            lines = entries[-1].splitlines()
            print("  latest:     " + lines[0].lstrip("# "))
            for line in lines[1:8]:
                print("    " + line[:160])
            if len(lines) > 8:
                print("    …")
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


def cmd_secret(args) -> int:
    """Give an instance a key without it passing through a conversation (and so through transcripts, journal
    write-ups and, for a cloud-brained instance, the provider's prompt)."""
    from .core import delete_secret, read_secrets, store_secret
    root = instance_root(args.name)
    if args.action == "list":
        store = read_secrets(root)
        for k, e in sorted(store.items()):
            print(f"{k:16} hosts {', '.join(e['hosts'])} · {e.get('header')} {e.get('scheme', '')} · {e.get('created', '?')[:16]}")
        if not store:
            print(f"{args.name} has no keys")
        return 0
    if not args.key:
        print("which key? e.g. shaft secret wren set github --hosts api.github.com", file=sys.stderr)
        return 2
    if args.action == "delete":
        found = delete_secret(root, args.key)
        print(f"deleted {args.name}'s key {args.key}" if found else f"{args.name} has no key {args.key}")
        return 0 if found else 1
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


def cmd_hosts(name: str, action: str | None, host: str | None) -> int:
    from .core import set_write_host, write_hosts
    instance_root(name)
    if action:
        if not host:
            print("which host? e.g. shaft hosts wren allow api.github.com", file=sys.stderr)
            return 2
        set_write_host(name, host, action == "allow")
        log(f"{name}: write access to {host} {'allowed' if action == 'allow' else 'removed'} by the operator")
    hosts = sorted(write_hosts(name))
    print(f"{name} may send POST/PUT/PATCH/DELETE to: {', '.join(hosts) if hosts else 'no host yet'} "
          f"(GET/HEAD: any public address)")
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


def cmd_battery(args) -> int:
    from . import battery
    names = args.names or instance_names()
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
    p.add_argument("action", choices=["on", "off", "status", "key", "serve"])
    p = sub.add_parser("model", help="SHAFT's own Bonsai 2 model server (launchd service)")
    p.add_argument("action", choices=["on", "off", "status"])
    p = sub.add_parser("embed", help="SHAFT's embedding service for memory search (launchd service)")
    p.add_argument("action", choices=["on", "off", "status"])
    p = sub.add_parser("autonomy", help="the autonomous schedule (SHAFT's scheduler service): on, off, status")
    p.add_argument("action", choices=["on", "off", "status", "serve"])
    p = sub.add_parser("top", help="live view of every instance, the services and the messages between instances; "
                                   "starts a cycle for an idle one (q quits)")
    p.add_argument("--once", action="store_true", help="print one snapshot as text and exit")
    p = sub.add_parser("status", help="how the instances are doing")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("log", help="recent journal entries")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p.add_argument("-n", type=int, default=3)
    p = sub.add_parser("stats", help="an instance's measured performance (judge + calibration)")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p = sub.add_parser("judge", help="judging: 'backfill' rates origin/difficulty/novelty of cycles judged before")
    p.add_argument("action", choices=["backfill"])
    p.add_argument("names", nargs="*")
    p = sub.add_parser("battery", help="the capability battery: run, report, recall (questions), backup")
    p.add_argument("action", choices=["run", "report", "recall", "backup"])
    p.add_argument("names", nargs="*")
    p.add_argument("--until", default="", help="run: stop at this time (HH:MM), like the night window")
    p.add_argument("--only", default="", help="run: only these categories (comma-separated)")
    p.add_argument("--no-control", action="store_true", help="run: skip the control")
    p.add_argument("--control-only", action="store_true",
                   help="run: only the control, on coding and research (checks the items discriminate)")
    p.add_argument("--limit", type=int, default=None, help="run: at most this many items")
    p.add_argument("--force", action="store_true", help="recall: write new questions (starts a new era)")
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
    p = sub.add_parser("tools", help="an instance's supervisor tools: list them, or switch one on/off")
    p.add_argument("name")
    p.add_argument("action", nargs="?", choices=["on", "off"])
    p.add_argument("tool", nargs="?")
    p = sub.add_parser("secret", help="an instance's own keys: give it one (value read from your terminal), list, delete")
    p.add_argument("name")
    p.add_argument("action", choices=["set", "list", "delete"])
    p.add_argument("key", nargs="?")
    p.add_argument("--hosts", default="", help="comma-separated hosts the key may be sent to")
    p.add_argument("--header", default="Authorization")
    p.add_argument("--scheme", default="Bearer", help="e.g. Bearer, token; '' for the raw value")
    p = sub.add_parser("hosts", help="hosts an instance may send POST/PUT/PATCH/DELETE to")
    p.add_argument("name")
    p.add_argument("action", nargs="?", choices=["allow", "deny"])
    p.add_argument("host", nargs="?")
    p = sub.add_parser("check", help="run the test gate against an instance's code")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    args = parser.parse_args(argv)

    if args.cmd == "talk":
        extra = (["--show-thinking"] if args.show_thinking else []) + (["--as", args.speaker] if args.speaker else [])
        if (mine := busy_with(args.name)):
            print(f"{args.name} is busy ({mine}); " + ("pausing it for your conversation…" if mine.startswith(
                ("live", "sleep", "reflect")) else "your conversation starts when it finishes…"), file=sys.stderr)
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
    if args.cmd == "api":
        from . import api
        return api.command(args.action)
    if args.cmd == "model":
        return autonomy.service("model", args.action)
    if args.cmd == "embed":
        return autonomy.service("embed", args.action)
    if args.cmd == "top":
        ui_python = PROJECT / ".venv-ui" / "bin" / "python"
        extra = ["--once"] if args.once else []
        if not ui_python.exists():
            print(f"shaft top needs {ui_python} (python3 -m venv .venv-ui && .venv-ui/bin/pip install "
                  f"prompt_toolkit rich)", file=sys.stderr)
            return 2
        os.execv(ui_python, [str(ui_python), "-E", "-s", "-m", "supervisor.top", *extra])
    if args.cmd == "tools":
        return cmd_tools(args.name, args.action, args.tool)
    if args.cmd == "secret":
        return cmd_secret(args)
    if args.cmd == "hosts":
        return cmd_hosts(args.name, args.action, args.host)
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
    if args.cmd == "judge":
        for name in args.names or instance_names():
            instance_root(name)
            n = metrics.backfill(name)
            metrics.write_stats(name)
            print(f"{name}: rated {n} earlier cycle(s)")
        return 0
    if args.cmd == "battery":
        return cmd_battery(args)
    if args.cmd == "inbox":
        return messages.clear_notes() if args.clear else messages.show_inbox()
    if args.cmd == "reply":
        return messages.reply(args.name, args.text)
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
