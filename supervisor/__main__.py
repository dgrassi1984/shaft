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
from datetime import date, datetime, timedelta
from pathlib import Path

from . import actions, autonomy, evolution, finetune, gate, messages, metrics, mind, runner, surprise, version
from .core import (DEFAULT_INSTANCE, HOME, INSTANCES, MIND_PATHS, add_all_but_mind, NAME_RE, PROJECT, SUP_DIR, TEMPLATE, commit_state,
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


def cmd_new(name: str, source: str | None, seed: bool = False) -> int:
    """Create instance/<name> forked from `template` (or from another instance's branch).

    A fork inherits code, skills and plugins (its "genes"), never the parent's mind:
    self-model, personality, goals, journal, episodes and notes start fresh. The lineage
    stays in git history. With `seed`, it also starts knowing what the parent knows about the operator
    (its people file and facts, marked as the parent's), never what the parent is (docs/plans/PLAN-thelos.md).
    """
    if not NAME_RE.match(name) or name == "operator":
        print("instance names: lowercase letters, digits and '-', starting with a letter (not 'operator')",
              file=sys.stderr)
        return 2
    root = INSTANCES / name
    if root.exists() or git("rev-parse", "--verify", "-q", f"refs/heads/instance/{name}", check=False).stdout:
        print(f"instance {name!r} already exists", file=sys.stderr)
        return 2
    if mind.exists(name):
        print(f"a mind store for {name!r} already exists at {mind.store(name)} (an earlier instance of that name); "
              "move it away first", file=sys.stderr)
        return 2
    if seed and not source:
        print("--seed copies from a parent instance: name it with --from", file=sys.stderr)
        return 2
    if source:
        instance_root(source)
    if seed:
        try:
            operator, seeds = _seed_files(source)
        except ValueError as e:
            print(f"cannot seed from {source}: {e}", file=sys.stderr)
            return 2
    base = f"instance/{source}" if source else "template"
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
    print(f"created {name} (branch instance/{name}, worktree {root.relative_to(PROJECT)})")
    if seed:
        missing = [p for p in _seed_paths(operator) if p not in seeds]
        print(f"seeded from {source}: {', '.join(seeds) or 'nothing'}"
              + (f" ({source} has no {', '.join(missing)})" if missing else ""))
    return 0


def cmd_delete(name: str, yes: bool, keep_archive: bool) -> int:
    """`shaft delete`: the confirmation, then delete.delete (docs/plans/PLAN-kill-delete.md)."""
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
                ok, test_log = gate.run_tests(root, HOME)
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
    """Recreate missing worktrees from branches — e.g. after a fresh clone — and install the push guard."""
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
    # instance branches, whose history holds the minds of the past (PLAN-mind-out-of-git.md D4).
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
    today = date.today().isoformat()
    for name in names:
        root = INSTANCES / name
        calls = errors = tokens = 0
        for line in safe_read(root, "logs/llm_usage.jsonl", "", errors="replace").splitlines():   # never via a link
            rec = json.loads(line)
            if rec["ts"].startswith(today):
                calls, errors = calls + 1, errors + bool(rec.get("error"))
                tokens += rec.get("completion_tokens") or 0
        entries = journal_entries(root)
        js = metrics.judgments(name)
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
        from .core import calendar_grants, mail_grants
        if (grants := mail_grants(name)):
            print(f"  mail        {', '.join(g['account'] + '/' + g['label'] for g in grants)} (shaft mail {name} off)")
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
        if value == "off":
            new = None
        else:
            try:
                new = int(value) if limit == "wake-floor" else round(float(value), 2)
            except ValueError:
                new = 0
            if new <= 0:
                print(f"{limit} must be a positive {'whole number of minutes' if limit == 'wake-floor' else 'amount'}"
                      f" (or off), not {value!r}", file=sys.stderr)
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
    """Scheduled jobs (docs/plans/PLAN-jobs.md): an instance's own plugins, run on the timer their manifest declares."""
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
    """Where this instance's messages to you alert you (docs/plans/PLAN-operator-channel.md): auto (where you last wrote
    to it), web or telegram. An open conversation's channel comes first either way."""
    from . import channel
    instance_root(name)
    if value:
        channel.set_preference(name, value)
        log(f"{name}: operator channel set to {value} by the operator")
    print(channel.describe(name))
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
        print("which key? e.g. shaft secret wren set github --hosts api.github.com", file=sys.stderr)
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
    Google account (docs/plans/PLAN-own-mail.md, docs/plans/PLAN-own-drive.md), signed in by the operator in the browser; the broker
    refreshes it per run."""
    from . import google
    from .core import secrets_env, store_google_key
    env = secrets_env()
    clients = sorted({k[len("GMAIL_"):-len("_CLIENT_ID")].lower().replace("_", "-") for k in env
                      if k.startswith("GMAIL_") and k.endswith("_CLIENT_ID")})
    if not (args.address and args.client):
        print(f"usage: shaft secret {args.name} google <key> <address> --client <name> [--scope "
              f"{'|'.join(google.KEY_SCOPES)}] (e.g. shaft secret thelos google drive assistant@example.com --client "
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


def cmd_google(action: str | None, alias: str | None, address: str | None, client: str = "") -> int:
    """The operator's Google accounts, shared by mail and calendar (docs/plans/PLAN-calendar.md): log one in (both scopes, one
    consent), list them with their clients and granted scopes, or move the account files of docs/plans/PLAN-mail's folder."""
    from . import google
    from .core import secrets_env
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
        print(f"logged in: Google account {alias} is {saved['address']} (read-only, client {saved['client']}). "
              f"Granted: {got}.")
        missing = [google.SCOPE_NAMES[s] for s in google.SCOPES if s not in google.scopes(saved)]
        if missing:
            print(f"NOT granted: {', '.join(missing)} (unticked on Google's consent page, or not on the app's Data "
                  f"Access list): the tools that need it refuse, naming this login. To add it, run this login again "
                  "and tick it.", file=sys.stderr)
        print(f"Grant with shaft mail <instance> grant {alias} <label> · shaft calendar <instance> grant {alias} "
              "<calendar>")
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
        print(f"unknown google action {action!r}: login or migrate (alone: list the accounts)", file=sys.stderr)
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


