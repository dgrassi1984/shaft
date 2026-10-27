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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import gate, localmodel, preferences
from .core import (PROJECT, PYTHON, SUP_DIR, instance_config, instance_names, log, paused, set_update_queued,
                   update_queued)
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
    until = preferences.self_paused(name, now)
    last = (_state().get("wakes") or {}).get(name)
    if not last and not until:
        return "due"
    at = datetime.fromisoformat(last) + interval if last else now
    at = max(at, until) if until else at
    return "due" if at <= now else f"{at:%H:%M}"


def _until(at: datetime, now: datetime | None = None) -> str:
    """When a pause ends, as the operator reads it: the time, and the date when it is not today."""
    return f"{at:%H:%M}" if at.date() == (now or datetime.now()).date() else f"{at:%Y-%m-%d %H:%M}"


def next_cycle(name: str) -> str:
    """Where a message left for the instance's next cycle stands: what the web UI tells the operator who sends one."""
    if paused(name):
        return f"{name} is paused: it reads this in its first cycle after you resume it"
    if (until := preferences.self_paused(name)):
        return f"{name} asked for a pause until {_until(until)}: it reads this in its first cycle after that"
    interval = wake_interval(name)
    at = next_wake(name, interval=interval)
    if at == "never":
        return f"{name} has no scheduled cycles (WAKE_INTERVAL_MINUTES=0): it reads this when you next talk"
    why = _awake(name)
    if at == "due" and why:              # a wake that comes due while it is awake is skipped, not queued (serve)
        at = f"{datetime.now() + interval:%H:%M}"
    when = "due now" if at == "due" else f"≈{at}"
    if why.startswith("live"):
        return f"a cycle is running and read its inbox when it began: this waits for the next one ({when})"
    return f"for {name}'s next cycle ({when})" + (f", after what it is doing now ({why})" if why else "")


def _every(interval: timedelta) -> str:
    m = int(interval.total_seconds() // 60)
    return f"{m // 60} hours" if m % 60 == 0 and m >= 120 else "hour" if m == 60 else f"{m} minutes"


def describe_schedule(name: str, config: dict[str, str] | None = None) -> str:
    """What an instance should know about when it runs — from the scheduler and its own config.env."""
    if not (AGENTS / f"{SERVICES['scheduler']['label']}.plist").exists():
        return ("Your autonomous schedule is off: you run only when the operator talks to you or starts a cycle "
                "by hand. You can ask for a pause of up to 24 hours with request_pause.")
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
            f"(a missed sleep is made up later that day).{night} You can ask for a pause of up to 24 hours with "
            "request_pause. Within a cycle, what you do is up to you.")


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


def decide_wakes(now: datetime, st: dict, names: list[str], interval_of, awake_of, *,
                 self_paused_of=None) -> tuple[list[str], dict[str, str]]:
    """(instances to wake now, {instance: why its due wake is skipped}). Pure: the scheduler's rule."""
    start, skip = [], {}
    for name in names:
        interval = interval_of(name)
        last = (st.get("wakes") or {}).get(name)
        if interval is None or (last and now - datetime.fromisoformat(last) < interval):
            continue
        until = self_paused_of(name, now) if self_paused_of else None
        why = f"paused at its own request until {_until(until, now)}" if until and until > now else awake_of(name)
        if why:
            skip[name] = why
        else:
            start.append(name)
    return start, skip


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
    stop_children_on_signal(children)
    while True:
        for job, (proc, started) in list(children.items()):
            if proc.poll() is not None and job == "gate warm":      # it says how it ended itself (warm_ended)
                warm_ended(warm, proc.returncode, time.monotonic() - started)
                del children[job]
            elif proc.poll() is not None:
                err = proc.stderr.read().strip()[-300:] if proc.stderr else ""
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
        if not (SUP_DIR / "paused").exists():
            st, now = _state(), datetime.now()
            _batteries(children, now, sleep_due(now, st))
            if sleep_due(now, st) and "sleep" not in children:
                children["sleep"] = (_start(["sleep", "--all"]), time.monotonic())
            names = [n for n in instance_names() if not paused(n) and not updating(n, children)]   # not skipped:
            # a wake due during an update starts right after it
            start, skip = decide_wakes(now, st, names, wake_interval,
                                       lambda n: "waking already" if f"wake {n}" in children else _awake(n),
                                       self_paused_of=preferences.self_paused)
            for name, why in skip.items():
                record("wake", name)   # skipped, not queued: the next wake is a full interval later
                log(f"scheduler: {name}'s wake skipped — "
                    + (why if why.startswith("paused at its own request") else f"already awake ({why})"))
            for name in start:
                record("wake", name)
                children[f"wake {name}"] = (_start(["wake", name]), time.monotonic())
            from . import jobs   # the instances' own plugins on their timers, and the wakes those jobs asked for
            jobs.tick(children, now, _start, instance_names(),
                      lambda n: "waking already" if f"wake {n}" in children else
                      "being updated" if updating(n, children) else
                      "paused at its own request" if preferences.self_paused(n, now) else _awake(n))
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


def _start(args: list[str]) -> subprocess.Popen:
    log(f"scheduler: starting {' '.join(args)}")
    return subprocess.Popen([PYTHON, "-E", "-s", "-m", "supervisor", *args], cwd=PROJECT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def wake(name: str) -> int:
    """One scheduled wake: a life cycle, unless the instance is awake already (then it is skipped)."""
    # Recheck in the child: a pause may have begun after the scheduler selected this wake.
    if (until := preferences.self_paused(name)):
        log(f"{name}: wake skipped — paused at its own request until {_until(until)}")
        return 0
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
    if (until := preferences.self_paused(name)):
        raise RuntimeError(f"{name} is paused at its own request until {_until(until)} (shaft prefs {name} lift)")
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
