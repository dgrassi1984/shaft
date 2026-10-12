"""One sandboxed run of an instance, and everything the supervisor enforces around it.

Before: take the instance's run lock, snapshot its code, journal and core personality,
start a broker. After: stop the broker, restore the journal if it was rewritten
(append-only), restore the core personality if it changed too soon, run the test gate
on code changes (revert on failure), commit the instance's state to its branch.

A terminal conversation is different: its process stays up while it waits for the operator,
so it holds the lock (and is checked and committed) one reply at a time — an open but quiet
`shaft talk` never blocks the API or a write-up. Open conversations register themselves
(open_conversations): autonomous cycles wait while one is open, and no journal write-up
closes a conversation that is still going on.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from . import gate, sandbox
from .broker import Broker
from .core import (API_TALKS, BROKER_MAX_CALLS, CORE_MIN_HOURS, CYCLE_TIMEOUT_S, HOME, INSTANCES, LLM_PORTS, PROJECT, PYTHON,
                   SLEEP_TIMEOUT_S, SUP_DIR, UnsafePath,
                   commit_state, describe_knobs, git_wt, instance_root, journal_note, log, safe_mkdir,
                   safe_write, secrets_env)

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


def judge_ok() -> bool:
    from . import judge
    return judge.available()


def spawn_recover(name: str) -> None:
    """Write the journal entry for a finished conversation in the background — leaving is instant."""
    SUP_DIR.mkdir(exist_ok=True)
    out = (SUP_DIR / "recover.log").open("a")
    subprocess.Popen([PYTHON, "-E", "-s", "-m", "supervisor", "recover", name], cwd=PROJECT,
                     stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    log(f"{name}: background journal write-up started")


def request_yield(names: list[str]) -> None:
    """Ask running autonomous cycles to wrap up at their next turn (flag inside each instance)."""
    for n in names:
        try:
            safe_write(INSTANCES / n, ".tmp/yield", datetime.now().isoformat(timespec="seconds"))
        except UnsafePath as e:
            log(f"{n}: could not ask it to yield: {e}")
            continue
        log(f"{n}: asked to yield the model (operator conversation)")


class Presence:
    """An open terminal conversation, registered for as long as it lasts. The file's lock is held by
    the conversation's process, so a crashed conversation stops counting at once."""

    def __init__(self, name: str, talk_id: str, kind: str = "talk"):
        self.path = SUP_DIR / "conversations" / name / f"{talk_id}.{kind}"
        self.f = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            f = self.path.open("a+")
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                mine = os.stat(self.path).st_ino == os.fstat(f.fileno()).st_ino
            except FileNotFoundError:
                mine = False
            if mine:
                self.f = f
                return self
            f.close()   # a cleaner removed it between our open and lock: register again

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def open_conversations(name: str) -> dict[str, str]:
    """{talk_id: 'talk' | 'api'} for the instance's conversations still going on: terminal talks whose
    client is running, and conversations the running API has not closed and written up yet."""
    found: dict[str, str] = {}
    folder = SUP_DIR / "conversations" / name
    for p in sorted(folder.glob("*.*")) if folder.is_dir() else []:
        try:
            f = p.open("r")
        except FileNotFoundError:
            continue
        with f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                found[p.stem] = p.suffix[1:]
                continue
            try:   # nobody holds it: its conversation ended without cleaning up
                if os.stat(p).st_ino == os.fstat(f.fileno()).st_ino:
                    p.unlink()
            except FileNotFoundError:
                pass
            fcntl.flock(f, fcntl.LOCK_UN)
    from .version import service
    if service("api"):   # the API decides when its conversations end (idle time) — not this process's knob
        try:
            talks = json.loads(API_TALKS.read_text()) if API_TALKS.exists() else {}
        except (OSError, ValueError):
            talks = {}
        for t in talks.values():
            if t.get("model") == name and not t.get("written"):
                found[t["talk_id"]] = "api"
    return found


