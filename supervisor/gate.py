"""Self-modification gate: detect an instance's code changes, test them, revert on failure.

The supervisor never imports agent code. It snapshots the instance's code
before a run; if the run changed it, the test suites run, and failing changes
are restored byte-for-byte from the snapshot.
The supervisor's own tests run in two parts; the shared part's pass is recorded
per commit (docs/plans/PLAN-gate-cache.md).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from . import sandbox
from .core import knob, positive_int

GATE_TIMEOUT_S, _ = knob(
    "SHAFT_GATE_TIMEOUT_S", "300", positive_int, "seconds each gate test stage may run before it times out",
    group="Runs and conversations", services=("runs", "api", "scheduler"))
GATE_WORKERS, _ = knob(
    "SHAFT_GATE_WORKERS", "4", positive_int, "processes each part of the supervisor's tests runs in at a gate",
    group="Runs and conversations", services=("runs", "api", "scheduler"))

# .python-version and requirements.txt declare the instance's own Python environment (supervisor/pyenv.py): code
# like the rest, tested when changed and committed when kept, so a lost .venv/ can be rebuilt from them.
CODE_PATHS = ("shaft", "tests", "plugins", ".python-version", "requirements.txt")
# Not config.env: the operator edits it too, at any time — kant's mid-cycle switch to Opus was reverted as the
# instance's change when its plugin failed the tests (2026-10-13). The instance changes it only through the
# broker's set_config (its sandbox cannot write the file), which records each line it set; revert_config undoes
# exactly those, and everything else in the file is the operator's.
PROJECT = Path(__file__).resolve().parent.parent


class Link:
    """A link standing where code belongs, as a snapshot records it: its target, never what it points to. Code paths
    hold no links — the template has none, and nothing an instance writes needs one — so a link there fails the gate
    whatever its target (runner._settle) and is never approved, kept or committed; restore takes it away. A snapshot
    passed over a link (read_tree and read_beneath refuse one, and the refusal was swallowed): `shaft/` swapped for a
    link to another folder of the instance vanished from the snapshot, the gate never saw it, and `python -m shaft`
    ran the linked package (Codex, final review of the integration)."""
    __slots__ = ("target",)

    def __init__(self, target: str):
        self.target = target

    def __eq__(self, other) -> bool:
        return isinstance(other, Link) and other.target == self.target

    def __hash__(self) -> int:
        return hash(("link", self.target))

    def __repr__(self) -> str:
        return f"Link({self.target!r})"


class Invalid:
    """Something standing where code belongs that is neither a plain file of one name nor a folder: a file with more
    than one name (a hard link: writing it changes its other names too), a pipe, a socket, a device. Recorded so the
    gate sees it — read_tree passed a pipe over without a trace — and refused like a Link (Codex on f82df7e)."""
    __slots__ = ("why",)

    def __init__(self, why: str):
        self.why = why

    def __eq__(self, other) -> bool:
        return isinstance(other, Invalid) and other.why == self.why

    def __hash__(self) -> int:
        return hash(("invalid", self.why))

    def __repr__(self) -> str:
        return f"Invalid({self.why!r})"


class RestoreFailed(Exception):
    """A path could not be put back: nothing may run on the code that is left (runner refuses the run)."""


def links(code: dict) -> list[str]:
    """The paths of a snapshot that are links."""
    return sorted(p for p, v in code.items() if isinstance(v, Link))


def refused(code: dict) -> list[str]:
    """The paths of a snapshot that can never be kept: links and Invalid entries."""
    return sorted(p for p, v in code.items() if isinstance(v, (Link, Invalid)))


def _kind(fmt: int) -> str:
    import stat
    return {stat.S_IFIFO: "a pipe", stat.S_IFSOCK: "a socket", stat.S_IFCHR: "a device",
            stat.S_IFBLK: "a device"}.get(fmt, "not a plain file")


def snapshot(instance_root: Path) -> dict[str, bytes | Link]:
    """The code files' bytes, listed and read through descriptors (core.walk_beneath, read_beneath): never through a
    link the instance planted, even one swapped in after a check — a failed change is restored from this, so a file
    read through a link would be written back into the instance's folder. A link at or under a code path is an entry
    of its own (Link: its target), so a link added, removed or retargeted is a change. TreeTooBig past the bounds
    (core.Budget, SHAFT_FILE_READ_MAX_MB for a single file): never a part of the code taken for all of it."""
    import stat
    from .core import Budget, TooBig, TreeTooBig, UnsafePath, dir_beneath, read_beneath, walk_beneath
    files: dict[str, bytes | Link] = {}
    budget = Budget()   # one for all the code paths: SHAFT_SNAPSHOT_MAX_MB in all; past it TreeTooBig, never a part
    skip = lambda rel: "__pycache__" in rel.split("/") or rel.split("/")[-1] == ".DS_Store"   # noqa: E731 — Finder's
    top = dir_beneath(instance_root)
    try:
        for name in CODE_PATHS:
            try:
                mode = os.stat(name, dir_fd=top, follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                files[name] = Link(os.readlink(name, dir_fd=top))
            elif stat.S_ISDIR(mode):
                for rel, f in walk_beneath(instance_root, name, skip, links=True, budget=budget, others=True):
                    if isinstance(f, str):
                        files[f"{name}/{rel}"] = Link(f)
                    elif isinstance(f, int):
                        files[f"{name}/{rel}"] = Invalid(_kind(f))
                    elif f is not None:
                        names = os.fstat(f.fileno()).st_nlink
                        files[f"{name}/{rel}"] = Invalid(f"a file with {names} names") if names > 1 else f.read()
            elif stat.S_ISREG(mode):
                names = os.stat(name, dir_fd=top, follow_symlinks=False).st_nlink
                try:
                    files[name] = Invalid(f"a file with {names} names") if names > 1 else \
                        read_beneath(instance_root, [name])
                except TooBig as e:
                    raise TreeTooBig(f"{instance_root}/{name}: {e}") from None
            else:
                files[name] = Invalid(_kind(stat.S_IFMT(mode)))
    finally:
        os.close(top)
    return files


def changed(before: dict, after: dict) -> list[str]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def _clear_links_above(instance_root: Path, rel: str) -> None:
    """Remove every link standing on the way to root/rel — the link itself, never its target — from the top down,
    through each folder's descriptor: an approved folder replaced by a link is then made again, not written through."""
    import stat
    from .core import dir_beneath
    parts = Path(rel).parts
    for i in range(1, len(parts)):
        fd = dir_beneath(instance_root, parts[:i - 1])
        try:
            mode = os.stat(parts[i - 1], dir_fd=fd, follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                os.unlink(parts[i - 1], dir_fd=fd)
                return                                   # nothing below it is there any more
            if not stat.S_ISDIR(mode):
                return
        except FileNotFoundError:
            return
        finally:
            os.close(fd)


def _write_fresh(instance_root: Path, rel: str, data: bytes) -> None:
    """root/rel as a new file holding `data`: written to a temporary name beside it (O_EXCL, no link followed) and
    renamed over what stands there, through the folder's descriptor — a link, a pipe or one name of a hard-linked
    file is replaced, never written through. Writing in place (O_TRUNC) changed every other name of the file (Codex
    on f82df7e). The mode of a plain file it replaces is kept (an executable script)."""
    import stat
    from .core import dir_beneath
    parts = Path(rel).parts
    fd = dir_beneath(instance_root, parts[:-1], make=True)
    tmp = f".shaft-restore-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        try:
            st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            mode = stat.S_IMODE(st.st_mode) if stat.S_ISREG(st.st_mode) else 0o644
            if stat.S_ISDIR(st.st_mode):
                raise RestoreFailed(f"{rel}: a folder stands where the file belongs")
        except FileNotFoundError:
            mode = 0o644
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as f:
                f.write(data)
                os.fchmod(f.fileno(), mode)
            os.rename(tmp, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)


def restore(instance_root: Path, before: dict, paths: list[str]) -> None:
    """Each path back as it was, written or removed through descriptors, no link followed: a link standing where an
    approved folder or file belongs is removed (the link, never its target) and the folder made again. RestoreFailed
    when a path cannot be put back — it was left as it was, silently, until Codex's final review."""
    from .core import UnsafePath, unlink_beneath
    for rel in sorted(paths, key=lambda r: (r.count("/"), r)):   # a linked folder goes before what was below it
        try:
            _clear_links_above(instance_root, rel)
            if rel not in before:
                unlink_beneath(instance_root, rel)
                continue
            if not isinstance(before[rel], bytes):
                raise RestoreFailed(f"{rel}: {before[rel]!r} is never put back as code")
            _write_fresh(instance_root, rel, before[rel])
        except RestoreFailed:
            raise
        except (OSError, UnsafePath) as e:
            raise RestoreFailed(f"{instance_root}/{rel} could not be put back: {type(e).__name__}: {e}") from None


_UNREADABLE = object()


def _entry(instance_root: Path, rel: str):
    """What stands at root/rel now, as a snapshot would record it: bytes, a Link, or None (nothing, or a folder — no
    file of that name); _UNREADABLE when it cannot be reached without following a link or read."""
    import stat
    from .core import UnsafePath, dir_beneath, read_beneath
    parts = Path(rel).parts
    try:
        fd = dir_beneath(instance_root, parts[:-1])
    except FileNotFoundError:
        return None
    except (OSError, UnsafePath):
        return _UNREADABLE
    try:
        st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        mode = st.st_mode
        if stat.S_ISLNK(mode):
            return Link(os.readlink(parts[-1], dir_fd=fd))
    except FileNotFoundError:
        return None
    finally:
        os.close(fd)
    if stat.S_ISDIR(mode):
        return None
    if not stat.S_ISREG(mode):
        return Invalid(_kind(stat.S_IFMT(mode)))
    if st.st_nlink > 1:
        return Invalid(f"a file with {st.st_nlink} names")
    try:
        return read_beneath(instance_root, parts)
    except (OSError, UnsafePath, ValueError):
        return _UNREADABLE


def unrestored(instance_root: Path, approved: dict, paths: list[str]) -> list[str]:
    """Of `paths`, those not back as `approved` after a restore, read afresh path by path (no link followed): the
    check runner makes before anything may run on the code again."""
    return [p for p in paths if _entry(instance_root, p) != approved.get(p)]


def config_without(text: str, writes: list[dict]) -> tuple[str, list[str]]:
    """config.env text with the instance's set_config writes undone, newest first, and the keys put back.
    A line changed again after the instance set it (the operator's later edit) is left as it is."""
    from .core import config_line, config_with
    back = set()
    for w in reversed(writes):
        if config_line(text, w["key"]) == w["new"]:
            text = config_with(text, w["key"], w["old"])
            back.add(w["key"])
    return text, sorted(back)


def revert_config(instance_root: Path, writes: list[dict]) -> list[str]:
    """Undo the instance's set_config writes in its config.env, keeping every other line. Returns the keys."""
    from .core import config_lock, contained, safe_read, safe_write
    if not writes:
        return []
    path = contained(instance_root, "config.env")
    with config_lock(path):                          # not over an operator's write landing at the same moment
        text, back = config_without(safe_read(instance_root, "config.env", ""), writes)
        if back:
            safe_write(instance_root, "config.env", text)
    return back


COMMITS_ID_S = 10                  # how long commits_id waits for git
TREES = ".supervisor/gate-tests"   # under the project: the tests' sandbox probes need it inside the home folder
TREE_IDLE_S = 86400                # a committed tree no gate has used for a day is removed


def _git(repo: Path, *args: str, timeout_s: float | None = None) -> bytes:
    """git's stdout. With `timeout_s`, a git that has not answered by then is killed and raises."""
    p = subprocess.Popen(["git", "-C", str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = p.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise RuntimeError(f"git -C {repo} {' '.join(args)}: no answer in {timeout_s}s") from None
    if p.returncode:
        raise RuntimeError(f"git -C {repo} {' '.join(args)}: {err.decode(errors='replace').strip()}")
    return out


def _extract(repo: Path, rev: str, dest: Path) -> None:
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(_git(repo, "archive", "--format=tar", rev))) as tar:
        tar.extractall(dest, filter="data")


def _commits(project: Path, timeout_s: float | None = None) -> tuple[str, str]:
    template = project / "template"
    head = _git(project, "rev-parse", "HEAD", timeout_s=timeout_s).decode().strip()
    tpl = (_git(template, "rev-parse", "HEAD", timeout_s=timeout_s).decode().strip()
           if (template / ".git").exists() else "")
    return head, tpl


def commits_id(project: Path | None = None) -> str:
    """The name of the committed tree's folder for the project's commits now: what the scheduler's warm-up watches,
    every tick — so git gets COMMITS_ID_S to answer, never the scheduler's whole loop."""
    head, tpl = _commits(Path(project or PROJECT), COMMITS_ID_S)
    return head[:16] + (f"-{tpl[:16]}" if tpl else "")


def committed_tree(project: Path | None = None) -> tuple[Path, str]:
    """A clean copy of the project's last commit, with its template's last commit at template/, where the gate runs
    the supervisor's tests: several sessions edit the main checkout at once, and a half-written sandbox.py there
    failed 37 supervisor tests and reverted shaft's good plugins/write_gate change (2026-10-16 09:40). One copy per
    pair of commits, made once and reused, in a folder named like the project's and committed as its own repository
    (the runner refuses a project without .git; a test's git commands never reach the real one). Nothing of the
    running system is in it: an empty instances/ and a .env without secrets, so the sandbox probes still aim at files
    that exist, and no test can touch a real instance. The shared part's records and lock live beside the tree in
    the commits' folder and are removed with it. Returns the copy and the commits' label."""
    import shutil
    import time
    project = Path(project or PROJECT)
    template = project / "template"
    head, tpl = _commits(project)
    label = head[:10] + (f" template {tpl[:10]}" if tpl else "")
    base = project / TREES
    base.mkdir(parents=True, exist_ok=True)
    commits = base / (head[:16] + (f"-{tpl[:16]}" if tpl else ""))
    tree = commits / project.name
    if not tree.is_dir():
        staging = Path(tempfile.mkdtemp(prefix=".new-", dir=base))
        _extract(project, head, staging / project.name)
        if tpl:
            _extract(template, tpl, staging / project.name / "template")
        for d in (".supervisor", "instances"):           # the tests' own scratch folders, not the real ones
            (staging / project.name / d).mkdir(exist_ok=True)
        (staging / project.name / ".env").write_text("# the gate's copy of the project: no secrets\n")
        for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "--no-verify", "-m", label)):
            _git(staging / project.name, "-c", "user.name=shaft gate", "-c", "user.email=gate@shaft.invalid",
                 "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args)
        try:
            staging.rename(commits)
        except OSError:                                   # another gate run made it first
            shutil.rmtree(staging, ignore_errors=True)
    now = time.time()
    os.utime(commits, (now, now))
    for old in base.iterdir():
        if old != commits and old.is_dir() and now - old.stat().st_mtime > TREE_IDLE_S:
            shutil.rmtree(old, ignore_errors=True)
    return tree, label


PLUGIN_CHECK_S = 120   # the plugins' check only imports and validates them


class StageTimedOut(Exception):
    """A stage of the gate ran past its time. Counted as a failure: the gate never lets a change through unjudged."""

    def __init__(self, stage: str, seconds: int, tail: str = ""):
        super().__init__(f"{stage}: timed out after {seconds}s, counted as failed")
        self.stage, self.seconds, self.tail = stage, seconds, tail


STOP_GRACE_S = 2   # how long a timed-out stage has to exit on SIGTERM before its whole process group is killed


def _end_group(p: subprocess.Popen) -> None:
    """Kill everything left in the stage's process group and return once it is empty. subprocess.run's timeout
    killed only the direct child (sandbox-exec, exec'd into python): a worker a test had started went on writing the
    instance's code after the gate had failed and runner._settle had reverted the change (found 2026-10-16)."""
    import signal
    import time
    deadline = time.monotonic() + 10
    while True:   # SIGKILL again until none is left: a member may have forked while the first one was delivered
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):   # empty, or only a member no signal of ours can reach
            return
        p.wait()                                         # the leader, reaped, so it no longer counts as a member
        if time.monotonic() > deadline:
            return
        time.sleep(0.02)


