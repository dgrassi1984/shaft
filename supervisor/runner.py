"""One sandboxed run of an instance, and everything the supervisor enforces around it.

Before: take the instance's run lock, settle code an earlier run left unsettled (_reconcile),
snapshot its code, journal and core personality, start a broker. After: end the run's whole
process group, stop the broker, restore the journal if it was rewritten (append-only), restore
the core personality if it changed too soon, run the test gate on code changes (revert on
failure), commit the instance's state to its branch.

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
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from . import gate, localmodel, protected, sandbox
from .broker import Broker
from .core import (API_TALKS, BROKER_MAX_CALLS, err_text, DOCS_COPY, EMBED_PORT, CORE_MIN_HOURS, CYCLE_TIMEOUT_S, HOME, INSTANCES,
                   LLM_PORTS, PROJECT, PYTHON, SLEEP_TIMEOUT_S, SUP_DIR, TreeTooBig, UnsafePath,
                   commit_state, describe_knobs, git_wt, instance_config, instance_root, journal_note, list_beneath,
                   log, safe_mkdir, safe_read, safe_read_bytes, safe_write, safe_write_bytes, secrets_env, set_aside, unlink_beneath)

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
        f.seek(0)
        f.truncate()   # no holder: the trial worker reads this line instead of trying the lock (trial.py)
        f.flush()
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


def model_server(name: str) -> str | None:
    """The model server the instance's calls go to ('127.0.0.1:8780', or a self-hosted one through the broker:
    'gpu-box.example:8080'), or None when it thinks with a cloud model. Two instances compete for the model only when
    both use the same server."""
    from . import codexcli, selfhosted
    cfg = instance_config(name)
    url = cfg.get("LLM_BASE_URL", "").strip()
    if codexcli.serves(cfg.get("LLM_MODEL")):
        return None
    if url == "broker" and selfhosted.serves(cfg.get("LLM_MODEL")):
        return selfhosted.host_port(cfg["LLM_MODEL"])
    return localmodel.server_of(url)


def sharing_model(name: str, others: list[str]) -> list[str]:
    """Those of `others` whose runs take the model `name` needs: the same model server. A cloud model is never
    shared — wren on MiMo and kant on Opus were asked to wrap up for every conversation with shaft, on Bonsai
    (2026-10-13), and an instance on a cloud model waits for nobody."""
    server = model_server(name)
    return [] if server is None else [n for n in others if model_server(n) == server]


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

    def __init__(self, name: str, talk_id: str, kind: str = "talk", folder: str = "conversations"):
        self.path = SUP_DIR / folder / name / f"{talk_id}.{kind}"
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


def _held(folder: Path) -> dict[str, str]:
    """{stem: suffix} of the Presence files in `folder` whose process still holds them; stale ones are removed."""
    found: dict[str, str] = {}
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
    return found


def replying(name: str) -> list[str]:
    """Replies to the operator that want the instance right now — waiting for it or writing (Reply). A running
    cycle pauses only for these, and resumes as soon as they are done, even while the conversation stays open
    ("pause only while replying")."""
    return sorted(_held(SUP_DIR / "replying" / name))


def Reply(name: str, kind: str) -> Presence:
    """Register one reply (terminal, API or Telegram) for as long as it waits for or holds the instance."""
    return Presence(name, f"{kind}-{os.getpid()}-{time.monotonic_ns()}", "reply", folder="replying")


def open_conversations(name: str) -> dict[str, str]:
    """{talk_id: 'talk' | 'api'} for the instance's conversations still going on: terminal talks whose
    client is running, and conversations the running API has not closed and written up yet. They are not
    written up while open; they no longer keep its cycles paused (replying does)."""
    found = _held(SUP_DIR / "conversations" / name)
    from .version import service
    if service("api", stale=False):   # the API decides when its conversations end — not this process's knob
        try:
            talks = json.loads(API_TALKS.read_text()) if API_TALKS.exists() else {}
        except (OSError, ValueError):
            talks = {}
        for t in talks.values():
            if t.get("model") == name and not t.get("written"):
                found[t["talk_id"]] = "api"
    return found


def is_busy(name: str) -> bool:
    try:
        with run_lock(name, wait=False):
            return False
    except Busy:
        return True


def _core(root: Path) -> str:
    """The core personality's traits (bullet lines) from every `## Core` block — captions and placeholders
    don't count. All blocks, not the first: a second Core block (wren, 2026-10-13) held a core change the
    pace rule had refused, out of the rule's sight. A personality that is a link reads as none (_personality)."""
    return _core_of(_personality(root))


def _core_of(text: str) -> str:
    return "\n".join(l.strip() for block in CORE_RE.findall(text) for l in block.splitlines()
                     if l.lstrip().startswith(("- ", "* ")))


# The mind files the supervisor checks after every run, read without following a link anywhere (core.safe_read): what
# they hold is copied back into the instance's folder (a restored journal or core, journal.rejected-*.md) and the
# personality goes to a judge, so a link planted in their place would hand either any file the supervisor can read
# (Codex review B1, 2026-10-24). A link reads as no file.
JOURNAL, PERSONALITY = "memory/journal.md", "self/personality.md"


def _personality(root: Path) -> str:
    return safe_read(root, PERSONALITY, "", errors="replace")


def _core_is_empty(text: str) -> bool:
    return not text.strip()


def enforce_journal(root: Path, before: str) -> str | None:
    """The journal is append-only. Returns a note if it had to be repaired. A journal swapped for a link (or a folder,
    a pipe) is never read: it is moved aside as it is and the journal restored — reading through it copied the file it
    pointed to into the instance's journal.rejected-*.md (Codex review B1, 2026-10-24)."""
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    try:
        try:
            after = safe_read(root, JOURNAL, errors="replace")
        except FileNotFoundError:
            after = ""
        except UnsafePath:
            moved = set_aside(root, JOURNAL, f"journal.rejected-{stamp}")
            safe_write(root, JOURNAL, before)
            return (f"My journal was replaced during this run by something that is not a file (a link, a folder), "
                    f"so the supervisor restored it without reading what was there, and moved that, unread, to "
                    f"{moved}.")
        if after.startswith(before):
            return None
        rejected = f"memory/journal.rejected-{stamp}.md"
        safe_write(root, rejected, after)
        safe_write(root, JOURNAL, before)
    except (OSError, UnsafePath) as e:   # memory/ itself is a link: nothing in it is read or written
        return f"My journal could not be checked or restored ({type(e).__name__}: {e}): the supervisor follows no link."
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
        caption = "*(values and temperament — change rarely)*"
        restored = CORE_RE.sub(lambda _: f"## Core\n{caption}\n\n{before}\n\n", _personality(root), count=1)
        first = CORE_RE.search(restored)
        tail = CORE_RE.sub("", restored[first.end():]) if first else ""   # no second Core block survives
        try:
            safe_write(root, PERSONALITY, (restored[:first.end()] + tail if first else restored).rstrip() + "\n")
        except UnsafePath as e:
            return (f"My core personality changed too soon, but it could not be restored ({e}): the supervisor "
                    "follows no link.")
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
    held = _Held(name, kind)
    held.acquire(wait_lock)
    try:
        return _run(name, root, kind, agent_args, interactive, output, driver, held=held)
    except RunRefused as e:
        return _refused(name, e)
    finally:
        held.release()
        if kind == "live":
            _second_judge_later()


def _second_judge_later() -> None:
    """The second judge grades the cycles its sample picked once the instance is free again — detached, so the
    wake that ran the cycle ends now and no message waits minutes for a second opinion."""
    if not any((SUP_DIR / "judgments" / "second-cases").glob("*.json")):
        return
    with (SUP_DIR / "second-judge.log").open("a") as out:
        subprocess.Popen([PYTHON, "-E", "-s", "-m", "supervisor", "judge", "second"], cwd=PROJECT,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)


PAUSABLE = ("live", "sleep", "reflect")   # autonomous runs a conversation pauses at once


class _Held:
    """The instance's run lock as a handle a pausable run can give up during a conversation and take back."""

    def __init__(self, name: str, kind: str):
        self.name, self.kind, self.stack = name, kind, None

    def acquire(self, wait: bool) -> None:
        stack = ExitStack()
        stack.enter_context(run_lock(self.name, wait=wait, kind=self.kind))
        self.stack = stack

    def release(self) -> None:
        if self.stack:
            self.stack.close()
            self.stack = None