def talking() -> list[str]:
    """Instances in a conversation with the operator (open, even if quiet right now)."""
    from .core import instance_names
    return [n for n in instance_names()
            if open_conversations(n) or (busy_with(n) or "").startswith(("talk", "api"))]


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
    rejected = f"memory/journal.rejected-{datetime.now():%Y%m%d-%H%M%S}.md"
    safe_write(root, rejected, after)
    safe_write(root, "memory/journal.md", before)
    return (f"My journal was rewritten instead of appended to during this run, so the supervisor "
            f"restored it. The rewritten version is kept in {rejected}.")


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
        safe_write(root, "self/personality.md",
                   CORE_RE.sub(lambda _: f"## Core\n{caption}\n\n{before}\n\n", p.read_text(), count=1))
        wait = last + timedelta(hours=CORE_MIN_HOURS)
        return (f"I changed my core personality only {(datetime.now() - last).total_seconds() / 3600:.0f} h "
                f"after the last core change; the core may change at most every {CORE_MIN_HOURS} h, so the "
                f"supervisor restored it. Earliest next core change: {wait:%Y-%m-%d %H:%M}.")
    stamp.write_text(datetime.now().isoformat(timespec="seconds"))
    log(f"{name}: core personality changed")
    return None


def run_agent(name: str, kind: str, agent_args: list[str], interactive: bool, wait_lock: bool = True,
              output: list[str] | None = None, driver=None) -> int:
    """Run the instance once. If `output` is given, the agent's stdout is captured into it.
    If `driver` is given, the agent runs in its own process group with pipes on stdin/stdout/stderr
    and driver(proc) talks to it; it returns when the run ends."""
    if not (PROJECT / ".git").exists():
        print(f"{PROJECT} is not a git repository; the supervisor needs one to record changes.", file=sys.stderr)
        return 2
    root = instance_root(name)
    with run_lock(name, wait=wait_lock, kind=kind):
        return _run(name, root, kind, agent_args, interactive, output, driver)


def _snapshot(root: Path) -> dict:
    journal, pers = root / "memory" / "journal.md", root / "self" / "personality.md"
    return {"code": gate.snapshot(root), "journal": journal.read_text() if journal.exists() else "",
            "personality": pers.read_text() if pers.exists() else "", "core": _core(root)}


def _launch(name: str, root: Path, kind: str, agent_args: list[str], budget: int | None):
    """Start the run's broker and return (broker, command, environment) for the agent."""
    broker = Broker(name, PROJECT, secrets_env(), SUP_DIR / "broker.log", max_calls=BROKER_MAX_CALLS)
    broker_url = broker.start()
    SUP_DIR.mkdir(exist_ok=True)
    prof = SUP_DIR / f"{name}.sb"
    prof.write_text(sandbox.profile(root, HOME, LLM_PORTS + [broker.port]))
    safe_mkdir(root, ".tmp")
    cmd = sandbox.wrap(prof, [PYTHON, "-E", "-s", "-m", "shaft", *agent_args])
    extra = {"SHAFT_DEADLINE": f"{time.time() + budget - 60:.0f}"} if budget else {}
    from .autonomy import describe_schedule
    extra["SHAFT_SCHEDULE"] = describe_schedule()
    extra["SHAFT_OPEN_TALKS"] = ",".join(open_conversations(name))   # not to be written up yet
    env = sandbox.agent_env(os.environ, SHAFT_ROOT=str(root), TMPDIR=str(root / ".tmp"),
                            SHAFT_BROKER_URL=broker_url, SHAFT_BROKER_TOKEN=broker.token, **extra)
    return broker, cmd, env


