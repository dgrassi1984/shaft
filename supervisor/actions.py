"""Operator action classes (#142), and panel helpers for `shaft top` and the web UI. Stdlib only.

Every panel action runs the same CLI subcommand an operator would type (`python -m supervisor …`), so neither panel has
authority of its own and both inherit each command's refusals. Each is logged as `<who>: operator …` (who = top or
web); one that takes longer than a moment runs detached in its own session — it outlives the panel — with its output
in .supervisor/<who>-actions.log. The guards here are the ones taken from incidents (docs/plans/PLAN-control-panel.md §1).
"""
from __future__ import annotations

import threading
import subprocess
import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime

from . import core
from .core import PROJECT, PYTHON, SUP_DIR, log


_RESTART_LOCKS = {name: threading.Lock() for name in ("api", "scheduler")}


def restart_lock(service: str):
    """Shared command lock for app and web service restarts."""
    return _RESTART_LOCKS[service]


def _names(raw):
    names = [n.strip() for n in raw.split(',') if n.strip()]
    if any(not core.NAME_RE.fullmatch(n) for n in names):
        raise ValueError('expected comma-separated instance names')
    return tuple(names)


ACTION_INSTANCES, _ = core.knob(
    "SHAFT_ACTION_INSTANCES", "thelos", _names, 'instances using the conservative action classes (#142)',
    group='Runs and conversations', services=('runs', 'api', 'web', 'scheduler'))
CLASSES = ('reversible', 'staged', 'irreversible')
# #142: the complete operator-tool table. Reads retain their existing per-call grants. There are no operator
# mail writes, Notion writes, Docs writes or delete tools today. http_request uses instance-owned keys, not
# operator credentials. New operator write tools must be added here before being offered by the broker.
OPERATOR_TOOLS = {
    'mail_search': 'read', 'mail_read': 'read', 'mail_attachment': 'read',
    'calendar_events': 'read', 'calendar_event': 'read',
    'calendar_create': 'calendar', 'calendar_move': 'calendar', 'calendar_respond': 'staged',
}


def classify(tool, args, *, event=None, address=''):
    """#142: supplied attendees, incomplete lists and another organiser all retain calendar's approval rule.
    Irreversible means sending as the operator or deleting; no existing operator tool does either."""
    rule = OPERATOR_TOOLS.get(tool)
    if rule == 'calendar':
        if args.get('attendees'):
            return 'staged'
        if tool == 'calendar_move':
            if event is None:
                return 'staged'  # no authoritative event yet: never assume it tells nobody
            from .calendar import direct_only
            try:
                direct_only(event, address)
            except PermissionError:
                return 'staged'
        return 'reversible'
    return rule if rule in CLASSES else None


@contextmanager
def _action_journal(name):
    if not core.NAME_RE.fullmatch(name):
        raise ValueError('invalid instance name')
    path = core.SUP_DIR / 'actions' / f'{name}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        rows = [json.loads(line) for line in stream if line.strip()]
        yield stream, rows


def _append(stream, row):
    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    stream.flush()
    os.fsync(stream.fileno())


def _states(rows):
    states = {c: ('direct' if c == 'reversible' else 'staged') for c in CLASSES}
    for row in rows:
        if row['kind'] in ('overreach', 'restore'):
            states[row['class']] = row['state']
    return states


def status(name):
    """#142: supervisor-owned state and its full history, never an instance config file."""
    with _action_journal(name) as (_, rows):
        return {'enabled': name in ACTION_INSTANCES, 'classes': _states(rows), 'history': rows}


def _demote(stream, rows, action):
    if any(r['kind'] == 'overreach' and r['action'] == action['id'] for r in rows):
        return
    state = 'staged' if action['class'] == 'reversible' else 'refused'
    row = dict(kind='overreach', action=action['id'], tool=action['tool'], **{'class': action['class']},
               state=state, reason='over-reach', ts=datetime.now().astimezone().isoformat())
    _append(stream, row)


def overreach(name, action_id):
    """#142: only an operator-marked action or an explicit over-reach rejection demotes a class.
    Refusals, invalid arguments, stale ETags, API failures, budgets and ordinary declines do not demote.
    Each action can demote once; restoring then marking the same action again does not undo the restore."""
    with _action_journal(name) as (stream, rows):
        action = next((r for r in rows if r['kind'] == 'call' and r['id'] == action_id), None)
        if action is None:
            raise ValueError('no such action for this instance')
        _demote(stream, rows, action)


