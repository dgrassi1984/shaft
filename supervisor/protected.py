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


def make(code: dict[str, bytes], parent: Path, name: str, config: bytes | None = None,
         instance: Path | None = None) -> Path:
    """Materialize exactly gate.snapshot's code bytes, within its bounds and the copy byte limit. `instance`: the
    instance's folder, whose other entries (its notes, memory, mind) the copy links to (_link_data)."""
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
        if instance is not None:
            _link_data(tree, instance)
        for folder, dirs, files in os.walk(tree, topdown=False):
            if Path(folder) != tree / ".tmp":
                Path(folder).chmod(0o555)
        return tree
    except BaseException:
        remove(tree)
        raise


def _link_data(tree: Path, instance: Path) -> None:
    """Link every top-level entry of the instance's folder the copy lacks, except a code path, back to the instance.
    Its code finds its data from its own file (wren: Path(__file__).parent.parent / "notes/ledger/…"); in a copy of
    the code alone that found nothing, and on 2026-10-29 the gate reverted every change wren made. A code path the
    snapshot lacks stays absent, never a link to the unprotected working code; what the links reach is the sandbox's
    to say, as it was before the copy (gate.run_tests `data_root`, the launch's own profile)."""
    from .gate import CODE_PATHS
    instance = Path(instance).resolve()
    for entry in sorted(os.listdir(instance)):
        if entry not in CODE_PATHS and not os.path.lexists(tree / entry) and not importable(instance / entry):
            (tree / entry).symlink_to(instance / entry)


def importable(path: Path) -> bool:
    """Whether a top-level entry could supply an import from the copy, which leads the import path (the launch puts it
    first, the gate runs `-m unittest` in it): a module file (unittest.py would replace the gate's own runner, json.py
    the launched code's json) or a package with an __init__. Never linked: its bytes were not checked (Codex review of
    the 2026-10-29 fix). A plain folder, as notes/, can only be a namespace portion, which never shadows a module."""
    import importlib.machinery
    suffixes = tuple(importlib.machinery.all_suffixes()) + (".pth",)
    if path.name.lower().endswith(suffixes):
        return True
    try:
        return path.is_dir() and any(f.lower().startswith("__init__.") for f in os.listdir(path))
    except OSError:
        return True   # unreadable: not linked


@contextmanager
def copy(code: dict[str, bytes], parent: Path, name: str, config: bytes | None = None,
         instance: Path | None = None):
    tree = make(code, parent, name, config, instance)
    try:
        yield tree
    finally:
        from .kill import end_left_in
        try:
            end_left_in(tree, orphans_only=False)
        finally:
            remove(tree)
