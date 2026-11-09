"""Explicit operator replies to delivered messages. Append-only evidence for statistics only."""
from __future__ import annotations

import fcntl
import json
from datetime import datetime

from .core import jsonl_lines
from . import core, pushes


def records() -> list[dict]:
    try:
        lines = jsonl_lines((core.SUP_DIR / "reply-links.jsonl").read_text())
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("in_reply_to"), str):
            rows.append(row)
    return rows


def record(name: str, target: object, key: str) -> None:
    """Record an accepted reply, once per client message. Never let optional evidence fail a send.

    Compare the untrusted identity to the log, scoped to this instance and the operator; never open it as a path.
    """
    if target is None:
        return
    try:
        if (not isinstance(target, str) or not target or len(target) > 255
                or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in target)):
            core.log("reply link: ignored malformed message identity")
            return
        if not any(r.get("file") == target and r.get("from") == name and r.get("to") == "operator"
                   for r in pushes.records(core.SUP_DIR)):
            core.log("reply link: ignored unknown message identity")
            return
        path = core.SUP_DIR / "reply-links.jsonl"
        with path.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            for line in f:
                try:
                    old = json.loads(line)
                except ValueError:
                    continue
                if isinstance(old, dict) and old.get("instance") == name and old.get("key") == key:
                    return
            f.write(json.dumps({"instance": name, "in_reply_to": target, "key": key,
                                "ts": datetime.now().isoformat(timespec="seconds")}) + "\n")
    except OSError:
        try:
            core.log("reply link: could not store optional evidence")
        except OSError:
            pass
