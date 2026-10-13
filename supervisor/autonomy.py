"""Autonomous schedule: each instance wakes for a cycle every WAKE_INTERVAL_MINUTES (its own config.env,
default 120), and all of them sleep daily from SLEEP_AT — kept by SHAFT's own scheduler service
(com.shaft.scheduler, always running). A wake that comes due while the instance is already awake (a cycle,
a conversation, its sleep) is skipped, not queued; the next one is a full interval later.

It used to be two timed launchd jobs. On 2026-10-13 macOS put the login session's launchd domain in
"on-demand-only mode" while the Mac stayed awake: timed jobs stopped starting ("pending spawn"), and the
03:25 cycle and the 04:00 sleep silently never happened. A running service starts its own children, so
the clock no longer depends on launchd timers; a missed sleep is caught up later the same day.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .core import PROJECT, PYTHON, SUP_DIR, instance_config, instance_names, log, paused
from .runner import Busy, is_busy, open_conversations, run_agent, talking

AGENTS = Path.home() / "Library" / "LaunchAgents"
WAKE_DEFAULT_MIN = 120                  # minutes between wakes when config.env does not say
WAKE_FLOOR_MIN = 10                     # config.env is the instance's own file: no faster than this
SLEEP_AT = (4, 0)                       # hour, minute — daily; caught up later that day if missed
STATE = SUP_DIR / "schedule.json"       # last wake per instance / last nightly sleep, written by the runs
LEGACY_JOBS = ("com.shaft.tick", "com.shaft.sleep")


# Long-running services (KeepAlive): SHAFT's own model server and the OpenAI-compatible API.
SERVICES = {
    "model": {"label": "com.shaft.model",
              "args": [str(PROJECT / ".venv-bonsai" / "bin" / "python"), "-E", "-s", "-m", "supervisor.bonsai_server"]},
    "api": {"label": "com.shaft.api", "args": [PYTHON, "-E", "-s", "-m", "supervisor", "api", "serve"]},
    "embed": {"label": "com.shaft.embed",
              "args": [str(PROJECT / ".venv-bonsai" / "bin" / "python"), "-E", "-s", "-m", "supervisor.embed_server"]},
    "scheduler": {"label": "com.shaft.scheduler",
                  "args": [PYTHON, "-E", "-s", "-m", "supervisor", "autonomy", "serve"]},
}


def service(name: str, action: str) -> int:
    svc = SERVICES[name]
    label, domain = svc["label"], f"gui/{os.getuid()}"
    path = AGENTS / f"{label}.plist"
    if action == "status":
        r = _launchctl("print", f"{domain}/{label}")
        state = next((l.strip() for l in r.stdout.splitlines() if l.strip().startswith("state =")), "not loaded")
        pid = next((l.strip() for l in r.stdout.splitlines() if l.strip().startswith("pid =")), "")
        print(f"{label:16} {state} {pid} · {'installed' if path.exists() else 'not installed'}")
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
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30}))
    r = _launchctl("bootstrap", domain, str(path))
    if r.returncode != 0:
        print(f"{label}: launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    _launchctl("kickstart", f"{domain}/{label}")   # an explicit start works even in on-demand-only mode
    log(f"service {name} on")
    print(f"{label} installed: starts at login, restarted if it stops")
    return 0


def wake_interval(name: str) -> timedelta | None:
    """The instance's WAKE_INTERVAL_MINUTES (config.env), at least WAKE_FLOOR_MIN; 0 = no scheduled wakes."""
    raw = instance_config(name).get("WAKE_INTERVAL_MINUTES", "")
    try:
        minutes = int(raw) if raw else WAKE_DEFAULT_MIN
    except ValueError:
        log(f"{name}: WAKE_INTERVAL_MINUTES={raw!r} is not a number of minutes; using {WAKE_DEFAULT_MIN}")
        minutes = WAKE_DEFAULT_MIN
    return None if minutes <= 0 else timedelta(minutes=max(minutes, WAKE_FLOOR_MIN))


