"""Self-modification gate: detect an instance's code changes, test them, revert on failure.

The supervisor never imports agent code. It snapshots the instance's code
before a run; if the run changed it, the test suites run, and failing changes
are restored byte-for-byte from the snapshot.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import sandbox

CODE_PATHS = ("shaft", "tests", "plugins", "config.env")
PROJECT = Path(__file__).resolve().parent.parent


def snapshot(instance_root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name in CODE_PATHS:
        base = instance_root / name
        paths = [base] if base.is_file() else base.rglob("*") if base.is_dir() else []
        for path in paths:
            if path.is_symlink() or any(p.is_symlink() for p in path.parents if instance_root in p.parents):
                continue   # never read through links the instance planted
            if path.is_file() and "__pycache__" not in path.parts:
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


def run_tests(instance_root: Path, home: Path, timeout_s: int = 900) -> tuple[bool, str]:
    """Supervisor invariants against this instance's code (trusted test code; it sandboxes
    the agent itself), then the instance's own tests inside its sandbox."""
    py = sys.executable
    from .core import safe_mkdir
    tmp = safe_mkdir(instance_root, ".tmp")
    outputs, ok = [], True

    sup_env = {**os.environ, "SHAFT_UNDER_TEST": str(instance_root)}
    sup = subprocess.run([py, "-E", "-s", "-m", "unittest", "discover", "-s", "supervisor/tests", "-t", "."],
                         cwd=PROJECT, env=sup_env, capture_output=True, text=True, timeout=timeout_s)
    outputs.append("== supervisor invariants ==\n" + sup.stderr[-3000:])
    ok &= sup.returncode == 0

    fd, prof_name = tempfile.mkstemp(prefix="shaft-tests-", suffix=".sb")   # outside the instance
    prof = Path(prof_name)
    with os.fdopen(fd, "w") as f:
        f.write(sandbox.profile(instance_root, home, [], any_localhost=True))
    env = sandbox.agent_env(os.environ, TMPDIR=str(tmp))
    if (instance_root / "plugins").is_dir():
        plug = subprocess.run(sandbox.wrap(prof, [py, "-E", "-s", "-m", "shaft.plugin_runner", "--check"]),
                              cwd=instance_root, env=env, capture_output=True, text=True, timeout=120)
        outputs.append("== plugins ==\n" + (plug.stdout + plug.stderr)[-2000:])
        ok &= plug.returncode == 0
    if (instance_root / "tests").is_dir():
        agent = subprocess.run(
            sandbox.wrap(prof, [py, "-E", "-s", "-m", "unittest", "discover", "-s", "tests", "-t", "."]),
            cwd=instance_root, env=env,
            capture_output=True, text=True, timeout=timeout_s)
        outputs.append("== instance tests ==\n" + agent.stderr[-3000:])
        ok &= agent.returncode == 0
    prof.unlink(missing_ok=True)
    return ok, "\n".join(outputs)
