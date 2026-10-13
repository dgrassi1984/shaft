"""Autonomous schedule: a cycle for every instance every TICK_HOURS, and a sleep for all of them daily
from SLEEP_AT — kept by SHAFT's own scheduler service (com.shaft.scheduler, always running).

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

from .core import PROJECT, PYTHON, SUP_DIR, instance_names, log, paused
from .runner import Busy, is_busy, run_agent, talking

AGENTS = Path.home() / "Library" / "LaunchAgents"
TICK_HOURS = 2
SLEEP_AT = (4, 0)                       # hour, minute — daily; caught up later that day if missed
STATE = SUP_DIR / "schedule.json"       # last tick / last nightly sleep, written by the runs themselves
LEGACY_JOBS = ("com.shaft.tick", "com.shaft.sleep")


# Long-running services (KeepAlive): SHAFT's own model server and the OpenAI-compatible API.
SERVICES = {
    "model": {"label": "com.shaft.model",
              "args": [str(PROJECT / ".venv-bonsai" / "bin" / "python"), "-E", "-s", "-m", "supervisor.bonsai_server"]},
    "api": {"label": "com.shaft.api", "args": [PYTHON, "-E", "-s", "-m", "supervisor", "api", "serve"]},
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


def describe_schedule() -> str:
    """What an instance should know about when it runs — derived from the installed scheduler."""
    if not (AGENTS / f"{SERVICES['scheduler']['label']}.plist").exists():
        return ("Your autonomous schedule is off: you run only when Daniele talks to you or starts a cycle "
                "by hand.")
    return (f"The supervisor wakes you for an autonomous cycle every {TICK_HOURS} hours and for your sleep cycle "
            f"every day at {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d} (no cycle starts while Daniele is talking with you; a "
            f"missed sleep is made up later that day). You cannot change this clock; within a cycle, what you do "
            f"is up to you.")


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def record(event: str) -> None:
    """Called by the runs themselves ('tick', 'sleep'), so a manual run counts too."""
    st = _state()
    st[event] = datetime.now().isoformat(timespec="seconds")
    SUP_DIR.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def due(now: datetime, st: dict) -> list[str]:
    """What is due now: 'sleep' once per day from SLEEP_AT; 'tick' TICK_HOURS after the last one."""
    out = []
    sleep_from = now.replace(hour=SLEEP_AT[0], minute=SLEEP_AT[1], second=0, microsecond=0)
    last_sleep = datetime.fromisoformat(st["sleep"]) if st.get("sleep") else None
    if now >= sleep_from and (last_sleep is None or last_sleep < sleep_from):
        out.append("sleep")
    last_tick = datetime.fromisoformat(st["tick"]) if st.get("tick") else None
    if last_tick is None or now - last_tick >= timedelta(hours=TICK_HOURS):
        out.append("tick")
    return out


def serve(poll_s: int = 30) -> int:
    """The scheduler service: start what is due as child processes, one at a time; log every decision."""
    log(f"scheduler started (cycle every {TICK_HOURS} h, sleep daily from {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d})")
    told = ""
    while True:
        for job in due(datetime.now(), _state()):
            if (SUP_DIR / "paused").exists():
                break
            if job == "tick" and (busy := talking()):
                if told != f"talk {busy}":
                    log(f"scheduler: cycle due, waiting — the operator is in a conversation with {', '.join(busy)}")
                    told = f"talk {busy}"
                continue
            told = ""
            args = ["sleep", "--all"] if job == "sleep" else ["tick"]
            log(f"scheduler: starting {' '.join(args)}")
            started = time.monotonic()
            r = subprocess.run([PYTHON, "-E", "-s", "-m", "supervisor", *args], cwd=PROJECT,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if job == "tick":
                record("tick")   # also when it found nothing to do: the next one is TICK_HOURS later
            log(f"scheduler: {' '.join(args)} ended exit={r.returncode} after {time.monotonic() - started:.0f}s"
                + (f" — {r.stderr.strip()[-300:]}" if r.returncode and r.stderr.strip() else ""))
            if job == "sleep" and r.returncode not in (0, 3):   # 3: some paused; a crash must not loop
                record("sleep")
        time.sleep(poll_s)


def tick() -> int:
    """One autonomous round: a life cycle for every awake, idle instance (sequentially —
    the model serves one request at a time)."""
    record("tick")
    if (SUP_DIR / "paused").exists():
        log("tick skipped: all paused")
        return 0
    if (busy := talking()):
        log(f"tick skipped: the operator is in a conversation with {', '.join(busy)}")
        return 0
    worst = 0
    for name in instance_names():
        if paused(name):
            continue
        if is_busy(name):
            log(f"tick: {name} busy (conversation or another run) — skipped")
            continue
        try:
            code = run_agent(name, "live", ["live"], interactive=False, wait_lock=False)
        except Busy:
            continue
        worst = max(worst, code)
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
        print(f"last cycle round {st.get('tick', 'never')} · last nightly sleep {st.get('sleep', 'never')} · "
              f"due now: {', '.join(due(now, st)) or 'nothing'}")
        return 0
    code = service("scheduler", "on" if action == "on" else "off")
    log(f"autonomy {action}")
    print(f"autonomy {action}: " + (f"a cycle every {TICK_HOURS} h, sleep daily from {SLEEP_AT[0]:02d}:"
                                    f"{SLEEP_AT[1]:02d}" if action == "on" else "scheduler removed"))
    return code
