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

from .core import (NETWORK_ERRORS, gigabytes, instance_config, instance_names, knob, log, positive_int, temperature,
                   whole)

PORT = 8780   # SHAFT's own Bonsai server (bonsai_server.py); the first SHAFT_LLM_PORTS entry by default
LABEL = "com.shaft.model"
LOCALHOST = {"localhost", "127.0.0.1", "::1"}
IDLE_MINUTES, IDLE_SRC = knob(
    "SHAFT_MODEL_IDLE_MINUTES", "15", positive_int, "minutes SHAFT's own model server stays up unused before it exits "
    "(it starts again on demand)", group="Local model", services=("model",))
START_TIMEOUT_S, START_SRC = knob(   # a cold disk after a reboot
    "SHAFT_MODEL_START_TIMEOUT_S", "300", positive_int, "seconds to wait for the model server to start on demand",
    group="Local model", services=("runs", "api", "scheduler"))
# The server's own settings, read when it starts (bonsai_server.main).
THINK_BUDGET, _ = knob(   # Daniele: "not too low"
    "SHAFT_THINK_BUDGET", "4096", whole, "reasoning tokens SHAFT's own model may think per reply before it is made to "
    "answer (0: no limit)", group="Local model", services=("model",))
TEMP, _ = knob(   # Daniele, 2026-10-13 (0.1 = near-greedy: verbatim loops; pack 1.0)
    "SHAFT_MODEL_TEMP", "0.6", temperature, "SHAFT's own model's sampling temperature", group="Local model",
    services=("model",))
# mlx_lm trims the prompt cache to this when the next request starts. 8 GB holds the entries of a 40k-token
# conversation with float16 KV; the Mac has 64 GB.
PROMPT_CACHE_GB, _ = knob(
    "SHAFT_PROMPT_CACHE_GB", "8", gigabytes, "gigabytes of prompt cache SHAFT's own model server keeps",
    group="Local model", services=("model",))
START_OK, START_FAILED = "model server started on demand", "model server failed to start on demand"   # supervisor.log


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
    """The instances whose model calls go to the server: config.env straight on it, or a self-hosted model the
    broker sends to it (SHAFT_SELF_HOSTED). While there is one, it never idles out — and it being down is a fault
    (down_problem). The racer -> local fallback does not pin it: that path starts it itself (ensure)."""
    from . import selfhosted   # here: selfhosted imports this module

    def url(cfg: dict) -> str:
        u, model = cfg.get("LLM_BASE_URL", "").strip(), cfg.get("LLM_MODEL")
        return selfhosted.server(model)["url"] if u == "broker" and selfhosted.serves(model) else u
    return [n for n in (instance_names() if names is None else names) if is_service(url(config_of(n)), port)]


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
    reason (not installed, did not come up in time) — never silently goes on without it — and logs it (START_FAILED:
    the panel's alert while it stays down, down_problem)."""
    if answers():
        return
    try:
        _start(who, timeout_s)
    except RuntimeError as e:
        log(f"{START_FAILED} for {who}: {e}")
        raise


def _start(who: str, timeout_s: float | None) -> None:
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
    log(f"{START_OK} for {who} in {time.monotonic() - started:.1f}s")


def down_problem(log_lines: list[str], since: str = "", port: int = PORT, names: list[str] | None = None,
                 config_of=instance_config) -> str | None:
    """Why the server not answering is a fault, or None when it is just idle (it exited idle, as designed, and its
    next user starts it). A fault: it is not installed (no user could start it), an instance's calls go to it
    (pinned_by: it never idles out while there is one), or the latest on-demand start in `log_lines` (supervisor.log)
    failed, at or after `since` ('YYYY-mm-dd HH:MM:SS': the caller passes the later of its alert window and the
    server's last start, so any start after it clears it)."""
    if not plist().exists():
        return "not installed, so nothing can start it (shaft model on)"
    users = pinned_by(port, names, config_of)
    if users:
        return f"{', '.join(users)} {'uses' if len(users) == 1 else 'use'} it (calls go to :{port})"
    for line in reversed(log_lines):
        if START_OK in line:
            return None
        if START_FAILED in line:
            what = line[line.index(START_FAILED) + len(START_FAILED):].strip()   # "for kant's live: <reason>"
            return f"its on-demand start failed at {line[12:20]} {what}"[:300] if line[1:20] >= since else None
    return None
