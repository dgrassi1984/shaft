"""Being paused by the supervisor while the operator talks with me.
Starting a conversation must immediately pause its background cycle.

The supervisor writes .tmp/pause-<my pid> and sends SIGUSR1. If I am waiting on the model, the call is cut off
at once (the server stops generating when the connection drops, so the model is free for the conversation); if
I am running a tool, I stop right after it. I acknowledge with .tmp/paused-<pid>, wait until the pause file is
gone, then redo the step that was cut off. Only autonomous runs (cycle, sleep, reflection) install this.
The files carry my pid: other runs (the conversation's write-up) clean only files whose process is gone — a
shared name let one delete my pause file and resume me while the conversation still had the instance.
"""
from __future__ import annotations

import json
import os
import signal
import time
from contextlib import contextmanager
from pathlib import Path

_state = {"installed": False, "requested": False, "in_call": False}


class Paused(BaseException):
    """Raised inside a model call cut off by a pause. BaseException: no `except Exception` may swallow it."""


def _root() -> Path:
    return Path(os.environ.get("SHAFT_ROOT") or ".")


def _mine(name: str) -> Path:
    return _root() / ".tmp" / f"{name}-{os.getpid()}"


def _on_signal(signum, frame):
    _state["requested"] = True
    if _state["in_call"]:
        raise Paused()


def install() -> None:
    signal.signal(signal.SIGUSR1, _on_signal)
    _state["installed"] = True
    tmp = _root() / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    _mine("pausable").write_text(str(os.getpid()))   # SIGUSR1 kills a process without this handler: say I have it


@contextmanager
def interruptible():
    """Around a model call: a pause cuts it off (only if this run installed pausing)."""
    _state["in_call"] = _state["installed"]
    try:
        yield
    finally:
        _state["in_call"] = False


def requested() -> bool:
    return _state["installed"] and (_state["requested"] or _mine("pause").exists())


def wait_while_paused() -> None:
    (_root() / ".tmp").mkdir(parents=True, exist_ok=True)
    _mine("paused").write_text(str(os.getpid()))   # the supervisor settles my work so far and frees the instance
    try:
        while _mine("pause").exists():
            time.sleep(1)
    finally:
        _mine("paused").unlink(missing_ok=True)
        _state["requested"] = False


def activity(**fields) -> None:
    """What I am doing right now, for the supervisor's live view (`shaft top`): .tmp/activity.json, replaced
    at each step. Never fails the run."""
    try:
        tmp = _root() / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        path = tmp / "activity.json"
        current = {}
        if fields.pop("keep", False) and path.exists():
            current = json.loads(path.read_text())
        current.update(fields, ts=time.time(), pid=os.getpid())
        (tmp / "activity.json.tmp").write_text(json.dumps(current, ensure_ascii=False))
        os.replace(tmp / "activity.json.tmp", path)
    except (OSError, ValueError):
        pass
