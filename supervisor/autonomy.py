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
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import localmodel
from .core import PROJECT, PYTHON, SUP_DIR, instance_config, instance_names, log, paused
from .runner import Busy, is_busy, run_agent

AGENTS = Path.home() / "Library" / "LaunchAgents"
WAKE_DEFAULT_MIN = 120                  # minutes between wakes when config.env does not say
WAKE_FLOOR_MIN = 10                     # config.env is the instance's own file: no faster than this
SLEEP_AT = (4, 0)                       # hour, minute — daily; caught up later that day if missed
STATE = SUP_DIR / "schedule.json"       # last wake per instance / last nightly sleep, written by the runs
LEGACY_JOBS = ("com.shaft.tick", "com.shaft.sleep")
BOOTSTRAP_TRIES = 20                    # x 0.5 s: launchd lets go of a booted-out job a moment after bootout returns


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


def _every(interval: timedelta) -> str:
    m = int(interval.total_seconds() // 60)
    return f"{m // 60} hours" if m % 60 == 0 and m >= 120 else "hour" if m == 60 else f"{m} minutes"


def describe_schedule(name: str, config: dict[str, str] | None = None) -> str:
    """What an instance should know about when it runs — from the scheduler and its own config.env."""
    if not (AGENTS / f"{SERVICES['scheduler']['label']}.plist").exists():
        return ("Your autonomous schedule is off: you run only when the operator talks to you or starts a cycle "
                "by hand.")
    interval = wake_interval(name, config)
    wakes = (f"wakes you for an autonomous cycle every {_every(interval)} (WAKE_INTERVAL_MINUTES in your "
             f"config.env — change it with set_config — at least {wake_floor(name)}; a wake that comes while you are already awake — a cycle, a "
             f"conversation, your sleep — is skipped)") if interval else \
        "does not wake you for cycles (WAKE_INTERVAL_MINUTES=0 in your config.env; change it with set_config)"
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
    processes (an instance on a cloud model does not wait behind one on the local model); log every decision."""
    log(f"scheduler started (wakes per instance, WAKE_INTERVAL_MINUTES, default {WAKE_DEFAULT_MIN}; "
        f"sleep daily from {SLEEP_AT[0]:02d}:{SLEEP_AT[1]:02d})")
    from .version import record_service   # the settings it holds (settings.not_live)
    record_service("scheduler", {})
    from .browser import startup_note
    if (note := startup_note()):
        log(note)
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
            _batteries(children, now, sleep_due(now, st))
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
            from . import jobs   # the instances' own plugins on their timers, and the wakes those jobs asked for
            jobs.tick(children, now, _start, instance_names(),
                      lambda n: "waking already" if f"wake {n}" in children else _awake(n))
            if "trials" not in children:   # trials of proposed changes; on the local model only in its idle time
                from . import trial
                if trial.has_work():
                    children["trials"] = (_start(["trial", "work"]), time.monotonic())
        time.sleep(poll_s)


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
