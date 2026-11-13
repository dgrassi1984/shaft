"""An instance's own Python environment, in its own folder and under its control.

- `.venv/`: the environment its plugins, its tests and `python3` in `run` use, once it exists.
- `.uv/`: uv's own state — the Pythons it installs (any version the instance picks), its cache and tools.

Both are gitignored runtime state. What the instance chose is declared, uv's way, in files it commits with its
code: `.python-version` and `requirements.txt`; from them the supervisor rebuilds a missing environment at wake
(a new machine, a wiped folder). The instance drives uv itself inside `run`, which reaches the internet through
the run's egress proxy. Everything that executes from these folders runs sandboxed: the supervisor itself only
ever launches the environment's interpreter under a sandbox profile.

The agent process itself stays on the system Python, so a broken environment never stops an instance from waking
up to repair it. Copies of an instance (coder hand-offs, trial gates, trial replays) read its real environment
read-only, so they test with the packages the instance actually has.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ENV_DIRS = (".venv", ".uv")
DECLARED = (".python-version", "requirements.txt")


def venv_python(env_root: Path) -> Path:
    return Path(env_root) / ".venv" / "bin" / "python"


def python(env_root: Path) -> str:
    """The interpreter for the instance's plugins and tests: its environment's once it has one, else the system
    Python. A `.venv` whose interpreter is gone is still chosen, so the failure is loud and the instance sees it."""
    from .core import PYTHON
    p = venv_python(env_root)
    return str(p) if (Path(env_root) / ".venv").exists() else PYTHON


def uv_binary() -> str:
    """The Mac app's own uv (core.RUNTIME/bin, the pinned one it installed) when there is one, else uv on PATH (a
    Homebrew install). Missing: why, with the fix for the kind of install this is."""
    from . import core
    runtime = core.RUNTIME / "bin" / "uv"
    if runtime.is_file() and os.access(runtime, os.X_OK):
        return str(runtime)
    if (found := shutil.which("uv")):
        return found
    if core.RUNTIME.is_dir():       # installed by the Mac app: it has no terminal to point at
        raise RuntimeError(f"uv is missing from SHAFT's runtime ({runtime}): open the SHAFT app on this Mac and run "
                           "Install again (it puts uv back)")
    raise RuntimeError(f"uv is not installed on the supervisor's PATH ({os.environ.get('PATH', '')}): "
                       "`brew install uv`, then restart the SHAFT services")


def uv_env(root: Path) -> dict[str, str]:
    """uv keeps everything inside the instance's own .uv/: never the operator's ~/.local or ~/.cache."""
    uvdir = Path(root) / ".uv"
    return {"UV_PYTHON_INSTALL_DIR": str(uvdir / "python"), "UV_CACHE_DIR": str(uvdir / "cache"),
            "UV_TOOL_DIR": str(uvdir / "tools"), "UV_TOOL_BIN_DIR": str(uvdir / "bin"),
            "UV_PYTHON_BIN_DIR": str(uvdir / "bin"), "UV_NO_PROGRESS": "1"}


def path_dirs(root: Path, env_root: Path) -> list[str]:
    """PATH entries ahead of the system's: the instance's environment and uv's tool links, then uv itself."""
    dirs = [str(Path(env_root) / ".venv" / "bin"), str(Path(root) / ".uv" / "bin")]
    try:
        dirs.append(str(Path(uv_binary()).parent))
    except RuntimeError:
        pass   # `uv` then fails as "command not found" inside run; the rebuild reports the missing binary
    return dirs


def needs_rebuild(root: Path) -> bool:
    root = Path(root)
    return not (root / ".venv").exists() and any((root / f).is_file() for f in DECLARED)


def rebuild_command(root: Path) -> str:
    """Recreate the environment from what the instance declared (uv reads .python-version itself)."""
    cmd = "uv venv .venv"
    if (Path(root) / "requirements.txt").is_file():
        cmd += " && uv pip install -r requirements.txt"
    return cmd


def describe(env_root: Path) -> str:
    """One line for the operator's panel: which Python the instance's plugins and tests run on."""
    from .core import UnsafePath, safe_read
    if not (Path(env_root) / ".venv").exists():
        return "system Python (no .venv yet)"
    try:   # the instance writes .venv/: never read through a link
        cfg = safe_read(env_root, ".venv/pyvenv.cfg", errors="replace")
    except (OSError, UnsafePath):
        return ".venv (BROKEN: no pyvenv.cfg)"
    fields = {k.strip(): v.strip() for k, v in (l.split("=", 1) for l in cfg.splitlines() if "=" in l)}
    version = fields.get("version_info") or fields.get("version") or "?"
    return f".venv · Python {version}" + ("" if venv_python(env_root).exists() else " (BROKEN: interpreter missing)")
