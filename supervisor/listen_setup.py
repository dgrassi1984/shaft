"""The Listen worker's own environment, `.venv-listen`: made and kept by `shaft listen on`, with the app's uv.

A Mac without admin rights has no Homebrew: no ffmpeg and often no uv on PATH (issue #58). So `on` builds the
environment with the uv SHAFT runs on (pyenv.uv_binary: the Mac app's .runtime/bin first) and the Python SHAFT runs on,
installs the pinned engines plus imageio-ffmpeg, whose wheel carries a self-contained ffmpeg, and links that ffmpeg as
`.venv-listen/bin/ffmpeg`, the one listen_pipeline.ffmpeg() picks first. Every step can be repeated: a rerun on a
current environment installs nothing."""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from . import core, listen_pipeline, pyenv

# The engines Listen was measured with (docs/meetings.md), and the ffmpeg build both the worker and the clips run.
PACKAGES = ("pyannote.audio==4.1.1", "mlx-whisper==0.4.3", "soundfile==0.14.0", "numpy==2.5.5",
            "imageio-ffmpeg==0.8.2")
VENV = listen_pipeline.VENV_FFMPEG.parent.parent
NOT_SET_UP = "Listen is not set up on the SHAFT server: run `shaft listen on` there, then Process Again."


class SetupFailed(RuntimeError):
    """A step of the setup that did not succeed; the message is what to tell the operator."""


def python() -> Path:
    return VENV / "bin" / "python"


def missing() -> str | None:
    """Why no Listen worker can take a meeting now (NOT_SET_UP), or None. A worker that is installed but stopped or
    restarting still takes it later, so only a missing environment or service counts."""
    from . import autonomy
    if not python().exists() or not autonomy.installed("listen"):
        return NOT_SET_UP
    return None


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, **kwargs)
    except FileNotFoundError as e:
        raise SetupFailed(f"{command[0]} is missing: {e}") from None
    except subprocess.CalledProcessError as e:
        said = e.stderr.strip() if isinstance(e.stderr, str) and e.stderr.strip() else f"exit {e.returncode}"
        raise SetupFailed(f"{' '.join(command[:3])} … failed: {said}") from None


def setup() -> list[str]:
    """Make or update `.venv-listen` and link its ffmpeg; returns what was done, one line each. uv's own output goes
    to the terminal (a first install downloads the engines, a few GB). Raises SetupFailed or RuntimeError (no uv)."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SetupFailed("Listen runs only on an Apple Silicon Mac (its speech recognition uses MLX).")
    uv = pyenv.uv_binary()
    done = []
    if not python().exists():   # none, or one copied from another Mac whose interpreter link points nowhere
        _run([uv, "venv", "--clear", "--python", core.PYTHON, str(VENV)])
        done.append(f"made {VENV} on {core.PYTHON}")
    _run([uv, "pip", "install", "--python", str(python()), *PACKAGES])
    done.append("engines: " + ", ".join(PACKAGES))
    exe = _run([str(python()), "-I", "-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
               capture_output=True, text=True).stdout.strip()
    link = listen_pipeline.VENV_FFMPEG
    if not (link.is_symlink() and os.readlink(link) == exe):
        staged = link.with_name(".ffmpeg-link")
        staged.unlink(missing_ok=True)
        staged.symlink_to(exe)
        os.replace(staged, link)   # never a moment without one for a running worker
    version = _run([str(link), "-version"], capture_output=True, text=True).stdout.splitlines()
    done.append(f"ffmpeg: {link} → {exe} ({version[0] if version else 'version unknown'})")
    return done
