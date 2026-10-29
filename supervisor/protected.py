"""Supervisor-owned code copies for launches and gate tests (#86)."""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


def remove(tree: Path) -> None:
    """Remove a copy, including its read-only folders, after its processes have ended."""
    holder = tree.parent
    # A test can plant links in .tmp: chmod only opened directory descriptors, never their path names.
    for folder, dirs, files, fd in os.fwalk(holder, follow_symlinks=False):
        os.fchmod(fd, 0o700)
    shutil.rmtree(holder)


def sweep(parent: Path) -> None:
    """Remove copies left by dead supervisors; a quiet conversation still owns its copy."""
    from .kill import end_left_in
    for holder in parent.iterdir():
        try:
            pid = int(holder.name.split("-", 1)[0])
        except ValueError:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            end_left_in(holder, orphans_only=False)
            remove(holder / "code")
        except PermissionError:
            pass


def make(code: dict[str, bytes], parent: Path, name: str, config: bytes | None = None) -> Path:
    """Materialize exactly gate.snapshot's code bytes, within its bounds and the copy byte limit."""
    from .core import COPY_MAX_GB, TreeTooBig
    if any(not isinstance(data, bytes) for data in code.values()):
        raise ValueError("refused entries cannot become executable code")
    if sum(map(len, code.values())) + len(config or b"") > COPY_MAX_GB * 2**30:
        raise TreeTooBig("protected code copy exceeds SHAFT_COPY_MAX_GB")
    parent.mkdir(parents=True, exist_ok=True)
    sweep(parent)
    holder = Path(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=parent))
    tree = holder / name
    try:
        tree.mkdir()
        for rel, data in code.items():
            path = tree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o444)
        if config is not None:   # trusted supervisor tests read config.env alongside the code
            (tree / "config.env").write_bytes(config)
            (tree / "config.env").chmod(0o444)
        # Gate tests need scratch space; it contains no code and is the only writable part of their copy.
        (tree / ".tmp").mkdir(mode=0o700)
        for folder, dirs, files in os.walk(tree, topdown=False):
            if Path(folder) != tree / ".tmp":
                Path(folder).chmod(0o555)
        return tree
    except BaseException:
        remove(tree)
        raise


@contextmanager
def copy(code: dict[str, bytes], parent: Path, name: str, config: bytes | None = None):
    tree = make(code, parent, name, config)
    try:
        yield tree
    finally:
        from .kill import end_left_in
        try:
            end_left_in(tree, orphans_only=False)
        finally:
            remove(tree)