def stop_group(p: subprocess.Popen, grace_s: float = STOP_GRACE_S) -> None:
    """End the process group `p` leads, on any way out of a run or a command — finished, failed, timed out,
    interrupted: SIGTERM to all of it (and SIGCONT, so a stopped member gets it too), up to `grace_s` for it to empty,
    then SIGKILL until nothing is left (_end_group). Only the direct child was killed, and only on a timeout, so a
    process the agent or a `run` command had started in the background went on writing the instance's code after the
    gate had judged it (B9, review of 2026-10-24). Returns once the group is empty (or only holds a member no signal
    of ours can reach); `p` is reaped. A process that starts its own session (setsid) leaves the group and is not
    reached."""
    import signal
    import time
    try:
        os.killpg(p.pid, signal.SIGTERM)
        os.killpg(p.pid, signal.SIGCONT)
    except (ProcessLookupError, PermissionError):
        p.poll()
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        p.poll()                                  # the leader, reaped once it exits: a zombie still counts as a member
        try:
            os.killpg(p.pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.02)
    _end_group(p)


_STAGES: set[subprocess.Popen] = set()   # the stages running in this process (end_stages)


def end_stages() -> None:
    """End every stage this process is running, with all it started (stop_group): a stage starts its own session, so
    its parent's death alone does not end it — a hung test of a warm-up stopped with the scheduler ran on (Codex, M2)."""
    for p in list(_STAGES):
        stop_group(p)


