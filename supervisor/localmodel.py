"""The local Bonsai server (com.shaft.model) runs only while something uses it (Daniele, 2026-10-19).

It holds ~7.6 GB loaded and takes ~2 s to load (measured 2026-10-19, page cache warm). Before, it ran always; for
five days its only user was the racer -> local fallback, which last fired on 2026-10-14.

- Pinned: while any instance's config.env points straight at it (LLM_BASE_URL on its port), it never exits on its
  own — such an instance's agent calls it directly from the sandbox, between runs and in the middle of one, paused
  or not (a paused instance can still be talked to).
- Otherwise it exits (code 0) once no request is in progress and none has arrived for SHAFT_MODEL_IDLE_MINUTES
  (15; 0 = never). launchd restarts it only after a crash (KeepAlive SuccessfulExit=false), not after that exit.
- Every supervisor path that reaches it calls ensure() first: a run of an instance on it (runner), the racer ->
  local fallback (selfhosted), a local trial replay, an exam of the battery, evolution's baseline. ensure() starts the
  launchd service if the port does not answer and waits until it does.
"""
from __future__ import annotations

import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .core import NETWORK_ERRORS, instance_config, instance_names, knob, log

PORT = 8780   # SHAFT's own Bonsai server (bonsai_server.py); the first SHAFT_LLM_PORTS entry by default
LABEL = "com.shaft.model"
LOCALHOST = {"localhost", "127.0.0.1", "::1"}
IDLE_MINUTES, IDLE_SRC = knob("SHAFT_MODEL_IDLE_MINUTES", "15", int)
START_TIMEOUT_S, START_SRC = knob("SHAFT_MODEL_START_TIMEOUT_S", "300", int)   # a cold disk after a reboot


def server_of(url: str) -> str | None:
    """'127.0.0.1:8780' for a model URL, any loopback spelling made one; None for no URL or the broker."""
    url = (url or "").strip()
    if not url or url == "broker":
        return None
    parts = urllib.parse.urlsplit(url)
    host = "127.0.0.1" if parts.hostname in LOCALHOST else parts.hostname
    return f"{host}:{parts.port or (443 if parts.scheme == 'https' else 80)}"


def is_service(url: str, port: int = PORT) -> bool:
    """Whether `url` is the local Bonsai server this module starts (on `port`)."""
    return server_of(url) == f"127.0.0.1:{port}"


def pinned_by(port: int = PORT, names: list[str] | None = None, config_of=instance_config) -> list[str]:
    """The instances whose config.env points straight at the server: while there is one, it never idles out."""
    return [n for n in (instance_names() if names is None else names)
            if is_service(config_of(n).get("LLM_BASE_URL", ""), port)]


def idle_exit_due(now: float, last_activity: float, in_flight: int, pinned: list[str],
                  idle_minutes: int = IDLE_MINUTES) -> bool:
    """Pure: the server's own rule for exiting — unpinned, nothing in progress, quiet for idle_minutes."""
    return (idle_minutes > 0 and not pinned and in_flight == 0
            and now - last_activity >= idle_minutes * 60)


def answers(port: int = PORT, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=timeout) as r:
            return r.status == 200
    except NETWORK_ERRORS:
        return False


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def ensure(who: str, timeout_s: float | None = None) -> None:
    """Make the local Bonsai server answer, starting its launchd service if it does not. Raises RuntimeError with the
    reason (not installed, did not come up in time) — never silently goes on without it."""
    if answers():
        return
    if not plist().exists():
        raise RuntimeError(f"the local model server is not installed ({plist()} missing): shaft model on")
    domain, started = f"gui/{os.getuid()}", time.monotonic()
    r = _launchctl("kickstart", f"{domain}/{LABEL}")   # no-op if it is starting already
    if r.returncode != 0:   # booted out (e.g. by hand) but still installed: load it, then start it
        _launchctl("bootstrap", domain, str(plist()))
        r = _launchctl("kickstart", f"{domain}/{LABEL}")
        if r.returncode != 0:
            raise RuntimeError(f"could not start {LABEL}: launchctl kickstart: {(r.stderr or r.stdout).strip()}")
    deadline = started + (START_TIMEOUT_S if timeout_s is None else timeout_s)
    while not answers():
        if time.monotonic() > deadline:
            raise RuntimeError(f"{LABEL} did not answer on :{PORT} within {deadline - started:.0f}s of starting it "
                               f"(see .supervisor/{LABEL}.log)")
        time.sleep(0.5)
    log(f"model server started on demand for {who} in {time.monotonic() - started:.1f}s")