def _tee(stream, echo, keep: list[str]):
    """Copy a child's stream to `echo` (if any) as it arrives, keeping the last ~4 KB in `keep`
    (all of it when nothing is echoed: that is a captured output)."""
    import threading

    def pump():
        for chunk in iter(lambda: stream.read(512), ""):
            if echo:
                echo.write(chunk)
                echo.flush()
                keep.append(chunk)
                while sum(map(len, keep)) > 4096 and len(keep) > 1:
                    keep.pop(0)
            else:
                keep.append(chunk)
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    return t


def _popen(cmd, root: Path, env: dict) -> subprocess.Popen:
    # Own process group: the terminal's Ctrl-C reaches the client, which decides what it means.
    return subprocess.Popen(cmd, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)


def _finish(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=10)


def _run(name: str, root: Path, kind: str, agent_args: list[str], interactive: bool,
         output: list[str] | None = None, driver=None) -> int:
    log(f"{name}: start {kind} {describe_knobs()}")
    before = _snapshot(root)
    budget = None if interactive else (SLEEP_TIMEOUT_S if kind == "sleep" else CYCLE_TIMEOUT_S)
    broker, cmd, env = _launch(name, root, kind, agent_args, budget)
    # In a conversation Ctrl-C belongs to the agent (it ends the talk and writes its journal
    # entry). If the supervisor took it too, subprocess.run would kill the agent mid-entry.
    old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN) if interactive and not driver else None
    err_tail: list[str] = []
    out_all: list[str] = []
    try:
        if driver:
            proc = _popen(cmd, root, env)
            try:
                driver(proc)
            finally:
                _finish(proc)
            code = proc.returncode
        else:
            proc = subprocess.Popen(cmd, cwd=root, env=env, stderr=subprocess.PIPE, text=True,
                                    stdout=subprocess.PIPE if output is not None else None)
            readers = [_tee(proc.stderr, sys.stderr, err_tail)]   # still shown live; the tail explains a failure
            if output is not None:
                readers.append(_tee(proc.stdout, None, out_all))
            try:
                code = proc.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
            finally:
                for r in readers:
                    r.join(timeout=5)
            if output is not None:
                output.append("".join(out_all))
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
    (root / ".tmp" / "yield").unlink(missing_ok=True)
    if code < 0 and not interactive:
        journal_note(root, "Supervisor: run stopped", f"My {kind} run was stopped from outside (signal {-code}) "
                     "before I finished it. Whatever I had already written to my files was kept.")
    elif code not in (0, 124, 130) and not interactive and not driver:
        why = "".join(err_tail).strip()[-1200:] or "(it printed nothing)"
        journal_note(root, f"Supervisor: {kind} failed", f"My {kind} run ended with an error (exit {code}), so it "
                     f"did not complete. What it printed last:\n\n```\n{why}\n```")
    if not _settle(name, root, kind, before, broker.handoffs):
        return code or 1
    if kind == "live":
        from . import metrics
        metrics.after_cycle(name)
    commit_state(name, "measurement", [])
    return code