def cmd_mail(target: str | None, action: str | None, alias: str | None, label: str | None) -> int:
    """The operator's Gmail an instance may read, one label at a time (docs/plans/PLAN-mail.md): grant, revoke, show. The
    accounts are `shaft google`'s. The grant is read on every mail call, so a revoke takes effect on the instance's
    next call."""
    from . import google, mail
    from .core import mail_grants, secrets_env, set_mail_grants

    def show(name: str) -> None:
        grants = mail_grants(name)
        print(f"{name} may read: " + (", ".join(f"{g['account']}/{g['label']}" for g in grants) if grants else "no mail"))

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
    elif action == "grant":
        if not google.account(alias):
            known = ", ".join(google.accounts()) or "none"
            print(f"no Google account {alias!r} (logged in: {known}); first: shaft google login {alias} <address>",
                  file=sys.stderr)
            return 2
        try:   # the label must exist in that account now
            mail.label_id(google.Account(alias, secrets_env()).require(google.GMAIL), label)
        except (google.AccountError, google.ApiError) as e:
            print(f"not granted: {e}", file=sys.stderr)
            return 2
        set_mail_grants(target, grants + [{"account": alias, "label": label}])
        log(f"{target}: mail grant {alias}/{label} added by the operator")
    else:   # revoke
        rest = [g for g in grants if g != {"account": alias, "label": label}]
        if rest == grants:
            print(f"{target} has no grant {alias}/{label}", file=sys.stderr)
            show(target)
            return 1
        set_mail_grants(target, rest)
        log(f"{target}: mail grant {alias}/{label} revoked by the operator")
    show(target)
    print(f"(from {target}'s next mail call; the prompt line changes from its next run)")
    return 0


