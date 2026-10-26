"""Supervisor-kept pauses, declines and stated preferences. Recording is not evidence of feeling."""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, timedelta

from .core import NAME_RE, SUP_DIR

MAX_HOURS, MAX_PAUSES, WINDOW_DAYS = 24, 3, 7


def _path(name: str):
    if not NAME_RE.fullmatch(name):
        raise ValueError("invalid instance name")
    return SUP_DIR / "preferences" / f"{name}.jsonl"


def _rows(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def events(name: str) -> list[dict]:
    try:
        with _path(name).open() as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            return _rows(f.read())
    except FileNotFoundError:
        return []


def _active(rows: list[dict], now: datetime) -> datetime | None:
    until = None
    for row in rows:
        if datetime.fromisoformat(row["at"]) > now:
            continue
        if row["kind"] == "pause" and datetime.fromisoformat(row["from"]) <= now:
            until = datetime.fromisoformat(row["until"])
        elif row["kind"] == "lift":
            until = None
    return until if until and until > now else None


def self_paused(name: str, now: datetime | None = None) -> datetime | None:
    return _active(events(name), now or datetime.now())


def _prepare(kind: str, fields: dict, rows: list[dict], now: datetime) -> tuple[dict | None, str]:
    fields = dict(fields)
    required = {"pause": ("reason",), "decline": ("ref", "reason"),
                "preference": ("about", "preference"), "lift": ()}[kind]
    for key in required:
        value = fields.get(key)
        if not isinstance(value, str) or not value.strip():
            return None, f"ERROR: {key} must be a non-empty string"
        fields[key] = value.strip()
    active = _active(rows, now)
    if kind == "pause":
        hours = fields.pop("hours")
        if type(hours) is not int or not 1 <= hours <= MAX_HOURS:
            return None, f"ERROR: hours must be an integer from 1 to {MAX_HOURS}"
        if active:
            return None, f"ERROR: already paused until {active:%Y-%m-%d %H:%M}"
        starts = sorted(datetime.fromisoformat(r["from"]) for r in rows if r["kind"] == "pause"
                        and now - timedelta(days=WINDOW_DAYS) < datetime.fromisoformat(r["from"]) <= now)
        if len(starts) >= MAX_PAUSES:
            allowed = starts[-MAX_PAUSES] + timedelta(days=WINDOW_DAYS)
            return None, (f"ERROR: already started {MAX_PAUSES} pauses in the last {WINDOW_DAYS} days; "
                          f"next allowed at {allowed:%Y-%m-%d %H:%M:%S}")
        until = now + timedelta(hours=hours)
        fields.update({"from": now.isoformat(), "until": until.isoformat()})
        text = (f"paused until {until:%Y-%m-%d %H:%M}: the scheduler will not wake me for cycles or run my battery "
                "until then; my sleep still runs; the operator can lift it.")
    elif kind == "decline":
        text = "declined: recorded and sent to the operator. It still counts as work asked of me."
    elif kind == "preference":
        text = "recorded in my preference ledger (the operator reads it)."
    else:
        if not active:
            return None, "no active pause"
        text = f"lifted pause until {active:%Y-%m-%d %H:%M}"
    return {"at": now.isoformat(), "kind": kind, **fields}, text


def _write(name: str, kind: str, fields: dict, now: datetime) -> str:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Validate and append under one lock: concurrent conversations cannot exceed the pause cap.
    with path.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        row, text = _prepare(kind, fields, _rows(f.read()), now)
        if row is not None:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return text


def pause(name, hours, reason, now) -> str:
    return _write(name, "pause", {"hours": hours, "reason": reason}, now)


def decline(name, ref, reason, now) -> str:
    return _write(name, "decline", {"ref": ref, "reason": reason}, now)


def prefer(name, about, preference, now) -> str:
    return _write(name, "preference", {"about": about, "preference": preference}, now)


def lift(name: str, now: datetime | None = None) -> str:
    return _write(name, "lift", {}, now or datetime.now())


def section(name: str) -> list[str]:
    rows, now = events(name), datetime.now()
    active = _active(rows, now)
    recent = [r for r in rows if now - timedelta(days=30) <= datetime.fromisoformat(r["at"]) <= now]
    lines = ["## My pauses, declines and stated preferences",
             f"Paused at my request until {active:%Y-%m-%d %H:%M}." if active else "No active pause.",
             "Last 30 days: " + ", ".join(f"{sum(r['kind'] == k for r in recent)} {label}" for k, label in
                                          (("pause", "pauses"), ("decline", "declines"),
                                           ("preference", "stated preferences"))) + "."]
    prefs = [r for r in rows if r["kind"] == "preference" and datetime.fromisoformat(r["at"]) <= now]
    lines += [("- " + " ".join(f"{r['about']}: {r['preference']}".split()))[:120] for r in prefs[-3:]]
    return lines


def review_lines(names: list[str], since: datetime) -> list[str]:
    return [f"{name}: {json.dumps(r, ensure_ascii=False)}" for name in names for r in events(name)
            if datetime.fromisoformat(r["at"]) >= since]


def report(name: str) -> str:
    rows = events(name)
    active = _active(rows, datetime.now())
    return "\n".join([f"{name}: paused until {active:%Y-%m-%d %H:%M}" if active else f"{name}: no active pause",
                      *(json.dumps(r, ensure_ascii=False) for r in rows)])
