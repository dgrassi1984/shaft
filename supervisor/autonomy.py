"""Autonomous schedule: each instance wakes for a cycle every WAKE_INTERVAL_MINUTES (its own config.env,
default 120), and all of them sleep daily from SLEEP_AT — kept by SHAFT's own scheduler service
(com.shaft.scheduler, always running). A wake that comes due while the instance is already awake (a cycle,
a conversation, its sleep) is skipped, not queued; the next one is a full interval later.

A timed wake also needs something to do (wake_gate, review C15/G1/G4): an unread message, a commitment coming due
before the next check, a job result since its last cycle, or no cycle for SHAFT_IDLE_WAKE_MINUTES; with none it is
skipped, the reason kept in schedule.json and shown. What used to wait for that interval wakes the instance at once
instead (event wakes, through the jobs' wake path): a message from the operator or another instance, and a commitment
falling due or reaching a daily overdue reminder.

It used to be two timed launchd jobs. On 2026-10-13 macOS put the login session's launchd domain in
"on-demand-only mode" while the Mac stayed awake: timed jobs stopped starting ("pending spawn"), and the
03:25 cycle and the 04:00 sleep silently never happened. A running service starts its own children, so
the clock no longer depends on launchd timers; a missed sleep is caught up later the same day.
"""
from __future__ import annotations

import codecs
import contextlib
import fcntl
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import gate, localmodel
from .core import (CYCLE_TIMEOUT_S, INSTANCES, PROJECT, PYTHON, SLEEP_TIMEOUT_S, SUP_DIR, instance_config,
                   instance_names, knob, list_beneath, log, paused, positive_int, set_update_queued, update_queued)
from .runner import Busy, is_busy, run_agent

AGENTS = Path.home() / "Library" / "LaunchAgents"
WAKE_DEFAULT_MIN = 120                  # minutes between wakes when config.env does not say
WAKE_FLOOR_MIN = 1                      # no default floor: the instance's own interval, unless the operator set one
SLEEP_AT = (4, 0)                       # hour, minute — daily; caught up later that day if missed
STATE = SUP_DIR / "schedule.json"       # last wake per instance / last nightly sleep, written by the runs
LEGACY_JOBS = ("com.shaft.tick", "com.shaft.sleep")
BOOTSTRAP_TRIES = 20                    # x 0.5 s: launchd lets go of a booted-out job a moment after bootout returns
IDLE_WAKE_MIN, _ = knob(
    "SHAFT_IDLE_WAKE_MINUTES", "30", positive_int, "minutes an instance may go without a cycle before a timed wake "
    "starts one with nothing new for it (no unread message, commitment coming due or job result)",
    group="Runs and conversations", services=("runs", "web", "scheduler"))
IDLE_BACKOFF = 2                        # how much longer the idle wake waits after a "nothing worth a cycle" exit
NOTHING_TO_DO = "nothing to do"         # how a timed wake the gate skipped is said (log, schedule.json, status)


# Long-running services (KeepAlive): SHAFT's own model server, the OpenAI-compatible API, the web UI, …. The model
# server runs only while used (localmodel.py): not at login, restarted after a crash but not after its own idle exit
# (code 0).
SERVICES = {
    "model": {"label": localmodel.LABEL,
              "args": [str(PROJECT / ".venv-bonsai" / "bin" / "python"), "-E", "-s", "-m", "supervisor.bonsai_server"],
              "plist": {"RunAtLoad": False, "KeepAlive": {"SuccessfulExit": False}}},
    "api": {"label": "com.shaft.api", "args": [PYTHON, "-E", "-s", "-m", "supervisor", "api", "serve"],
            "plist": {"ProcessType": "Interactive"}},
    "embed": {"label": "com.shaft.embed",
              "args": [str(PROJECT / ".venv-bonsai" / "bin" / "python"), "-E", "-s", "-m", "supervisor.embed_server"]},
    "listen": {"label": "com.shaft.listen",
               "args": [str(PROJECT / ".venv-listen" / "bin" / "python"), "-E", "-s", "-m", "supervisor.listen_worker"],
               "plist": {"ProcessType": "Background"}},
    "scheduler": {"label": "com.shaft.scheduler",
                  "args": [PYTHON, "-E", "-s", "-m", "supervisor", "autonomy", "serve"]},
    "telegram": {"label": "com.shaft.telegram", "args": [PYTHON, "-E", "-s", "-m", "supervisor", "telegram", "serve"]},
    "web": {"label": "com.shaft.web", "args": [PYTHON, "-E", "-s", "-m", "supervisor", "web", "serve"],
            "plist": {"ProcessType": "Interactive"}},
}


def running(name: str) -> bool:
    """Whether launchd has the service's process up now (what `service(name, "status")` prints as its state)."""
    r = _launchctl("print", f"gui/{os.getuid()}/{SERVICES[name]['label']}")
    return any(l.strip() == "state = running" for l in r.stdout.splitlines())


def launchd_pid(name: str) -> int | None:
    """The PID launchd runs the service as (None if it runs none)."""
    r = _launchctl("print", f"gui/{os.getuid()}/{SERVICES[name]['label']}")
    pid = next((l.strip().removeprefix("pid = ") for l in r.stdout.splitlines() if l.strip().startswith("pid = ")), "")
    return int(pid) if pid.isdigit() else None


def installed(name: str) -> bool:
    """Whether the service's launchd plist is installed (`service(name, "on")` wrote it and `off` has not removed it)."""
    return (AGENTS / f"{SERVICES[name]['label']}.plist").exists()


def service_problem(name: str) -> str:
    """Why a service is not up, in launchd's words: not installed, not loaded, or its state and last exit code."""
    label = SERVICES[name]["label"]
    if not installed(name):
        return f"the {name} service is not installed"
    r = _launchctl("print", f"gui/{os.getuid()}/{label}")
    if r.returncode != 0:
        return f"the {name} service is installed but launchd has not loaded it"
    fields: dict[str, str] = {}
    for line in r.stdout.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep:
            fields.setdefault(key, value)
    code = fields.get("last exit code")
    return (f"the {name} service is {fields.get('state', 'in an unknown state')} in launchd"
            + (f" (last exit code {code})" if code else "") + ", and it has not recorded that it runs")


def _plist(name: str) -> dict:
    """What `service(name, "on")` writes: one builder for it and for service_current."""
    svc = SERVICES[name]
    label = svc["label"]
    # The Mac app's install keeps its own tools (uv) in core.RUNTIME/bin: the services and the runs they start find them.
    from . import core
    try:
        runtime_bin = core.runtime_dir("bin")
    except core.RuntimeLayoutError as e:     # a link where the app's folder belongs: not put on any PATH
        log(f"service {name}: the runtime's bin/ is left off PATH: {e}")
        runtime_bin = None
    service_path = ("/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" if runtime_bin is None
                    else f"{runtime_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    return {
        "Label": label, "ProgramArguments": svc["args"], "WorkingDirectory": str(PROJECT),
        "StandardOutPath": str(SUP_DIR / f"{label}.log"), "StandardErrorPath": str(SUP_DIR / f"{label}.log"),
        "EnvironmentVariables": {"PATH": service_path},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30, **svc.get("plist", {})}