def cmd_calendar(target: str | None, action: str | None, alias: str | None, which: str | None) -> int:
    """The operator's calendars an instance may read, one calendar per grant (docs/plans/PLAN-calendar.md): grant (by
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
        # PLAN-calendar-write M0: write implies read; upgrading replaces the same record, never a parallel grant.
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
    """The mind stores (supervisor/mind.py, docs/plans/PLAN-mind-out-of-git.md). backfill and check only read the project
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
    if args.action == "replay":
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
    p.add_argument("action", choices=["on", "off", "status", "key", "inbox-key", "serve"])
    p.add_argument("--force", action="store_true", help="on/off even if it would kill or cut off runs in progress")
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
    p.add_argument("action", nargs="?", choices=["set", "remove"])
    p.add_argument("--file")
    p.add_argument("--expect-sha")
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
    p = sub.add_parser("web", help="the web UI for managing the instances, on your tailnet (docs/plans/PLAN-webui.md): on, off, "
                                   "status, pair [--name NAME] (a one-time link for a browser), unpair DEVICE|--all")
    p.add_argument("action", choices=["on", "off", "status", "pair", "unpair", "serve"])
    p.add_argument("device", nargs="?", help="unpair: the browser's id or name (shaft web status lists them)")
    p.add_argument("--name", help="pair: what to call this browser (e.g. laptop, phone)")
    p.add_argument("--all", action="store_true", help="unpair: every browser")
    p = sub.add_parser("telegram", help="talk with each instance from Telegram, one bot per instance, one paired account")
    p.add_argument("action", choices=["on", "off", "status", "token", "pair", "unpair", "serve"])
    p.add_argument("name", nargs="?", help="token: the instance the bot is for")
    p = sub.add_parser("model", help="SHAFT's own Bonsai 2 model server (launchd service)")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--force", action="store_true", help="on/off even if it would kill or cut off runs in progress")
    p = sub.add_parser("embed", help="SHAFT's embedding service for memory search (launchd service)")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--force", action="store_true", help="on/off even if it would kill or cut off runs in progress")
    p = sub.add_parser("autonomy", help="the autonomous schedule (SHAFT's scheduler service): on, off, status")
    p.add_argument("action", choices=["on", "off", "status", "serve"])
    p.add_argument("--force", action="store_true", help="on/off even if it would kill or cut off runs in progress")
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
                                     "'second' runs the second judge; 'value-regrade' re-asks only value, dry run only (paid)")
    p.add_argument("action", choices=["backfill", "second", "value-regrade"])
    p.add_argument("names", nargs="*")
    p.add_argument("--min-value", type=int, default=0, help="value-regrade: minimum stored value")
    p.add_argument("--value-for", default="own_capability,own_record", help="value-regrade: comma-separated beneficiaries")
    p.add_argument("--since", default="", help="value-regrade: cycles since DATE (ISO format)")
    p.add_argument("--limit", type=int, default=None, help="value-regrade: newest N matching cycles")
    p = sub.add_parser("battery", help="the capability battery: run, report, recall (questions), backup, off|on per instance")
    p.add_argument("action", choices=["run", "report", "recall", "backup", "off", "on"])
    p.add_argument("names", nargs="*")
    p.add_argument("--until", default="", help="run: stop at this time (HH:MM), like the night window")
    p.add_argument("--only", default="", help="run: only these categories (comma-separated)")
    p.add_argument("--no-control", action="store_true", help="run: skip the control")
    p.add_argument("--control-only", action="store_true",
                   help="run: only the control, on coding and research (checks the items discriminate)")
    p.add_argument("--limit", type=int, default=None, help="run: at most this many items")
    p.add_argument("--force", action="store_true", help="recall: write new questions (starts a new era)")
    p = sub.add_parser("surprise", help="unexpected tool results, their recurrence, live cost (docs/plans/PLAN-surprise baseline)")
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
                       "baseline, and how each sleep handled the surprises (docs/plans/PLAN-surprise.md §4)")
    p.add_argument("names", nargs="*")
    p.add_argument("--since", required=True, help="when the window began (M3 live): YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    p.add_argument("--until", default="", help="when it ends (default: now)")
    p.add_argument("--min-cycles", type=int, default=30, help="judged cycles the window needs (§4.1: shaft 30)")
    # docs/plans/PLAN-consciousness.md: measurements and kill tests (read the record; only anchoring and tom --score /
    # --baselines pay a judge, only experiment replays cycles)
    p = sub.add_parser("consults", help="what each instance spent consulting other models, per model and run, and how "
                       "its consulted cycles fared (docs/plans/PLAN-model-choice.md)")
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
    p = sub.add_parser("tom", help="what instances expected others to answer, and what they did (C4)")
    p.add_argument("names", nargs="*")
    p.add_argument("--score", action="store_true", help="score the expectations now decidable (a judge per reply)")
    p.add_argument("--baselines", action="store_true", help="also the two information-matched baselines (paid)")
    p = sub.add_parser("prefs", help="an instance's pauses, declines and stated preferences (E1); 'lift' ends its pause")
    p.add_argument("name")
    p.add_argument("action", nargs="?", choices=["lift"])
    p = sub.add_parser("experiment", help="kill tests of PLAN-consciousness: cards, attention, commitments, forecast "
                       "(knob trials) and workspace (planted probe); --report shows the result")
    p.add_argument("item", choices=["cards", "attention", "commitments", "forecast", "workspace"])
    p.add_argument("name")
    p.add_argument("--n", type=int, default=6, help="past cycles (knob trials: per trial; workspace: states, default 6)")
    p.add_argument("--model", default="", help="replays' model (default: the trial default; workspace: the trial "
                   "fallback for an instance on the local model)")
    p.add_argument("--dry-run", action="store_true", help="say what would run; replay nothing")
    p.add_argument("--report", action="store_true", help="only print the result so far")
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
    p = sub.add_parser("update", help="merge the template into instances (only when idle; gate-tested)")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("new", help="create an instance")
    p.add_argument("name")
    p.add_argument("--from", dest="source", help="fork code, skills and plugins (not memory) from this instance")
    p.add_argument("--seed", action="store_true",
                   help="with --from: also start knowing what the parent knows about you (its people file and "
                        "facts, marked as its own), never what it is")
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
                        "Slides: docs/plans/PLAN-own-drive.md)")
    p.add_argument("--header", default="Authorization")
    p.add_argument("--scheme", default="Bearer", help="e.g. Bearer, token; '' for the raw value")
    p = sub.add_parser("share", help="a folder of yours an instance may read (read-only, seen as shared/); 'off' removes it")
    p.add_argument("name")
    p.add_argument("folder", nargs="?", help="a directory, or 'off'")
    p = sub.add_parser("browser", help="the Chromium every instance browses with (docs/plans/PLAN-browser.md): 'install "
                                       "[--playwright X.Y.Z]' (re)installs it; alone shows it and each profile")
    p.add_argument("action", nargs="?", choices=["install"])
    p.add_argument("--playwright", default=None, help="install: the Playwright version (default: the supervisor's pin)")
    p = sub.add_parser("google", help="your Google accounts, for mail and calendar: 'login <alias> <address> "
                                      "[--client <name>]' (both scopes, read-only), 'migrate' (from .supervisor/mail/); "
                                      "alone lists them with their clients and granted scopes")
    p.add_argument("action", nargs="?", choices=["login", "migrate"])
    p.add_argument("alias", nargs="?", help="with login: the account's alias (e.g. work)")
    p.add_argument("address", nargs="?", help="with login: its address")
    p.add_argument("--client", default="", help="with login: the OAuth client (GMAIL_<CLIENT>_CLIENT_ID/_SECRET in .env); "
                                                "default: the alias. One per Google organisation: personal Google needs its own")
    p = sub.add_parser("mail", help="your Gmail an instance may read, one label at a time: "
                                    "'<name> grant|revoke <alias> <label>', '<name> off', '<name>' shows; alone lists all "
                                    "(accounts: shaft google)")
    p.add_argument("target", nargs="?", help="an instance")
    p.add_argument("action", nargs="?", help="grant, revoke, off")
    p.add_argument("alias", nargs="?", help="the account alias")
    p.add_argument("label", nargs="?", help="the Gmail label")
    p = sub.add_parser("calendar", help="your calendars an instance may read, one per grant: "
                                        "'<name> grant|revoke|grant-write|revoke-write <alias> primary|<title>|<id>', '<name> off', '<name>' "
                                        "shows; 'calendars <alias>' lists an account's; alone lists all grants")
    p.add_argument("target", nargs="?", help="an instance, or 'calendars'")
    p.add_argument("action", nargs="?", help="grant, revoke, grant-write, revoke-write, off — or, after calendars, the account alias")
    p.add_argument("alias", nargs="?", help="the account alias")
    p.add_argument("which", nargs="?", help="primary, a calendar's title, or its ID")
    p = sub.add_parser("trial", help="trials of proposed changes: list, show, run (operator), replay, work")
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
    q.add_argument("--model", choices=["auto", "cloud", "local"], default="auto")
    q = tsub.add_parser("replay", help="replay one past cycle once with the current code (debug); keeps a copy")
    q.add_argument("name")
    q.add_argument("ts", help="the episode's ts")
    q.add_argument("--model", default="local", help="'local' (its own configuration) or a cloud model id")
    q.add_argument("--keep", help="copy the replayed folder here")
    q.add_argument("--cycles", type=int, default=1, help="consecutive live cycles from one past state")
    q.add_argument("--sleep", action="store_true", help="sleep between each pair of replayed live cycles")
    tsub.add_parser("work", help="run the queued trials' tests and replays that may run now (the scheduler does this)")
    p = sub.add_parser("check", help="run the test gate against an instance's code")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p = sub.add_parser("gate", help="the test gate's upkeep: warm runs the supervisor's shared tests on the current "
                                    "commit unless their pass is recorded (the scheduler does it after each merge)")
    p.add_argument("action", choices=["warm"])
    args = parser.parse_args(argv)

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
        print(refused, file=sys.stderr)
        return 1
    if args.cmd == "api":
        from . import api
        return api.command(args.action)
    if args.cmd == "config":
        from . import opconfig
        return opconfig.config_command(args.name, args.action, args.items, args.expect, args.expect_unset)
    if args.cmd == "settings":
        from . import settings
        return settings.command(args.action, args.items, args.expect, args.expect_unset, args.json, args.from_web)
    if args.cmd == "drive":
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
        return web.command(args.action, args.name if args.action == "pair" else args.device, args.all)
    if args.cmd == "telegram":
        from . import telegram
        return telegram.command(args.action, args.name)
    if args.cmd == "model":
        return autonomy.service("model", args.action)
    if args.cmd == "embed":
        return autonomy.service("embed", args.action)
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
        return cmd_google(args.action, args.alias, args.address, args.client)
    if args.cmd == "mail":
        if args.target == "login":   # docs/plans/PLAN-calendar M1: one login for every Google service
            print("shaft mail login is gone: shaft google login <alias> <address> [--client <name>]", file=sys.stderr)
            return 2
        if args.target and args.action not in (None, "grant", "revoke", "off"):
            print(f"unknown mail action {args.action!r}: grant, revoke or off", file=sys.stderr)
            return 2
        return cmd_mail(args.target, args.action, args.alias, args.label)
    if args.cmd == "calendar":
        if args.target not in (None, "calendars") and args.action not in (None, "grant", "revoke", "grant-write", "revoke-write", "off"):
            print(f"unknown calendar action {args.action!r}: grant, revoke, grant-write, revoke-write or off", file=sys.stderr)
            return 2
        return cmd_calendar(args.target, args.action, args.alias, args.which)
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
    if args.cmd == "review":                 # read-only: judges nothing, so it costs nothing and waits for no run
        for name in args.names:
            instance_root(name)
        print(metrics.review_text(args.names or instance_names(), args.hours), end="")
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
    if args.cmd == "tom":
        from . import tom
        names = args.names or instance_names()
        for name in names:
            instance_root(name)
            if args.score or args.baselines:
                print(f"{name}: {tom.score(name)} expectation(s) scored")
            if args.baselines:
                print(f"{name}: {tom.baselines(name)} baseline prediction(s) made")
            if name in tom.stopped:
                print(f"{name}: stopped — {tom.stopped.pop(name)}", file=sys.stderr)
        print(tom.report(names))
        return 0
    if args.cmd == "prefs":
        from . import preferences
        instance_root(args.name)
        print(preferences.lift(args.name) if args.action == "lift" else preferences.report(args.name))
        return 0
    if args.cmd == "experiment":
        from . import experiments, trial
        instance_root(args.name)
        try:
            if args.item == "workspace":
                if not args.report:
                    r = experiments.run_workspace(args.name, args.n, model=args.model, dry_run=args.dry_run)
                    print(f"{args.name}: {r['states']} state(s), {r['replays']} replay(s) on {r['model']}"
                          + (f", {r['done']} done" if not args.dry_run else " (dry run: nothing replayed)"))
                if args.report or not args.dry_run:
                    print(experiments.workspace_report(args.name))
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
    if args.cmd == "mind":
        return cmd_mind(args)
    if args.cmd == "list":
        print("\n".join(instance_names()))
        return 0
    if args.cmd == "setup":
        return cmd_setup()
    if args.cmd == "update":
        return cmd_update(instance_names() if args.all else (args.names or [DEFAULT_INSTANCE]))
    if args.cmd == "new":
        return cmd_new(args.name, args.source, args.seed)
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
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    if args.cmd == "gate":
        try:
            os.nice(10)                     # behind the instances' own work: nothing waits on it
        except OSError:
            pass
        gate.stop_stages_on_signal()        # stopped with the scheduler: its test run goes too
        return gate.warm()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
