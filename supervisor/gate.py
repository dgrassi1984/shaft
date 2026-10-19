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


def snapshot(instance_root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name in CODE_PATHS:
        base = instance_root / name
        paths = [base] if base.is_file() else base.rglob("*") if base.is_dir() else []
        for path in paths:
            if path.is_symlink() or any(p.is_symlink() for p in path.parents if instance_root in p.parents):
                continue   # never read through links the instance planted
            if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store":   # Finder's
                files[str(path.relative_to(instance_root))] = path.read_bytes()
    return files


def changed(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def restore(instance_root: Path, before: dict[str, bytes], paths: list[str]) -> None:
    from .core import UnsafePath, contained
    for rel in paths:
        try:
            target = contained(instance_root, *Path(rel).parts)
        except UnsafePath:
            continue
        if rel in before:
            contained(instance_root, *Path(rel).parent.parts).mkdir(parents=True, exist_ok=True)
            target.write_bytes(before[rel])
        elif target.exists():
            target.unlink()


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
    from .core import config_lock, contained, safe_write
    if not writes:
        return []
    path = contained(instance_root, "config.env")
    with config_lock(path):                          # not over an operator's write landing at the same moment
        text, back = config_without(path.read_text() if path.is_file() else "", writes)
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
    `instance_root` is a scratch copy (coder hand-off, trial gate) — read-only from the copy. A stage that times out
    fails the gate, named in the output and the supervisor log: raised instead, it left the change on disk
    unsettled, neither kept nor reverted, and the next run took it as its baseline — ungated. Nothing left in a stage's
    process group outlives it (_stage), so no leftover there writes the instance's code after the caller's revert."""
    from . import pyenv
    from .core import PYTHON, log, safe_mkdir
    env_root = Path(env_root or instance_root)
    py = pyenv.python(env_root)
    tmp = safe_mkdir(instance_root, ".tmp")
    outputs, ok = [], True
    full: list[str] = []   # the supervisor stage uncut, kept in a file when the gate fails (keep_log)

    try:
        tree, label = committed_tree()
    except Exception as e:   # fail closed: a change kept unsettled would pass ungated as the next run's baseline
        return False, f"== supervisor invariants ==\nno committed copy of the supervisor to test with: {e!r}"
    fd, prof_name = tempfile.mkstemp(prefix="shaft-tests-", suffix=".sb")   # outside the instance
    prof = Path(prof_name)
    with os.fdopen(fd, "w") as f:
        f.write(sandbox.profile(instance_root, home, [], env_root=env_root))
    try:
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
    finally:
        prof.unlink(missing_ok=True)
    if not ok:
        outputs.append(f"full log: {keep_log(instance_root, full + outputs[1:])}")
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
