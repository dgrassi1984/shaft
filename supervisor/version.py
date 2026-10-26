"""Which code is actually running: the supervisor, an instance's own code, and the long-running services.

Services (the model server, the API) record the commit they started from; a service started from older
code than what is checked out now is flagged — it needs a restart to pick up the change. They also record every
setting (core.knob) they read and the value they read: a setting changed since is not live in that service until it
restarts (settings.not_live).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from functools import lru_cache

from .core import SUP_DIR, git, git_wt, instance_config, instance_root

SERVICES = SUP_DIR / "services"
CODE = {"model": ["supervisor/bonsai_server.py", "supervisor/bonsai_lora.py"], "api": ["supervisor"],
        "embed": ["supervisor/embed_server.py"]}   # what each service runs


def supervisor_commit() -> str:
    """Short commit of main, '+changes' if supervisor/ or bin/ have uncommitted edits."""
    head = git("rev-parse", "--short", "HEAD", check=False).stdout.strip() or "unknown"
    dirty = git("status", "--porcelain", "--", "supervisor", "bin", check=False).stdout.strip()
    return head + ("+changes" if dirty else "")


SHOWN_FOR = 5.0                                   # seconds a page may show an old checkout
_shown = {"at": -1e9, "commit": "", "by": None}


def shown_commit() -> str:
    """supervisor_commit for a page to show: two git processes cost ~37 ms, and every page drew it (the nav's footer,
    2026-10-26). A few seconds old at most; a service's start-up record still reads it fresh. Kept per reader: a
    supervisor_commit replaced (a test's) is never answered with what the one before it said."""
    if time.monotonic() - _shown["at"] >= SHOWN_FOR or _shown["by"] is not supervisor_commit:
        _shown.update(commit=supervisor_commit(), at=time.monotonic(), by=supervisor_commit)
    return _shown["commit"]


@lru_cache(maxsize=128)
def commit_describe(commit: str) -> str:
    """Describe a recorded commit, retaining its start-up dirty marker. Cache by commit, never by HEAD."""
    sha, _, changes = commit.partition("+")
    label = git("describe", "--tags", "--always", sha, check=False).stdout.strip() or sha
    return label + ("+changes" if changes else "")


def service_version(rec: dict | None) -> str:
    """Older records have only a commit; no record means the running version is unknown."""
    if not rec:
        return "unknown — restart to record"
    return rec.get("describe") or commit_describe(rec["commit"])


def record_service(name: str, settings: dict) -> None:
    """Called by a service at start-up. The record keeps the knobs this process has read (core.HELD), and is written
    again whenever it reads another one later — a module imported on first use reads its knobs then."""
    from . import core
    rec = {"pid": os.getpid(), "commit": supervisor_commit(), "started": datetime.now().isoformat(timespec="seconds"),
           "settings": settings, "knobs": dict(core.HELD)}
    rec["describe"] = commit_describe(rec["commit"])

    def write() -> None:
        rec["knobs"] = dict(core.HELD)
        SERVICES.mkdir(parents=True, exist_ok=True)
        path = SERVICES / f"{name}.json"
        tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rec, indent=1))
        os.replace(tmp, path)                      # a reader never sees half a record

    write()
    core._on_knob = write


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def service(name: str, stale: bool = True) -> dict | None:
    """The running service's record (None if it is not running or never recorded itself). stale=False skips
    the git check of whether its code changed since it started (for frequent callers)."""
    try:
        rec = json.loads((SERVICES / f"{name}.json").read_text())
    except (OSError, ValueError):
        return None
    if not _alive(rec.get("pid", 0)):
        return None
    if not stale:
        return rec
    changed = git("diff", "--name-only", rec["commit"].split("+")[0], "--", *CODE.get(name, ["supervisor"]),
                  check=False)
    rec["stale"] = changed.returncode == 0 and bool(changed.stdout.strip())   # its code changed since it started
    return rec


def instance(name: str) -> dict:
    root = instance_root(name)
    head = git_wt(root, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or "unknown"
    behind = git("rev-list", "--count", f"instance/{name}..template", check=False).stdout.strip()
    return {"commit": head, "template_behind": int(behind) if behind.isdigit() else None}


def describe(name: str) -> str:
    """One line for `shaft talk` and `shaft status`."""
    inst = instance(name)
    parts = [f"supervisor {supervisor_commit()}"]
    behind = inst["template_behind"]
    parts.append(f"{name} code {inst['commit']}"
                 + ("" if not behind else f" ({behind} template change(s) not merged: shaft update {name})"))
    cfg = instance_config(name)
    if cfg.get("LLM_BASE_URL") == "broker":
        from . import selfhosted
        m = cfg.get("LLM_MODEL", "?")
        where = f"self-hosted at {selfhosted.host_port(m)}, local fallback" if selfhosted.serves(m) else "cloud"
        parts.append(f"model {m} via the broker ({where})")
        return " · ".join(parts)
    model = service("model")
    if model and ":8780" not in cfg.get("LLM_BASE_URL", ":8780"):
        parts.append(f"model {cfg.get('LLM_MODEL', '?')} at {cfg.get('LLM_BASE_URL')}")
    elif model:
        s = model.get("settings", {})
        parts.append(f"model server {model['commit']} since {model['started'][11:16]} "
                     f"(temp {s.get('temp')}, think {s.get('think_budget')})"
                     + (" — older code, restart: shaft model on" if model["stale"] else ""))
    else:
        from . import localmodel
        parts.append("model server: idle — starts on demand" if localmodel.plist().exists()
                     else "model server: not installed (shaft model on)")
    return " · ".join(parts)
