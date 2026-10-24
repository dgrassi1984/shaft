"""Self-modification gate: detect an instance's code changes, test them, revert on failure.

The supervisor never imports agent code. It snapshots the instance's code
before a run; if the run changed it, the test suites run, and failing changes
are restored byte-for-byte from the snapshot.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import sandbox

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


class RestoreFailed(Exception):
    """A path could not be put back: nothing may run on the code that is left (runner refuses the run)."""


def links(code: dict) -> list[str]:
    """The paths of a snapshot that are links."""
    return sorted(p for p, v in code.items() if isinstance(v, Link))


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
                for rel, f in walk_beneath(instance_root, name, skip, links=True, budget=budget):
                    if isinstance(f, str):
                        files[f"{name}/{rel}"] = Link(f)
                    elif f is not None:
                        files[f"{name}/{rel}"] = f.read()
            elif stat.S_ISREG(mode):
                try:
                    files[name] = read_beneath(instance_root, [name])
                except TooBig as e:
                    raise TreeTooBig(f"{instance_root}/{name}: {e}") from None
            else:
                raise UnsafePath(f"{instance_root}/{name} is neither a file, a folder nor a link")
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


def restore(instance_root: Path, before: dict, paths: list[str]) -> None:
    """Each path back as it was, written or removed through descriptors, no link followed: a link standing where an
    approved folder or file belongs is removed (the link, never its target) and the folder made again. RestoreFailed
    when a path cannot be put back — it was left as it was, silently, until Codex's final review."""
    from .core import UnsafePath, safe_write_bytes, unlink_beneath
    for rel in sorted(paths, key=lambda r: (r.count("/"), r)):   # a linked folder goes before what was below it
        try:
            _clear_links_above(instance_root, rel)
            if rel not in before:
                unlink_beneath(instance_root, rel)
                continue
            if isinstance(before[rel], Link):
                raise RestoreFailed(f"{rel}: a link is never put back as code")
            try:
                safe_write_bytes(instance_root, rel, before[rel])     # in place: its mode (an executable) is kept
            except UnsafePath:
                if not unlink_beneath(instance_root, rel):            # a link put in its place goes, never its target
                    raise
                safe_write_bytes(instance_root, rel, before[rel])
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
        mode = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode
        if stat.S_ISLNK(mode):
            return Link(os.readlink(parts[-1], dir_fd=fd))
    except FileNotFoundError:
        return None
    finally:
        os.close(fd)
    if stat.S_ISDIR(mode):
        return None
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


TREES = ".supervisor/gate-tests"   # under the project: the tests' sandbox probes need it inside the home folder
TREE_IDLE_S = 86400                # a committed tree no gate has used for a day is removed


def _git(repo: Path, *args: str) -> bytes:
    """git's stdout."""
    p = subprocess.Popen(["git", "-C", str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if p.returncode:
        raise RuntimeError(f"git -C {repo} {' '.join(args)}: {err.decode(errors='replace').strip()}")
    return out


def _extract(repo: Path, rev: str, dest: Path) -> None:
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(_git(repo, "archive", "--format=tar", rev))) as tar:
        tar.extractall(dest, filter="data")


def committed_tree(project: Path | None = None) -> tuple[Path, str]:
    """A clean copy of the project's last commit, with its template's last commit at template/, where the gate runs
    the supervisor's tests: several sessions edit the main checkout at once, and a half-written sandbox.py there
    failed 37 supervisor tests and reverted shaft's good plugins/write_gate change (2026-10-16 09:40). One copy per
    pair of commits, made once and reused, in a folder named like the project's and committed as its own repository
    (the runner refuses a project without .git; a test's git commands never reach the real one). Nothing of the
    running system is in it: an empty instances/ and a .env without secrets, so the sandbox probes still aim at files
    that exist, and no test can touch a real instance. Returns the copy and the commits' label."""
    import shutil
    import time
    project = Path(project or PROJECT)
    template = project / "template"
    head = _git(project, "rev-parse", "HEAD").decode().strip()
    tpl = _git(template, "rev-parse", "HEAD").decode().strip() if (template / ".git").exists() else ""
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


def _stage(stage: str, argv: list[str], timeout_s: int, **kw) -> subprocess.CompletedProcess:
    """One stage, in its own process group, all of which ends with it. A timeout gets SIGTERM, then SIGKILL after
    STOP_GRACE_S, and raises StageTimedOut with the tail of what the stage printed. On a normal exit whatever it left
    running in the group is killed too: a failed gate is followed by a revert that a leftover could undo. A process
    that starts its own session (setsid) leaves the group and is not reached."""
    import signal
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True, **kw)
    timed_out = False
    try:
        try:
            out, err = p.communicate(timeout=timeout_s)
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
    if timed_out:
        raise StageTimedOut(stage, timeout_s, (out + err)[-2000:])   # what it printed before it was stopped
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def run_tests(instance_root: Path, home: Path, timeout_s: int = 900,
              env_root: Path | None = None) -> tuple[bool, str]:
    """Supervisor invariants against this instance's code (trusted test code, run from the last commit — never from
    what another session is editing; it sandboxes the agent itself), then its plugins' check and its own tests inside
    its sandbox, on its own Python environment (supervisor/pyenv.py). `env_root`: where that environment lives when
    `instance_root` is a scratch copy (coder hand-off, trial gate) — read-only from the copy. A stage that times out,
    and any error of the gate's own, fails the gate, named in the output and the supervisor log: raised instead, it
    left the change on disk unsettled, neither kept nor reverted, and the next run took it as its baseline — ungated.
    Nothing left in a stage's process group outlives it (_stage), so no leftover there writes the instance's code after
    the caller's revert."""
    from . import pyenv
    from .core import PYTHON, log, safe_mkdir
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
        sup_env = {**os.environ, "SHAFT_UNDER_TEST": str(instance_root)}
        sup = _stage(f"supervisor invariants ({label})",
                     [PYTHON, "-E", "-s", "-m", "unittest", "discover", "-s", "supervisor/tests", "-t", "."],
                     timeout_s, cwd=tree, env=sup_env)
        outputs.append(f"== supervisor invariants ({label}) ==\n"
                       + "".join(f"{t}\n" for t in failing_tests(sup.stderr)) + sup.stderr[-3000:])
        full.append(f"== supervisor invariants ({label}) ==\n" + sup.stderr)
        ok &= sup.returncode == 0

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
        log(f"gate: {instance_root}: {t}")
        outputs.append(f"== {t.stage} ==\n" + (t.tail + "\n" if t.tail else "")
                       + f"{t}; the checks after it did not run")
        ok = False
    except Exception as e:
        # Fail closed, like a timeout: any failure of the gate's own machinery (no sandbox-exec, a link where .tmp
        # should be, a full disk) is a failed gate. Only StageTimedOut was caught, so anything else left settlement
        # halfway — the change neither tested nor reverted, the next run's baseline (B8, review of 2026-10-24).
        log(f"gate: {instance_root}: the gate itself failed, counted as failed: {type(e).__name__}: {e}")
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


def run_instance_tests(instance_root: Path, prof: Path, env: dict, py: str, timeout_s: int = 900) -> tuple[bool, str]:
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