CONTROL = re.compile(r"^(pause|paused|pausable|deadline)-(\d+)$")


def _clean_tmp(root: Path) -> None:
    """Remove pause/deadline files and the activity file of runs that are gone — never a live run's: the
    conversation's write-up once deleted a paused cycle's pause file and resumed it while the instance was
    still the conversation's (2026-10-13)."""
    for entry in list_beneath(root, ".tmp"):   # nothing reached through a link is listed, read or removed
        m = CONTROL.match(entry)
        if m and not _pid_alive(int(m.group(2))):
            unlink_beneath(root, f".tmp/{entry}")
    try:
        act = safe_read(root, ".tmp/activity.json", "")
        if act and not _pid_alive(int(json.loads(act).get("pid", 0))):
            unlink_beneath(root, ".tmp/activity.json")
    except (OSError, ValueError, AttributeError):
        unlink_beneath(root, ".tmp/activity.json")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


PAUSE_ACK_S = 300   # how long a paused run's current tool may take to finish before the pause is given up


def _pause(name: str, root: Path, kind: str, proc, held: _Held, run: dict, broker, until: float | None = None) -> float:
    """A reply to the operator needs this instance during its autonomous run ("its background
    cycle should be immediately paused", and later "pause only while replying"): signal it — a model call in flight is cut off, freeing the
    model; a tool in progress finishes first — settle what it did so far, give the instance to the conversation,
    and take it back when the conversation ends. Returns the seconds it was paused. A run that does not acknowledge
    within PAUSE_ACK_S, or by `until` (its budget's end, monotonic), is asked to wrap up instead, once (review B13: the
    wait had no deadline, and a run stuck before its acknowledgement held the reply, and its own budget, forever)."""
    t0 = time.monotonic()
    pid = proc.pid
    marker = root / ".tmp" / f"pausable-{pid}"
    if not marker.is_file() or run.get("pause_given_up"):
        # Its code does not handle the signal (SIGUSR1 would kill it), or it did not acknowledge a pause in time: ask it
        # to wrap up instead, once.
        if not run.get("asked_to_yield"):
            request_yield([name])
            run["asked_to_yield"] = True
        return 0.0
    safe_write(root, f".tmp/pause-{pid}", datetime.now().isoformat(timespec="seconds"))
    try:
        os.kill(proc.pid, signal.SIGUSR1)
    except ProcessLookupError:
        pass
    ack = root / ".tmp" / f"paused-{pid}"
    give_up = min(t0 + PAUSE_ACK_S, until) if until is not None else t0 + PAUSE_ACK_S
    while not ack.exists():
        if proc.poll() is not None:   # it ended on its own meanwhile
            unlink_beneath(root, f".tmp/pause-{pid}")
            return 0.0
        if time.monotonic() >= give_up:   # it resumes at once if it acknowledges later: no pause file is left
            unlink_beneath(root, f".tmp/pause-{pid}")
            run["pause_given_up"] = True
            log(f"{name}: {kind} did not acknowledge its pause within {time.monotonic() - t0:.0f}s — asked to wrap "
                f"up instead; the reply to the operator waits for it")
            return _pause(name, root, kind, proc, held, run, broker)
        time.sleep(0.25)
    log(f"{name}: {kind} paused — a reply to the operator needs the instance")
    _settle(name, root, kind, run["before"], broker.handoffs[run["seen"]:], broker.config_writes[run["cseen"]:])
    held.release()
    while replying(name):
        time.sleep(1)
    held.acquire(wait=True)
    # The conversation's changes are not mine.
    approved = _reconcile(name, root)
    run["before"] = _snapshot(root, approved)
    run["seen"], run["cseen"] = len(broker.handoffs), len(broker.config_writes)
    paused = time.monotonic() - t0
    if run.get("deadline"):
        run["deadline"] += paused
        safe_write(root, f".tmp/deadline-{pid}", f"{run['deadline']:.0f}")   # the paused time is not taken from it
    unlink_beneath(root, f".tmp/pause-{pid}")
    log(f"{name}: {kind} resumed after {paused:.0f}s")
    return paused


_READ = object()


def _snapshot(root: Path, code=_READ) -> dict:
    """What a run starts from. `code`: the code _reconcile verified as approved, taken as the baseline as it is — read
    again here, a write landing between that check and this read became the run's baseline, never gated."""
    pers = _personality(root)
    if code is _READ:
        try:
            code = gate.snapshot(root)
        except TreeTooBig as e:   # never an empty snapshot: the run's code is then not checked, nor kept (_settle)
            log(f"{root.name}: code snapshot NOT taken: {e}")
            code = None
    return {"code": code, "journal": safe_read(root, JOURNAL, "", errors="replace"), "personality": pers,
            "core": _core_of(pers)}


def _uncommitted_code(root: Path) -> dict[str, bytes | None]:
    """The code (gate.CODE_PATHS) that differs from the instance's last commit, as git sees it: {path: its committed
    bytes, or None where the commit has no such file}. Left out: what the gate never snapshots (__pycache__,
    .DS_Store) and a path committed as anything but a plain file. Read through git_wt (the worktree's own .git is
    the instance's to forge), in bytes: a file name that is not UTF-8 must not hide a change."""
    def run(*args: str) -> list[str]:
        out = git_wt(root, "--no-optional-locks", *args, text=False).stdout
        return [os.fsdecode(e) for e in out.split(b"\0") if e]
    dirty = sorted({e[3:] for e in run("status", "--porcelain", "-z", "--untracked-files=all", "--no-renames", "--",
                                       *gate.CODE_PATHS)})
    dirty = [p for p in dirty if "__pycache__" not in Path(p).parts and Path(p).name != ".DS_Store"]
    if not dirty:
        return {}
    committed = {}
    for entry in run("ls-tree", "-r", "-z", "HEAD", "--", *gate.CODE_PATHS):
        meta, _, path = entry.partition("\t")
        committed[path] = meta.split()                     # mode, type, object
    out: dict[str, bytes | None] = {}
    for path in dirty:
        if path not in committed:
            out[path] = None
        elif committed[path][0] in ("100644", "100755"):
            out[path] = git_wt(root, "cat-file", "blob", committed[path][2], text=False).stdout
    return out


class RunRefused(Exception):
    """A run must not start: its code cannot be checked against the approved code. Logged as "run refused" (an alert
    on the panel) and given back as exit REFUSED."""


REFUSED = 75   # EX_TEMPFAIL: the run did not start; nothing was changed
IN_PROGRESS = ("MERGE_HEAD", "rebase-apply", "rebase-merge", "CHERRY_PICK_HEAD", "REVERT_HEAD")


def _in_progress(root: Path) -> str | None:
    """A merge, rebase, cherry-pick or revert under way in the instance's worktree (the operator's, by hand): its
    state files in the worktree's git dir, located through git_wt — never through the worktree's own .git."""
    paths = git_wt(root, "rev-parse", *[a for n in IN_PROGRESS for a in ("--git-path", n)]).stdout.splitlines()
    if len(paths) != len(IN_PROGRESS) or not all(os.path.isabs(p) for p in paths):
        raise RunRefused(f"cannot tell whether a merge is in progress in {root}: git said {paths!r}")
    return next((n for n, p in zip(IN_PROGRESS, paths) if os.path.lexists(p)), None)


