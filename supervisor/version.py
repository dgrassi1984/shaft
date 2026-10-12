"""Which code is actually running: the supervisor, an instance's own code, and the long-running services.

Services (the model server, the API) record the commit they started from; a service started from older
code than what is checked out now is flagged — it needs a restart to pick up the change.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from .core import SUP_DIR, git, git_wt, instance_root

SERVICES = SUP_DIR / "services"


def supervisor_commit() -> str:
    """Short commit of main, '+changes' if supervisor/ or bin/ have uncommitted edits."""
    head = git("rev-parse", "--short", "HEAD", check=False).stdout.strip() or "unknown"
    dirty = git("status", "--porcelain", "--", "supervisor", "bin", check=False).stdout.strip()
    return head + ("+changes" if dirty else "")


def record_service(name: str, settings: dict) -> None:
    """Called by a service at start-up."""
    SERVICES.mkdir(parents=True, exist_ok=True)
    (SERVICES / f"{name}.json").write_text(json.dumps({
        "pid": os.getpid(), "commit": supervisor_commit(),
        "started": datetime.now().isoformat(timespec="seconds"), "settings": settings}, indent=1))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def service(name: str) -> dict | None:
    """The running service's record (None if it is not running or never recorded itself)."""
    try:
        rec = json.loads((SERVICES / f"{name}.json").read_text())
    except (OSError, ValueError):
        return None
    if not _alive(rec.get("pid", 0)):
        return None
    current = supervisor_commit()
    rec["stale"] = rec["commit"].split("+")[0] != current.split("+")[0]
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
    model = service("model")
    if model:
        s = model.get("settings", {})
        parts.append(f"model server {model['commit']} since {model['started'][11:16]} "
                     f"(temp {s.get('temp')}, think {s.get('think_budget')})"
                     + (" — older code, restart: shaft model on" if model["stale"] else ""))
    else:
        parts.append("model server: not running")
    return " · ".join(parts)
