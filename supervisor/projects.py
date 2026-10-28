"""Read-only project boundaries (#106); the fold matches shaft/projects.py."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime

from . import core, metrics

FILE = "memory/projects.jsonl"
ID = re.compile(r"p\d{4}-\d{4}-[1-9]\d*")
OUTCOMES = ("done", "abandoned", "failed")


def _at(value):
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def _budget(value, integer=False):
    return (type(value) is int if integer else type(value) in (int, float)) and math.isfinite(value) and value > 0


def _fold(text: str) -> dict[str, dict]:
    items = {}
    for line in text.splitlines():
        rec = json.loads(line)
        if not isinstance(rec, dict):
            raise ValueError("project event must be an object")
        pid, at = rec.get("id"), _at(rec.get("at"))
        if not isinstance(pid, str) or not ID.fullmatch(pid) or at is None:
            continue
        if rec.get("event") == "opened" and pid not in items:
            if (any(not isinstance(rec.get(k), str) or not rec[k].strip() for k in ("name", "success"))
                    or _at(rec.get("deadline")) is None or _at(rec["deadline"]) <= at
                    or any(rec.get(k) is not None and not _budget(rec[k], k == "budget_calls")
                           for k in ("budget_calls", "budget_usd"))):
                continue
            items[pid] = {**rec, "status": "open"}
        elif rec.get("event") == "closed":
            item = items.get(pid)
            if (item and item["status"] == "open" and rec.get("outcome") in OUTCOMES
                    and isinstance(rec.get("evidence"), str) and rec["evidence"].strip() and at >= _at(item["at"])):
                item.update(status=rec["outcome"], evidence=rec["evidence"], closed_at=rec["at"])
    return items


def lines(items: dict, episodes: list[dict], verdicts: list[dict], now: datetime) -> list[str]:
    judged = {r["episode_ts"]: r.get("criterion_met") for r in verdicts if not r.get("left_out")}
    out = []
    for pid, item in items.items():
        if item["status"] != "open":
            continue
        eps = sorted((ep for ep in episodes if ep.get("kind") == "live" and
                      (ep.get("intent") or {}).get("project") == pid), key=lambda ep: ep["ts"])
        streak = 0
        for ep in reversed(eps):
            met = judged.get(ep["ts"])
            if met is None:
                continue
            if met not in ("no", "partial"):
                break
            streak += 1
        calls = dollars = 0
        missing_calls = missing_usd = 0
        for ep in eps:
            usage = ep.get("project_usage") or {}
            c, usd = usage.get("calls"), usage.get("usd")
            if type(c) is int and c >= 0:
                calls += c
            else:
                missing_calls += 1
            if type(usd) in (int, float) and math.isfinite(usd) and usd >= 0:
                dollars += usd
            else:
                missing_usd += 1
        left = (_at(item["deadline"]) - _at(now.isoformat())).total_seconds()
        span = (_at(item["deadline"]) - _at(item["at"])).total_seconds()
        share = left / span
        past, risk = [], []
        if left <= 0:
            past.append("deadline")
        elif share <= .2:
            risk.append("deadline")
        for key, used in (("budget_calls", calls), ("budget_usd", dollars)):
            budget = item.get(key)
            if budget is not None:
                if used >= budget:
                    past.append(key)
                elif used >= .8 * budget:
                    risk.append(key)
        if streak >= 3:
            past.append("repeated failure")
        elif streak >= 2:
            risk.append("repeated failure")
        status = "past boundary: " + ", ".join(past) if past else "at risk: " + ", ".join(risk) if risk else "on track"
        unknown = f"; usage unknown for calls {missing_calls}, USD {missing_usd} cycles" if missing_calls or missing_usd else ""
        if unknown and not past and not risk:
            status = "at risk: usage unknown"
        out.append(f"- {pid} {item['name']}: {status}; deadline {item['deadline']}, {left / 3600:.1f} h left "
                   f"({share:.0%} of span); calls {calls}/{item.get('budget_calls') or 'unbounded'}; "
                   f"USD {dollars:.6f}/{item.get('budget_usd') or 'unbounded'}; judged no/partial streak {streak}"
                   f"{unknown}; done means: {item['success']}")
    return out


def section(name: str) -> list[str]:
    try:
        items = _fold(core.safe_read(core.INSTANCES / name, FILE, ""))
        if not any(it["status"] == "open" for it in items.values()):
            return ["## Projects", "Open projects: 0"]
        rows = lines(items, metrics.live_episodes(name), metrics.judgments(name), datetime.now())
        return ["## Projects", f"Open projects: {len(rows)}", *rows]
    except (OSError, ValueError, TypeError) as e:
        return [f"Projects could not be read: {type(e).__name__}: {e}"]