def _reconcile(name: str, root: Path) -> dict:
    """Settle code an earlier run left unsettled, before a new run takes its baseline: a run that died before
    _settle (the supervisor killed, the Mac shut down, an exception) left its edits on disk, and the next run's
    snapshot took them as its starting point, so they were never gated (B8, review of 2026-10-24). Whatever code
    is not the approved code (baseline.py: the last commit, plus what the gate passed that git does not hold —
    ignored and untracked files included, which `git status` never showed: Codex on e77d5e5) is checked now as that
    run's would have been: kept if the tests pass; put back as approved if they fail, the rejected bytes kept in
    .supervisor/rejected/ first. Before an instance's first record, git's view is the approved code. config.env and
    drive.md are not code (the operator's: _commit_outside_config, _commit_outside_drive). Nothing is guessed:
    a merge or rebase in progress (the operator's work), or code that cannot be compared, refuses the run
    (RunRefused) — it failed open until Codex's review. Returns the code it verified as approved: the caller's run
    starts from exactly that (_snapshot), so a write landing after this check is the run's change, and gated."""
    from . import baseline

    def unapproved() -> tuple[dict, dict]:
        try:
            if (what := _in_progress(root)):
                raise RunRefused(f"a git {what} is in progress in its worktree; finish or abort it "
                                 f"(git -C {root} status) and the next run checks the code")
            now = gate.snapshot(root)
            restore = baseline.unapproved(name, root, now)
            if restore is None:                                # no record yet: the first run after the upgrade
                restore = _uncommitted_code(root)
            for p in gate.refused(now):                        # never approved code: a link, a hard link, a pipe
                restore.setdefault(p, None)                    # the approved file it stands in for comes back
        except RunRefused:
            raise
        except TreeTooBig as e:                                # never an empty or partial answer (core.Budget)
            raise RunRefused(f"its code is too big to check against its approved code ({e}; the bounds are "
                             "SHAFT_SNAPSHOT_MAX_MB, SHAFT_TREE_MAX_FILES, SHAFT_TREE_MAX_DEPTH)") from None
        except (baseline.BaselineError, subprocess.CalledProcessError, OSError, ValueError, UnsafePath,
                SystemExit) as e:
            why = err_text(e) or f"{type(e).__name__}: {e}"
            raise RunRefused(f"its code could not be compared with its approved code: {why}") from None
        base = {p: b for p, b in now.items() if p not in restore}
        base.update({p: b for p, b in restore.items() if b is not None})
        return now, base

    now, base = unapproved()
    touched = gate.changed(base, now)
    if not touched:
        return now
    log(f"{name}: code is not the approved code before a run (an earlier run ended unsettled): gating {touched}")
    journal_note(root, "Supervisor: unchecked code from an earlier run",
                 "When this run started, my code was not the code the supervisor last approved, in "
                 + ", ".join(touched) + ": an earlier run ended before the supervisor could check it (or it was "
                 "edited from outside). It was checked now, as every run's changes are: kept if the tests pass, put "
                 "back as last approved if they fail (the rejected files are kept for the operator).")
    rejected = SUP_DIR / "rejected" / f"{name}-{datetime.now():%Y%m%d-%H%M%S-%f}"
    _settle(name, root, "interrupted", _snapshot(root, base), [], rejected_to=rejected)
    now, base = unapproved()                                   # settled: the code is now the approved code, or
    if (left := gate.changed(base, now)):                      # something is writing it as it is checked
        raise RunRefused(f"its code is still not the approved code after it was settled: {', '.join(left)}")
    return now


def _refused(name: str, e: RunRefused) -> int:
    """Say it where the operator looks: the supervisor log (the panel shows its "refused" lines as alerts), stderr."""
    log(f"{name}: run refused — {e}")
    print(f"{name}: run refused — {e}", file=sys.stderr)
    return REFUSED


def _launch(name: str, root: Path, kind: str, agent_args: list[str], budget: int | None, approved: dict | None = None):
    """Start the broker and launch from _reconcile's approved bytes (#86), keeping root as cwd and mind.
    Return (broker, command, environment); the caller removes SHAFT_CODE_ROOT after the agent ends."""
    if localmodel.is_service(instance_config(name).get("LLM_BASE_URL", "")):
        localmodel.ensure(f"{name}'s {kind}")   # it runs only while used (localmodel.py)
    broker = Broker(name, PROJECT, secrets_env(), SUP_DIR / "broker.log", max_calls=BROKER_MAX_CALLS)
    broker.run_kind = kind
    broker.run_started = datetime.now().isoformat(timespec="seconds")
    broker.start()
    SUP_DIR.mkdir(exist_ok=True)
    rebuild_env(name, root, broker)
    extra = {"SHAFT_DEADLINE": f"{time.time() + budget - 60:.0f}"} if budget else {}
    from .autonomy import describe_schedule
    extra["SHAFT_SCHEDULE"] = describe_schedule(name)
    extra["SHAFT_RUN_STARTED"] = broker.run_started   # the episode carries it: its consults are matched to it exactly
    extra["SHAFT_OPEN_TALKS"] = ",".join(open_conversations(name))   # not to be written up yet
    tree = None
    try:
        if approved is None:
            raise RunRefused("no approved code for the launch")
        try:
            tree = protected.make(approved, SUP_DIR / "protected" / name, name, instance=root)
        except (OSError, TreeTooBig, ValueError) as e:
            raise RunRefused(f"could not protect its approved code: {e}") from None
        extra["SHAFT_CODE_ROOT"] = str(tree)
        cmd, env = agent_command(root, broker, agent_args, SUP_DIR / f"{name}.sb", LLM_PORTS, extra)
    except BaseException:
        try:
            broker.stop()
        finally:
            if tree is not None:
                protected.remove(tree)
        raise
    return broker, cmd, env


def rebuild_env(name: str, root: Path, broker: Broker) -> None:
    """A Python environment the instance declared (.python-version, requirements.txt) but no longer has — a new
    machine, a wiped folder — is rebuilt before it starts, exactly as its own `run` would do it: same sandbox, same
    proxy. A failure is logged and noted for the instance, never fatal: it can repair the environment itself."""
    from . import pyenv
    if not pyenv.needs_rebuild(root):
        return
    try:
        out = broker._run(pyenv.rebuild_command(root), timeout_s=300)
    except Exception as e:
        out = f"{type(e).__name__}: {e}"
    ok = out.startswith("exit 0")
    log(f"{name}: Python environment {'rebuilt' if ok else 'NOT rebuilt'} from its declaration — {out.splitlines()[0]}")
    if not ok:
        journal_note(root, "My Python environment could not be rebuilt", f"`{pyenv.rebuild_command(root)}` failed:\n\n"
                     f"```\n{out[-2000:]}\n```\n\nRepair it with `run` (uv), or fix .python-version / requirements.txt.")


