"""A real browser for instances: one Chromium shared by every instance, a private profile each.

"provide it a profile on a shared chromium, and leave all the rest to it." The supervisor only
- installs Playwright's Chromium builds once, in <project>/.browsers/ (`shaft browser install`, as the operator,
  outside any sandbox). The `run` and job sandboxes read it; nothing sandboxed writes it;
- gives each instance a profile folder, <root>/.browser/: cookies, storage, the passwords it keeps for its own
  accounts. It never travels: not to git, the coder, trial copies or the mind store;
- passes both, with the egress proxy, in the environment of every sandboxed process (Broker.sandbox_env).
Playwright in its .venv (pinned to SHAFT_PLAYWRIGHT), its plugin, signing up and logging in are the instance's.

A browser runs only where the sandbox reaches nothing but the egress proxy (`run`), or a job's own restricted broker:
the agent's sandbox, which its plugins inherit, carries the broker token and reaches the model ports, and does not
read .browsers. Playwright sends loopback through the proxy too, which refuses it (M0).
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

from .core import PROJECT

PLAYWRIGHT = "1.64.0"        # the version `shaft browser install` installs by default (Chromium revision 1248)
BROWSERS = PROJECT / ".browsers"
PROFILE = ".browser"
KINDS = ("chromium", "chromium-headless-shell")   # a visible window and headless; not firefox/webkit


def version() -> str | None:
    """The Playwright version the shared install belongs to, or None if there is none."""
    try:
        return (BROWSERS / "VERSION").read_text().strip() or None
    except OSError:
        return None


def profile(root: Path) -> Path:
    return Path(root) / PROFILE


def env(root: Path) -> dict[str, str]:
    """What a sandboxed process needs to find the shared Chromium and its own profile. Without an install,
    SHAFT_PLAYWRIGHT is absent: Playwright's own error then names the missing executable."""
    out = {"PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS), "SHAFT_BROWSER_PROFILE": str(profile(root))}
    if (v := version()):
        out["SHAFT_PLAYWRIGHT"] = v
    return out


def install(playwright: str = PLAYWRIGHT) -> int:
    """Install Playwright's Chromium builds for `playwright` into .browsers/, replacing what was there.

    Built in a sibling folder and swapped in at the end, so a failed download leaves the old install working and a
    finished one leaves no stale revision behind. VERSION is written last: it is what instances pin to."""
    from .core import log
    from .pyenv import uv_binary
    new, old = BROWSERS.with_name(".browsers.new"), BROWSERS.with_name(".browsers.old")
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    cmd = [uv_binary(), "tool", "run", "--from", f"playwright=={playwright}", "playwright", "install", *KINDS]
    print(f"installing Playwright {playwright}'s Chromium into {BROWSERS} …", flush=True)
    r = subprocess.run(cmd, env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(new)})
    if r.returncode != 0:
        shutil.rmtree(new, ignore_errors=True)
        print(f"install failed (exit {r.returncode}); {BROWSERS} is unchanged", flush=True)
        return r.returncode
    (new / "VERSION").write_text(playwright + "\n")
    shutil.rmtree(old, ignore_errors=True)
    if BROWSERS.exists():
        BROWSERS.rename(old)
    new.rename(BROWSERS)
    shutil.rmtree(old, ignore_errors=True)
    log(f"browser: installed Playwright {playwright}'s Chromium in {BROWSERS}")
    print(f"done: {describe_install()}. Instances pin playwright=={playwright} (SHAFT_PLAYWRIGHT).", flush=True)
    return 0


def _size(path: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _mb(n: int) -> str:
    return f"{n / 1024 ** 2:.0f} MB" if n >= 1024 ** 2 else f"{n / 1024:.0f} KB"


def describe_install() -> str:
    if not (v := version()):
        return "not installed (shaft browser install)"
    revisions = sorted(p.name for p in BROWSERS.iterdir() if p.is_dir() and not p.name.startswith("."))   # .links
    return f"Playwright {v} · {', '.join(revisions)}"


def describe(root: Path) -> str:
    """One line for the operator's panel: the shared install and this instance's profile."""
    if not (v := version()):
        return "not installed (shaft browser install)"
    p = profile(root)
    return f"Playwright {v} · " + (f"profile {_mb(_size(p))}" if p.is_dir() else "no profile yet")


def startup_note() -> str | None:
    """The scheduler's start-up line when there is no shared install (None when there is one)."""
    return None if version() else f"browser: no shared Chromium in {BROWSERS} — instances cannot browse until " \
                                  "the operator runs `shaft browser install`"


def _processes() -> list[tuple[int, int, str]]:
    r = subprocess.run(["/bin/ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out.append((int(parts[0]), int(parts[1]), parts[2]))
    return out


def sweep(root: Path) -> list[int]:
    """Kill orphaned browsers on this instance's profile: a Chromium whose launcher is gone (parent launchd) would
    hold the profile's lock and make the next launch fail. Chromium normally quits with its Playwright driver (M0);
    this catches one the instance detached. A browser whose launcher is alive — a job's, while a cycle ends — is
    left alone. Returns the pids killed."""
    flag = f"--user-data-dir={profile(Path(root).resolve())}"
    killed = []
    for pid, ppid, command in _processes():
        if ppid == 1 and (flag + " " in command + " "):
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
    if killed:
        from .core import log
        log(f"browser: killed orphaned Chromium on {profile(root)}: pids {killed}")
    return killed

