"""A cycle the instance itself ended as "nothing worth a cycle" (the broker's nothing_to_do).

An instance woken with nothing to do had two ways out: invent work, or end without a goal and be counted as a failure
("never declared an intent"). This is the third: it says so, the supervisor records it, and that cycle is its own
class: never a failure, not in the success rate, not sent to a judge. The scheduler then lets the next idle wake wait
twice as long (autonomy.decide_wakes), back to normal from the next cycle of any other kind: the instance sets its own
pace and the supervisor follows. Any event (a message, a due commitment, a job result) wakes it as ever.

One row per exit in SUP_DIR/idle_exits/<name>.jsonl {at, reason}. Honoured only in an autonomous cycle that has sent
or drafted nothing: an exit declared after work went out would hide that cycle from the judge.
"""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, timedelta
from pathlib import Path

from . import core
from .core import jsonl_lines

REASON_MAX = 300
OUTWARD = ("send_message", "mail_draft", "mail_draft_update", "calendar_create", "calendar_move", "calendar_respond")
WINDOW_DAYS = 14


def path(name: str) -> Path:
    return core.SUP_DIR / "idle_exits" / f"{name}.jsonl"


def rows(name: str, as_of: datetime | None = None) -> list[dict]:
    p = path(name)
    out = []
    for line in jsonl_lines(p.read_text()) if p.exists() else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("at"), str):
            out.append(row)
    if as_of is not None:
        cut = as_of.isoformat(timespec="seconds")
        out = [r for r in out if r["at"] <= cut]
    return out


def record(name: str, reason: str, now: datetime | None = None) -> dict:
    row = {"at": (now or datetime.now()).isoformat(timespec="seconds"), "reason": " ".join(str(reason).split())[:REASON_MAX]}
    p = path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def exits(name: str, episodes: list[dict]) -> set[str]:
    """The ts of the live episodes that were idle exits: one whose run window (run_started, ts] holds an exit row.
    An episode with no run_started is matched from the previous episode's end."""
    stamps = sorted(r["at"] for r in rows(name))
    if not stamps:
        return set()
    out, previous = set(), None
    for ep in sorted(episodes, key=lambda e: e["ts"]):
        start = ep.get("run_started") or previous
        if isinstance(start, str) and any(start < at <= ep["ts"] for at in stamps):
            out.add(ep["ts"])
        previous = ep["ts"]
    return out


def stats_lines(name: str, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    since = (now - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")
    recent = [r for r in rows(name) if since <= r["at"] <= now.isoformat(timespec="seconds")]
    if not recent:
        return []
    return [f"- Cycles I ended as nothing worth a cycle (last {WINDOW_DAYS} days): {len(recent)}. They are not judged "
            f"and never count as failures; after one, my next idle wake waits twice as long. Last: "
            f"{recent[-1]['at'][:16].replace('T', ' ')}"]
