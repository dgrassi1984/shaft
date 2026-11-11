"""A cycle the instance itself ended as "nothing worth a cycle" (the broker's nothing_to_do).

An instance woken with nothing to do had two ways out: invent work, or end without a goal and be counted as a failure
("never declared an intent"). This is the third: it says so, the supervisor records it, and that cycle is its own
class: never a failure, not in the success rate, not sent to a judge. The scheduler then lets the next idle wake wait
twice as long (autonomy.decide_wakes), back to normal from the next cycle of any other kind: the instance sets its own
pace and the supervisor follows. Any event (a message, a due commitment, a job result) wakes it as ever.

Rows of SUP_DIR/idle_exits/<name>.jsonl: an exit {at, run, reason}, or its withdrawal {at, run, revoked}. `run` is
the run's start time as the supervisor stamped it (runner: SHAFT_RUN_STARTED), which the episode carries: an exit
belongs to one run, never to whatever episode claims a time window around it.

It must not become a way to hide work from the judge, so it counts only for a cycle that did nothing outward:
  - before it: no message, draft, calendar change, delegation, config or key change, trial, order or prediction
    (PRIOR); reading and looking around (`run`, `http_request`, file tools) is how one finds there is nothing to do;
  - after it: any further supervisor tool call other than a read of its own record (QUIET) withdraws the exit, and
    the cycle is an ordinary one again (never a refusal: the call goes through);
  - a cycle that kept a code change, or one a kept commitment cites as its evidence, is judged like any other.
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
PRIOR = OUTWARD + ("delegate_coding", "set_config", "secret_set", "secret_delete", "trial", "record_order", "predict")
QUIET = ("nothing_to_do", "record", "gauges")   # what may still be called after the exit without withdrawing it
WINDOW_DAYS = 14
MAX_RUN = timedelta(hours=6)   # far longer than any cycle; a match further apart is not that run


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


def _append(name: str, row: dict) -> dict:
    p = path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def record(name: str, run: str, reason: str, now: datetime | None = None) -> dict:
    return _append(name, {"at": (now or datetime.now()).isoformat(timespec="seconds"), "run": str(run),
                          "reason": " ".join(str(reason).split())[:REASON_MAX]})


def revoke(name: str, run: str, why: str, now: datetime | None = None) -> dict:
    return _append(name, {"at": (now or datetime.now()).isoformat(timespec="seconds"), "run": str(run),
                          "revoked": str(why)[:REASON_MAX]})


def standing(name: str, as_of: datetime | None = None) -> dict[str, dict]:
    """run -> its exit row, for the exits not withdrawn."""
    out: dict[str, dict] = {}
    for row in rows(name, as_of):
        run = row.get("run")
        if not isinstance(run, str) or not run:
            continue
        if "revoked" in row:
            out.pop(run, None)
        elif "reason" in row:
            out.setdefault(run, row)
    return out


def _at(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def exits(name: str, episodes: list[dict], changed: list[str] = ()) -> set[str]:
    """The ts of the live episodes that were idle exits: the episode carrying the run id of a standing exit (the
    earliest one, if several claim it: the episode file is the instance's), ended at or after the exit was recorded,
    and with no code change kept in its run (`changed`: the kept changes' times)."""
    open_ = standing(name)
    if not open_:
        return set()
    kept = sorted(at for ts in changed if (at := _at(ts)) is not None)
    out, used = set(), set()
    dated = [(at, e) for e in episodes if isinstance(e, dict) and (at := _at(e.get("ts"))) is not None]
    for _, ep in sorted(dated, key=lambda pair: pair[0]):   # by time, not by string: the files mix 'T' and space
        run = ep.get("run_started")
        row = open_.get(run) if isinstance(run, str) else None
        if row is None or run in used:
            continue
        used.add(run)
        start, end, said = _at(run), _at(ep["ts"]), _at(row["at"])
        if None in (start, end, said) or not start <= said <= end:
            continue
        if end - start > MAX_RUN or any(said < at < end for at, other in dated if other is not ep):
            continue
        if any(start <= at <= end + timedelta(minutes=2) for at in kept):
            continue
        out.add(ep["ts"])
    return out


def stats_lines(name: str, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    since = (now - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")
    recent = [r for r in standing(name).values() if since <= r["at"] <= now.isoformat(timespec="seconds")]
    if not recent:
        return []
    return [f"- Cycles I ended as nothing worth a cycle (last {WINDOW_DAYS} days): {len(recent)}. They are not judged "
            f"and never count as failures; after one, my next idle wake waits twice as long. Last: "
            f"{recent[-1]['at'][:16].replace('T', ' ')}"]