def _settle(name: str, root: Path, kind: str, before: dict, handoffs: list[dict]) -> bool:
    """Everything enforced after the instance acted: append-only journal, core-personality pace, the
    test gate on code (revert on failure), the commit, probation and the personality check.
    Returns False if the commit failed."""
    for note in (enforce_journal(root, before["journal"]), enforce_core(name, root, before["core"])):
        if note:
            log(f"{name}: {note}")
            journal_note(root, "Supervisor", note)

    touched = gate.changed(before["code"], gate.snapshot(root))
    kept: list[str] = []
    if touched:
        ok, test_log = gate.run_tests(root, HOME)
        if ok:
            kept = touched
            log(f"{name}: code change passed tests: {touched}")
        else:
            gate.restore(root, before["code"], touched)
            log(f"{name}: code change FAILED tests, reverted: {touched}")
            journal_note(root, "Supervisor: code change reverted",
                         "My changes to " + ", ".join(touched) + " were reverted because the tests "
                         "failed. Last lines of the test output:\n\n```\n" + test_log[-1500:] + "\n```")
    applied = [h for h in handoffs if h.get("applied")]
    note = "\n".join(f"Delegated to {h['model']} at the instance's request (${h['usd']:.2f}): {h['summary'][:300]} "
                     f"[{', '.join(h['files'])}]" for h in applied)
    if kept:
        delegated = sorted({f for h in applied for f in h["files"]} & set(kept))
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "files": kept, "delegated_files": delegated,
                  "handoffs": len(applied)}
        (SUP_DIR / "code_changes").mkdir(parents=True, exist_ok=True)
        with (SUP_DIR / "code_changes" / f"{name}.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
    if not commit_state(name, kind, kept, note=note):
        return False
    from . import metrics   # judged outcomes, calibration stats, probation of self-changes
    if kept:
        head = git_wt(root, "rev-parse", "HEAD").stdout.strip()
        metrics.start_probation(name, head, kept)
    pers_path = root / "self" / "personality.md"
    if pers_path.exists() and pers_path.read_text() != before["personality"] and judge_ok():
        try:
            metrics.check_personality(name)
            metrics.write_stats(name)
        except Exception as e:   # measurement never breaks a run
            log(f"{name}: personality check failed: {type(e).__name__}: {e}")
    return True


class Turn:
    """The instance, held for one reply of an open conversation: `with turn():` takes the run lock
    (waiting for another run if needed), and on leaving checks and commits what the reply changed."""

    def __init__(self, name: str, root: Path, kind: str):
        self.name, self.root, self.kind = name, root, kind
        self.broker: Broker | None = None
        self.held: ExitStack | None = None

    def begin(self, on_wait=None) -> None:
        if self.held:
            return
        stack = ExitStack()
        try:
            stack.enter_context(run_lock(self.name, wait=False, kind=self.kind))
        except Busy:
            holder = busy_with(self.name) or "another run"
            if not holder.startswith(("talk", "api")):
                request_yield([self.name])   # an autonomous run wraps up at its next turn
            if on_wait:
                on_wait(holder)
            stack.enter_context(run_lock(self.name, wait=True, kind=self.kind))
        self.held = stack
        self.before = _snapshot(self.root)
        self.handoffs_seen = len(self.broker.handoffs) if self.broker else 0

    def end(self) -> None:
        if not self.held:
            return
        try:
            handoffs = self.broker.handoffs[self.handoffs_seen:] if self.broker else []
            _settle(self.name, self.root, self.kind, self.before, handoffs)
        finally:
            self.held.close()
            self.held = None

    @contextmanager
    def __call__(self, on_wait=None):
        self.begin(on_wait)
        try:
            yield
        finally:
            self.end()


def run_conversation(name: str, kind: str, agent_args: list[str], talk_id: str, driver) -> int:
    """A conversation whose agent process stays up between messages (the terminal client).

    It is registered as open for its whole length, but holds the instance only while it works:
    driver(proc, turn) wraps each reply in `with turn():`. The process starts holding the lock
    (its start-up reads my state), and the driver's first `with turn():` — waiting for the agent
    to be ready — releases it."""
    if not (PROJECT / ".git").exists():
        print(f"{PROJECT} is not a git repository; the supervisor needs one to record changes.", file=sys.stderr)
        return 2
    root = instance_root(name)
    with Presence(name, talk_id, kind):
        turn = Turn(name, root, kind)
        turn.begin()
        log(f"{name}: start {kind} {talk_id} {describe_knobs()}")
        try:
            broker, cmd, env = _launch(name, root, kind, agent_args, None)
        except BaseException:
            turn.held.close()
            raise
        turn.broker = broker
        try:
            proc = _popen(cmd, root, env)
            try:
                driver(proc, turn)
            finally:
                turn.end()
                _finish(proc)
            code = proc.returncode
        except KeyboardInterrupt:
            code = 130
        finally:
            broker.stop()
    log(f"{name}: end {kind} {talk_id} exit={code} broker_calls={broker.counts or {}}")
    return code