def agent_command(root: Path, broker: Broker, agent_args: list[str], profile: Path, llm_ports: list[int],
                  extra: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """The sandboxed command and environment for the agent in `root`, talking to `broker` (already started):
    it may write only in `root` and reach only the model ports, the embedding service, its broker and its egress
    proxy (the public internet); it may read the operator's shared folder if the broker carries one (shaft share),
    is told which of the operator's mail labels and calendars its broker serves it (SHAFT_MAIL, shaft mail;
    SHAFT_CALENDAR, shaft calendar), whether its model sees images (SHAFT_VISION, docs/plans/PLAN-vision.md),
    and the instance's Python environment when `root` is a copy (trial replay). SHAFT_CODE_ROOT in `extra`
    redirects live launches to their protected code; replay callers already own a copy and leave it unset."""
    ports = list(llm_ports) + [EMBED_PORT, broker.port, broker.egress.port]
    profile.write_text(sandbox.profile(root, HOME, ports, shared=broker.shared, env_root=broker.env_root))
    safe_mkdir(root, ".tmp")
    try:
        stage_docs(root)
    except (OSError, UnsafePath) as e:   # not fatal: the instance finds no docs and says so (its skill tells it to)
        log(f"{root.name}: {DOCS_COPY}/ NOT refreshed: {type(e).__name__}: {e}")
    cmd = sandbox.wrap(profile, [PYTHON, "-E", "-s", "-m", "shaft", *agent_args])
    if (code := extra.get("SHAFT_CODE_ROOT")):
        with profile.open("a") as f:
            f.write(sandbox.protected_code(Path(code), HOME))
        # #86: -I excludes cwd and Python environment overrides. Only the approved tree supplies shaft;
        # cwd and SHAFT_ROOT remain the instance's mind and working edits. Trial replays use their own copies.
        cmd = sandbox.wrap(profile, [PYTHON, "-I", "-c",
                           "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
                           "runpy.run_module('shaft', run_name='__main__', alter_sys=True)", code, *agent_args])
    env = sandbox.agent_env(os.environ, SHAFT_ROOT=str(root), TMPDIR=str(root / ".tmp"),
                            SHAFT_BROKER_URL=f"http://127.0.0.1:{broker.port}", SHAFT_BROKER_TOKEN=broker.token,
                            SHAFT_EMBED_URL=f"http://127.0.0.1:{EMBED_PORT}/v1/embeddings",
                            **({"SHAFT_SHARED": str(broker.shared)} if broker.shared else {}),
                            **({"SHAFT_MAIL": granted} if (granted := broker.mail_env()) else {}),
                            **({"SHAFT_CALENDAR": granted} if (granted := broker.calendar_env()) else {}),
                            **({"SHAFT_VISION": "1"} if _sees_images(root, extra) else {}),
                            **broker.sandbox_env(), **extra)
    return cmd, env


# What the instance reads to tell the operator how to set SHAFT up: how to use it, not how it was built (docs/plans/).
DOCS_SOURCES = ("README.md", "docs/*.md")


def stage_docs(root: Path, project: Path = PROJECT) -> None:
    """SHAFT's own docs, copied fresh into the instance's shaft-docs/ at every start (the operator, 2026-10-20: an
    instance should read the docs and tell him how to configure SHAFT; skills/helping-the-operator-configure.md). Its
    code reads only its own folder, so the docs come to it; a changed, renamed or deleted doc is current there from
    its next start. Its sandbox reads the folder and never writes it (sandbox._no_docs_writes); it is never committed
    or snapshotted (core.DOCS_COPY). Nothing in the folder is followed: a link or anything else that is not one of
    the docs is removed, and every write opens each component without following a link (safe_write_bytes)."""
    import shutil
    docs = {p.name: p.read_bytes() for pattern in DOCS_SOURCES for p in sorted(project.glob(pattern)) if p.is_file()}
    folder = root / DOCS_COPY
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        folder.unlink()   # the link itself, never its target
    safe_mkdir(root, DOCS_COPY)
    for entry in folder.iterdir():
        if entry.name in docs and entry.is_file() and not entry.is_symlink():
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for name, data in docs.items():
        here = folder / name
        if not (here.is_file() and here.read_bytes() == data):   # unchanged docs are left alone
            safe_write_bytes(root, f"{DOCS_COPY}/{name}", data)


def _sees_images(root: Path, extra: dict[str, str] | None = None) -> bool:
    """Whether the effective model accepts images: a replay override wins over config.env; unknown counts as no."""
    from . import vision
    from .core import config_values
    if extra is not None and "LLM_MODEL" in extra:
        return vision.sees_images_or_no(extra["LLM_MODEL"])
    try:
        model = config_values(safe_read(root, "config.env")).get("LLM_MODEL")
    except (OSError, UnsafePath, ValueError):
        return False
    return vision.sees_images_or_no(model)


def _tee(stream, echo, keep: list[str]):
    """Copy a child's stream to `echo` (if any) as it arrives, keeping the last ~4 KB in `keep`
    (all of it when nothing is echoed: that is a captured output)."""
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
    return _track(subprocess.Popen(cmd, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True), root)


# Every run's agent this process leads, by pid (its process group's id), with its instance's folder: what a service
# that starts runs ends before it goes (stop_runs_on_signal).
_LIVE: dict[int, tuple[subprocess.Popen, Path]] = {}
_LIVE_LOCK = threading.Lock()


def _track(proc: subprocess.Popen, root: Path) -> subprocess.Popen:
    with _LIVE_LOCK:
        _LIVE[proc.pid] = (proc, Path(root))
    return proc


def _end_run(proc: subprocess.Popen, root: Path) -> None:
    """Nothing a run started outlives it, before anything of it is settled (B9): its whole process group, then any
    process left in the instance's folder that slipped out of the group (kill.end_left_in)."""
    from . import kill
    try:
        gate.stop_group(proc)
        kill.end_left_in(root)
    finally:
        with _LIVE_LOCK:
            _LIVE.pop(proc.pid, None)


def end_all_runs() -> None:
    with _LIVE_LOCK:
        live = list(_LIVE.values())
    for proc, root in live:
        _end_run(proc, root)


def _finish(proc: subprocess.Popen, root: Path) -> None:
    """A driven agent's end: up to 15 s to finish on its own (its journal entry, its last reply), then its whole
    process group is ended — and on a normal end too, whatever it left running (_end_run, B9)."""
    if proc.poll() is None:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
    _end_run(proc, root)


def _start(cmd, root: Path, env: dict, capture: bool, interactive: bool) -> subprocess.Popen:
    """An undriven run's agent, leading a process group of its own, so that it and everything it starts end together
    (gate.stop_group). An autonomous run gets its own session too; a terminal conversation stays in the terminal's
    session and is handed the terminal (_give_terminal), so Ctrl-C and what is typed still reach it."""
    group = {"process_group": 0} if interactive else {"start_new_session": True}
    return _track(subprocess.Popen(cmd, cwd=root, env=env, stderr=subprocess.PIPE, text=True,
                                   stdout=subprocess.PIPE if capture else None, **group), root)


def _give_terminal(proc: subprocess.Popen):
    """Make the agent's group the terminal's foreground group, as the supervisor's own group was while they shared it;
    returns how to take it back, or None (no terminal in the foreground, or not the main thread)."""
    if threading.current_thread() is not threading.main_thread():
        return None
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd) or os.tcgetpgrp(fd) != os.getpgrp():
            return None
    except (OSError, ValueError):
        return None
    old = signal.signal(signal.SIGTTOU, signal.SIG_IGN)   # a background group's tcsetpgrp would stop it otherwise
    try:
        os.tcsetpgrp(fd, proc.pid)
        os.killpg(proc.pid, signal.SIGCONT)               # stopped on SIGTTIN, had it read before it had the terminal
    except OSError:
        signal.signal(signal.SIGTTOU, old)
        return None

    def take_back() -> None:
        try:
            os.tcsetpgrp(fd, os.getpgrp())
        except OSError:
            pass
        finally:
            signal.signal(signal.SIGTTOU, old)
    return take_back


class _Stopped(Exception):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def _trap_stop(saved: dict) -> None:
    """SIGTERM or SIGHUP to the supervisor during an undriven run (launchd stopping a service, a closed terminal)
    raises _Stopped in the main thread, so the run's process group is ended before the supervisor goes: the agent
    leads its own group now, and those signals used to reach it only because it shared the supervisor's. `saved`
    gets each handler replaced, before it is, to be put back. Nothing off the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return

    def on_stop(signum, frame):
        for s in STOP_SIGNALS:                            # one is enough: nothing interrupts the cleanup
            signal.signal(s, signal.SIG_IGN)
        raise _Stopped(signum)
    for s in STOP_SIGNALS:
        if signal.getsignal(s) is not signal.SIG_IGN:     # ignored stays so: `nohup shaft live` outlives its terminal
            saved[s] = signal.getsignal(s)
            signal.signal(s, on_stop)


def _trap_runs(saved: dict) -> None:
    """SIGTERM or SIGHUP to a process whose runs are driven from threads (the API service) or that keeps a
    conversation's agent between replies (the terminal client): every run's processes are ended first (end_all_runs),
    then the signal does what it was sent to do. Before, stopping the API service left its agents running with their
    locks gone (Codex on e77d5e5). What those runs changed is settled at their instance's next run (_reconcile).
    `saved` gets the handlers replaced, to be put back; nothing off the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return

    def on_signal(signum, frame):
        for s in STOP_SIGNALS:
            signal.signal(s, signal.SIG_IGN)
        log(f"stopping (signal {signum}): ending the processes of {len(_LIVE)} run(s) first")
        end_all_runs()
        for s in STOP_SIGNALS:
            signal.signal(s, saved.get(s, signal.SIG_DFL))
        os.kill(os.getpid(), signum)
    for s in STOP_SIGNALS:
        if signal.getsignal(s) is not signal.SIG_IGN:
            saved[s] = signal.getsignal(s)
            signal.signal(s, on_signal)


def stop_runs_on_signal() -> None:
    """For the main thread of a service that starts runs in its own process (the API): see _trap_runs."""
    _trap_runs({})


def _record_cycle(name: str) -> None:
    """A live cycle starts: the scheduler's cycle clock (autonomy.record 'cycle': wake_gate's `since`, the commitment
    wakes' cutoff, the shown skip cleared). Stamped here, under the run lock and once its launch is ready, so every live
    cycle counts — a scheduled wake, `shaft live`, `shaft tick` — and a wake that found the instance busy or was refused
    counts none (Codex on the C15 lane: wake() stamped it before taking the lock, and a Busy wake moved the commitment
    cutoff past commitments no cycle saw). Trial replays never come here (trial.py). A failed stamp is logged, never
    the run's end."""
    from .autonomy import record
    try:
        record("cycle", name)
    except OSError as e:
        log(f"{name}: its cycle could not be recorded in schedule.json — {type(e).__name__}: {e}")