def stop_stages_on_signal() -> None:
    """For a process whose work is a gate stage (`shaft gate warm`): on SIGTERM or SIGINT end the stages first, then
    let the signal do what it was sent to do."""
    import signal

    def on_signal(signum, frame):
        end_stages()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, on_signal)


def _stage(stage: str, argv: list[str], timeout_s: int, input: str | None = None,
           **kw) -> subprocess.CompletedProcess:
    """One stage, in its own process group, all of which ends with it. A timeout gets SIGTERM, then SIGKILL after
    STOP_GRACE_S, and raises StageTimedOut with the tail of what the stage printed. On a normal exit whatever it left
    running in the group is killed too: a failed gate is followed by a revert that a leftover could undo. A process
    that starts its own session (setsid) leaves the group and is not reached. `input`: written to its stdin, which is
    then closed (otherwise stdin is inherited)."""
    import signal
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         stdin=subprocess.PIPE if input is not None else None, start_new_session=True, **kw)
    _STAGES.add(p)
    timed_out = False
    try:
        try:
            out, err = p.communicate(input=input, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                out, err = p.communicate(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:   # it ignores SIGTERM, or a member still holds the pipes
                _end_group(p)
                try:
                    out, err = p.communicate(timeout=STOP_GRACE_S)
                except subprocess.TimeoutExpired:   # held by a process that left the group: give up on the output
                    out, err = "", ""
                    for f in (p.stdout, p.stderr):
                        f.close()
    finally:
        _end_group(p)
        _STAGES.discard(p)
    if timed_out:
        raise StageTimedOut(stage, timeout_s, (out + err)[-2000:])   # what it printed before it was stopped
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


SPLIT_RUNNER = "supervisor/gate_suite.py"   # in the committed tree: runs the instance part or the shared part
SHARED_RECORD = "shared-passed-{}.json"     # beside a committed tree, one per key: its shared part passed
                                            # (docs/plans/PLAN-gate-cache.md)
SHARED_LOCK = "shared.lock"                 # beside it too: one run of the shared part per commit at a time
WARM_REFRESH_S = TREE_IDLE_S // 2           # a record this old is run again by the warm-up while gates still take it
WARM_FAILED = 3                             # warm()'s exit when it failed and logged the alert itself


def _python_id() -> str:
    """The interpreter the supervisor's tests run on, as it is on disk: an upgrade in place changes it."""
    from .core import PYTHON
    real = os.path.realpath(PYTHON)
    st = os.stat(real)
    return f"{real}:{st.st_size}:{st.st_mtime_ns}"


def _env_id(env: dict) -> str:
    """The settings the tests see (SHAFT_*), not the instance the gate checks: one digest."""
    import hashlib
    items = sorted((k, v) for k, v in env.items() if k.startswith("SHAFT_") and k != "SHAFT_UNDER_TEST")
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()


def shared_key(tree: Path, env: dict) -> dict:
    """What a shared pass depends on: the commits (the committed tree's folder, named by both), the Python, the
    settings. Anything else differing makes a record someone else's."""
    return {"commits": tree.parent.name, "python": _python_id(), "env": _env_id(env)}


def record_path(tree: Path, key: dict) -> Path:
    """One record per key, so gates whose settings differ (a terminal's `shaft check`, a service) keep their own
    instead of overwriting each other's."""
    import hashlib
    return tree.parent / SHARED_RECORD.format(hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16])


def recorded_pass(tree: Path, key: dict, max_age_s: float = TREE_IDLE_S) -> dict | None:
    """The shared part's recorded pass for this key, or None: no record, another key, no test run, older than
    `max_age_s` (TREE_IDLE_S: a library upgraded under the same commit), dated in the future, or not the shape _record
    writes."""
    try:
        r = json.loads(record_path(tree, key).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(r, dict) or any(r.get(k) != v for k, v in key.items()):
        return None
    if type(r.get("tests_run")) is not int or r["tests_run"] <= 0 or type(r.get("seconds")) not in (int, float):
        return None
    try:
        age = (datetime.now() - datetime.fromisoformat(r["passed_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None
    return r if 0 <= age <= max_age_s else None


def _record(tree: Path, key: dict, tests_run: int, seconds: float, label: str) -> None:
    rec = {**key, "label": label, "tests_run": tests_run, "seconds": round(seconds, 1),
           "passed_at": datetime.now().isoformat(timespec="seconds")}
    path = record_path(tree, key)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, path)


@contextmanager
def _shared_lock(tree: Path, timeout_s: int, stage: str):
    """One run of the shared part per commit at a time: another gate on the same commit waits for its result instead
    of starting a second one. The wait is bounded by the stage's timeout, and running out of it fails the gate."""
    import fcntl
    import time
    with open(tree.parent / SHARED_LOCK, "a") as f:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise StageTimedOut(stage, timeout_s, "waiting for another gate's run of the shared part on "
                                                          "this commit\n") from None
                time.sleep(0.2)
        yield


def _part(part: str, tree: Path, env: dict, timeout_s: int, label: str) -> tuple[bool, str, str, int | None, float]:
    """One part of the supervisor's tests, by the committed tree's own runner: (passed, the section for the
    output, the whole stderr, tests run, seconds). It passes only with exit 0 and the runner's own report carrying
    the token given on its stdin — never a line a test printed."""
    import secrets
    import time
    from . import gate_suite
    from .core import PYTHON
    nonce = secrets.token_hex(16)
    t0 = time.monotonic()
    p = _stage(f"supervisor invariants ({label}), {part} part",
               [PYTHON, "-E", "-s", "-m", "supervisor.gate_suite", part, "--nonce-stdin", "--workers", str(GATE_WORKERS)],
               timeout_s, input=nonce + "\n", cwd=tree, env=env)
    seconds = time.monotonic() - t0
    r = gate_suite.report(p.stdout, nonce)
    ok = p.returncode == 0 and r is not None and r["ok"]
    head = (f"-- {part} part: {r['tests_run']} tests --\n" if r is not None else
            f"-- {part} part: no report from its runner (exit {p.returncode}), counted as failed --\n")
    # The cut section is for the failures: the timings line (kilobytes of test names) stays in the full log only.
    err = "".join(l for l in p.stderr.splitlines(keepends=True) if not l.startswith(gate_suite.TIMES))
    section = head + "".join(f"{t}\n" for t in failing_tests(err)) + err[-3000:]
    return ok, section, p.stderr, (r["tests_run"] if r is not None else None), seconds


def _log(msg: str) -> None:
    """The supervisor log, best effort: a log that cannot be written (a full disk, its permissions) must not make the
    gate raise — raising leaves the change unsettled, the next run's baseline, ungated (B8; Codex on M1)."""
    from . import core
    try:
        core.log(msg)
    except OSError as e:
        print(f"{msg} (supervisor.log could not be written: {e})", file=sys.stderr)


def _shared_part(tree: Path, label: str, env: dict, timeout_s: int, max_age_s: float) -> tuple[bool, str, str, bool]:
    """The shared part, under the commit's lock: from a record of this key no older than `max_age_s`, else run, and
    recorded when it passes with tests run. (passed, section, whole stderr, whether it ran now)."""
    key = shared_key(tree, env)
    with _shared_lock(tree, timeout_s, f"supervisor invariants ({label}), shared part"):
        if (rec := recorded_pass(tree, key, max_age_s)):
            return True, (f"-- shared part: passed on this commit at {rec['passed_at'][11:16]} ({rec['tests_run']} "
                          f"tests, {rec['seconds']:.0f} s), not run again --\n"), "", False
        try:
            ok, section, full, ran, seconds = _part("shared", tree, env, timeout_s, label)
        except BaseException:   # timed out or could not run: no pass now, so an older one must not stand in for it
            record_path(tree, key).unlink(missing_ok=True)
            raise
        if ok and ran:
            try:
                _record(tree, key, ran, seconds, label)
                _log(f"gate: shared supervisor tests passed on {label} ({ran} tests, {seconds:.0f}s), recorded")
            except OSError as e:   # the pass stands; the next gate runs the part again
                _log(f"gate: shared supervisor tests passed on {label} but the record could not be written: {e}")
        elif ok:
            _log(f"gate: the shared supervisor tests on {label} ran no test: nothing recorded")
        else:   # a pass recorded earlier no longer holds: a failed refresh must not leave it for the gates (Codex, M2)
            record_path(tree, key).unlink(missing_ok=True)
        return ok, section, full, True


def warm(timeout_s: int = GATE_TIMEOUT_S) -> int:
    """The scheduler's warm-up (`shaft gate warm`, autonomy.gate_warm): run the shared part on the project's commits
    now unless a record of the gates' key is younger than WARM_REFRESH_S, so the first gate after a merge reads it
    instead of running it while holding its instance. A failure is logged as an alert — on main, every instance's code
    change would fail its gate — and recorded nowhere. 0: passed or fresh; WARM_FAILED: failed, timed out or could not run,
the alert logged (any other exit, such as a crash before this, is the scheduler's to report)."""
    label = "?"
    try:
        tree, label = committed_tree()
        if not (tree / SPLIT_RUNNER).is_file():
            raise RuntimeError(f"the committed tree {tree} has no {SPLIT_RUNNER}")
        env = {k: v for k, v in os.environ.items() if k != "SHAFT_UNDER_TEST"}   # the key gates compute
        ok, _, full, ran = _shared_part(tree, label, env, timeout_s, WARM_REFRESH_S)
    except StageTimedOut as t:
        _log(f"gate: warm-up of the shared supervisor tests on {label} FAILED: {t}")
        return WARM_FAILED
    except Exception as e:
        _log(f"gate: warm-up of the shared supervisor tests on {label} FAILED: {type(e).__name__}: {e}")
        return WARM_FAILED
    if ok:
        return 0
    names = failing_tests(full)
    try:
        kept = f"; full log {keep_log(Path('gate-warm'), [f'== shared part ({label}) ==', full])}"
    except OSError:
        kept = ""
    _log(f"gate: shared supervisor tests FAILED on {label} — every instance's code change fails its gate until this "
         f"is fixed" + (f"; failing: {'; '.join(names)}" if names else "") + kept)
    return WARM_FAILED


BUDGET_SHARE = 2     # each part must pass within this share of the stage limit: room for a loaded machine
TEST_BUDGET_S = 10   # and no one supervisor test may take longer: the change that adds a slow test fails, not a later one


def budget(timeout_s: int = GATE_TIMEOUT_S) -> int:
    """`shaft gate budget`, the check before a merge (agent-overlay.yaml's gate, docs/plans/PLAN-gate-5min.md): both
    parts of the supervisor's tests on the last commits, the way a gate runs them (committed_tree, _part), the
    instance part against the template, its own cases included. Fails when a part fails, takes more than
    1/BUDGET_SHARE of the stage limit, holds a test slower than TEST_BUDGET_S, or runs a test twice; prints where the
    time went either way. Uncommitted changes are not in it. 0 within budget, 1 not."""
    from . import gate_suite
    tree, label = committed_tree()
    # No instance under test: the invariants then read the tree's template/ and run the template's own cases too (every
    # CONFIDENCE_MODE, TemplateMeasurement), which they skip for an instance (Codex review).
    env = {k: v for k, v in os.environ.items() if k != "SHAFT_UNDER_TEST"}
    print(f"gate budget on {label}: each part within {timeout_s // BUDGET_SHARE}s of the {timeout_s}s stage limit, "
          f"no test over {TEST_BUDGET_S}s, {GATE_WORKERS} workers")
    within = True
    for part in ("instance", "shared"):
        try:
            ok, section, full, ran, seconds = _part(part, tree, env, timeout_s, label)
        except StageTimedOut as t:
            print(f"\n{part} part: {t}\n{t.tail}")
            within = False
            continue
        times = gate_suite.times(full) or {}
        slow = [(n, s) for n, s in times.get("slowest_tests", []) if s > TEST_BUDGET_S]
        twice = times.get("run_twice", [])
        over = seconds > timeout_s / BUDGET_SHARE
        within &= ok and not over and not slow and not twice
        print(f"\n{part} part: {ran} tests in {seconds:.0f}s"
              + ("" if ok else " FAILED") + (f", over its {timeout_s // BUDGET_SHARE}s budget" if over else ""))
        for n, s in slow:
            print(f"  over {TEST_BUDGET_S}s: {n} ({s:.1f}s)")
        for n in twice:
            print(f"  run twice (a TestCase imported into another module?): {n}")
        print("  slowest modules: " + ", ".join(f"{n.rpartition('.')[2]} {s:.0f}s"
                                                for n, s in times.get("slowest_modules", [])[:5]))
        print("  slowest tests:   " + ", ".join(f"{n.split(' ')[0]} {s:.1f}s" for n, s in times.get("slowest_tests", [])[:5]))
        if not ok:
            print(section)
    print("\nWITHIN BUDGET" if within else "\nOVER BUDGET")
    return 0 if within else 1


def _supervisor_tests(instance_root: Path, tree: Path, label: str, timeout_s: int) -> tuple[bool, str, str]:
    """The supervisor's own tests against this instance (docs/plans/PLAN-gate-cache.md): the shared part from its
    record when this commit's passed already, else run (and recorded when it passes); then the instance part, every
    time. (passed, the section for the output, the whole output for the kept log)."""
    if not (tree / SPLIT_RUNNER).is_file():
        raise RuntimeError(f"the committed tree {tree} has no {SPLIT_RUNNER}: the supervisor's tests cannot be split")
    env = {**os.environ, "SHAFT_UNDER_TEST": str(instance_root)}
    shared_ok, shared, shared_full, _ = _shared_part(tree, label, env, timeout_s, TREE_IDLE_S)
    inst_ok, inst, inst_full, _, _ = _part("instance", tree, env, timeout_s, label)
    return shared_ok and inst_ok, shared + inst, shared_full + inst_full


def run_tests(instance_root: Path, home: Path, timeout_s: int = GATE_TIMEOUT_S,
              env_root: Path | None = None) -> tuple[bool, str]:
    """Supervisor invariants against this instance's code (trusted test code, run from the last commit — never from
    what another session is editing; it sandboxes the agent itself) in two parts (_supervisor_tests): the modules that
    read the instance, every time, and the rest once per commit, their pass recorded beside the committed tree
    (docs/plans/PLAN-gate-cache.md). Then its plugins' check and its own tests inside
    its sandbox, on its own Python environment (supervisor/pyenv.py). `env_root`: where that environment lives when
    `instance_root` is a scratch copy (coder hand-off, trial gate) — read-only from the copy. A stage that times out,
    and any error of the gate's own, fails the gate, named in the output and the supervisor log: raised instead, it
    left the change on disk unsettled, neither kept nor reverted, and the next run took it as its baseline — ungated.
    Nothing left in a stage's process group outlives it (_stage), so no leftover there writes the instance's code after
    the caller's revert."""
    from . import pyenv
    from .core import safe_mkdir
    env_root = Path(env_root or instance_root)
    outputs, ok = [], True
    full: list[str] = []   # the supervisor stage uncut, kept in a file when the gate fails (keep_log)

    try:
        tree, label = committed_tree()
    except Exception as e:   # fail closed: a change kept unsettled would pass ungated as the next run's baseline
        return False, f"== supervisor invariants ==\nno committed copy of the supervisor to test with: {e!r}"
    prof = None
    try:
        py = pyenv.python(env_root)
        tmp = safe_mkdir(instance_root, ".tmp")       # UnsafePath when the instance made .tmp a link: a failed gate
        fd, prof_name = tempfile.mkstemp(prefix="shaft-tests-", suffix=".sb")   # outside the instance
        prof = Path(prof_name)
        with os.fdopen(fd, "w") as f:
            f.write(sandbox.profile(instance_root, home, [], env_root=env_root))
            # #86: tests may use scratch space, but cannot chmod, replace or rewrite the code they judge.
            code_root = instance_root.resolve()
            f.write(f"(deny file-write* (literal {sandbox._q(code_root)}))\n")
            for name in CODE_PATHS:
                f.write(f"(deny file-write* (subpath {sandbox._q(code_root / name)}))\n")
        sup_ok, section, sup_full = _supervisor_tests(instance_root, tree, label, timeout_s)
        outputs.append(f"== supervisor invariants ({label}) ==\n" + section)
        full.append(f"== supervisor invariants ({label}) ==\n" + sup_full)
        ok &= sup_ok

        env = sandbox.agent_env(os.environ, TMPDIR=str(tmp), SHAFT_PYTHON=py)
        if (instance_root / "plugins").is_dir():
            plug = _stage("plugins", sandbox.wrap(prof, [py, "-E", "-s", "-m", "shaft.plugin_runner", "--check"]),
                          min(PLUGIN_CHECK_S, timeout_s), cwd=instance_root, env=env)
            outputs.append("== plugins ==\n" + (plug.stdout + plug.stderr)[-2000:])
            ok &= plug.returncode == 0
        if (instance_root / "tests").is_dir():
            passed, out = run_instance_tests(instance_root, prof, env, py, timeout_s)
            outputs.append("== instance tests ==\n" + out)
            ok &= passed
    except StageTimedOut as t:
        _log(f"gate: {instance_root}: {t}")
        outputs.append(f"== {t.stage} ==\n" + (t.tail + "\n" if t.tail else "")
                       + f"{t}; the checks after it did not run")
        ok = False
    except Exception as e:
        # Fail closed, like a timeout: any failure of the gate's own machinery (no sandbox-exec, a link where .tmp
        # should be, a full disk) is a failed gate. Only StageTimedOut was caught, so anything else left settlement
        # halfway — the change neither tested nor reverted, the next run's baseline (B8, review of 2026-10-24).
        _log(f"gate: {instance_root}: the gate itself failed, counted as failed: {type(e).__name__}: {e}")
        outputs.append(f"== gate error ==\n{type(e).__name__}: {e}\n(the gate could not run its checks: counted as "
                       "failed, the checks after it did not run)")
        ok = False
    finally:
        if prof is not None:
            prof.unlink(missing_ok=True)
    if not ok:
        try:
            outputs.append(f"full log: {keep_log(instance_root, full + outputs[len(full):])}")
        except OSError as e:
            outputs.append(f"(the full log could not be kept: {e})")
    return ok, "\n".join(outputs)


GATE_LOGS_KEPT = 50


def failing_tests(output: str) -> list[str]:
    """The FAIL:/ERROR: header of every test a unittest run names as failed, in order, each once. The gate puts them
    ahead of each stage's cut output, so they survive any later cut."""
    return list(dict.fromkeys(m.group(0) for m in re.finditer(r"^(?:FAIL|ERROR): .+$", output, re.M)))


def keep_log(instance_root: Path, parts: list[str]) -> Path:
    """A failed gate's whole output in .supervisor/gate-logs/, newest GATE_LOGS_KEPT kept: what is returned is cut
    to fit a journal note, and on 2026-10-19 that cut lost the name of every one of 8 failing supervisor tests, so
    neither the instance nor the operator could tell what had reverted five good changes."""
    from .core import SUP_DIR
    d = SUP_DIR / "gate-logs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{Path(instance_root).name}-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    path.write_text("\n".join(parts))
    for old in sorted(d.glob("*.log"), key=lambda q: q.stat().st_mtime)[:-GATE_LOGS_KEPT]:
        old.unlink(missing_ok=True)
    return path


NO_TESTS_RAN = 5   # unittest's exit code when discovery finds no tests (Python 3.12+)


def run_instance_tests(instance_root: Path, prof: Path, env: dict, py: str, timeout_s: int = GATE_TIMEOUT_S) -> tuple[bool, str]:
    """The instance's own tests, inside its sandbox. `-t .` only when tests/ is a package:
    a plain folder of test files must work too (a correct coder-written test was rejected
    because tests/ had no __init__.py)."""
    top = ["-t", "."] if (instance_root / "tests" / "__init__.py").exists() else []
    agent = _stage("instance tests", sandbox.wrap(prof, [py, "-E", "-s", "-m", "unittest", "discover", "-s", "tests",
                                                         *top]), timeout_s, cwd=instance_root, env=env)
    if agent.returncode == NO_TESTS_RAN:
        # A tests/ folder with nothing in it (kant's held only a stale __pycache__) is not a failing suite: read as
        # one, it rolled back every update and self-change. A test file that fails to import still fails (exit 1).
        return True, agent.stderr[-3000:] + "\n(no tests in tests/: nothing to fail)"
    return agent.returncode == 0, agent.stderr[-3000:]