def service_current(name: str) -> bool:
    """Whether `service(name, "on")` would change nothing: the plist on disk is exactly what it would write and launchd
    has the job loaded. A rerun of an install uses it to leave a right service (and the runs it holds) alone."""
    path = AGENTS / f"{SERVICES[name]['label']}.plist"
    try:
        if plistlib.loads(path.read_bytes()) != _plist(name):
            return False
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    return _launchctl("print", f"gui/{os.getuid()}/{SERVICES[name]['label']}").returncode == 0


def service(name: str, action: str) -> int:
    svc = SERVICES[name]
    label, domain = svc["label"], f"gui/{os.getuid()}"
    path = AGENTS / f"{label}.plist"
    if action == "status":
        r = _launchctl("print", f"{domain}/{label}")
        state = next((l.strip() for l in r.stdout.splitlines() if l.strip().startswith("state =")), "not loaded")
        pid = next((l.strip() for l in r.stdout.splitlines() if l.strip().startswith("pid =")), "")
        print(f"{label:16} {state} {pid} · {'installed' if path.exists() else 'not installed'}"
              + (" · starts on demand, exits when idle (localmodel.py)" if name == "model" else ""))
        return 0
    _launchctl("bootout", f"{domain}/{label}")
    if action == "off":
        path.unlink(missing_ok=True)
        log(f"service {name} off")
        print(f"{label} stopped and removed")
        return 0
    SUP_DIR.mkdir(exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(_plist(name)))
    # bootout returns before launchd has let go of the old job: a bootstrap right after it failed with
    # "5: Input/output error" and left the service unloaded (`shaft model on`, 2026-10-19; it worked a moment later).
    for _ in range(BOOTSTRAP_TRIES):
        r = _launchctl("bootstrap", domain, str(path))
        if r.returncode == 0:
            break
        time.sleep(0.5)
    if r.returncode != 0:
        print(f"{label}: launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    _launchctl("kickstart", f"{domain}/{label}")   # an explicit start works even in on-demand-only mode
    log(f"service {name} on")
    print(f"{label} installed: " + ("started now; exits when idle, started again on demand (localmodel.py)"
                                    if name == "model" else "starts at login, restarted if it stops"))
    return 0


def wake_interval(name: str, config: dict[str, str] | None = None) -> timedelta | None:
    """The instance's WAKE_INTERVAL_MINUTES, bounded only by an operator floor; 0 = no scheduled wakes."""
    raw = (instance_config(name) if config is None else config).get("WAKE_INTERVAL_MINUTES", "")
    try:
        minutes = int(raw) if raw else WAKE_DEFAULT_MIN
    except ValueError:
        log(f"{name}: WAKE_INTERVAL_MINUTES={raw!r} is not a number of minutes; using {WAKE_DEFAULT_MIN}")
        minutes = WAKE_DEFAULT_MIN
    return None if minutes <= 0 else timedelta(minutes=max(minutes, wake_floor(name)))


def wake_floor(name: str) -> int:
    """The shortest interval the instance may wake at: the operator's own floor for it (`shaft limits <name> wake-floor
    <minutes>`, kept in the supervisor's folder — the instance cannot lower it), else WAKE_FLOOR_MIN (1: no floor)."""
    from .core import _tool_settings
    own = _tool_settings(name).get("wake_floor_minutes")
    return own if isinstance(own, int) and not isinstance(own, bool) and own >= 1 else WAKE_FLOOR_MIN


def next_wake(name: str, now: datetime | None = None, interval: timedelta | None | bool = False) -> str:
    """When the scheduler wakes the instance next: 'HH:MM', 'due' or 'never' (WAKE_INTERVAL_MINUTES=0). The one rule
    the dashboard, `shaft top` and the web chat's Next cycle say. `interval`: already read (False: read it here)."""
    interval = wake_interval(name) if interval is False else interval
    if interval is None:
        return "never"
    now = now or datetime.now()
    last = (_state().get("wakes") or {}).get(name)
    if not last:
        return "due"
    at = datetime.fromisoformat(last) + interval
    return "due" if at <= now else f"{at:%H:%M}"


def next_cycle(name: str) -> str:
    """Where a message left for the instance's next cycle stands: what the web UI tells the operator who sends one.
    The operator's message wakes it (event_wake) as soon as it is idle, not at its next timed wake."""
    if paused(name):
        return f"{name} is paused: it reads this in its first cycle after you resume it"
    if wake_interval(name) is None:
        return f"{name} has no scheduled cycles (WAKE_INTERVAL_MINUTES=0): it reads this when you next talk"
    why = _awake(name)
    if why.startswith("live"):
        return "a cycle is running and read its inbox when it began: this starts the next one once it ends"
    return f"for {name}'s next cycle, which this starts " + (f"after what it is doing now ({why})" if why else "now")


def _every(interval: timedelta) -> str:
    m = int(interval.total_seconds() // 60)
    return f"{m // 60} hours" if m % 60 == 0 and m >= 120 else "hour" if m == 60 else f"{m} minutes"


def describe_schedule(name: str, config: dict[str, str] | None = None) -> str:
    """What an instance should know about when it runs — from the scheduler and its own config.env."""
    if not (AGENTS / f"{SERVICES['scheduler']['label']}.plist").exists():
        return ("Your autonomous schedule is off: you run only when the operator talks to you or starts a cycle "
                "by hand.")
    interval = wake_interval(name, config)
    wakes = (f"looks every {_every(interval)} whether to wake you for an autonomous cycle (WAKE_INTERVAL_MINUTES in "
             f"your config.env — change it with set_config — at least {wake_floor(name)}): it wakes you when your "
             f"inbox has an unread message, a commitment of yours comes due before it looks again, or a job of yours allowed "
             f"to wake you delivered or published something since your last cycle, and otherwise once you have had no "
             f"cycle for "
             f"{IDLE_WAKE_MIN} minutes; a wake that comes while you are already awake — a cycle, a conversation, your "
             f"sleep — is skipped. A message from the operator, a message from another instance (at most once per "
             f"{wake_floor(name)} minutes) and a commitment falling due wake you as soon as you are idle") if interval \
        else "does not wake you for cycles (WAKE_INTERVAL_MINUTES=0 in your config.env; change it with set_config)"
    from . import battery
    night = (f" Some nights between {battery.WINDOW_START[0]:02d}:{battery.WINDOW_START[1]:02d} and your sleep it "
             f"runs its capability battery on you — fixed tasks, scored, nothing kept in your files — so a wake "
             f"in that window may be skipped." if battery.EVERY_DAYS > 0 else "")
    return (f"The supervisor {wakes}, and runs your sleep cycle every day at {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d} "
            f"(a missed sleep is made up later that day).{night} Within a cycle, what you do is up to you.")


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def editing_lock():
    """schedule.json's lock, for anyone rewriting it (record, and `shaft delete`: supervisor/delete.py)."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE.with_name("schedule.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def record(event: str, name: str = "", why: str = "") -> None:
    """Called by the runs themselves ('sleep'; 'cycle' per instance: a live cycle as it starts, under its run lock —
    runner._record_cycle, scheduled or by hand; 'wake': `shaft wake`) and by the scheduler ('wake': one it starts;
    'skip': a due one it skipped, and `why`). Each per-instance event restarts the
    instance's interval (wakes); 'cycle' is also its last cycle (cycles: wake_gate's clock), and a skip is shown until
    the next cycle (skipped). Read, changed and replaced under editing_lock (review B16): the scheduler and every wake
    and sleep run write this file, and an unlocked rewrite lost a concurrent one's stamp — or, read half-written, made
    every wake and the night's sleep due at once."""
    now = datetime.now().isoformat(timespec="seconds")
    with editing_lock():
        st = _state()
        if event in ("wake", "cycle", "skip"):
            st.setdefault("wakes", {})[name] = now
            if event == "cycle":
                st.setdefault("cycles", {})[name] = now
                st.get("skipped", {}).pop(name, None)
                st.get("idle_exits", {}).pop(name, None)   # any new cycle puts the idle wait back to normal
            elif event == "skip":
                st.setdefault("skipped", {})[name] = {"at": now, "why": why}
        elif event == "idle_revoke":
            st.get("idle_exits", {}).pop(name, None)
        elif event == "idle_exit":   # the instance ended its cycle as nothing worth one (idle.py): its next idle
            st.setdefault("idle_exits", {})[name] = now   # wake waits IDLE_BACKOFF times as long; no interval restarts
        else:
            st[event] = now
        tmp = STATE.with_name(f"schedule.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(STATE)


def last_skip(name: str, st: dict | None = None) -> str:
    """Why the instance's last due wake was skipped ('' when a cycle ran since): what status and the dashboard show."""
    skip = ((_state() if st is None else st).get("skipped") or {}).get(name)
    return f"{skip.get('why', '')} (at {skip.get('at', '?')[11:16]})" if isinstance(skip, dict) else ""


def sleep_due(now: datetime, st: dict) -> bool:
    """Once per day from SLEEP_AT, caught up later that day if missed."""
    sleep_from = now.replace(hour=SLEEP_AT[0], minute=SLEEP_AT[1], second=0, microsecond=0)
    last_sleep = datetime.fromisoformat(st["sleep"]) if st.get("sleep") else None
    return now >= sleep_from and (last_sleep is None or last_sleep < sleep_from)


def scheduler_installed() -> bool:
    return (AGENTS / f"{SERVICES['scheduler']['label']}.plist").exists()


def next_sleep(now: datetime | None = None) -> str | None:
    """Next nightly sleep, or None when the scheduler is not installed; missed sleeps are due now."""
    if not scheduler_installed():
        return None
    now = now or datetime.now()
    if sleep_due(now, _state()):
        return "due"
    at = now.replace(hour=SLEEP_AT[0], minute=SLEEP_AT[1], second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return f"{at:%Y-%m-%d %H:%M}"


def replay_next_sleep(now: datetime) -> str | None:
    """Next scheduled sleep strictly after the replay start, without today's scheduler state."""
    if not (AGENTS / f"{SERVICES['scheduler']['label']}.plist").exists():
        return None
    at = now.replace(hour=SLEEP_AT[0], minute=SLEEP_AT[1], second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return f"{at:%Y-%m-%d %H:%M}"


def replay_next_wake(name: str, now: datetime) -> str:
    """A replay starts a cycle now; its next wake is one interval later."""
    interval = wake_interval(name)
    return f"{now + interval:%H:%M}" if interval is not None else "never"


@dataclass
class WakeFacts:
    """What a timed wake would find for the instance (wake_facts), for wake_gate."""
    unread: int = 0         # messages in its inbox/
    due: str = ""           # first newly due commitment or overdue reminder
    results: int = 0        # can_wake job runs that delivered or published since its last cycle
    error: str = ""         # what could not be read: the wake goes ahead, saying so
    overdue: int = 0        # unsettled promises already due when the last cycle began
    standing: str = ""      # a project past a boundary, or an operator order a day old with no result (wakereasons.py)


def wake_gate(now: datetime, since: datetime | None, until: datetime, idle_after: timedelta, facts) -> tuple[bool, str]:
    """(wake?, why) for a due timed wake (review C15/G1/G4). Pure and model-free: an unread message, a commitment
    due before `until` (the next check), a job result since `since` (its last cycle), a standing reason (a project
    past a boundary, an operator order a day old with no result: wakereasons.py), or no cycle for `idle_after`.
    `facts`: a callable returning WakeFacts, read only when the idle rule does not decide."""
    if since is None:
        return True, "no cycle on record"
    if now - since >= idle_after:
        return True, f"no cycle for {int((now - since).total_seconds() // 60)} min"
    f = facts()
    if f.error:
        return True, f"what it has to do could not be read ({f.error})"
    if f.unread:
        return True, f"{f.unread} unread message(s) in its inbox"
    if f.due:
        return True, f"commitment {f.due}"
    if f.results:
        return True, f"{f.results} job result(s) since its last cycle"
    if f.standing:
        return True, f.standing
    due_text = f"none newly due; {f.overdue} overdue" if f.overdue else f"no commitment due before {until:%H:%M}"
    return False, (f"{NOTHING_TO_DO}: inbox empty, {due_text}, no job result since its "
                   f"last cycle at {since:%H:%M}; it wakes anyway after {int(idle_after.total_seconds() // 60)} min "
                   f"without a cycle")


def decide_wakes(now: datetime, st: dict, names: list[str], interval_of, awake_of, facts_of=None,
                 idle_after: timedelta | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """({instance to wake now: why}, {instance: why its due wake is skipped}). Pure: the scheduler's rule. With
    `facts_of(name, since, until)` -> WakeFacts, a due wake also passes wake_gate; one it stops is skipped with a reason
    starting NOTHING_TO_DO."""
    start, skip = {}, {}
    idle_after = idle_after or timedelta(minutes=IDLE_WAKE_MIN)
    for name in names:
        interval = interval_of(name)
        last = (st.get("wakes") or {}).get(name)
        if interval is None or (last and now - datetime.fromisoformat(last) < interval):
            continue
        why = awake_of(name)
        if why:
            skip[name] = why
            continue
        if facts_of is None:
            start[name] = "its interval passed"
            continue
        cycle = (st.get("cycles") or {}).get(name)
        since = datetime.fromisoformat(cycle) if cycle else None
        until = now + interval
        # After a cycle the instance itself ended as nothing worth doing, the idle wake waits longer, once (not
        # compounding); the entry is gone at its next cycle, and an unread message or a due commitment wakes it anyway.
        idle = idle_after * IDLE_BACKOFF if (st.get("idle_exits") or {}).get(name) else idle_after
        go, why = wake_gate(now, since, until, idle, lambda: facts_of(name, since, until))
        (start if go else skip)[name] = why
    return start, skip


def wake_facts(name: str, since: datetime | None, until: datetime) -> WakeFacts:
    """The facts wake_gate reads, from the supervisor's own records and the instance's inbox listing; no model. A
    watch result (watch.py) is an inbox message. Never raises: what fails is the facts' `error`."""
    from . import commitments, jobs
    try:
        now = datetime.now()
        quiet = jobs.quiet_jobs(name)
        unread = sum(n.endswith(".md") and jobs.inbox_can_wake(name, n, quiet)
                     for n in list_beneath(INSTANCES / name, "inbox"))
        due = commitments.coming_due(name, since, until)
        reminders = commitments.overdue_reminders(name, since, now)
        overdue = sum(i["status"] in ("open", "overdue") and (at := commitments._at(i["due"])) is not None
                      and at < now and since is not None and at <= commitments._local(since)
                      for i in commitments.ledger(name, now).values())
        results = jobs.results_since(name, since, quiet) if since else 0
    except (Exception, SystemExit) as e:
        return WakeFacts(error=f"{type(e).__name__}: {e}")
    reason = f"{due[0]['id']} due {due[0]['due_at']:%H:%M}" if due else \
        f"{reminders[0]['id']} overdue" if reminders else ""
    standing = ""
    try:   # its own try: a project file the instance broke must not hide its unread mail from the gate
        from . import wakereasons
        standing = wakereasons.due(name, now)[1]
    except (Exception, SystemExit) as e:
        log(f"{name}: standing wake reasons could not be read: {type(e).__name__}: {e}")
    return WakeFacts(unread=unread, results=results, due=reason, overdue=overdue, standing=standing)


def event_wake(name: str, why: str, from_instance: bool = False) -> None:
    """An event wake (review C15/G1): a message for the instance, or a commitment of its falling due, asks for a wake now
    through the jobs' wake path (jobs.request_wake: it starts once the instance is idle, never while paused) instead of
    waiting for the next timed one. Not for an instance with no scheduled wakes (WAKE_INTERVAL_MINUTES=0) or a paused one
    (its unread inbox wakes it after the resume, wake_gate). Another instance's message wakes it at most once per its wake
    floor since its last cycle — two instances answering each other are paced, not looped: one inside the floor is
    queued for the floor's end (not_before), not dropped (Codex on the C15 lane). Never raises: what asked is delivered
    already."""
    try:
        if paused(name) or wake_interval(name) is None:
            return
        from . import jobs
        if why in (jobs.pending_wakes().get(name) or {}).get("by", []):
            return
        not_before = None
        if from_instance:
            cycle = (_state().get("cycles") or {}).get(name)
            floor = wake_floor(name)
            if cycle and datetime.now() < (at := datetime.fromisoformat(cycle) + timedelta(minutes=floor)):
                not_before = at
        jobs.request_wake(name, why, not_before)
        log(f"{name}: wake asked — {why}" + (f" (not before {not_before:%H:%M}: a cycle began less than "
                                            f"{floor} min ago)" if not_before else ""))
    except (Exception, SystemExit) as e:   # SystemExit: instance_root of an instance with no folder (deleted since)
        try:
            log(f"{name}: could not ask a wake for {why}: {type(e).__name__}: {e}")
        except Exception:
            pass


def commitment_wakes(now: datetime, st: dict, names: list[str], children: dict, errors: dict[str, str]) -> None:
    """Event wakes for commitments: one that came due since the instance's last cycle (any open one due by now, for an
    instance with no cycle on record yet) asks for a wake (event_wake). The cycle it starts begins after the due time,
    so each asks once; while one is pending, event_wake does not ask again. One whose wake is starting waits for the
    next tick. Unsettled promises also ask once per daily anniversary after their due time.
    `errors`: the last read error per instance, logged once."""
    from . import commitments
    for name in names:
        cycle = (st.get("cycles") or {}).get(name)
        if f"wake {name}" in children:
            continue
        try:
            since = datetime.fromisoformat(cycle) if cycle else None
            due = commitments.coming_due(name, since, now)
            reminders = commitments.overdue_reminders(name, since, now)
        except (Exception, SystemExit) as e:
            if errors.get(name) != (err := f"{type(e).__name__}: {e}"):
                errors[name] = err
                log(f"scheduler: {name}'s commitments could not be read for its wakes — {err}")
            continue
        errors.pop(name, None)
        for item in due:
            event_wake(name, f"commitment {item['id']} due")
        for item in reminders:
            event_wake(name, f"commitment {item['id']} overdue")


def update_jobs(children: dict, names: list[str], awake_of) -> list[str]:
    """Instances whose queued `shaft update` to start now: queued, not updating or waking already, and idle."""
    return [n for n in names if update_queued(n) and f"update {n}" not in children
            and f"wake {n}" not in children and not awake_of(n)]


def updating(name: str, children: dict) -> bool:
    """A queued or running update holds the instance's wakes back: its next cycle runs the new code."""
    return f"update {name}" in children or update_queued(name) is not None


def update_ended(name: str, returncode: int) -> None:
    """A queued update dequeues itself once it holds the lock, and re-queues itself when it finds the instance busy
    (exit 0). One that failed before either is dropped, not retried every tick while it holds the wakes back."""
    if returncode != 0 and update_queued(name) is not None:
        set_update_queued(name, False)
        log(f"scheduler: {name}'s queued update failed before it could run (exit {returncode}); dropped — "
            f"run `shaft update {name}` to see why")


def _awake(name: str) -> str:
    """Why the instance counts as awake right now ('' if it does not). An open conversation waiting for the
    operator does not: a cycle starts, and pauses only while a reply is being written."""
    from .runner import busy_with, replying
    if (b := busy_with(name)):
        return b
    if replying(name):
        return "a reply to the operator is waiting for it"
    return ""


def serve(poll_s: int = 30) -> int:
    """The scheduler service: wake each instance on its own interval and run the nightly sleep, as child
    processes (an instance on a cloud model does not wait behind one on the local model); log every decision.
    Also warm the gate's shared supervisor tests for new commits (gate_warm) while the scheduler is not paused."""
    log(f"scheduler started (wakes per instance, WAKE_INTERVAL_MINUTES, default {WAKE_DEFAULT_MIN}; "
        f"sleep daily from {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d})")
    from .version import record_service   # the settings it holds (settings.not_live); "drain": it can drain-restart
    record_service("scheduler", {"drain": 1})
    from .browser import startup_note
    if (note := startup_note()):
        log(note)
    children: dict[str, tuple[subprocess.Popen, float]] = {}
    warm = WarmState()
    commitment_errors: dict[str, str] = {}
    wake_skips: dict[str, tuple[str, datetime]] = {}
    stop_children_on_signal(children)
    drain: dict | None = None   # the drain-restart under way (drain_tick)
    while True:
        for job, (proc, started) in list(children.items()):
            if proc.poll() is not None and job == "gate warm":      # it says how it ended itself (warm_ended)
                warm_ended(warm, proc.returncode, time.monotonic() - started)
                del children[job]
            elif proc.poll() is not None:
                err = stderr_tail(proc)
                log(f"scheduler: {job} ended exit={proc.returncode} after {time.monotonic() - started:.0f}s"
                    + (f" — {err}" if proc.returncode and err else ""))
                if job == "sleep" and proc.returncode not in (0, 3):   # 3: some paused; a crash must not loop
                    record("sleep")
                if job.startswith("update "):
                    update_ended(job.removeprefix("update "), proc.returncode)
                del children[job]
        drain, what = drain_tick(children, drain)
        if what == "drained":
            return relaunch()
        if what == "draining":   # nothing new starts: what is due stays due for the scheduler that comes next
            from . import calendar_proposals
            calendar_proposals.expire()   # not a child: the supervisor's own, as while paused
            time.sleep(poll_s)
            continue
        # Before the pause check, as `shaft update` itself runs while paused.
        for name in update_jobs(children, instance_names(), _awake):
            children[f"update {name}"] = (_start(["update", name]), time.monotonic())
        # Calendar expiry belongs to the supervisor, including when all instances are paused (plan M2).
        from . import calendar_proposals
        calendar_proposals.expire()
        if not (SUP_DIR / "paused").exists():
            st, now = _state(), datetime.now()
            _batteries(children, now, sleep_due(now, st))
            if sleep_due(now, st) and "sleep" not in children:
                children["sleep"] = (_start(["sleep", "--all"]), time.monotonic())
            names = [n for n in instance_names() if not paused(n) and not updating(n, children)]   # not skipped:
            # a wake due during an update starts right after it
            start, skip = decide_wakes(now, st, names, wake_interval,
                                       lambda n: "waking already" if f"wake {n}" in children else _awake(n),
                                       wake_facts, timedelta(minutes=IDLE_WAKE_MIN))
            for name, why in skip.items():
                why = why if why.startswith(NOTHING_TO_DO) else f"already awake ({why})"
                record("skip", name, why)   # skipped, not queued: the next wake is a full interval later
                reason = re.sub(r"\b\d{2}:\d{2}\b", "<time>", why)   # a moving check time is the same reason
                previous, at = wake_skips.get(name, ("", datetime.min))
                if reason != previous or now - at >= timedelta(hours=1):
                    log(f"{name}: wake skipped — {why}")
                    wake_skips[name] = reason, now
            for name, why in start.items():
                record("wake", name)
                log(f"scheduler: waking {name} — {why}")
                try:   # a project or order that just woken it has had its turn for a day (wakereasons.PACE)
                    from . import wakereasons
                    wakereasons.mark(name, why, now)
                except (Exception, SystemExit) as e:
                    log(f"{name}: wake reason not marked: {type(e).__name__}: {e}")
                children[f"wake {name}"] = (_start(["wake", name]), time.monotonic())
            commitment_wakes(now, st, names, children, commitment_errors)   # started by jobs.tick, once it is idle
            from . import jobs   # the instances' own plugins on their timers, and the wakes those jobs asked for
            jobs.tick(children, now, _start, instance_names(),
                      lambda n: "waking already" if f"wake {n}" in children else
                      "being updated" if updating(n, children) else _awake(n))
            try:   # new mail and calendar changes for instances that opted in (a background pass, no model)
                from . import watch
                watch.tick(children, now, names)
            except Exception as e:
                log(f"scheduler: the mail and calendar watch did not start — {type(e).__name__}: {e}")
            regate_recorded(children, warm, _start)
            gate_warm(children, warm, _start)   # the shared supervisor tests of a new commit, before a gate needs them
            if "trials" not in children:   # trials of proposed changes; on the local model only in its idle time
                from . import trial
                if trial.has_work():
                    children["trials"] = (_start(["trial", "work"]), time.monotonic())
        time.sleep(poll_s)


CHILD_STOP_S = 15   # how long the scheduler's children get to stop, inside launchd's 20 s before it kills them


def stop_children_on_signal(children: dict) -> None:
    """SIGTERM or SIGHUP to the scheduler (launchd stopping it) is passed to each child first — a wake or sleep ends
    its run's processes and notes the run as stopped, a battery restores the instance's files — and the scheduler goes
    once they have (or after CHILD_STOP_S), as the signal intended. A child's agent leads its own process group now,
    which launchd's stop of the scheduler's group no longer reaches (Codex on e77d5e5)."""
    def on_signal(signum, frame):
        for s in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)
        live = [(job, proc) for job, (proc, _) in children.items() if proc.poll() is None]
        log(f"scheduler: stopping (signal {signum}); stopping {', '.join(j for j, _ in live) or 'no child'} first")
        for _, proc in live:
            proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + CHILD_STOP_S
        for job, proc in live:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                log(f"scheduler: {job} did not stop within {CHILD_STOP_S}s: killed")
                proc.kill()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    for s in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(s) is not signal.SIG_IGN:
            signal.signal(s, on_signal)


# -- drain-restart (docs/deploy.md step 3) ----------------------------------------------------------------------------
# A SIGTERM stops the scheduler's live runs on purpose (above), so a deploy waited for a moment with no instance awake,
# and with instances covering each other's cycles that never came (2026-11-03: 90 min, none). `shaft autonomy restart`
# asks for a drain instead: the scheduler starts nothing new, lets what runs end, then restarts itself on the code
# checked out now. The request is a file, not a signal: a user signal to a scheduler without a handler for it (one
# that predates this) would kill it, and launchd its runs with it.
RELAUNCH_WAIT_S = 30   # how long a drained scheduler waits for launchd's SIGTERM from its own kickstart -k


def _restart_path() -> Path:
    return SUP_DIR / "scheduler-restart.json"   # the request: {pid, at, requested, by, deadline}


def _drain_path() -> Path:
    return SUP_DIR / "scheduler-drain.json"     # what the drain is doing: the request, state, waiting, ended


def default_drain_s() -> int:
    """How long a drain may wait: the longest one child it waits for may run on its own budget (a sleep), and a
    cycle's budget again for its settling. It follows those knobs."""
    return SLEEP_TIMEOUT_S + CYCLE_TIMEOUT_S


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)                       # a reader never sees half of it


@contextlib.contextmanager
def _request_lock():
    """One writer of the request at a time: a request made, withdrawn, dropped as stale or taken as done (Codex on the
    drain: a stale drop unlinked a newer request written in between, and a cancel could land after the drain had
    committed to its restart, reporting that scheduling resumes)."""
    SUP_DIR.mkdir(parents=True, exist_ok=True)
    with open(SUP_DIR / "scheduler-restart.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def request_restart(pid: int, within_s: int | None = None, by: str = "the operator") -> dict:
    """Ask the scheduler running as `pid` for a drain-restart within `within_s` (default_drain_s)."""
    now = time.time()
    req = {"pid": pid, "at": now, "requested": datetime.fromtimestamp(now).isoformat(timespec="seconds"), "by": by,
           "deadline": now + (within_s or default_drain_s())}
    with _request_lock():
        _write_json(_restart_path(), req)
    return req


def _drop_request() -> bool:
    """Under _request_lock."""
    try:
        _restart_path().unlink()
        return True
    except FileNotFoundError:
        return False


def cancel_restart() -> bool:
    """Withdraw a pending request (whether there was one): the scheduler resumes at its next tick. One the scheduler
    has taken as done is gone already — it is restarting, and nothing withdraws that."""
    with _request_lock():
        return _drop_request()


def restart_request(pid: int | None = None) -> dict | None:
    """The pending request, None if there is none or it cannot be read. With `pid` (the scheduler asking, under
    _request_lock): a request made of another scheduler process — the one this one replaced — is stale, so it is
    dropped and never drains a scheduler that already runs the new code (no restart loop); an unreadable one is
    dropped too, and said. Without it (a reader: status, a worker) nothing is dropped."""
    try:
        req = json.loads(_restart_path().read_text())
        if not all(isinstance(req.get(k), (int, float)) for k in ("pid", "at", "deadline")):
            raise ValueError("no pid, at or deadline")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        if pid is not None:
            log(f"scheduler: the restart request {_restart_path()} is unreadable ({e}): dropped")
            _drop_request()
        return None
    if pid is not None and req["pid"] != pid:
        log(f"scheduler: a restart asked of scheduler {req['pid']} at {req.get('requested', '?')} is not for this one "
            f"({pid}): dropped")
        _drop_request()
        return None
    return req


def parent_draining() -> bool:
    """For a long worker the scheduler started (`shaft trial work`, `shaft battery run`): its scheduler is draining,
    so it starts no new unit of work (a trial pair, a battery arm) and ends with the one it is running. One started by
    hand is not the scheduler's: it goes on."""
    req = restart_request()
    return req is not None and req["pid"] == os.getppid()


def drain_status() -> dict | None:
    """The drain's own record (scheduler-drain.json): state draining, drained, abandoned or cancelled."""
    try:
        return json.loads(_drain_path().read_text())
    except (OSError, ValueError):
        return None


def draining_now() -> dict | None:
    """The drain under way, None if none (its record says draining and its scheduler is still alive)."""
    from .version import _alive
    st = drain_status()
    return st if st and st.get("state") == "draining" and _alive(st.get("pid", 0)) else None


def drain_step(children: dict, now: float, deadline: float, watch_busy: bool) -> tuple[str, list[str]]:
    """Pure: (what — 'wait', 'done' or 'abandon' —, what it still waits for). Every live child is waited for and
    none is signalled: each ends on its own budget, and the scheduler's long workers (trials, a battery) end after
    their current unit (parent_draining). Past the deadline the drain is abandoned: it never cuts a run."""
    waiting = [job for job, (proc, _) in children.items() if proc.poll() is None] + \
        (["the mail and calendar watch"] if watch_busy else [])
    if not waiting:
        return "done", []
    return ("abandon" if now >= deadline else "wait"), waiting


def drain_tick(children: dict, drain: dict | None) -> tuple[dict | None, str]:
    """The scheduler's drain, every tick after its children are reaped: (the drain now, what — '' to schedule as usual,
    'draining' to start nothing, 'drained' to restart now). Says each step in the log and in scheduler-drain.json.
    Under _request_lock: a cancel lands either before the restart is committed (it resumes) or after (too late)."""
    from . import watch
    with _request_lock():
        req = restart_request(os.getpid())
        if req is None:
            if drain:
                log("scheduler: restart cancelled — scheduling resumes")
                _write_json(_drain_path(), {**drain, "state": "cancelled", "ended": _now_iso()})
            return None, ""
        what, waiting = drain_step(children, time.time(), req["deadline"], watch.busy())
        by = datetime.fromtimestamp(req["deadline"])
        if drain is None or drain.get("at") != req["at"]:
            drain = {**req, "state": "draining", "waiting": None}
            log(f"scheduler: draining for a restart (asked by {req.get('by', '?')} at "
                f"{req.get('requested', '?')[11:16]}): nothing new starts; waiting for {', '.join(waiting) or 'nothing'}; "
                f"by {by:%H:%M} at the latest")
        minutes = round((time.time() - req["at"]) / 60)
        # The outcome is written before the request goes: `restart --wait` reads a request gone with no outcome as
        # dropped.
        if what == "done":
            from .version import supervisor_commit
            drain = {**drain, "state": "drained", "waiting": [], "ended": _now_iso()}
            _write_json(_drain_path(), drain)
            _drop_request()
            log(f"scheduler: drained after {minutes} min — restarting on the code checked out now "
                f"({supervisor_commit()})")
            return drain, "drained"
        if what == "abandon":
            _write_json(_drain_path(), {**drain, "state": "abandoned", "waiting": waiting, "ended": _now_iso()})
            _drop_request()
            log(f"scheduler: restart FAILED to drain within {minutes} min (by {by:%H:%M}) — still running "
                f"{', '.join(waiting)}; scheduling resumes on the code it started with. Nothing was cut: "
                f"`shaft autonomy restart` asks again")
            return None, ""
        if waiting != drain["waiting"]:
            drain = {**drain, "waiting": waiting}
            _write_json(_drain_path(), drain)
        return drain, "draining"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def relaunch() -> int:
    """A drained scheduler comes back on the code checked out now. Under launchd it does not just exit: an explicit
    `launchctl kickstart -k` of its own label (from its own session, outside its process group) has launchd stop it —
    no child is left for its SIGTERM to stop — and start the new one. An explicit start works in launchd's
    on-demand-only mode, where KeepAlive's own restart may not (2026-10-13, autonomy.py `service`). One started by
    hand only exits."""
    if launchd_pid("scheduler") != os.getpid():
        log("scheduler: drained; not launchd's scheduler (started by hand), so it only exits")
        return 0
    subprocess.Popen(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{SERVICES['scheduler']['label']}"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    time.sleep(RELAUNCH_WAIT_S)   # launchd's SIGTERM ends this process in here (stop_children_on_signal)
    log(f"scheduler: launchctl kickstart -k did not stop it within {RELAUNCH_WAIT_S}s; it exits, and launchd's "
        f"KeepAlive starts it again")
    return 0


FIRST_CUTOVER = (
    "Without a drain, a restart now stops what it runs. To restart it without cutting a run: `shaft pause` (it starts "
    "no wake, sleep, job, battery, warm-up or trial; a queued update still starts), wait until no process is left in "
    "its process group but the scheduler itself — `pgrep -lf -g \"$(ps -o pgid= -p \"$(pgrep -f 'supervisor autonomy "
    "serve')\" | tr -d ' ')\" | grep -v 'autonomy serve'` prints nothing —, then `launchctl kickstart -k "
    "gui/$(id -u)/com.shaft.scheduler` and `shaft resume`. Each instance gets a pause and a resume note.")


def restart(within_min: int | None = None, wait: bool = False, cancel: bool = False, poll_s: float = 5) -> int:
    """`shaft autonomy restart`: a drain-restart of the running scheduler (drain_tick). It cuts no run, so it needs no
    guard. --wait: until the new scheduler has recorded itself (0), or the drain was abandoned or cancelled (1)."""
    from .version import service as service_record
    if cancel:
        if cancel_restart():
            print("restart request withdrawn: the scheduler resumes at its next tick")
            return 0
        st = drain_status() or {}
        if st.get("state") == "drained" and datetime.now() - datetime.fromisoformat(st["ended"]) < timedelta(minutes=10):
            print(f"too late: the scheduler drained at {st['ended'][11:16]} and is restarting (or has)", file=sys.stderr)
            return 1
        print("no restart request is pending")
        return 0
    rec = service_record("scheduler", stale=False)
    if not running("scheduler") or not rec:
        print("the scheduler is not running: `shaft autonomy on` starts it", file=sys.stderr)
        return 1
    if not (rec.get("settings") or {}).get("drain"):
        print(f"the running scheduler (pid {rec['pid']}, {rec.get('describe') or rec.get('commit')}, since "
              f"{rec.get('started', '?')}) predates the drain-restart and cannot drain. {FIRST_CUTOVER}",
              file=sys.stderr)
        return 2
    req = request_restart(rec["pid"], within_min * 60 if within_min else None)
    log(f"operator asked the scheduler (pid {rec['pid']}) for a drain-restart, by "
        f"{datetime.fromtimestamp(req['deadline']):%H:%M} at the latest")
    print(f"asked scheduler {rec['pid']} to restart once its runs end: it starts nothing new from its next tick, and "
          f"gives up (cutting nothing) at {datetime.fromtimestamp(req['deadline']):%H:%M}. `shaft autonomy status` shows "
          f"what it waits for.")
    if not wait:
        return 0
    said = None
    while True:
        new = service_record("scheduler", stale=False)
        if new and new["pid"] != rec["pid"]:
            print(f"restarted: scheduler {new['pid']} on {new.get('describe') or new.get('commit')}")
            return 0
        st = drain_status()
        if st and st.get("at") == req["at"] and st.get("state") in ("abandoned", "cancelled"):
            print(f"not restarted: the drain was {st['state']}"
                  + (f" — still running {', '.join(st.get('waiting') or [])}" if st["state"] == "abandoned" else ""),
                  file=sys.stderr)
            return 1
        if st and st.get("at") == req["at"] and st.get("waiting") != said and st.get("state") == "draining":
            said = st.get("waiting")
            print(f"waiting for {', '.join(said) or 'nothing'}")
        if not restart_request() and not (st and st.get("at") == req["at"]):
            print("not restarted: the request was dropped before the scheduler took it up (see the log)",
                  file=sys.stderr)
            return 1
        if time.time() > req["deadline"] + RELAUNCH_WAIT_S + 60:
            print("not restarted: no new scheduler by the deadline (see the log)", file=sys.stderr)
            return 1
        time.sleep(poll_s)


def drain_line() -> str:
    """One line on a pending or running drain-restart for `shaft status` and `shaft autonomy status` ('' if none)."""
    if (st := draining_now()):
        return (f"scheduler restarting (asked {st.get('requested', '?')[11:16]}): nothing new starts; waiting for "
                f"{', '.join(st.get('waiting') or []) or 'its next tick'}; by "
                f"{datetime.fromtimestamp(st['deadline']):%H:%M} at the latest")
    if (req := restart_request()):
        return (f"scheduler restart asked at {req.get('requested', '?')[11:16]}: taken up at its next tick (by "
                f"{datetime.fromtimestamp(req['deadline']):%H:%M} at the latest)")
    return ""


def battery_jobs(children: dict, now: datetime, sleeping_due: bool, names: list[str], due, awake_of) \
        -> tuple[list[str], list[str]]:
    """(battery jobs to stop, instances whose battery to start). Pure: the scheduler's battery rule. A battery
    runs only inside the night window, which closes before the sleep (never in the
    sleep's way); a due sleep or the window's end stops one that is still running."""
    from . import battery
    _, end = battery.window(now)
    stop = [job for job in children if job.startswith("battery ") and (now >= end or sleeping_due)]
    start = [] if sleeping_due else [n for n in names if f"battery {n}" not in children and due(n, now)
                                     and not awake_of(n)]
    return stop, start


def _batteries(children: dict, now: datetime, sleeping_due: bool) -> None:
    from . import battery
    names = [n for n in instance_names() if not paused(n)]
    stop, start = battery_jobs(children, now, sleeping_due, names, battery.due, _awake)
    for job in stop:
        proc, _ = children[job]
        if proc.poll() is None and not getattr(proc, "stopping", False):
            proc.send_signal(signal.SIGTERM)   # it stops its current item, restores the instance's files
            proc.stopping = True
            log(f"scheduler: stopping {job} — {'a sleep is due' if sleeping_due else 'the night window closed'}")
    _, end = battery.window(now)
    for name in start:
        children[f"battery {name}"] = (_start(["battery", "run", name, "--until", f"{end:%H:%M}"]),
                                       time.monotonic())


WARM_RETRY_S = 3600   # a timeout is retried one hour after it ended, until it passes or the commits move
WARM_CHECK_S = 3600   # the same commit is looked at again hourly: the child exits at once while its record is fresh


@dataclass
class WarmState:
    """Warm-up attempts, test failures, timeout retry clock, and the last held-work re-gate attempt."""
    pair: str | None = None
    started: float = float("-inf")
    failed: set[str] = field(default_factory=set)
    unreadable: bool = False
    timed_out: bool = False
    ended: float = float("-inf")
    regated_pass: str | None = None
    regated_at: float = float("-inf")


def gate_warm(children: dict, st: WarmState, start, now: float | None = None) -> None:
    """Start `shaft gate warm` (gate.warm) when main has moved to commits with no warm-up
    yet, and hourly for the same ones, so the first gate after a merge reads the shared supervisor tests' pass instead
    of running them while holding its instance. A test failure is not tried again on its commits; a timeout is
    retried hourly after it ends. Every gate on unrecorded commits runs the shared part anyway."""
    if "gate warm" in children:
        return
    now = time.monotonic() if now is None else now
    try:
        try:
            pair = gate.commits_id()
        except Exception as e:                # no commit to read: say so once
            if not st.unreadable:
                st.unreadable = True
                log(f"scheduler: the gate's warm-up cannot read the project's commits: {type(e).__name__}: {e}")
            return
        st.unreadable = False
        wait = now - st.ended < WARM_RETRY_S if st.timed_out else now - st.started < WARM_CHECK_S
        if pair in st.failed or (pair == st.pair and wait):
            return
        children["gate warm"] = (start(["gate", "warm"]), time.monotonic())
        st.pair, st.started, st.timed_out = pair, now, False
    except Exception as e:                    # never the scheduler's loop: wakes, sleeps and jobs go on (Codex, M2)
        try:
            log(f"scheduler: the gate's warm-up could not start: {type(e).__name__}: {e}")
        except Exception:
            pass


def warm_ended(st: WarmState, returncode: int, seconds: float = 0.0) -> None:
    """A warm-up child ended. A failure marks its commits as tried. warm() logs its own alert for a failure it knew
    (gate.WARM_FAILED or WARM_TIMED_OUT), so those are said without the words the panel alerts on; another exit — a
    crash before warm() could say anything — is the alert."""
    st.timed_out = returncode == gate.WARM_TIMED_OUT
    st.ended = st.started + seconds
    if returncode not in (0, gate.WARM_TIMED_OUT) and st.pair:
        st.failed.add(st.pair)
    if returncode == 0:
        log(f"scheduler: gate warm ended after {seconds:.0f}s: the shared supervisor tests' pass is recorded")
    elif returncode == gate.WARM_TIMED_OUT:
        log(f"scheduler: gate warm ended after {seconds:.0f}s without a pass (timed out, said above); "
            f"tries again in {WARM_RETRY_S // 60} min on {st.pair}")
    elif returncode == gate.WARM_FAILED:
        log(f"scheduler: gate warm ended after {seconds:.0f}s without a pass (said above); not tried again on "
            f"{st.pair}")
    else:
        log(f"scheduler: gate warm ended exit={returncode} after {seconds:.0f}s, before it could say why; not tried "
            f"again on {st.pair}")


def regate_recorded(children: dict, st: WarmState, start) -> None:
    """A gate's shared pass also releases held work, once per pass newer than the last re-gate attempt."""
    if "gate regate" in children:
        return
    try:
        if (req := restart_request()) and req["pid"] == os.getpid():
            return
        from . import gate_held
        if not gate_held.waiting():
            return
        tree = PROJECT / gate.TREES / gate.commits_id() / PROJECT.name
        env = {k: v for k, v in os.environ.items() if k != "SHAFT_UNDER_TEST"}
        rec = gate.recorded_pass(tree, gate.shared_key(tree, env))
        # Hourly too: a re-gate that found the instance busy has nothing newer to wait for.
        if rec and (rec["passed_at"] != st.regated_pass
                    or time.monotonic() - st.regated_at >= WARM_RETRY_S):
            regate_after_warm(children, start)
            if "gate regate" in children:
                st.regated_pass, st.regated_at = rec["passed_at"], time.monotonic()
    except Exception as e:
        with contextlib.suppress(Exception):
            log(f"scheduler: the gate's recorded pass could not be read: {type(e).__name__}: {e}")


def regate_after_warm(children: dict, start) -> None:
    """A warm-up found the gate's baseline green: start `shaft gate regate` (runner.regate_held) when an instance has a
    code change held while it was red. One at a time; nothing here raises into
    the scheduler's loop."""
    if "gate regate" in children:
        return
    try:
        from . import gate_held
        if gate_held.waiting():
            children["gate regate"] = (start(["gate", "regate"]), time.monotonic())
    except Exception as e:
        try:
            log(f"scheduler: the held code changes' re-gate could not start: {type(e).__name__}: {e}")
        except Exception:
            pass


STDERR_KEPT = 4000   # characters of a child's stderr kept: its tail says why it failed


def _start(args: list[str]) -> subprocess.Popen:
    log(f"scheduler: starting {' '.join(args)}")
    proc = subprocess.Popen([PYTHON, "-E", "-s", "-m", "supervisor", *args], cwd=PROJECT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    drain_stderr(proc)
    return proc


def drain_stderr(proc: subprocess.Popen) -> None:
    """Read the child's stderr (a binary pipe) while it runs, keeping its last STDERR_KEPT characters (review B14).
    Read only after it ended, a child that wrote more than the pipe holds blocked on the write and never ended (`gate
    warm`'s was never read at all), and a grandchild still holding the pipe blocked the scheduler's loop in that read.
    os.read returns what is there: an ended child's last line is kept even while a grandchild holds the pipe."""
    tail: list[str] = []
    decode = codecs.getincrementaldecoder("utf-8")(errors="replace").decode

    def read():
        try:
            while (chunk := os.read(proc.stderr.fileno(), 4096)):
                tail.append(decode(chunk))
                if sum(map(len, tail)) > 2 * STDERR_KEPT:
                    tail[:] = ["".join(tail)[-STDERR_KEPT:]]
        except (OSError, ValueError):   # closed under it: what was read is kept
            pass
        with contextlib.suppress(OSError):
            proc.stderr.close()
    proc.stderr_tail = tail
    proc.stderr_reader = threading.Thread(target=read, name=f"stderr-{proc.pid}", daemon=True)
    proc.stderr_reader.start()


STDERR_JOIN_S = 1.0   # how long an ended child's reader gets to finish: a grandchild holding the pipe never ends it


def stderr_tail(proc) -> str:
    """The last 300 characters an ended child wrote to stderr (drain_stderr); '' for one started without the reader."""
    reader = getattr(proc, "stderr_reader", None)
    if not isinstance(reader, threading.Thread):
        return ""
    reader.join(timeout=STDERR_JOIN_S)
    return "".join(proc.stderr_tail).strip()[-300:]


def wake(name: str) -> int:
    """One scheduled wake: a life cycle, unless the instance is awake already (then it is skipped). It restarts the
    instance's interval; the cycle itself is stamped once it holds the instance (runner._record_cycle), so a skipped
    one keeps the last cycle's stamp and its shown skip reason."""
    record("wake", name)
    try:
        return run_agent(name, "live", ["live"], interactive=False, wait_lock=False)
    except Busy:
        log(f"{name}: wake skipped — it is already awake ({_awake(name) or 'another run'})")
        return 0


def start_wake(name: str) -> str:
    """By hand (`shaft top`, the web UI, `shaft cycle`): one wake of an idle instance, started detached (it outlives
    the panel and counts as the instance's wake, so the scheduler's next one is a full interval later). Refuses, with
    the reason, an instance that is paused or already awake."""
    if paused(name):
        raise RuntimeError(f"{name} is paused (shaft resume {name})")
    if (why := _awake(name)):
        raise RuntimeError(f"{name} is already awake ({why})")
    log(f"operator started a cycle for {name} (by hand: shaft top, the web UI or shaft cycle)")
    with (SUP_DIR / f"manual-wake-{name}.log").open("a") as out:
        subprocess.Popen([PYTHON, "-E", "-s", "-m", "supervisor", "wake", name], cwd=PROJECT,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    return f"started a cycle for {name}"


def tick() -> int:
    """By hand: wake every awake-able instance once now (one after another), skipping any already awake."""
    if (SUP_DIR / "paused").exists():
        log("tick skipped: all paused")
        return 0
    worst = 0
    for name in instance_names():
        if paused(name):
            continue
        if (why := _awake(name)):
            log(f"tick: {name} skipped — already awake ({why})")
            continue
        worst = max(worst, wake(name))
    return worst


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def autonomy(action: str) -> int:
    domain = f"gui/{os.getuid()}"
    for label in LEGACY_JOBS:   # the timed launchd jobs this replaced
        if (AGENTS / f"{label}.plist").exists() or _launchctl("print", f"{domain}/{label}").returncode == 0:
            _launchctl("bootout", f"{domain}/{label}")
            (AGENTS / f"{label}.plist").unlink(missing_ok=True)
            log(f"autonomy: removed legacy launchd job {label}")
    if action == "serve":
        return serve()
    if action == "status":
        service("scheduler", "status")
        st, now = _state(), datetime.now()
        for name in instance_names():
            interval, last = wake_interval(name), (st.get("wakes") or {}).get(name)
            nxt = "never (WAKE_INTERVAL_MINUTES=0)" if interval is None else (
                "due now" if not last or now - datetime.fromisoformat(last) >= interval
                else f"next {(datetime.fromisoformat(last) + interval):%H:%M}")
            every = f"every {_every(interval)}" if interval else "no wakes"
            cycle = (st.get("cycles") or {}).get(name)
            print(f"{name:10} {every:18} last cycle {cycle or 'never'} · {nxt}"
                  + (f"\n{'':10} last wake skipped: {skipped}" if (skipped := last_skip(name, st)) else ""))
        print(f"a due wake runs only with something to do, or after {IDLE_WAKE_MIN} min without a cycle "
              f"(SHAFT_IDLE_WAKE_MINUTES)")
        print(f"nightly sleep from {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d} · last {st.get('sleep', 'never')}"
              + (" · due now" if sleep_due(now, st) else ""))
        if (line := drain_line()):
            print(line)
        return 0
    code = service("scheduler", "on" if action == "on" else "off")
    log(f"autonomy {action}")
    print(f"autonomy {action}: " + (f"each instance wakes every WAKE_INTERVAL_MINUTES (default {WAKE_DEFAULT_MIN}), "
                                    f"sleep daily from {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d}" if action == "on"
                                    else "scheduler removed"))
    return code
