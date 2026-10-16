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

from . import autonomy, evolution, finetune, gate, messages, metrics, mind, runner, surprise, version
from .core import (DEFAULT_INSTANCE, HOME, INSTANCES, MIND_PATHS, add_all_but_mind, NAME_RE, PROJECT, SUP_DIR, TEMPLATE, commit_state,
                   git, git_wt, instance_names, instance_root, journal_entries, journal_note, log, paused,
                   safe_mkdir, safe_write)
from .runner import (Busy, busy_with, is_busy, model_server, request_yield, run_agent, run_lock, sharing_model,
                     spawn_recover)


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
    if mind.exists(name):
        print(f"a mind store for {name!r} already exists at {mind.store(name)} (an earlier instance of that name); "
              "move it away first", file=sys.stderr)
        return 2
    if source:
        instance_root(source)
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
    safe_write(root, "memory/journal.md", f"# {name} — journal\n")
    journal_note(root, "Created", f"I was created by the supervisor {origin}. My memory starts here.")
    message = f"{name}: created {origin}"
    add_all_but_mind(root)
    git_wt(root, "commit", "-q", "--allow-empty", "-m", message)   # from the template: no code change, the lineage
    mind.init(name)
    mind.commit(name, message, root)
    log(f"created instance {name} on instance/{name} {origin}, its mind in {mind.store(name)}")
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
    An operator's config.env edit is first committed on its own, as a run would (between runs every
    config.env change is the operator's); anything else uncommitted stops the update for the operator.
    Untracked files are safe: the merge refuses to overwrite one and the rollback never touches one."""
    worst = 0
    for name in names:
        root = instance_root(name)
        try:
            with run_lock(name, wait=False, kind="update"):
                runner._commit_outside_config(name, root, [])
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
        worth = metrics.mean_value(metrics.valued(js)[-20:])
        try:
            last_self = mind.log(name, "-1", "--format=%cr", "--", "self/self.md").strip() or "never"
        except mind.MindError:
            last_self = f"unknown: no mind store (shaft mind cutover {name})"
        convs = runner.open_conversations(name)
        from .core import disabled_tools
        tools_off = disabled_tools(name)
        flags = [f for f, on in (("paused", paused(name)), ("busy", is_busy(name)),
                                 (f"{len(convs)} conversation(s) open", convs)) if on]
        print(f"\n■ {name}{' (' + ', '.join(flags) + ')' if flags else ''}  ·  branch instance/{name}")
        print(f"  journal     {len(entries)} entries · self-model last changed {last_self}")
        print(f"  judged      {len(js)} cycles" + (f" · success {rate:.0%}" if rate is not None else "")
              + (f" · achieved difficulty {5 * sum(rated[-20:]) / len(rated[-20:]):.1f}/5 (last {len(rated[-20:])})"
                 if rated else "")
              + (f" · value {worth:.1f}/5 (last {len(metrics.valued(js)[-20:])})" if worth is not None else ""))
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


def cmd_jobs(name: str | None, action: str | None) -> int:
    """Scheduled jobs (PLAN-jobs.md): an instance's own plugins, run on the timer their manifest declares."""
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
    """The operator's Google accounts, shared by mail and calendar (PLAN-calendar.md): log one in (both scopes, one
    consent), list them with their clients and granted scopes, or move the account files of PLAN-mail's folder."""
    from . import google
    from .core import secrets_env
    if action == "login":
        if not (alias and address):
            print("usage: shaft google login <alias> <address> [--client <name>]  (e.g. shaft google login example-co "
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
    """The operator's Gmail an instance may read, one label at a time (PLAN-mail.md): grant, revoke, show. The
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
    """The operator's calendars an instance may read, one calendar per grant (PLAN-calendar.md): grant (by
    `primary`, title or ID, stored as the ID), revoke, off, show; `calendars <alias>` lists an account's calendars.
    The grant is read on every calendar call, so a revoke takes effect on the instance's next call."""
    from . import calendar as gcal, google
    from .core import calendar_grants, secrets_env, set_calendar_grants

    def show(name: str) -> None:
        grants = calendar_grants(name)
        print(f"{name} may read: " + ("; ".join(f"{g['account']}/{g['name']} ({g['calendar']})" for g in grants)
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
    elif action == "grant":
        try:
            cal = gcal.resolve(session(alias), which)
        except (google.AccountError, google.ApiError) as e:
            print(f"not granted: {e}", file=sys.stderr)
            return 2
        set_calendar_grants(target, grants + [{"account": alias, "calendar": cal["id"], "name": cal["title"]}])
        log(f"{target}: calendar grant {alias}/{cal['id']} ({cal['title']}) added by the operator")
    else:   # revoke: by `primary` (its ID is the account's address), the grant's ID or its title, without asking Google
        ids = {(google.account(alias) or {}).get("address", "").casefold()} if which == "primary" else set()
        rest = [g for g in grants if not (g["account"] == alias and (which in (g["calendar"], g["name"])
                                                                     or g["calendar"].casefold() in ids))]
        if rest == grants:
            print(f"{target} has no calendar grant {alias}/{which}", file=sys.stderr)
            show(target)
            return 1
        set_calendar_grants(target, rest)
        log(f"{target}: calendar grant {alias}/{which} revoked by the operator")
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
    """The mind stores (supervisor/mind.py, PLAN-mind-out-of-git.md). backfill and check only read the project
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
                               keep=Path(args.keep).resolve() if args.keep else None)
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
                         base=_pairs(args.base) or None, allow_empty=args.aa, model=args.model)
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
    p = sub.add_parser("telegram", help="talk with each instance from Telegram, one bot per instance, one paired account")
    p.add_argument("action", choices=["on", "off", "status", "token", "pair", "unpair", "serve"])
    p.add_argument("name", nargs="?", help="token: the instance the bot is for")
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
    p = sub.add_parser("review", help="what the instances did in the last hours and what of it was worth anything "
                                      "(judged value, success, repeated goals, the cycles worth reading)")
    p.add_argument("names", nargs="*")
    p.add_argument("--hours", type=float, default=24)
    p = sub.add_parser("stats", help="an instance's measured performance (judge + calibration)")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
    p = sub.add_parser("judge", help="judging: 'backfill' asks cycles judged before for the ratings and value they lack; "
                                     "'second' runs the second judge on the cycles its sample picked")
    p.add_argument("action", choices=["backfill", "second"])
    p.add_argument("names", nargs="*")
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
    p = sub.add_parser("surprise", help="unexpected tool results, their recurrence, live cost (PLAN-surprise baseline)")
    p.add_argument("names", nargs="*")
    p.add_argument("--freeze", action="store_true", help="also write the baseline the surprise loop is judged against")
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
    for verb in ("pause", "resume"):
        p = sub.add_parser(verb, help=f"{verb} autonomous runs (all, or one instance)")
        p.add_argument("name", nargs="?")
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
    p = sub.add_parser("secret", help="an instance's own keys: give it one (value read from your terminal), list, delete")
    p.add_argument("name")
    p.add_argument("action", choices=["set", "list", "delete"])
    p.add_argument("key", nargs="?")
    p.add_argument("--hosts", default="", help="comma-separated hosts the key may be sent to")
    p.add_argument("--header", default="Authorization")
    p.add_argument("--scheme", default="Bearer", help="e.g. Bearer, token; '' for the raw value")
    p = sub.add_parser("share", help="a folder of yours an instance may read (read-only, seen as shared/); 'off' removes it")
    p.add_argument("name")
    p.add_argument("folder", nargs="?", help="a directory, or 'off'")
    p = sub.add_parser("google", help="your Google accounts, for mail and calendar: 'login <alias> <address> "
                                      "[--client <name>]' (both scopes, read-only), 'migrate' (from .supervisor/mail/); "
                                      "alone lists them with their clients and granted scopes")
    p.add_argument("action", nargs="?", choices=["login", "migrate"])
    p.add_argument("alias", nargs="?", help="with login: the account's alias (e.g. example-co)")
    p.add_argument("address", nargs="?", help="with login: its address")
    p.add_argument("--client", default="", help="with login: the OAuth client (GMAIL_<CLIENT>_CLIENT_ID/_SECRET in .env); "
                                                "default: the alias. One per Google organisation: gmail.com needs its own")
    p = sub.add_parser("mail", help="your Gmail an instance may read, one label at a time: "
                                    "'<name> grant|revoke <alias> <label>', '<name> off', '<name>' shows; alone lists all "
                                    "(accounts: shaft google)")
    p.add_argument("target", nargs="?", help="an instance")
    p.add_argument("action", nargs="?", help="grant, revoke, off")
    p.add_argument("alias", nargs="?", help="the account alias")
    p.add_argument("label", nargs="?", help="the Gmail label")
    p = sub.add_parser("calendar", help="your calendars an instance may read, one per grant: "
                                        "'<name> grant|revoke <alias> primary|<title>|<id>', '<name> off', '<name>' "
                                        "shows; 'calendars <alias>' lists an account's; alone lists all grants")
    p.add_argument("target", nargs="?", help="an instance, or 'calendars'")
    p.add_argument("action", nargs="?", help="grant, revoke, off — or, after calendars, the account alias")
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
    q.add_argument("-n", type=int, default=0)
    q.add_argument("--episodes", nargs="*", default=[])
    q.add_argument("--model", choices=["auto", "cloud", "local"], default="auto")
    q = tsub.add_parser("replay", help="replay one past cycle once with the current code (debug); keeps a copy")
    q.add_argument("name")
    q.add_argument("ts", help="the episode's ts")
    q.add_argument("--model", default="local", help="'local' (its own configuration) or a cloud model id")
    q.add_argument("--keep", help="copy the replayed folder here")
    tsub.add_parser("work", help="run the queued trials' tests and replays that may run now (the scheduler does this)")
    p = sub.add_parser("check", help="run the test gate against an instance's code")
    p.add_argument("name", nargs="?", default=DEFAULT_INSTANCE)
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
                  f"prompt_toolkit rich)", file=sys.stderr)
            return 2
        os.execv(ui_python, [str(ui_python), "-E", "-s", "-m", "supervisor.top", *extra])
    if args.cmd == "jobs":
        return cmd_jobs(args.name, args.action)
    if args.cmd == "job":
        from . import jobs
        instance_root(args.name)
        return jobs.run(args.name, args.plugin)
    if args.cmd == "tools":
        return cmd_tools(args.name, args.action, args.tool)
    if args.cmd == "secret":
        return cmd_secret(args)
    if args.cmd == "share":
        return cmd_share(args.name, args.folder)
    if args.cmd == "google":
        return cmd_google(args.action, args.alias, args.address, args.client)
    if args.cmd == "mail":
        if args.target == "login":   # PLAN-calendar M1: one login for every Google service
            print("shaft mail login is gone: shaft google login <alias> <address> [--client <name>]", file=sys.stderr)
            return 2
        if args.target and args.action not in (None, "grant", "revoke", "off"):
            print(f"unknown mail action {args.action!r}: grant, revoke or off", file=sys.stderr)
            return 2
        return cmd_mail(args.target, args.action, args.alias, args.label)
    if args.cmd == "calendar":
        if args.target not in (None, "calendars") and args.action not in (None, "grant", "revoke", "off"):
            print(f"unknown calendar action {args.action!r}: grant, revoke or off", file=sys.stderr)
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
    if args.cmd == "trial":
        return cmd_trial(args)
    if args.cmd == "check":
        ok, output = gate.run_tests(instance_root(args.name), HOME)
        print(output)
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
