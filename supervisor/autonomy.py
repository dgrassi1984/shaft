"""Autonomous schedule: each instance wakes for a cycle every WAKE_INTERVAL_MINUTES (its own config.env,
default 120), and all of them sleep daily from SLEEP_AT — kept by SHAFT's own scheduler service
(com.shaft.scheduler, always running). A wake that comes due while the instance is already awake (a cycle,
a conversation, its sleep) is skipped, not queued; the next one is a full interval later.

A timed wake also needs something to do (wake_gate, review C15/G1/G4): an unread message, a commitment coming due
before the next check, a job result since its last cycle, or no cycle for SHAFT_IDLE_WAKE_MINUTES; with none it is
skipped, the reason kept in schedule.json and shown. What used to wait for that interval wakes the instance at once
instead (event wakes, through the jobs' wake path): a message from the operator or another instance, and a commitment
falling due.

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
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import gate, localmodel
from .core import (INSTANCES, PROJECT, PYTHON, SUP_DIR, instance_config, instance_names, knob, list_beneath, log,
                   paused, positive_int, set_update_queued, update_queued)
from .runner import Busy, is_busy, run_agent

AGENTS = Path.home() / "Library" / "LaunchAgents"
WAKE_DEFAULT_MIN = 120                  # minutes between wakes when config.env does not say
WAKE_FLOOR_MIN = 10                     # config.env is the instance's own file: no faster than this
SLEEP_AT = (4, 0)                       # hour, minute — daily; caught up later that day if missed
STATE = SUP_DIR / "schedule.json"       # last wake per instance / last nightly sleep, written by the runs
LEGACY_JOBS = ("com.shaft.tick", "com.shaft.sleep")
BOOTSTRAP_TRIES = 20                    # x 0.5 s: launchd lets go of a booted-out job a moment after bootout returns
IDLE_WAKE_MIN, _ = knob(
    "SHAFT_IDLE_WAKE_MINUTES", "30", positive_int, "minutes an instance may go without a cycle before a timed wake "
    "starts one with nothing new for it (no unread message, commitment coming due or job result)",
    group="Runs and conversations", services=("runs", "web", "scheduler"))
NOTHING_TO_DO = "nothing to do"         # how a timed wake the gate skipped is said (log, schedule.json, status)


# Long-running services (KeepAlive): SHAFT's own model server, the OpenAI-compatible API, the web UI, …. The model
# server runs only while used (localmodel.py): not at login, restarted after a crash but not after its own idle exit
# (code 0).
SERVICES = {
    "model": {"label": localmodel.LABEL,
              "args": [str(PROJECT / ".venv-bonsai" / "bin" / "python"), "-E", "-s", "-m", "supervisor.bonsai_server"],
              "plist": {"RunAtLoad": False, "KeepAlive": {"SuccessfulExit": False}}},
    "api": {"label": "com.shaft.api", "args": [PYTHON, "-E", "-s", "-m", "supervisor", "api", "serve"]},
    "embed": {"label": "com.shaft.embed",
              "args": [str(PROJECT / ".venv-bonsai" / "bin" / "python"), "-E", "-s", "-m", "supervisor.embed_server"]},
    "scheduler": {"label": "com.shaft.scheduler",
                  "args": [PYTHON, "-E", "-s", "-m", "supervisor", "autonomy", "serve"]},
    "telegram": {"label": "com.shaft.telegram", "args": [PYTHON, "-E", "-s", "-m", "supervisor", "telegram", "serve"]},
    "web": {"label": "com.shaft.web", "args": [PYTHON, "-E", "-s", "-m", "supervisor", "web", "serve"]},
}


def running(name: str) -> bool:
    """Whether launchd has the service's process up now (what `service(name, "status")` prints as its state)."""
    r = _launchctl("print", f"gui/{os.getuid()}/{SERVICES[name]['label']}")
    return any(l.strip() == "state = running" for l in r.stdout.splitlines())


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
    path.write_bytes(plistlib.dumps({
        "Label": label, "ProgramArguments": svc["args"], "WorkingDirectory": str(PROJECT),
        "StandardOutPath": str(SUP_DIR / f"{label}.log"), "StandardErrorPath": str(SUP_DIR / f"{label}.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30, **svc.get("plist", {})}))
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
    """The instance's WAKE_INTERVAL_MINUTES (config.env), at least WAKE_FLOOR_MIN; 0 = no scheduled wakes."""
    raw = (instance_config(name) if config is None else config).get("WAKE_INTERVAL_MINUTES", "")
    try:
        minutes = int(raw) if raw else WAKE_DEFAULT_MIN
    except ValueError:
        log(f"{name}: WAKE_INTERVAL_MINUTES={raw!r} is not a number of minutes; using {WAKE_DEFAULT_MIN}")
        minutes = WAKE_DEFAULT_MIN
    return None if minutes <= 0 else timedelta(minutes=max(minutes, wake_floor(name)))


def wake_floor(name: str) -> int:
    """The shortest interval the instance may set itself: WAKE_FLOOR_MIN, or the operator's own floor for it
    (`shaft limits <name> wake-floor <minutes>`, kept in the supervisor's folder — the instance cannot lower it)."""
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
             f"inbox has an unread message, a commitment of yours comes due before it looks again, or a job of yours "
             f"delivered or published something since your last cycle, and otherwise once you have had no cycle for "
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
            elif event == "skip":
                st.setdefault("skipped", {})[name] = {"at": now, "why": why}
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
    due: str = ""           # 'c1103-0001-1 due 14:30': its first open commitment coming due before the next check
    results: int = 0        # job runs that delivered or published something since its last cycle
    error: str = ""         # what could not be read: the wake goes ahead, saying so


def wake_gate(now: datetime, since: datetime | None, until: datetime, idle_after: timedelta, facts) -> tuple[bool, str]:
    """(wake?, why) for a due timed wake (review C15/G1/G4). Pure and model-free: an unread message, a commitment
    due before `until` (the next check), a job result since `since` (its last cycle), or no cycle for `idle_after`.
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
    return False, (f"{NOTHING_TO_DO}: inbox empty, no commitment due before {until:%H:%M}, no job result since its "
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
        go, why = wake_gate(now, since, until, idle_after, lambda: facts_of(name, since, until))
        (start if go else skip)[name] = why
    return start, skip


def wake_facts(name: str, since: datetime | None, until: datetime) -> WakeFacts:
    """The facts wake_gate reads, from the supervisor's own records and the instance's inbox listing; no model. A
    watch result (watch.py) is an inbox message. Never raises: what fails is the facts' `error`."""
    from . import commitments, jobs
    try:
        unread = sum(n.endswith(".md") for n in list_beneath(INSTANCES / name, "inbox"))
        due = commitments.coming_due(name, since, until)
        results = jobs.results_since(name, since) if since else 0
    except (Exception, SystemExit) as e:
        return WakeFacts(error=f"{type(e).__name__}: {e}")
    return WakeFacts(unread=unread, results=results, due=f"{due[0]['id']} due {due[0]['due_at']:%H:%M}" if due else "")


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
    next tick. `errors`: the last read error per instance, logged once."""
    from . import commitments
    for name in names:
        cycle = (st.get("cycles") or {}).get(name)
        if f"wake {name}" in children:
            continue
        try:
            due = commitments.coming_due(name, datetime.fromisoformat(cycle) if cycle else None, now)
        except (Exception, SystemExit) as e:
            if errors.get(name) != (err := f"{type(e).__name__}: {e}"):
                errors[name] = err
                log(f"scheduler: {name}'s commitments could not be read for its wakes — {err}")
            continue
        errors.pop(name, None)
        for item in due:
            event_wake(name, f"commitment {item['id']} due")


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
    from .version import record_service   # the settings it holds (settings.not_live)
    record_service("scheduler", {})
    from .browser import startup_note
    if (note := startup_note()):
        log(note)
    children: dict[str, tuple[subprocess.Popen, float]] = {}
    warm = WarmState()
    commitment_errors: dict[str, str] = {}
    stop_children_on_signal(children)
    while True:
        for job, (proc, started) in list(children.items()):
            if proc.poll() is not None and job == "gate warm":      # it says how it ended itself (warm_ended)
                warm_ended(warm, proc.returncode, time.monotonic() - started)
                del children[job]
                if proc.returncode == 0:                              # the baseline is green: held changes go again
                    regate_after_warm(children, _start)
            elif proc.poll() is not None:
                err = stderr_tail(proc)
                log(f"scheduler: {job} ended exit={proc.returncode} after {time.monotonic() - started:.0f}s"
                    + (f" — {err}" if proc.returncode and err else ""))
                if job == "sleep" and proc.returncode not in (0, 3):   # 3: some paused; a crash must not loop
                    record("sleep")
                if job.startswith("update "):
                    update_ended(job.removeprefix("update "), proc.returncode)
                del children[job]
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
                log(f"scheduler: {name}'s wake skipped — {why}")
            for name, why in start.items():
                record("wake", name)
                log(f"scheduler: waking {name} — {why}")
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


WARM_CHECK_S = 3600   # the same commit is looked at again hourly: the child exits at once while its record is fresh


@dataclass
class WarmState:
    """The scheduler's memory for gate_warm: the commits it last started a warm-up for, when, and those that failed."""
    pair: str | None = None
    started: float = float("-inf")
    failed: set[str] = field(default_factory=set)
    unreadable: bool = False


def gate_warm(children: dict, st: WarmState, start, now: float | None = None) -> None:
    """Start `shaft gate warm` (gate.warm, docs/plans/PLAN-gate-cache.md) when main has moved to commits with no warm-up
    yet, and hourly for the same ones, so the first gate after a merge reads the shared supervisor tests' pass instead
    of running them while holding its instance. A commit whose warm-up failed is not tried again: its failure is an
    alert, and every gate on it runs the shared part anyway."""
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
        if pair in st.failed or (pair == st.pair and now - st.started < WARM_CHECK_S):
            return
        st.pair, st.started = pair, now
        children["gate warm"] = (start(["gate", "warm"]), time.monotonic())
    except Exception as e:                    # never the scheduler's loop: wakes, sleeps and jobs go on (Codex, M2)
        try:
            log(f"scheduler: the gate's warm-up could not start: {type(e).__name__}: {e}")
        except Exception:
            pass


def warm_ended(st: WarmState, returncode: int, seconds: float = 0.0) -> None:
    """A warm-up child ended. A failure marks its commits as tried. warm() logs its own alert for a failure it knew
    (gate.WARM_FAILED), so that one is said here without the words the panel alerts on; any other failure exit — a
    crash before warm() could say anything — is the alert."""
    if returncode != 0 and st.pair:
        st.failed.add(st.pair)
    if returncode == 0:
        log(f"scheduler: gate warm ended after {seconds:.0f}s: the shared supervisor tests' pass is recorded")
    elif returncode == gate.WARM_FAILED:
        log(f"scheduler: gate warm ended after {seconds:.0f}s without a pass (said above); not tried again on "
            f"{st.pair}")
    else:
        log(f"scheduler: gate warm ended exit={returncode} after {seconds:.0f}s, before it could say why; not tried "
            f"again on {st.pair}")


def regate_after_warm(children: dict, start) -> None:
    """A warm-up found the gate's baseline green: start `shaft gate regate` (runner.regate_held) when an instance has a
    code change held while it was red (docs/plans/PLAN-thelos-review-2.md M2). One at a time; nothing here raises into
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
        return 0
    code = service("scheduler", "on" if action == "on" else "off")
    log(f"autonomy {action}")
    print(f"autonomy {action}: " + (f"each instance wakes every WAKE_INTERVAL_MINUTES (default {WAKE_DEFAULT_MIN}), "
                                    f"sleep daily from {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d}" if action == "on"
                                    else "scheduler removed"))
    return code
