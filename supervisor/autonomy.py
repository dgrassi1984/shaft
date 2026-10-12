"""Autonomous schedule: launchd runs `shaft tick` every 2 h and `shaft sleep --all` nightly."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .core import PROJECT, SUP_DIR, instance_names, log, paused
from .runner import Busy, is_busy, run_agent

AGENTS = Path.home() / "Library" / "LaunchAgents"
JOBS = {
    "com.shaft.tick": {"args": ["tick"], "schedule": {"StartInterval": 7200}},
    "com.shaft.sleep": {"args": ["sleep", "--all"], "schedule": {"StartCalendarInterval": {"Hour": 4, "Minute": 0}}},
}


def tick() -> int:
    """One autonomous round: a life cycle for every awake, idle instance (sequentially —
    the model serves one request at a time)."""
    if (SUP_DIR / "paused").exists():
        log("tick skipped: all paused")
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


def _plist(label: str, job: dict) -> dict:
    return {
        "Label": label,
        "ProgramArguments": [sys.executable, "-E", "-s", "-m", "supervisor", *job["args"]],
        "WorkingDirectory": str(PROJECT),
        "StandardOutPath": str(SUP_DIR / f"{label}.log"),
        "StandardErrorPath": str(SUP_DIR / f"{label}.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "RunAtLoad": False,
        **job["schedule"],
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def autonomy(action: str) -> int:
    domain = f"gui/{os.getuid()}"
    if action == "status":
        for label in JOBS:
            loaded = _launchctl("print", f"{domain}/{label}").returncode == 0
            installed = (AGENTS / f"{label}.plist").exists()
            print(f"{label:16} {'loaded' if loaded else 'not loaded'} · "
                  f"{'installed' if installed else 'not installed'}")
        return 0
    for label, job in JOBS.items():
        path = AGENTS / f"{label}.plist"
        _launchctl("bootout", f"{domain}/{label}")
        if action == "on":
            SUP_DIR.mkdir(exist_ok=True)
            AGENTS.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps(_plist(label, job)))
            r = _launchctl("bootstrap", domain, str(path))
            if r.returncode != 0:
                print(f"{label}: launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
                return 1
        else:
            path.unlink(missing_ok=True)
    log(f"autonomy {action}")
    print(f"autonomy {action}: " + ("tick every 2 h, sleep daily at 04:00" if action == "on" else "jobs removed"))
    return 0