def _every(interval: timedelta) -> str:
    m = int(interval.total_seconds() // 60)
    return f"{m // 60} hours" if m % 60 == 0 and m >= 120 else "hour" if m == 60 else f"{m} minutes"


def describe_schedule(name: str) -> str:
    """What an instance should know about when it runs — from the scheduler and its own config.env."""
    if not (AGENTS / f"{SERVICES['scheduler']['label']}.plist").exists():
        return ("Your autonomous schedule is off: you run only when Daniele talks to you or starts a cycle "
                "by hand.")
    interval = wake_interval(name)
    wakes = (f"wakes you for an autonomous cycle every {_every(interval)} (WAKE_INTERVAL_MINUTES in your "
             f"config.env — change it with set_config — at least {WAKE_FLOOR_MIN}; a wake that comes while you are already awake — a cycle, a "
             f"conversation, your sleep — is skipped)") if interval else \
        "does not wake you for cycles (WAKE_INTERVAL_MINUTES=0 in your config.env; change it with set_config)"
    return (f"The supervisor {wakes}, and runs your sleep cycle every day at {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d} "
            f"(a missed sleep is made up later that day). Within a cycle, what you do is up to you.")


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def record(event: str, name: str = "") -> None:
    """Called by the runs themselves ('sleep'; 'wake' per instance), so a manual run counts too."""
    st = _state()
    now = datetime.now().isoformat(timespec="seconds")
    if event == "wake":
        st.setdefault("wakes", {})[name] = now
    else:
        st[event] = now
    SUP_DIR.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def sleep_due(now: datetime, st: dict) -> bool:
    """Once per day from SLEEP_AT, caught up later that day if missed."""
    sleep_from = now.replace(hour=SLEEP_AT[0], minute=SLEEP_AT[1], second=0, microsecond=0)
    last_sleep = datetime.fromisoformat(st["sleep"]) if st.get("sleep") else None
    return now >= sleep_from and (last_sleep is None or last_sleep < sleep_from)


def decide_wakes(now: datetime, st: dict, names: list[str], interval_of, awake_of) -> tuple[list[str], dict[str, str]]:
    """(instances to wake now, {instance: why its due wake is skipped}). Pure: the scheduler's rule."""
    start, skip = [], {}
    for name in names:
        interval = interval_of(name)
        last = (st.get("wakes") or {}).get(name)
        if interval is None or (last and now - datetime.fromisoformat(last) < interval):
            continue
        why = awake_of(name)
        if why:
            skip[name] = why
        else:
            start.append(name)
    return start, skip


def _awake(name: str) -> str:
    """Why the instance counts as awake right now ('' if it does not)."""
    from .runner import busy_with
    if (b := busy_with(name)):
        return b
    if (convs := open_conversations(name)):
        return f"in a conversation ({', '.join(sorted(set(convs.values())))})"
    return ""


def serve(poll_s: int = 30) -> int:
    """The scheduler service: wake each instance on its own interval and run the nightly sleep, as child
    processes (an instance on a cloud model does not wait behind one on the local model); log every decision."""
    log(f"scheduler started (wakes per instance, WAKE_INTERVAL_MINUTES, default {WAKE_DEFAULT_MIN}; "
        f"sleep daily from {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d})")
    children: dict[str, tuple[subprocess.Popen, float]] = {}
    while True:
        for job, (proc, started) in list(children.items()):
            if proc.poll() is not None:
                err = proc.stderr.read().strip()[-300:] if proc.stderr else ""
                log(f"scheduler: {job} ended exit={proc.returncode} after {time.monotonic() - started:.0f}s"
                    + (f" — {err}" if proc.returncode and err else ""))
                if job == "sleep" and proc.returncode not in (0, 3):   # 3: some paused; a crash must not loop
                    record("sleep")
                del children[job]
        if not (SUP_DIR / "paused").exists():
            st, now = _state(), datetime.now()
            if sleep_due(now, st) and "sleep" not in children:
                children["sleep"] = (_start(["sleep", "--all"]), time.monotonic())
            names = [n for n in instance_names() if not paused(n)]
            start, skip = decide_wakes(now, st, names, wake_interval,
                                       lambda n: "waking already" if f"wake {n}" in children else _awake(n))
            for name, why in skip.items():
                record("wake", name)   # skipped, not queued: the next wake is a full interval later
                log(f"scheduler: {name}'s wake skipped — already awake ({why})")
            for name in start:
                record("wake", name)
                children[f"wake {name}"] = (_start(["wake", name]), time.monotonic())
        time.sleep(poll_s)


def _start(args: list[str]) -> subprocess.Popen:
    log(f"scheduler: starting {' '.join(args)}")
    return subprocess.Popen([PYTHON, "-E", "-s", "-m", "supervisor", *args], cwd=PROJECT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def wake(name: str) -> int:
    """One scheduled wake: a life cycle, unless the instance is awake already (then it is skipped)."""
    record("wake", name)
    try:
        return run_agent(name, "live", ["live"], interactive=False, wait_lock=False)
    except Busy:
        log(f"{name}: wake skipped — it is already awake ({_awake(name) or 'another run'})")
        return 0


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
            print(f"{name:10} {every:18} last wake {last or 'never'} · {nxt}")
        print(f"nightly sleep from {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d} · last {st.get('sleep', 'never')}"
              + (" · due now" if sleep_due(now, st) else ""))
        return 0
    code = service("scheduler", "on" if action == "on" else "off")
    log(f"autonomy {action}")
    print(f"autonomy {action}: " + (f"each instance wakes every WAKE_INTERVAL_MINUTES (default {WAKE_DEFAULT_MIN}), "
                                    f"sleep daily from {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d}" if action == "on"
                                    else "scheduler removed"))
    return code