def _run(name: str, root: Path, kind: str, agent_args: list[str], interactive: bool,
         output: list[str] | None = None, driver=None, held: _Held | None = None) -> int:
    log(f"{name}: start {kind} {describe_knobs()}")
    _clean_tmp(root)
    run = {"before": _snapshot(root, _reconcile(name, root)), "seen": 0, "cseen": 0}
    budget = None if interactive else (SLEEP_TIMEOUT_S if kind == "sleep" else CYCLE_TIMEOUT_S)
    broker, cmd, env = _launch(name, root, kind, agent_args, budget, run["before"].get("code"))
    run["deadline"] = float(env["SHAFT_DEADLINE"]) if env.get("SHAFT_DEADLINE") else None
    # In a conversation Ctrl-C belongs to the agent (it ends the talk and writes its journal
    # entry). If the supervisor took it too, subprocess.run would kill the agent mid-entry.
    old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN) if interactive and not driver else None
    old_stop: dict = {}
    stopped, refused = 0, None
    err_tail: list[str] = []
    out_all: list[str] = []
    try:
        if kind == "live":
            _record_cycle(name)
        if not driver:
            _trap_stop(old_stop)
        if driver:
            proc = _popen(cmd, root, env)
            try:
                driver(proc)
            finally:
                _finish(proc, root)
            code = proc.returncode
        else:
            proc = _start(cmd, root, env, output is not None, interactive)
            take_back, readers = None, []
            try:
                take_back = _give_terminal(proc) if interactive else None
                readers.append(_tee(proc.stderr, sys.stderr, err_tail))   # shown live; the tail explains a failure
                if output is not None:
                    readers.append(_tee(proc.stdout, None, out_all))
                pausable = held is not None and kind in PAUSABLE and not interactive
                ends = time.monotonic() + budget if budget else None
                while True:
                    try:
                        code = proc.wait(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if ends is not None and time.monotonic() > ends:
                        raise subprocess.TimeoutExpired(cmd, budget)
                    if pausable and replying(name):
                        paused = _pause(name, root, kind, proc, held, run, broker, until=ends)
                        ends = ends + paused if ends is not None else None
            finally:
                if take_back:
                    take_back()
                _end_run(proc, root)   # nothing the run started outlives it, however it ended (B9): settled next
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
    except _Stopped as e:
        stopped, code = e.signum, 128 + e.signum
    except RunRefused as e:                     # on resuming after a pause: stopped where it was, nothing settled
        refused = e
    finally:
        if old_sigint is not None:
            signal.signal(signal.SIGINT, old_sigint)
        for s, handler in old_stop.items():
            signal.signal(s, handler)
        try:
            broker.stop()
        finally:
            if env.get("SHAFT_CODE_ROOT"):
                protected.remove(Path(env["SHAFT_CODE_ROOT"]))
    if refused:
        return _refused(name, refused)
    if stopped:
        # The supervisor itself is being stopped: no gate now (launchd would kill it halfway). The run's group is
        # gone; what it changed is settled when the instance next runs (_reconcile). Then the signal does what it
        # was sent to do.
        log(f"{name}: {kind} stopped with the supervisor (signal {stopped}); its changes are settled at its next run")
        journal_note(root, "Supervisor: run stopped", f"My {kind} run was stopped with the supervisor (signal "
                     f"{stopped}) before I finished it. What I changed is checked when I next run.")
        os.kill(os.getpid(), stopped)
        return code
    log(f"{name}: end {kind} exit={code} broker_calls={broker.counts or {}}")
    unlink_beneath(root, ".tmp/yield")
    _clean_tmp(root)
    if code < 0 and not interactive:
        journal_note(root, "Supervisor: run stopped", f"My {kind} run was stopped from outside (signal {-code}) "
                     "before I finished it. Whatever I had already written to my files was kept.")
    elif code not in (0, 124, 130) and not interactive and not driver:
        why = "".join(err_tail).strip()[-1200:] or "(it printed nothing)"
        journal_note(root, f"Supervisor: {kind} failed", f"My {kind} run ended with an error (exit {code}), so it "
                     f"did not complete. What it printed last:\n\n```\n{why}\n```")
    if not _settle(name, root, kind, run["before"], broker.handoffs[run["seen"]:], broker.config_writes[run["cseen"]:]):
        return code or 1
    if kind == "live":
        from . import metrics
        metrics.after_cycle(name)
    commit_state(name, "measurement", [])
    return code


def _settle(name: str, root: Path, kind: str, before: dict, handoffs: list[dict],
            config_writes: list[dict] = (), rejected_to: Path | None = None, regating: dict | None = None,
            outcome: dict | None = None) -> bool:
    """Everything enforced after the instance acted: append-only journal, core-personality pace, the
    test gate on code and on the config.env lines it set (revert on failure), the commit, probation and
    the personality check. Returns False if the commit failed.
    A failed gate is reverted whatever its cause; when the cause was the supervisor's side (the baseline: _cause), the
    change is also held (gate_held) and re-gated once the baseline is green (regate_held). `regating`: the held change
    this settle gates again — a red baseline then counts one more try instead of holding it twice. `outcome`, when
    given: filled with how the gate ended ({"result": kept / candidate-failed / held / dropped / baseline-failed,
    "why", "failing", "mine"}); left empty when there was nothing to gate. Every gate is a row of record(kind="gate")."""
    for note in (enforce_journal(root, before["journal"]), enforce_core(name, root, before["core"])):
        if note:
            log(f"{name}: {note}")
            journal_note(root, "Supervisor", note)

    try:
        after = gate.snapshot(root) if before["code"] is not None else None
        too_big = None if after is not None else "it was already too big when the run began"
    except TreeTooBig as e:
        after, too_big = None, str(e)
    touched = gate.changed(before["code"], after) if after is not None else []
    linked = gate.refused(after) if after is not None else []
    touched = sorted(set(touched) | set(linked))      # a link in a code path is a change whatever it was before
    configured = sorted({w["key"] for w in config_writes}) if after is not None else []
    kept: list[str] = []
    approved = after          # what the gate judged: the next run's approved code; None: no record (a failed gate)
    if too_big:
        approved = None
        if rejected_to:       # code not a run's own, which cannot be copied aside: left as it is, not put back
            raise RunRefused(f"its code is too big to check ({too_big}); nothing was put back")
        _too_big_to_check(name, root, before["code"], config_writes, too_big)
    elif touched or configured:
        if linked:   # fail closed, untested: code paths hold no links, hard links or pipes (gate.Link, gate.Invalid)
            ok, test_log = False, ("== links ==\nA link, a hard link or a pipe where code belongs is never kept, "
                                   "whatever it points to: " + ", ".join(
                                       f"{p} -> {after[p].target}" if isinstance(after[p], gate.Link)
                                       else f"{p}: {after[p].why}" for p in linked))
        else:
            try:
                with protected.copy(after, SUP_DIR / "protected" / name, name,
                                    safe_read_bytes(root, "config.env", b""), instance=root) as tree:
                    ok, test_log = gate.run_tests(tree, HOME, env_root=root, data_root=root)
            except Exception as e:   # a failed copy is a failed gate, never an unchecked change kept
                # The copy is the supervisor's machinery (a protected-copy regression reverted thelos's changes):
                # the baseline's, unless the change itself made it impossible (too big, an entry never copied).
                ok, test_log = False, gate.Output(
                    f"== protected copy ==\n{type(e).__name__}: {e}",
                    "candidate" if isinstance(e, (TreeTooBig, ValueError)) else "baseline",
                    f"the gate's protected copy of my code failed ({type(e).__name__}: {e})")
        files = touched + (["config.env"] if configured else [])
        if ok:
            kept = files
            log(f"{name}: code change passed tests: {touched} config: {configured}")
            from . import gate_held
            gate_held.log_run(name, "re-gated" if regating else "kept", files, result="kept" if regating else "")
            if outcome is not None:
                outcome.update(result="kept", why="", failing=[], mine="")
        else:
            approved = None   # a failed gate records nothing: the record before it is what was put back
            saved = _keep_rejected(name, rejected_to, after, touched) if rejected_to else ""
            mine, not_mine = _keep_for_instance(root, after, touched)
            _put_back(name, root, before["code"], touched)
            back = gate.revert_config(root, list(config_writes))
            undone = touched + ([f"config.env ({', '.join(back)})"] if back else [])
            names = gate.failing_tests(test_log)
            kept_at = next((l[len("full log: "):] for l in test_log.splitlines() if l.startswith("full log: ")), "")
            cause, why = _cause(name, root, before["code"], test_log)   # after the put-back: nothing runs meanwhile
            failing = list(getattr(test_log, "failing", ())) or names
            held, result = "", "candidate-failed"
            if cause == "baseline":
                held, result = _hold(name, root, before["code"], after, touched, why, failing, regating)
            log(f"{name}: code change FAILED tests, reverted: {touched} config: {back}"
                + (f"; failing: {'; '.join(names)}" if names else "") + (f"; full log {kept_at}" if kept_at else "")
                + (f"; the rejected files are kept in {saved}" if saved else "")
                + (f"; a copy for the instance in {mine}" if mine else "")
                + (f"; no copy for the instance: {not_mine}" if not_mine else "")
                + (f"; not the change's failure: the gate's baseline is red ({why})"
                   + (f", the change held as {held}" if held else f", {result}") if cause == "baseline" else ""))
            from . import gate_held
            gate_held.log_run(name, "re-gated" if regating else result, files, why, failing, test_log, held,
                              result if regating else "")
            if outcome is not None:
                outcome.update(result=result, why=why, failing=failing, mine=mine)
            if held and not regating:
                text = (f"held: the gate's baseline is red — {why}. My change to {', '.join(touched)} did not fail "
                        "it; it was taken off my code and held, not lost, and it will be re-gated automatically "
                        "when the baseline is green (I will be told the outcome here)."
                        + (f" The config lines I set ({', '.join(back)}) were undone and are not held: set them "
                           "again then." if back else "")
                        + (f" A copy of what I wrote is in {mine} (a copy, not code)." if mine else "")
                        + ("\n\nFailing:\n" + "\n".join(f"- {n}" for n in failing) if failing else ""))
                journal_note(root, "Supervisor: code change held", text)
                _tell(name, root, f"code change held (the gate's baseline is red)", text)
            elif undone and not regating:
                journal_note(root, "Supervisor: code change reverted",
                             "My changes to " + ", ".join(undone) + " were reverted because the tests failed."
                             + (f" The failure was not theirs — the gate's baseline is red ({why}) — but they "
                                "could not be held." if cause == "baseline" else "")
                             + (f" The rejected files are kept in {saved}." if saved else "")
                             + (f" What I wrote is kept in {mine}, as it was when the tests ran (a copy, not code:"
                                " nothing runs it)." if mine else "")
                             + (f" A copy of what I wrote could not be kept ({not_mine})." if not_mine else "")
                             + ("\n\nFailing tests:\n" + "\n".join(f"- {n}" for n in names) if names else "")
                             + "\n\nLast lines of the test output:\n\n```\n" + test_log[-1500:] + "\n```")
    _commit_outside_config(name, root, list(config_writes) if "config.env" in kept else [])
    _commit_outside_drive(name, root)
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
    committed = commit_state(name, kind, kept, note=note, gated=after)
    _check_kept(name, root, after, kept)
    if approved is not None:
        _record_approved(name, root, approved)
    if not committed:
        return False
    from . import metrics   # judged outcomes, calibration stats, probation of self-changes
    if kept:
        head = git_wt(root, "rev-parse", "HEAD").stdout.strip()
        from . import trial
        tried = trial.match(name, root, kept, before["code"])
        metrics.start_probation(name, head, kept, trial=tried)
        if tried:
            log(f"{name}: kept change is trial {tried['id']}'s ({tried['verdict']})")
            if tried["verdict"] == "worse":
                journal_note(root, "Supervisor: change adopted against its trial",
                             f"I adopted the change I trialled in {tried['id']} although the trial found it worse "
                             f"(lost {tried['L']}, won {tried['W']}, tied {tried['T']} replayed cycles). It is on "
                             f"probation and will be reverted on any drop in my judged success.")
    pers = _personality(root)
    if pers and pers != before["personality"] and judge_ok():
        try:
            metrics.check_personality(name)
            metrics.write_stats(name)
        except Exception as e:   # measurement never breaks a run
            log(f"{name}: personality check failed: {type(e).__name__}: {e}")
    return True


def _cause(name: str, root: Path, approved: dict | None, test_log: str) -> tuple[str, str]:
    """Whose failure a failed gate was, and why (gate.Output): "baseline" — the supervisor's side — or "candidate".
    When the only check of the change that failed is the supervisor's instance part, its failing tests are run once
    more against the approved code (a protected copy, like the gate's): failing there too, they fail whatever the
    change, so the failure is the baseline's — a test that fails only with the change is the change's. A gate that says
    nothing of its cause (a plain text: the links check, a stand-in) is the change's, as every failure was before M2."""
    cause = getattr(test_log, "cause", None) or "candidate"
    why = getattr(test_log, "why", "") or "a check of the change failed"
    recheck = list(getattr(test_log, "recheck", ()))
    if cause != "candidate" or not recheck or not approved:
        return cause, why
    try:
        with protected.copy(approved, SUP_DIR / "protected" / name, name, safe_read_bytes(root, "config.env", b""),
                            instance=root) as tree:
            base = gate.instance_part_failures(tree)
    except Exception as e:   # no answer: the gate's verdict stands as the change's
        log(f"{name}: the failing supervisor tests could not be run on its approved code: {type(e).__name__}: {e}")
        return cause, why
    if base is not None and set(recheck) <= set(base):
        return "baseline", ("the supervisor's tests that read my code fail on my approved code too, so not because of "
                            f"the change: {'; '.join(recheck[:5])}" + (" …" if len(recheck) > 5 else ""))
    return cause, why


def _hold(name: str, root: Path, before: dict, after: dict, touched: list[str], why: str, failing: list[str],
          regating: dict | None) -> tuple[str, str]:
    """Hold a change the baseline failed (gate_held): (its id or "", the result). A held change gated again counts one
    more try, dropped after gate_held.TRIES. Nothing to hold (only config lines) or a hold that could not be written
    is "baseline-failed": reverted, as before M2, and said so."""
    from . import gate_held
    if regating is not None:
        if gate_held.retried(name, regating, why, failing):
            return regating["id"], "held"
        return "", "dropped"
    code = [p for p in touched if isinstance(after.get(p), bytes) or p not in after]
    if not code or code != touched:
        return "", "baseline-failed"
    try:
        head = git_wt(root, "rev-parse", "HEAD", check=False).stdout.strip()
    except Exception as e:   # the base digests are what re-applying checks; the commit is for reading
        head = f"unknown ({type(e).__name__}: {e})"
    try:
        hid = gate_held.save(name, head, before, after, touched, why, failing)
    except (OSError, ValueError) as e:
        log(f"{name}: a code change the gate's baseline failed could NOT be held: {type(e).__name__}: {e}")
        return "", "baseline-failed"
    log(f"{name}: code change held as {hid} until the gate's baseline is green: {touched}")
    return hid, "held"


def _tell(name: str, root: Path, subject: str, text: str) -> None:
    """A message from the gate in the instance's inbox (read at its next wake) and the message log. Best effort: a
    message that cannot be written is logged, never a reason to leave a settle halfway."""
    from . import messages
    stamp = datetime.now()
    try:
        path = safe_write(root, f"inbox/{stamp:%Y%m%d-%H%M%S-%f}-from-supervisor-gate.md",
                          f"From: supervisor (the test gate)\nTo: {name}\nKind: gate\nDate: {stamp:%Y-%m-%d %H:%M}\n"
                          f"Subject: {subject}\n\n{text.strip()}\n")
        messages.record(SUP_DIR, path, "supervisor", name, "gate", stamp, text)
    except (OSError, UnsafePath) as e:
        log(f"{name}: gate message NOT delivered ({subject}): {e}")


def regate_held(name: str) -> None:
    """Gate the instance's held changes again (gate_held), oldest first, once a warm-up found the gate's baseline green
    (`shaft gate regate`, started by the scheduler after it). Each is put back on the code as it is now — only if every
    file it touched is still as it was when held; otherwise it is dropped — and settled like any run's change: kept and
    committed if the gate passes, reverted if a test of it fails, held for another try if the baseline is red again.
    The instance is told each outcome in its inbox. Under its run lock, never waiting for it: a busy or paused instance
    is tried after the next green warm-up."""
    from . import gate_held
    from .core import paused
    if not gate_held.pending(name):
        return
    if paused(name):
        log(f"{name}: held code changes not re-gated now: the instance is paused")
        return
    root = instance_root(name)
    try:
        with run_lock(name, wait=False, kind="re-gate"):
            for meta in gate_held.pending(name):
                approved = _reconcile(name, root)
                files = sorted(meta["files"])
                when = meta.get("held_at", "?").replace("T", " ")
                if (moved := gate_held.applies(meta, approved)):
                    patch = _held_patch(name, meta)
                    gate_held.drop(name, meta["id"])
                    gate_held.log_run(name, "re-gated", files, f"no longer applies: {', '.join(moved)} changed since "
                                      "it was held", held=meta["id"], result="dropped")
                    log(f"{name}: held code change {meta['id']} dropped: no longer applies ({', '.join(moved)} changed)")
                    _tell(name, root, "held code change dropped (no longer applies)",
                          f"re-gate: dropped — my change to {', '.join(files)}, held at {when} because the gate's "
                          f"baseline was red, no longer applies: {', '.join(moved)} changed since. Nothing of it was "
                          f"put back. What it changed is in the patch below, if I still want it.\n\n```diff\n"
                          + patch + "```")
                    continue
                before = _snapshot(root, approved)
                gate_held.apply(name, root, meta)
                log(f"{name}: re-gating held code change {meta['id']}: {files}")
                out: dict = {}
                _settle(name, root, "re-gate", before, [], (), regating=meta, outcome=out)
                result = out.get("result", "")
                failing = "".join(f"\n- {n}" for n in out.get("failing") or [])
                text = {
                    "kept": f"re-gated: kept — my change to {', '.join(files)}, held at {when}, passed the gate on "
                            "the green baseline and is committed.",
                    "candidate-failed": f"re-gated: failed — my change to {', '.join(files)}, held at {when}, was "
                                        f"gated again on the green baseline and a check of it fails ({out.get('why')}); "
                                        "it was reverted and is no longer held."
                                        + (f" What I wrote is in {out['mine']}." if out.get("mine") else "")
                                        + (f"\n\nFailing:{failing}" if failing else ""),
                    "held": f"re-gated: still held — the gate's baseline is red again ({out.get('why')}); my change to "
                            f"{', '.join(files)} stays held and is tried again after the next green warm-up."
                            + (f"\n\nFailing:{failing}" if failing else ""),
                    "dropped": f"re-gated: dropped — my change to {', '.join(files)}, held at {when}, found the "
                               f"gate's baseline red {gate_held.TRIES} times ({out.get('why')}); it is no longer held."
                               + (f" What I wrote is in {out['mine']}." if out.get("mine") else "")
                               + (f"\n\nFailing:{failing}" if failing else ""),
                }.get(result)
                if text is None:   # nothing gated (the files already as held): it is done either way
                    gate_held.drop(name, meta["id"])
                    gate_held.log_run(name, "re-gated", files, "nothing to gate: the code already holds it",
                                      held=meta["id"], result="dropped")
                    text = (f"re-gated: nothing to do — my change to {', '.join(files)}, held at {when}, is already "
                            "in my code; it is no longer held.")
                elif result in ("kept", "candidate-failed"):
                    gate_held.drop(name, meta["id"])
                _tell(name, root, f"held code change re-gated: {result or 'nothing to do'}", text)
                if result in ("held", "dropped"):   # the baseline is red again: the others wait, their tries unspent
                    break
    except Busy:
        log(f"{name}: held code changes not re-gated now: busy ({busy_with(name) or 'a run'}); tried after the next "
            "green warm-up")
    except RunRefused as e:
        _refused(name, e)


def _held_patch(name: str, meta: dict) -> str:
    from . import gate_held
    try:
        text = (SUP_DIR / gate_held.HELD / name / meta["id"] / "candidate.patch").read_text(errors="replace")
    except OSError:
        return "(the patch could not be read)\n"
    return text[:4000] + ("\n[… cut]\n" if len(text) > 4000 else "")


def regate_all() -> int:
    """`shaft gate regate`: every instance's held changes, re-gated (regate_held). 0."""
    from . import gate_held
    for name in gate_held.waiting():
        try:
            regate_held(name)
        except Exception as e:   # one instance's trouble never keeps another's held changes waiting
            log(f"{name}: re-gating its held code changes FAILED: {type(e).__name__}: {e}")
    return 0


def _keep_rejected(name: str, folder: Path, code: dict[str, bytes], paths: list[str]) -> str:
    """Copy the bytes the gate is about to put back into `folder` (the supervisor's; the instance never reads it):
    code that was not a run's own may be the operator's. Returns the folder, or "" if nothing was there to keep."""
    there = [p for p in paths if p in code]
    try:
        for p in there:
            target = folder / p
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(code[p], gate.Link):   # a link: what it pointed to, as text, never the link
                target.with_name(target.name + ".link").write_text(code[p].target + "\n")
            elif isinstance(code[p], gate.Invalid):
                target.with_name(target.name + ".invalid").write_text(code[p].why + "\n")
            else:
                target.write_bytes(code[p])
    except OSError as e:                     # not kept: then not put back either, so nothing is lost
        raise RunRefused(f"the code the gate rejected could not be kept in {folder} ({e}); it was not put back: "
                         f"{', '.join(there)}") from None
    return str(folder) if there else ""


REVERTED = "notes/reverted"   # in the instance's own folder: a reverted change's files, as the instance wrote them


def _keep_for_instance(root: Path, code: dict, paths: list[str]) -> tuple[str, str]:
    """Copy what the gate is about to put back into the instance's own notes/reverted/<stamp>/, so the instance can
    read what it lost: a revert before any commit leaves no history (thelos, 2026-10-27: a failing test file it was
    building from was gone). notes/ is mind, not code: the copy is committed to its mind store and never gated or
    run. Every write goes through the no-link descriptors (safe_write*). Best effort, unlike _keep_rejected: a copy
    that cannot be made (a link the instance put in the way, a full disk) never stops the revert. Returns (the
    folder, relative to the instance, or ""; why no copy was made, or "")."""
    there = [p for p in paths if p in code]
    if not there:
        return "", ""
    rel = f"{REVERTED}/{datetime.now():%Y%m%d-%H%M%S-%f}"
    try:
        for p in there:
            v = code[p]
            if isinstance(v, gate.Link):          # a link: what it pointed to, as text, never the link
                safe_write(root, f"{rel}/{p}.link", v.target + "\n")
            elif isinstance(v, gate.Invalid):
                safe_write(root, f"{rel}/{p}.invalid", v.why + "\n")
            else:
                safe_write_bytes(root, f"{rel}/{p}", v, exclusive=True)
    except (OSError, UnsafePath) as e:
        return "", f"{type(e).__name__}: {e}"
    return rel + "/", ""


def _put_back(name: str, root: Path, approved: dict, paths: list[str]) -> None:
    """gate.restore, then a fresh read of every path it put back: nothing runs on code that is not the approved code
    (RunRefused, logged as "run refused": an alert on the panel). Codex's final review: restore gave up silently on
    a folder replaced by a link, nothing checked, and the next run launched what the link pointed to."""
    try:
        gate.restore(root, approved, paths)
        bad = gate.unrestored(root, approved, paths)
    except gate.RestoreFailed as e:
        raise RunRefused(f"its code could not be put back as approved: {e}") from None
    if bad:
        raise RunRefused(f"its code is not back as approved after a restore: {', '.join(bad)}")


def _check_kept(name: str, root: Path, gated: dict | None, kept: list[str]) -> None:
    """The kept code as the gate checked it, on disk too: a file written after the gate's snapshot (by a process that
    escaped the run) was committed as gated (commit_state) and is put back as gated here, loudly; nothing runs on
    what was slipped in (_put_back refuses the run if it cannot be put back)."""
    if not gated:
        return
    code = [p for p in kept if Path(p).parts[0] in gate.CODE_PATHS]
    drift = gate.unrestored(root, gated, code)
    if not drift:
        return
    log(f"{name}: FAILED check: {', '.join(drift)} written after the gate checked them — committed and put back as "
        "gated")
    journal_note(root, "Supervisor: code written after the gate",
                 "After the gate checked my code, " + ", ".join(drift) + " changed on disk (something still running "
                 "wrote it). What was committed is what the gate checked, and the file is put back to that.")
    _put_back(name, root, gated, drift)


def _record_approved(name: str, root: Path, code: dict[str, bytes]) -> None:
    """The settled code as the instance's approved code (baseline.py), for the next run's _reconcile."""
    from . import baseline
    try:
        baseline.write(name, root, code)
    except (Exception, SystemExit) as e:   # the next run gates what differs from the older record: logged, not fatal
        log(f"{name}: FAILED to record its approved code ({type(e).__name__}: {e}); the next run checks against "
            "the record before")


def _too_big_to_check(name: str, root: Path, before: dict | None, config_writes, why: str) -> None:
    """The gate fails closed when the code is too big to snapshot (core.Budget): no change of the run is checked, so
    none is kept — the files it began with go back, its set_config lines are undone, and the instance is told. What
    it added stays on disk, uncommitted, until its code is small enough to check again."""
    if before:
        _put_back(name, root, before, sorted(before))
    back = gate.revert_config(root, list(config_writes))
    log(f"{name}: code too big to check, nothing of this run's code kept: {why}")
    journal_note(root, "Supervisor: code change not checked",
                 f"My code (shaft/, tests/, plugins/, …) is too big for the gate to check ({why}; the bounds are "
                 "SHAFT_SNAPSHOT_MAX_MB, SHAFT_TREE_MAX_FILES and SHAFT_TREE_MAX_DEPTH), so none of this run's code "
                 "changes was checked or kept: the files it began with are back"
                 + (f", and my config.env lines ({', '.join(back)}) are undone" if back else "")
                 + ". Anything new stays uncommitted until my code is small enough to check.")


def _commit_outside_config(name: str, root: Path, writes: list[dict]) -> None:
    """config.env as changed from outside the instance — by the operator, who may edit it at any time, even
    mid-run — committed on its own, before the run's commit: never presented as the instance's change, and
    never undone with one of its changes on probation. `writes` are the instance's lines still in the file."""
    from .core import config_values
    try:
        text = safe_read(root, "config.env")
    except (OSError, UnsafePath):
        return
    outside, _ = gate.config_without(text, writes)
    head = git_wt(root, "show", "HEAD:config.env", check=False)
    if head.returncode == 0 and head.stdout == outside:
        return
    try:
        if [p for p in git_wt(root, "diff", "--cached", "--name-only", "-z").stdout.split("\0") if p]:
            log(f"{name}: config.env changed from outside, not committed: other changes are staged")
            return
        was, now = config_values(head.stdout if head.returncode == 0 else ""), config_values(outside)
        diff = [f"- {k}: {was.get(k, '(not set)')} -> {now.get(k, '(not set)')}"
                for k in sorted(was.keys() | now.keys()) if was.get(k) != now.get(k)]
        blob = git_wt(root, "hash-object", "-w", "--stdin", input=outside).stdout.strip()
        git_wt(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},config.env")
        git_wt(root, "-c", "user.name=SHAFT supervisor", "-c", "user.email=supervisor@shaft.localhost", "commit",
               "-q", "-m", f"{name}: config.env changed from outside the instance\n\nNot the instance's change (it "
               "sets config.env only through set_config): the operator's.\n" + "\n".join(diff or ["- (comments)"]))
    except subprocess.CalledProcessError as e:
        log(f"{name}: could not commit config.env changed from outside: {(e.stderr or e.stdout or '').strip()}")
        return
    log(f"{name}: committed config.env changed from outside the instance: {diff}")


def _commit_outside_drive(name: str, root: Path) -> None:
    """drive.md (what the instance is for, docs/plans/PLAN-thelos.md) as the operator left it, committed on its own: only they
    write it (the sandbox denies the instance), so any difference from HEAD is theirs. Uncommitted, it would stop
    `shaft update` (dirty worktree) and ride along in the instance's next commit as if it were its change. The
    instance is told in its journal: what it believes about its purpose may now be out of date."""
    path = root / "drive.md"
    if path.is_symlink() or path.is_dir():
        return
    drive = lambda: safe_read(root, "drive.md", "", errors="replace").strip()   # noqa: E731 — never through a link
    if not git_wt(root, "status", "--porcelain", "--untracked-files=all", "--", "drive.md").stdout.strip():
        return
    try:
        if [p for p in git_wt(root, "diff", "--cached", "--name-only", "-z").stdout.split("\0") if p]:
            log(f"{name}: drive.md changed from outside, not committed: other changes are staged")
            return
        git_wt(root, "add", "-A", "--", "drive.md")
        gone = not path.exists()
        git_wt(root, "-c", "user.name=SHAFT supervisor", "-c", "user.email=supervisor@shaft.localhost", "commit",
               "-q", "-m", f"{name}: drive.md changed by the operator\n\nNot the instance's change (its sandbox cannot "
               "write drive.md). " + ("Removed: the default drive applies." if gone else "Its drive is now:\n\n"
                                      + drive()), "--", "drive.md")
    except subprocess.CalledProcessError as e:
        log(f"{name}: could not commit drive.md changed from outside: {(e.stderr or e.stdout or '').strip()}")
        return
    journal_note(root, "My drive was changed by the operator",
                 ("The operator removed my drive.md: I am back to the drive every SHAFT instance starts with."
                  if gone else "The operator set what I am for (drive.md, in my system prompt from now on):\n\n"
                  + drive())
                 + "\n\nWhat my self-model and goals say about my purpose may now be out of date.")
    log(f"{name}: committed drive.md changed by the operator")


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
        stack.enter_context(Reply(self.name, self.kind))   # a running cycle pauses for it, and resumes after it
        try:
            try:
                stack.enter_context(run_lock(self.name, wait=False, kind=self.kind))
            except Busy:
                holder = busy_with(self.name) or "another run"
                if not holder.startswith(("talk", "api", *PAUSABLE)):
                    request_yield([self.name])   # anything else wraps up at its next turn; a cycle is paused instead
                if on_wait:
                    on_wait(holder)
                stack.enter_context(run_lock(self.name, wait=True, kind=self.kind))
        except BaseException:
            stack.close()                        # interrupted while waiting: no reply wants the instance any more
            raise
        try:
            approved = _reconcile(self.name, self.root)
        except RunRefused as e:
            stack.close()
            _refused(self.name, e)
            raise
        self.held = stack
        self.before = _snapshot(self.root, approved)
        self.handoffs_seen = len(self.broker.handoffs) if self.broker else 0
        self.config_seen = len(self.broker.config_writes) if self.broker else 0

    def end(self) -> None:
        if not self.held:
            return
        # The checks must finish: a Ctrl-C during the gate's tests (the talk client only intercepts it while a
        # reply streams) aborted them halfway — no revert, no commit, and an instance's broken plugin stayed
        # in its folder, blocking its updates (2026-10-13). So Ctrl-C is ignored until they are done.
        main = threading.current_thread() is threading.main_thread()
        old = signal.signal(signal.SIGINT, signal.SIG_IGN) if main else None
        try:
            handoffs = self.broker.handoffs[self.handoffs_seen:] if self.broker else []
            configured = self.broker.config_writes[self.config_seen:] if self.broker else []
            _settle(self.name, self.root, self.kind, self.before, handoffs, configured)
        finally:
            self.held.close()
            self.held = None
            if old is not None:
                signal.signal(signal.SIGINT, old)

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
    saved: dict = {}
    try:
        _trap_runs(saved)
        with Presence(name, talk_id, kind):
            return _conversation(name, root, kind, agent_args, talk_id, driver)
    except RunRefused:                           # Turn.begin said why
        return REFUSED
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)


def _conversation(name: str, root: Path, kind: str, agent_args: list[str], talk_id: str, driver) -> int:
    turn = Turn(name, root, kind)
    turn.begin()
    log(f"{name}: start {kind} {talk_id} {describe_knobs()}")
    try:
        broker, cmd, env = _launch(name, root, kind, agent_args, None, turn.before.get("code"))
    except BaseException:
        turn.held.close()
        raise
    turn.broker = broker
    broker.talk_id = talk_id   # record_order: an order heard in this conversation names it as its source
    try:
        proc = _popen(cmd, root, env)
        try:
            driver(proc, turn)
        finally:
            _finish(proc, root)   # its processes gone before the last reply is checked (B9)
            turn.end()
        code = proc.returncode
    except KeyboardInterrupt:
        code = 130
    finally:
        try:
            broker.stop()
        finally:
            if env.get("SHAFT_CODE_ROOT"):
                protected.remove(Path(env["SHAFT_CODE_ROOT"]))
    log(f"{name}: end {kind} {talk_id} exit={code} broker_calls={broker.counts or {}}")
    return code