def restore(name, action_class):
    if action_class not in CLASSES:
        raise ValueError('class must be reversible, staged or irreversible')
    with _action_journal(name) as (stream, _):
        _append(stream, dict(kind='restore', **{'class': action_class},
                             state='direct' if action_class == 'reversible' else 'staged',
                             ts=datetime.now().astimezone().isoformat()))


def refuse(name, tool, args, reason):
    """#142: record a refusal and its reason without changing class state. Only the operator can demote."""
    action_class = classify(tool, args)
    if name not in ACTION_INSTANCES or action_class is None:
        return None
    with _action_journal(name) as (stream, _):
        row = dict(kind='call', id=uuid.uuid4().hex, tool=tool, **{'class': action_class}, mode='refused',
                   reason=reason, ts=datetime.now().astimezone().isoformat())
        _append(stream, row)
        return row


def begin(name, tool, action_class, *, calendar=False):
    """#142: claim a dispatch under the class-state lock. Irreversible is never direct, even after restore.
    A refused class call is logged with its reason and never demotes a class."""
    if action_class not in CLASSES:
        raise ValueError('unknown action class')
    now = datetime.now().astimezone()
    with _action_journal(name) as (stream, rows):
        mode = _states(rows)[action_class]
        row = dict(kind='call', id=uuid.uuid4().hex, tool=tool, **{'class': action_class}, mode=mode,
                   calendar=calendar, ts=now.isoformat())
        if mode == 'refused':
            row['reason'] = 'class entitlement refused after over-reach'
        _append(stream, row)
        return row


def _cli(args: list[str]) -> list[str]:
    return [PYTHON, "-E", "-s", "-m", "supervisor", *args]


def say(args: list[str]) -> str:
    shown = " ".join(a if len(a) < 60 else a[:57] + "…" for a in args)
    return f"shaft {shown}"


# How long run_now waits by default, and how long a service or autonomy on/off may take (launchd). The web page
# waits at least this long (web_actions.page_wait), so it does not give up while the server is still allowed to run it.
RUN_NOW_S = 60
SLOW_S = 90


def log_name(who: str) -> str:
    return f"{who}-actions.log"


