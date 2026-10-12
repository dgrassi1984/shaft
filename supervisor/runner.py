"""One sandboxed run of an instance, and everything the supervisor enforces around it.

Before: take the instance's run lock, snapshot its code, journal and core personality,
start a broker. After: stop the broker, restore the journal if it was rewritten
(append-only), restore the core personality if it changed too soon, run the test gate
on code changes (revert on failure), commit the instance's state to its branch.
"""
from __future__ import annotations

import fcntl
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from . import gate, sandbox
from .broker import Broker
from .core import (BROKER_MAX_CALLS, CORE_MIN_HOURS, CYCLE_TIMEOUT_S, HOME, LLM_PORTS, PROJECT, SLEEP_TIMEOUT_S, SUP_DIR,
                   commit_state, describe_knobs, git_wt, instance_root, journal_note, log, secrets_env)

CORE_RE = re.compile(r"^## Core\n(.*?)(?=^## |\Z)", re.M | re.S)


class Busy(Exception):
    pass


@contextmanager
def run_lock(name: str, wait: bool, kind: str = ""):
    """One run per instance at a time (a conversation and an autonomous cycle never overlap)."""
    locks = SUP_DIR / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    f = (locks / f"{name}.lock").open("a+")   # not "w": a failed attempt must not wipe the holder's info
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        f.close()
        raise Busy(name) from None
    try:
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()} {kind} {datetime.now():%H:%M}\n")
        f.flush()
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def busy_with(name: str) -> str | None:
    """'sleep since 18:06' if another run holds the instance, else None."""
    if not is_busy(name):
        return None
    try:
        _, kind, since = (SUP_DIR / "locks" / f"{name}.lock").read_text().split()
        return f"{kind} since {since}"
    except (OSError, ValueError):
        return "a run"


def is_busy(name: str) -> bool:
    try:
        with run_lock(name, wait=False):
            return False
    except Busy:
        return True


def _core(root: Path) -> str:
    """The core personality's traits (bullet lines) — captions and placeholders don't count."""
    p = root / "self" / "personality.md"
    m = CORE_RE.search(p.read_text()) if p.exists() else None
    return "\n".join(l.strip() for l in (m.group(1) if m else "").splitlines()
                     if l.lstrip().startswith(("- ", "* ")))


def _core_is_empty(text: str) -> bool:
    return not text.strip()


def enforce_journal(root: Path, before: str) -> str | None:
    """The journal is append-only. Returns a note if it had to be repaired."""
    path = root / "memory" / "journal.md"
    after = path.read_text() if path.exists() else ""
    if after.startswith(before):
        return None
    rejected = root / "memory" / f"journal.rejected-{datetime.now():%Y%m%d-%H%M%S}.md"
    rejected.write_text(after)
    path.write_text(before)
    return (f"My journal was rewritten instead of appended to during this run, so the supervisor "
            f"restored it. The rewritten version is kept in {rejected.relative_to(root)}.")


def enforce_core(name: str, root: Path, before: str) -> str | None:
    """Core personality changes at most once per CORE_MIN_HOURS. Returns a note if refused."""
    after = _core(root)
    if after == before:
        return None
    stamp = SUP_DIR / f"core-changed-{name}"
    last = datetime.fromisoformat(stamp.read_text().strip()) if stamp.exists() else None
    if last and not _core_is_empty(before) and datetime.now() - last < timedelta(hours=CORE_MIN_HOURS):
        p = root / "self" / "personality.md"
        caption = "*(values and temperament — change rarely)*"
        p.write_text(CORE_RE.sub(lambda _: f"## Core\n{caption}\n\n{before}\n\n", p.read_text(), count=1))
        wait = last + timedelta(hours=CORE_MIN_HOURS)
        return (f"I changed my core personality only {(datetime.now() - last).total_seconds() / 3600:.0f} h "
                f"after the last core change; the core may change at most every {CORE_MIN_HOURS} h, so the "
                f"supervisor restored it. Earliest next core change: {wait:%Y-%m-%d %H:%M}.")
    stamp.write_text(datetime.now().isoformat(timespec="seconds"))
    log(f"{name}: core personality changed")
    return None


