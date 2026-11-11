"""Read-only project boundaries (#106); the fold matches shaft/projects.py."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime

from .core import jsonl_lines
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
    for line in jsonl_lines(text):
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


def assess(pid: str, item: dict, episodes: list[dict], judged: dict, now: datetime) -> dict:
    """An open project against its boundaries: the one computation behind the stats-head line and the Cycles panel."""
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
    if (missing_calls or missing_usd) and not past and not risk:
        status = "at risk: usage unknown"
    return {"status": status, "left": left, "share": share, "calls": calls, "usd": dollars, "streak": streak,
            "missing_calls": missing_calls, "missing_usd": missing_usd}


def lines(items: dict, episodes: list[dict], verdicts: list[dict], now: datetime) -> list[str]:
    judged = {r["episode_ts"]: r.get("criterion_met") for r in verdicts if not r.get("left_out")}
    out = []
    for pid, item in items.items():
        if item["status"] != "open":
            continue
        a = assess(pid, item, episodes, judged, now)
        unknown = (f"; usage unknown for calls {a['missing_calls']}, USD {a['missing_usd']} cycles"
                   if a["missing_calls"] or a["missing_usd"] else "")
        out.append(f"- {pid} {item['name']}: {a['status']}; deadline {item['deadline']}, {a['left'] / 3600:.1f} h left "
                   f"({a['share']:.0%} of span); calls {a['calls']}/{item.get('budget_calls') or 'unbounded'}; "
                   f"USD {a['usd']:.6f}/{item.get('budget_usd') or 'unbounded'}; judged no/partial streak {a['streak']}"
                   f"{unknown}; done means: {item['success']}")
    return out


def _plan_channel(name: str) -> bool:
    """A plan-billed model (no per-call cost): the stats head and the panel show no dollar usage for it."""
    from . import planchannel
    cfg = core.config_values(core.safe_read(core.INSTANCES / name, "config.env", ""))
    return cfg.get("LLM_BASE_URL") == "broker" and bool(planchannel.prefix(cfg.get("LLM_MODEL")))


def section(name: str) -> list[str]:
    try:
        items = _fold(core.safe_read(core.INSTANCES / name, FILE, ""))
        if not any(it["status"] == "open" for it in items.values()):
            return ["## Projects", "Open projects: 0"]
        rows = lines(items, metrics.live_episodes(name), metrics.judgments_for_stats(name), datetime.now())
        if _plan_channel(name):
            rows = [re.sub(r"USD [^;]+", "plan: no per-call cost", row) for row in rows]
        return ["## Projects", f"Open projects: {len(rows)}", *rows]
    except (OSError, ValueError, TypeError) as e:
        return [f"Projects could not be read: {type(e).__name__}: {e}"]


def panel(name: str, now: datetime | None = None) -> dict:
    """The instance's projects for the Cycles panel: open ones first (soonest deadline first) with how they stand
    against their boundaries, then closed ones (newest first) with their outcome and evidence. Never raises: an
    unreadable record is `error`, with no rows."""
    try:
        items = _fold(core.safe_read(core.INSTANCES / name, FILE, ""))
        now = now or datetime.now()
        opened = [(pid, it) for pid, it in items.items() if it["status"] == "open"]
        if opened:
            episodes = metrics.live_episodes(name)
            judged = {r["episode_ts"]: r.get("criterion_met")
                      for r in metrics.judgments_for_stats(name) if not r.get("left_out")}
            plan = _plan_channel(name)
        rows = []
        for pid, it in sorted(opened, key=lambda kv: _at(kv[1]["deadline"])):
            a = assess(pid, it, episodes, judged, now)
            rows.append({"id": pid, "name": it["name"], "success": it["success"], "status": "open",
                         "opened_at": it["at"], "deadline": it["deadline"], "health": a["status"],
                         "hours_left": round(a["left"] / 3600, 1), "calls": a["calls"],
                         "budget_calls": it.get("budget_calls"),
                         "usd": None if plan else round(a["usd"], 6), "budget_usd": it.get("budget_usd"),
                         "evidence": None, "closed_at": None})
        closed = [(pid, it) for pid, it in items.items() if it["status"] != "open"]
        for pid, it in sorted(closed, key=lambda kv: _at(kv[1]["closed_at"]), reverse=True):
            rows.append({"id": pid, "name": it["name"], "success": it["success"], "status": it["status"],
                         "opened_at": it["at"], "deadline": it["deadline"], "health": None, "hours_left": None,
                         "calls": None, "budget_calls": it.get("budget_calls"), "usd": None,
                         "budget_usd": it.get("budget_usd"), "evidence": it["evidence"], "closed_at": it["closed_at"]})
        return {"open": len(opened), "rows": rows, "error": ""}
    except (OSError, ValueError, TypeError, KeyError) as e:
        return {"open": 0, "rows": [], "error": f"could not read {FILE}: {type(e).__name__}: {e}"}
