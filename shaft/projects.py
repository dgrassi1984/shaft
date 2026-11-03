"""Append-only project records and measured task boundaries (#106)."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

from .tools import _spec

FILE = "memory/projects.jsonl"
ID = re.compile(r"p\d{4}-\d{4}-[1-9]\d*")
OUTCOMES = ("done", "abandoned", "failed")
OPEN = _spec("open_project", "Open a project with a deadline and a checkable result.", {
    "name": {"type": "string"}, "deadline": {"type": "string", "description": "ISO date/time; date includes the whole day"},
    "budget_calls": {"type": "integer", "minimum": 1}, "budget_usd": {"type": "number", "exclusiveMinimum": 0},
    "success": {"type": "string", "description": "One line: what done means."}}, ["name", "deadline", "success"])
CLOSE = _spec("close_project", "Close an open project with evidence.", {
    "id": {"type": "string"}, "outcome": {"type": "string", "enum": list(OUTCOMES)},
    "evidence": {"type": "string"}})


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


def fold(root: Path) -> dict[str, dict]:
    path = root / FILE
    return _fold(path.read_text() if path.is_file() else "")


def _append(root, rec):
    path = root / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")


def open_project(root: Path, args: dict, now: datetime) -> str:
    if any(not isinstance(args.get(k), str) or not args[k].strip() or len(args[k].splitlines()) != 1
           for k in ("name", "success")):
        return "ERROR: need name and success, each one nonempty line"
    due = _at(args.get("deadline"))
    if due and len(args["deadline"]) == 10:
        due = due.replace(hour=23, minute=59, second=59, microsecond=999999)
    now = _at(now.isoformat())
    if due is None or due <= now:
        return "ERROR: deadline must be a future ISO date/time"
    for key in ("budget_calls", "budget_usd"):
        if args.get(key) is not None and not _budget(args[key], key == "budget_calls"):
            return f"ERROR: {key} must be positive" + (" integer" if key == "budget_calls" else " number")
    items, prefix, n = fold(root), f"p{now:%m%d-%H%M}-", 1
    while prefix + str(n) in items:
        n += 1
    pid = prefix + str(n)
    _append(root, {"event": "opened", "id": pid, "at": now.isoformat(), "deadline": due.isoformat(),
                   **{k: args.get(k) for k in ("name", "success", "budget_calls", "budget_usd")}})
    return f"project {pid} opened"


def close_project(root: Path, args: dict, now: datetime) -> str:
    pid, evidence = args.get("id"), args.get("evidence")
    item = fold(root).get(pid) if isinstance(pid, str) else None
    if item is None or item["status"] != "open":
        return "ERROR: id must name an open project"
    if args.get("outcome") not in OUTCOMES or not isinstance(evidence, str) or not evidence.strip():
        return "ERROR: need outcome done, abandoned or failed, and evidence"
    if _at(now.isoformat()) < _at(item["at"]):
        return "ERROR: cannot close before opening"
    _append(root, {"event": "closed", "id": pid, "at": now.isoformat(),
                   "outcome": args["outcome"], "evidence": evidence.strip()})
    return f"project {pid} closed as {args['outcome']}"


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


def at_risk(root: Path, now: datetime) -> list[tuple[str, str]]:
    """My open projects at risk or past a boundary, as my context lists them: (id, its line)."""
    from . import memory, surprises
    items = fold(root)
    rows = lines(items, surprises.episodes(root), memory.verdict_rows(root), now)
    open_ = [pid for pid, item in items.items() if item["status"] == "open"]   # lines() keeps this order
    return [(pid, row) for pid, row in zip(open_, rows)
            if row.removeprefix(f"- {pid} {items[pid]['name']}: ").startswith(("at risk", "past boundary"))]


def block(root: Path, now: datetime, verdicts=()) -> str:
    from . import memory, surprises
    try:
        return "\n".join(lines(fold(root), surprises.episodes(root), memory.verdict_rows(root) + list(verdicts), now)) or "(none open)"
    except Exception as e:   # a broken record must not prevent live or sleep
        return f"(my projects could not be read: {type(e).__name__}: {e})"


def controls(root, now):
    from .agent import Control
    def call(handler, args):
        try:
            return handler(root, args, now())
        except (OSError, ValueError) as e:
            return f"ERROR: project not recorded: {type(e).__name__}: {e}"
    return {"open_project": Control(OPEN, lambda args: call(open_project, args)),
            "close_project": Control(CLOSE, lambda args: call(close_project, args))}