def run_agent(name: str, kind: str, agent_args: list[str], interactive: bool, wait_lock: bool = True,
              output: list[str] | None = None) -> int:
    """Run the instance once. If `output` is given, the agent's stdout is captured into it."""
    if not (PROJECT / ".git").exists():
        print(f"{PROJECT} is not a git repository; the supervisor needs one to record changes.", file=sys.stderr)
        return 2
    root = instance_root(name)
    with run_lock(name, wait=wait_lock, kind=kind):
        return _run(name, root, kind, agent_args, interactive, output)


def _run(name: str, root: Path, kind: str, agent_args: list[str], interactive: bool,
         output: list[str] | None = None) -> int:
    log(f"{name}: start {kind} {describe_knobs()}")
    code_before = gate.snapshot(root)
    journal_path = root / "memory" / "journal.md"
    journal_before = journal_path.read_text() if journal_path.exists() else ""
    core_before = _core(root)

    broker = Broker(name, PROJECT, secrets_env(), SUP_DIR / "broker.log", max_calls=BROKER_MAX_CALLS)
    broker_url = broker.start()
    SUP_DIR.mkdir(exist_ok=True)
    prof = SUP_DIR / f"{name}.sb"
    prof.write_text(sandbox.profile(root, HOME, LLM_PORTS + [broker.port]))
    (root / ".tmp").mkdir(exist_ok=True)
    cmd = sandbox.wrap(prof, [sys.executable, "-E", "-s", "-m", "shaft", *agent_args])
    budget = None if interactive else (SLEEP_TIMEOUT_S if kind == "sleep" else CYCLE_TIMEOUT_S)
    extra = {"SHAFT_DEADLINE": f"{time.time() + budget - 60:.0f}"} if budget else {}
    env = sandbox.agent_env(os.environ, SHAFT_ROOT=str(root), TMPDIR=str(root / ".tmp"),
                            SHAFT_BROKER_URL=broker_url, SHAFT_BROKER_TOKEN=broker.token, **extra)
    # In a conversation Ctrl-C belongs to the agent (it ends the talk and writes its journal
    # entry). If the supervisor took it too, subprocess.run would kill the agent mid-entry.
    old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN) if interactive else None
    try:
        proc = subprocess.run(cmd, cwd=root, env=env, timeout=budget,
                              stdout=subprocess.PIPE if output is not None else None, text=True)
        code = proc.returncode
        if output is not None:
            output.append(proc.stdout or "")
    except subprocess.TimeoutExpired:
        log(f"{name}: {kind} killed after {budget}s budget")
        print(f"{name}: {kind} exceeded its {budget}s budget and was stopped.", file=sys.stderr)
        journal_note(root, "Supervisor: run stopped", f"My {kind} run was stopped after its {budget // 60}-minute "
                     "budget, before I finished it. Whatever I had already written to my files was kept.")
        code = 124
    except KeyboardInterrupt:
        code = 130
    finally:
        if old_sigint is not None:
            signal.signal(signal.SIGINT, old_sigint)
        broker.stop()
    log(f"{name}: end {kind} exit={code} broker_calls={broker.counts or {}}")

    for note in (enforce_journal(root, journal_before), enforce_core(name, root, core_before)):
        if note:
            log(f"{name}: {note}")
            journal_note(root, "Supervisor", note)

    touched = gate.changed(code_before, gate.snapshot(root))
    kept: list[str] = []
    if touched:
        ok, test_log = gate.run_tests(root, HOME)
        if ok:
            kept = touched
            log(f"{name}: code change passed tests: {touched}")
        else:
            gate.restore(root, code_before, touched)
            log(f"{name}: code change FAILED tests, reverted: {touched}")
            journal_note(root, "Supervisor: code change reverted",
                         "My changes to " + ", ".join(touched) + " were reverted because the tests "
                         "failed. Last lines of the test output:\n\n```\n" + test_log[-1500:] + "\n```")
    if not commit_state(name, kind, kept):
        return code or 1
    from . import metrics   # judged outcomes, calibration stats, probation of self-changes
    if kept:
        head = git_wt(root, "rev-parse", "HEAD").stdout.strip()
        metrics.start_probation(name, head, kept)
    if kind == "live":
        metrics.after_cycle(name)
        commit_state(name, "measurement", [])
    return code
