"""Stop what an instance is running now (`shaft kill <name>`, docs/plans/PLAN-kill-delete.md).

A kill pauses the instance first (nothing new starts: wakes, the nightly sleep, jobs, battery, trial replays all skip
a paused instance), then stops the instance's OWN processes — never the supervisor process that holds the run. The
holder then does what it does for any run stopped from outside: journal note, append-only and core checks, the gate on
its code changes, commit, run lock released (runner._run). Killing the holder instead would skip the gate and the
commit, and the run's unchecked code would become the next run's baseline.

Which processes are the instance's: those whose working folder is inside instances/<name>/ and whose parent is a
supervisor process (`python -m supervisor …`, or the terminal client `-m supervisor.talk_ui`; never the gate's test
runner `-m supervisor.gate_suite`, which holds no run) — the agent of a cycle
or a reply (a voice session's too, kept by the API between turns), a job's plugin, a broker `run` command — with
everything below them, plus an agent or plugin orphaned by its holder's death. The parent rule keeps the operator's
own shells in that folder safe. A terminal conversation's client is stopped last, once its reply has settled. Spared:
the gate checking a finished run (its test runners and plugin check, while no agent of that holder is alive: killing
them would fail the gate and revert the code), and a battery's exams — they run in a copy of the instance, not in its
folder; the battery process itself gets SIGTERM, its designed stop (it cuts the exam, kills what the exam left running
in the copy, and deletes the copy, battery.py).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .core import INSTANCES, SUP_DIR, log, set_paused

# Also the terminal client, supervisor.talk_ui. Not the gate's test runner (supervisor.gate_suite): it holds no run, and
# taken for a holder, what a test started in an instance's folder was stopped as that instance's (2026-10-27).
SUPERVISOR = re.compile(r"(^|\s)-m supervisor(?!\.gate_suite(\s|$))(\.\w+)?(\s|$)")
AGENT = re.compile(r"(^|\s)-m shaft(\s|$)")                       # the agent: python -m shaft live|talk|serve…
ORPHAN = re.compile(r"(^|\s)-m shaft(\.plugin_runner)?(\s|$)")       # an agent or plugin whose supervisor is gone
GATE = re.compile(r"(^|\s)-m (unittest|pytest|supervisor\.gate_suite)(\s|$)|shaft\.plugin_runner --check")
TERM_GRACE_S = 15
PS, LSOF = "/bin/ps", "/usr/sbin/lsof"


@dataclass
class Proc:
    pid: int
    ppid: int
    command: str
    cwd: str = ""


def processes() -> dict[int, Proc]:
    """Every process of this user: pid, parent, command line, working folder (lsof; one call for all of them).

    Both tools by absolute path: the launchd services run with PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
    (autonomy.py), which has no /usr/sbin — a bare "lsof" raised FileNotFoundError on the web panel's kill
    (2026-10-20). No working folders means no process is ever the instance's, so an empty answer is an error, not
    an empty kill."""
    out = subprocess.run([PS, "-x", "-o", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    procs: dict[int, Proc] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = Proc(int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else "")
    r = subprocess.run([LSOF, "-a", "-d", "cwd", "-u", str(os.getuid()), "-Fpn"], capture_output=True, text=True)
    cwd = r.stdout
    if not procs or "\nn" not in "\n" + cwd:
        raise RuntimeError(f"cannot list processes and their folders: {PS} gave {len(procs)}, {LSOF} exited "
                           f"{r.returncode}: {r.stderr.strip()[:300]}")
    pid = None
    for line in cwd.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid in procs:
            procs[pid].cwd = line[1:]
    return procs


def _inside(path: str, root: Path) -> bool:
    return bool(path) and (path == str(root) or path.startswith(str(root) + "/"))


def _battery_of(name: str, p: Proc) -> bool:
    words = p.command.split()
    return bool(SUPERVISOR.search(p.command)) and any(
        words[i:i + 3] == ["battery", "run", name] for i in range(len(words)))


def _talk_of(name: str, p: Proc) -> bool:
    """The terminal client of a conversation with `name` (shaft talk: supervisor.talk_ui <name>, or the plain one)."""
    words = p.command.split()
    return bool(SUPERVISOR.search(p.command)) and any(
        (w == "supervisor.talk_ui" or (w == "supervisor" and words[i + 1:i + 2] == ["talk"]))
        and name in words[i + 1:i + 3] for i, w in enumerate(words))


def talk_clients(name: str, procs: dict[int, Proc]) -> list[int]:
    return sorted(p.pid for p in procs.values() if _talk_of(name, p))


def targets(name: str, procs: dict[int, Proc], root: Path | None = None) -> tuple[list[int], list[int]]:
    """(the instance's own processes to stop, its battery processes to SIGTERM), from a process table. Pure."""
    root = (root or INSTANCES / name).resolve()
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    batteries = [p.pid for p in procs.values() if _battery_of(name, p)]

    def under_battery(pid: int) -> bool:
        seen = set()
        while pid in procs and pid not in seen:
            seen.add(pid)
            if pid in batteries:
                return True
            pid = procs[pid].ppid
        return False

    heads = []
    for p in procs.values():
        parent = procs.get(p.ppid)
        orphan = (parent is None or p.ppid == 1) and bool(ORPHAN.search(p.command))   # its run's holder died
        if ((not orphan and (parent is None or not SUPERVISOR.search(parent.command))) or SUPERVISOR.search(p.command)
                or not _inside(p.cwd, root) or under_battery(p.pid)):
            continue
        if GATE.search(p.command) and not any(AGENT.search(procs[s].command) for s in children.get(p.ppid, [])
                                              if s in procs):
            continue   # the gate checking a run whose agent is gone: let it finish
        heads.append(p.pid)
    out: list[int] = []
    stack = list(heads)
    while stack:   # each head with everything below it, whatever its folder
        pid = stack.pop()
        if pid not in out:
            out.append(pid)
            stack.extend(children.get(pid, []))
    return sorted(out), sorted(batteries)


def _signal(pids: list[int], sig: int) -> list[int]:
    sent = []
    for pid in pids:
        try:
            os.kill(pid, sig)
            sent.append(pid)
        except ProcessLookupError:
            pass
    return sent


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


STRAY_GRACE_S = 2   # how long a stray has to exit on SIGTERM before SIGKILL


def end_left_in(folder: Path, orphans_only: bool = True) -> list[int]:
    """End what a finished run or exam left working in `folder`, outside its process group; SIGTERM, then SIGKILL
    after STRAY_GRACE_S. Never this process or its ancestors, nor a supervisor process. Returns the pids it signalled;
    [] when the process table cannot be read (logged: FAILED). One helper, two folders:

    - `orphans_only` (an instance's folder, runner._end_run): a process whose parent is gone (re-parented to
      launchd, pid 1) — started in a session of its own (setsid) once its starter ended — whose working folder is
      inside `folder`, with everything below it. A process whose parent is alive is not a stray: the operator's own
      shells there keep theirs. One that also left the folder (chdir) is not found; the sandbox is the boundary.
    - not (an exam's copy, battery: a random folder, never reused, where nobody works): every process working inside
      it, everything below each, and each one's ancestors while they share its session or process group — a parent
      in a session of its own that chdir'ed out of the copy while its child stayed in it was missed by the orphan
      rule (Codex, final review of the integration)."""
    import time
    top = Path(folder).resolve()
    try:
        procs = processes()
    except (RuntimeError, OSError) as e:
        log(f"FAILED to list the processes left in {top}: {e}")
        return []
    mine, pid = set(), os.getpid()
    while pid in procs and pid not in mine:
        mine.add(pid)
        pid = procs[pid].ppid
    mine.add(os.getpid())
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    spared = lambda p: p.pid in mine or p.pid == 1 or bool(SUPERVISOR.search(p.command))   # noqa: E731
    inside = [p.pid for p in procs.values() if _inside(p.cwd, top) and not spared(p)
              and (not orphans_only or p.ppid == 1)]
    heads = list(inside)
    if not orphans_only:
        for pid in inside:
            at = procs[pid]
            while (up := procs.get(at.ppid)) is not None and not spared(up) and _same_session(up.pid, at.pid):
                heads.append(up.pid)
                at = up
    found: list[int] = []
    stack = list(heads)
    while stack:                         # down from each: the same exclusions, and nothing below an excluded one
        pid = stack.pop()
        if pid not in found and pid in procs and not spared(procs[pid]):
            found.append(pid)
            stack.extend(children.get(pid, []))
    if not found:
        return []
    log(f"{top}: ending {len(found)} process(es) left running there outside their run's process group: "
        + "; ".join(f"{p} {procs[p].command[:60]}" for p in found if p in procs))
    _signal(found, signal.SIGTERM)
    deadline = time.monotonic() + STRAY_GRACE_S
    while time.monotonic() < deadline and any(_alive(p) for p in found):
        time.sleep(0.05)
    _signal([p for p in found if _alive(p)], signal.SIGKILL)
    return found


def _same_session(a: int, b: int) -> bool:
    """Whether two processes share a session or a process group (False for one that is gone)."""
    try:
        return os.getsid(a) == os.getsid(b) or os.getpgid(a) == os.getpgid(b)
    except (ProcessLookupError, PermissionError):
        return False


def running(name: str) -> list[str]:
    """What still holds or works for the instance: its run (lock), replies waiting for it, a terminal conversation,
    its own processes, a battery, a trial replay. Empty when it is free."""
    from .runner import _held, busy_with, replying
    out = []
    if b := busy_with(name):
        out.append(b)
    if r := replying(name):
        out.append(f"{len(r)} repl{'y' if len(r) == 1 else 'ies'} waiting for it")
    talks = [t for t, kind in _held(SUP_DIR / "conversations" / name).items() if kind == "talk"]
    if talks:
        out.append("a terminal conversation (shaft talk)")
    procs = processes()
    mine, batteries = targets(name, procs)
    if batteries:
        out.append("the battery")
    if mine:
        out.append(f"{len(mine)} process(es) in its folder: "
                   + ", ".join(f"{p} {procs[p].command.split(' -m ')[-1][:40]}" for p in mine[:4] if p in procs))
    jobs = [p for p in procs.values() if SUPERVISOR.search(p.command)
            and re.search(rf"(^|\s)job run {re.escape(name)}(\s|$)", p.command)]
    if jobs:
        out.append(f"{len(jobs)} job(s)")
    if replaying(name, procs):
        out.append("a trial replay")
    return out


def replaying(name: str, procs: dict[int, Proc]) -> list[int]:
    """The agents of the trial worker's replays of this instance's trials: they run in scratch copies
    (<tmp>/shaft-trial-…/<name>/, trial.replay_arm). A kill does not signal them — a replay cut from outside would be
    judged as a failed run — the worker stops them itself once the instance is paused (trial.run_pair)."""
    copy = re.compile(rf"/shaft-trial-[^/]+/{re.escape(name)}(/|$)")
    return sorted(p.pid for p in procs.values() if AGENT.search(p.command) and copy.search(p.cwd))


def runner_busy(name: str) -> bool:
    from .runner import busy_with
    return busy_with(name) is not None


def kill(name: str, wait_s: float = 120, say=print) -> int:
    """Pause the instance and stop what it runs now. 0: it is free; 1: something still holds it after `wait_s`."""
    set_paused(name, True, reason="a kill")
    log(f"kill {name} by operator: paused, stopping what it runs")
    stopped: dict[int, str] = {}
    termed: dict[int, float] = {}
    t0 = time.monotonic()
    while True:
        procs = processes()
        mine, batteries = targets(name, procs)
        for pid in _signal([p for p in mine + batteries if p not in termed], signal.SIGTERM):
            termed[pid] = time.monotonic()
            stopped[pid] = procs[pid].command
        if not mine and not runner_busy(name):
            # The terminal client last, once its reply is settled: stopping it mid-reply would skip that settle.
            for pid in _signal([p for p in talk_clients(name, procs) if p not in termed], signal.SIGTERM):
                termed[pid] = time.monotonic()
                stopped[pid] = procs[pid].command
        overdue = [p for p, at in termed.items() if p in mine and time.monotonic() - at > TERM_GRACE_S and _alive(p)]
        for pid in _signal(overdue, signal.SIGKILL):
            log(f"kill {name}: {pid} ignored SIGTERM for {TERM_GRACE_S}s, SIGKILL")
            termed[pid] = float("inf")
        left = running(name)
        if not left:
            break
        if time.monotonic() - t0 > wait_s:
            what = ", ".join(_short(c) for c in stopped.values()) or "nothing of its own"
            say(f"{name} paused; stopped {what}; after {wait_s:.0f}s it is still settling: {'; '.join(left)} "
                f"(shaft status {name})")
            log(f"kill {name}: still held after {wait_s:.0f}s: {'; '.join(left)}")
            return 1
        time.sleep(0.5)
    what = ", ".join(f"{_short(c)} ({p})" for p, c in stopped.items())
    say(f"killed {name}: " + (f"stopped {what}" if stopped else "nothing was running")
        + f"; paused (shaft resume {name})")
    log(f"kill {name}: done — " + (f"stopped {what}" if stopped else "nothing was running"))
    return 0


def _short(command: str) -> str:
    """'shaft live', 'battery run wren', 'shaft.plugin_runner news' — the part after the interpreter's -m."""
    return command.split(" -m ", 1)[-1].strip()[:60] if " -m " in command else command.split("/")[-1][:60]