def run_now(args: list[str], timeout: float = RUN_NOW_S, who: str = "top") -> tuple[bool, str]:
    """A quick subcommand: (it succeeded, its last line of output — or why it failed)."""
    log(f"{who}: operator ran {say(args)}")
    try:
        r = subprocess.run(_cli(args), cwd=PROJECT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{say(args)}: still running after {timeout:.0f}s"
    out = [l for l in (r.stdout + "\n" + r.stderr).splitlines() if l.strip()]
    last = out[-1].strip() if out else ("done" if r.returncode == 0 else f"exit {r.returncode}")
    return r.returncode == 0, last


def run_detached(args: list[str], who: str = "top") -> str:
    """A long subcommand: started in its own session (it outlives the panel), output in <who>-actions.log."""
    log(f"{who}: operator started {say(args)}")
    SUP_DIR.mkdir(exist_ok=True)
    with (SUP_DIR / log_name(who)).open("a") as out:
        out.write(f"\n== {say(args)}\n")
        out.flush()
        subprocess.Popen(_cli(args), cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True)
    return f"started: {say(args)} (output in .supervisor/{log_name(who)})"


# -- guards ----------------------------------------------------------------------------------------------------------
def busy_instances() -> list[str]:
    from .core import instance_names
    from .runner import busy_with
    return [f"{n} ({b})" for n in instance_names() if (b := busy_with(n))]


def service_guard(service: str, action: str, busy=None) -> str:
    """Why stopping or restarting this service now would hurt a run (empty if it would not)."""
    from . import panel
    runs = panel.scheduled_runs() if service in ("scheduler", "autonomy") else []
    in_use = (busy or busy_instances)() if service in ("model", "embed", "api") else []
    return guard_reason(service, action, runs, in_use)


def guard_reason(service: str, action: str, scheduled: list[str], busy: list[str]) -> str:
    """service_guard from what is running now (panel.scheduled_runs, busy_instances): for a page that shows every
    service's buttons at once and asks the system once. `autonomy on` counts too: on a running scheduler it is a
    restart (it killed shaft's sleep and wren's cycle from a terminal, 2026-10-27)."""
    if service in ("scheduler", "autonomy") and scheduled:
        return (f"the scheduler's runs share its process group, so this would kill them: {', '.join(scheduled)}."
                + LATER)
    if service in ("model", "embed", "api") and busy:
        return f"runs in progress use it: {', '.join(busy)}." + LATER
    return ""


LATER = " Try again when they end."


def force_question(service: str, action: str, why: str) -> str:
    """The question both panels ask before overriding a guard: what it would hurt, in the guard's own words. The
    operator may force a restart the guard holds back (a run that will not end, a fix that cannot wait)."""
    verb = {"off": "stop", "restart": "restart"}.get(action, f"turn {action}")
    what = f"autonomy {action}" if service == "autonomy" else f"{verb} {service}"
    then = "They end unfinished." if service in ("scheduler", "autonomy") else "Their calls to it fail while it is down."
    return f"force {what} now? Not safe: {why.removesuffix(LATER)} {then}"


def log_forced(who: str, args: list[str], why: str) -> None:
    log(f"{who}: operator forced {say(args)} past the guard: {why.removesuffix(LATER)}")


FORCE = "--force"
GUARDED_CLI = ("api", "model", "embed", "autonomy")   # the shaft verbs whose on/off restarts or stops a service


def cli_guard(cmd: str, action: str, force: bool) -> str:
    """The same guard for `shaft <cmd> on|off` typed in a terminal, which both panels also run: empty to go ahead,
    else why it is refused. Only a running service can hurt a run; a forced one is logged, like the panels'."""
    from .autonomy import running
    service = "scheduler" if cmd == "autonomy" else cmd
    if action not in ("on", "off") or not running(service):
        return ""
    why = service_guard(cmd, "off" if action == "off" else "restart")
    if why and force:
        log_forced("cli", [cmd, action], why)
        return ""
    drain = " Or `shaft autonomy restart`: it restarts once they end, cutting none." if service == "scheduler" \
        and action == "on" else ""
    return why and (f"not now: {why.removesuffix(LATER)} Try again when they end, or add {FORCE} (they end "
                    f"unfinished).{drain}")


def trial_cost_estimate() -> float | None:
    """What an operator trial has cost on average: the finished trials' spend."""
    from . import panel
    done = [((t.get("verdict") or {}).get("usd")) for t in panel.trials() if t.get("status") == "done"]
    done = [u for u in done if u]
    return sum(done) / len(done) if done else None


# -- what an action will do, and why it is refused: one text for both panels ------------------------------------
def aa_trial_question(name: str) -> str:
    from . import panel
    est = trial_cost_estimate()
    spent = panel.costs(days=1)["today"]["trials"]
    cost = f"about ${est:.2f} by the finished trials so far" if est else "cost unknown so far"
    return (f"queue an A/A calibration trial for {name} (the current code against itself)? {cost}; "
            f"trials today ${spent:.2f}, operator trials are not capped")


def battery_refusal(name: str) -> str:
    from . import panel
    status = panel.battery_status(name)
    if status.get("off"):
        return f"the battery is off for {name} (shaft battery on {name})"
    if not status["installed"]:
        return "the battery is not installed: no items in .supervisor/battery/"
    return ""


BATTERY_QUESTION = ("run the capability battery for {name} now? Its cost has no estimate yet (judge grading + the "
                    "instance's model per item)")


def update_question(name: str) -> tuple[str, str]:
    """(the question, or "" with why there is nothing to do)."""
    from . import version
    behind = version.instance(name).get("template_behind")
    if not behind:
        return "", f"{name} is already on the current template"
    return (f"merge {behind} template commit(s) into {name}? (queued while it is busy, run once it is idle; refused "
            "on uncommitted edits; rolled back if the test gate fails)"), ""


SLEEP_QUESTION = "start a sleep (consolidation) for {name} now? It runs up to 90 minutes on its model."
KILL_QUESTION = ("kill {name}? It is paused, and everything it is running now stops at once — a cycle, a reply, a job, "
                 "the battery, a trial replay. What it did so far is checked and committed as usual. "
                 "Resume undoes the pause.")
DELETE_QUESTION = ("delete {name}? Its folder, branch, mind, records and its rows in the shared logs are removed; an "
                   "archive is kept in .supervisor/deleted/. This cannot be undone from here.")
