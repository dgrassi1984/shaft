"""Operator pushes (#140), measured separately from value and operator reactions."""
from __future__ import annotations

import json
import re
from bisect import bisect_left
from datetime import datetime, timedelta
from pathlib import Path

from . import core

PUSH_BUDGET, _ = core.knob(
    "SHAFT_PUSH_BUDGET", "6", core.whole, "measured operator pushes per day (0: not shown); never blocks delivery",
    group="Runs and conversations", services=("runs", "api", "web"))


def _time(text: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(text)
        return at.astimezone().replace(tzinfo=None) if at.tzinfo else at
    except (TypeError, ValueError):
        return None


def records(sup_dir: Path) -> list[dict]:
    """The supervisor's message rows, without history()'s instance-controlled inbox read state."""
    try:
        lines = (sup_dir / "messages.jsonl").read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and _time(row.get("ts")):
            rows.append(row)
    return rows


def _is_push(row: dict, name: str) -> bool:
    return row.get("from") == name and row.get("to") == "operator" and bool(row.get("alert"))


def usage(name: str, sup_dir: Path, now: datetime | None = None) -> dict | None:
    if not PUSH_BUDGET:
        return None
    now = now or datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {"today": sum(_is_push(r, name) and start <= _time(r["ts"]) <= now for r in records(sup_dir)),
            "budget": PUSH_BUDGET}


def reactions(name: str, rows: list[dict], sup_dir: Path, now: datetime) -> list[dict]:
    """One outcome per push, using the next operator message or supervisor-logged conversation start.

    No response means none recorded yet as of now, within 24 h. It does not claim causation. Inbox read flags
    and panel dismissals have no event timestamps, so cannot establish a dismissal within a minute.
    """
    replies = [_time(r["ts"]) for r in rows if r.get("from") == "operator" and r.get("to") == name]
    try:
        lines = (sup_dir / "supervisor.log").read_text(errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    pattern = re.compile(r"^\[([^]]+)\] " + re.escape(name) + r": start (?:api|talk)(?: |$)")
    for line in lines:
        if (m := pattern.match(line)) and (at := _time(m[1])):
            replies.append(at)
    replies = sorted(at for at in replies if at and at <= now)
    out = []
    for row in rows:
        if not _is_push(row, name) or (sent := _time(row["ts"])) > now:
            continue
        i = bisect_left(replies, sent)
        delay = replies[i] - sent if i < len(replies) else timedelta.max
        outcome = "within_1h" if delay <= timedelta(hours=1) else (
            "within_24h" if delay <= timedelta(hours=24) else "no_response")
        out.append({"file": row["file"], "ts": row["ts"], "outcome": outcome})
    return out


def lines(name: str, judgments: list[dict], now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    days = [(now - timedelta(days=d)).date() for d in range(6, -1, -1)]
    rows = records(core.SUP_DIR)
    daily = {day: 0 for day in days}
    for row in rows:
        if _is_push(row, name) and (at := _time(row["ts"])) <= now and at.date() in daily:
            daily[at.date()] += 1
    budget = f" of budget {PUSH_BUDGET}" if PUSH_BUDGET else ""
    recent = [j for j in judgments if (at := _time(j.get("episode_ts"))) and days[0] <= at.date() <= days[-1]
              and at <= now]
    rated = [j for j in recent if j.get("should_have_waited") in ("yes", "no")]
    yes = sum(j["should_have_waited"] == "yes" for j in rated)
    share = f"{yes}/{len(rated)} ({yes / len(rated):.0%})" if rated else "not graded"
    counts = {k: 0 for k in ("within_1h", "within_24h", "no_response")}
    for row in reactions(name, rows, core.SUP_DIR, now):
        if _time(row["ts"]).date() in daily:
            counts[row["outcome"]] += 1
    return [f"pushes to the operator today: {daily[now.date()]}{budget}",
            "pushes per day (last 7 days" + (f", budget {PUSH_BUDGET}/day" if PUSH_BUDGET else "") + "): "
            + ", ".join(f"{day:%m-%d}: {daily[day]}" for day in days),
            f"should_have_waited=yes (last 7 days, judged push cycles): {share}; "
            f"na {sum(j.get('should_have_waited') == 'na' for j in recent)}, "
            f"ungraded {sum(j.get('should_have_waited') not in ('yes', 'no', 'na') for j in recent)}",
            f"operator reactions (last 7 days' pushes, as of now): within 1 h {counts['within_1h']}, "
            f"1–24 h {counts['within_24h']}, no response within 24 h recorded yet {counts['no_response']}"]
