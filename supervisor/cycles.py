"""Read-only cycle history and next-wake plan for the Cycles screen (web and /app/v1).

Reads episode logs one day's file at a time through panel._episode_files and panel._episode_text, caching only compact
row fields per day file so tool logs do not stay in memory.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import core, panel

FILTERS = ("", "yours", "short", "delivered")
GOAL_CHARS = 300


def _day_path(name: str, date: str) -> Path:
    return core.instance_root(name) / "memory" / "episodes" / f"{date}.jsonl"


def _lines(name: str, date: str) -> tuple[list[tuple[int, dict]], int]:
    """(line number, parsed record) for one day's episode file, oldest first, plus unparsed line count."""
    if "/" in date or "\\" in date or date in ("", ".", ".."):
        return [], 0
    path = _day_path(name, date)
    out: list[tuple[int, dict]] = []
    skipped = 0
    for n, line in enumerate(panel._episode_text(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(r, dict):
            out.append((n, r))
        else:
            skipped += 1
    return out, skipped


def _records(name: str, date: str) -> tuple[list[dict], int]:
    """Compact cycle rows for one day, oldest first, plus skipped lines; cached on the day's file."""
    if "/" in date or "\\" in date or date in ("", ".", ".."):
        return [], 0
    path = _day_path(name, date)

    def read() -> tuple[list[dict], int]:
        lines, skipped = _lines(name, date)
        out: list[dict] = []
        for _, r in lines:
            kind = panel.CYCLE_KINDS.get(r.get("kind"))
            if not kind:
                continue
            ts = r.get("ts") or ""
            intent = r.get("intent") if isinstance(r.get("intent"), dict) else {}
            finish = r.get("finish") if isinstance(r.get("finish"), dict) else {}
            goal = intent.get("goal")
            via = finish.get("delivered_via")
            out.append({
                "id": ts, "ts": ts, "kind": kind,
                "goal": str(goal)[:GOAL_CHARS] if goal is not None else None,
                "claim": finish.get("criterion_met") if kind == "cycle" else None,
                "verdict": None,
                "delivered": bool(via and via != "none") if kind == "cycle" else False,
                "origin": intent.get("origin"),
                "ended_by": r.get("ended_by"),
            })
        return out, skipped

    return panel.cached(f"cycles:{name}:{date}", [path], read)


def _verdicts(name: str) -> tuple[dict[str, dict], str]:
    """Judgments indexed by episode_ts, plus an error string ("" when fine)."""
    try:
        return {j["episode_ts"]: j for j in panel.judgments(name)}, ""
    except (OSError, ValueError, KeyError) as e:
        return {}, f"{type(e).__name__}: {e}"


def _matches(row: dict, filt: str) -> bool:
    if filt == "":
        return True
    if row["kind"] != "cycle":
        return False
    if filt == "yours":
        return row["origin"] == "operator"
    if filt == "short":
        return row["claim"] != "yes" or row["verdict"] in ("partial", "no")
    if filt == "delivered":
        return bool(row["delivered"])
    return False


def days(name: str, before: str | None = None, limit: int = 7, filt: str = "") -> dict:
    """Day groups newest first on or before `before` (`day <= before`), with filtered rows, counts, and next cursor."""
    verdicts, judge_error = _verdicts(name)
    files = sorted(panel._episode_files(name), key=lambda p: p.name, reverse=True)
    out: list[dict] = []
    next_day: str | None = None
    for f in files:
        day = f.stem
        if before and day > before:
            continue
        if len(out) >= limit:
            next_day = day
            break
        records, skipped = _records(name, day)
        rows = [{**r, "verdict": verdicts[r["ts"]].get("criterion_met")
                 if r["kind"] == "cycle" and r["ts"] in verdicts else None} for r in records]
        shown = sorted((r for r in rows if _matches(r, filt)), key=lambda r: r["ts"], reverse=True)
        if not shown:
            continue
        live = [r for r in rows if r["kind"] == "cycle"]
        counts = {
            "cycles": len(live),
            "yes": sum(1 for r in live if r["claim"] == "yes"),
            "partial": sum(1 for r in live if r["claim"] == "partial"),
            "no": sum(1 for r in live if r["claim"] == "no"),
            "none": sum(1 for r in live if r["claim"] not in ("yes", "partial", "no")),
            "judged": sum(1 for r in live if r["verdict"] is not None),
            "for_you": sum(1 for r in live if r["origin"] == "operator"),
        }
        out.append({"date": day, "skipped_lines": skipped, "counts": counts, "rows": shown})
    return {"days": out, "next": next_day, "judge_error": judge_error}


def plan(name: str, live: dict, now: datetime | None = None) -> dict:
    """The Next block: what the instance is doing now, when it is next checked, what it said it would do next, its
    open commitments and task queue, its goals, and its latest improvement plan."""
    from . import autonomy, commitments

    state = live.get("state")
    now_block = {"state": state, "since": live.get("since") or None,
                 "goal": (live.get("goal") or None) if state == "in a cycle" else None}

    paused = autonomy.paused(name)
    try:
        nxt = "paused" if paused else autonomy.next_wake(name, now)
        skipped = autonomy.last_skip(name)
    except Exception as e:   # a schedule that cannot be read must not take the screen down
        nxt, skipped = "unknown", f"{type(e).__name__}: {e}"
    check = {"next": nxt, "skipped": skipped, "paused": paused}

    next_step, unfinished = _next_step(name)

    commitments_error = ""
    try:
        ledger = commitments.ledger(name, now)
    except Exception as e:
        ledger, commitments_error = {}, f"{type(e).__name__}: {e}"
    items = [{"id": it["id"], "promise": it["promise"], "due": it["due"], "status": it["status"]}
             for it in ledger.values()
             if it["status"] in ("open", "overdue") and it.get("made_to") not in ("self", "own", name)]
    items.sort(key=lambda it: it["due"])

    root = core.instance_root(name)
    queue = _queue(root, now)
    goals = core.safe_read(root, "self/goals.md", "", errors="replace")
    improvement = _improvement(core.journal_entries(root))

    return {"now": now_block, "check": check, "next_step": next_step, "unfinished": unfinished,
            "commitments": items, "commitments_error": commitments_error, "queue": queue, "goals": goals,
            "improvement": improvement}


def _next_step(name: str) -> tuple[dict | None, list[str]]:
    """The newest live cycle with a next action, walking back over at most the two newest day files, and what that
    cycle left unfinished. A next_action that is not an object, or with no action, is skipped."""
    dates = sorted((f.stem for f in panel._episode_files(name)), reverse=True)[:2]
    for date in dates:
        for _, r in reversed(_lines(name, date)[0]):
            if r.get("kind") != "live":
                continue
            fin = r.get("finish")
            if not isinstance(fin, dict):
                continue
            nxt = fin.get("next_action")
            if not isinstance(nxt, dict):
                continue
            action = str(nxt.get("action") or "").strip()
            if not action:
                continue
            revise = str(nxt.get("revise_if") or "").strip() or None
            unfinished = [str(s) for s in (fin.get("unfinished") or []) if isinstance(s, str)]
            return {"ts": r.get("ts") or "", "action": action, "revise_if": revise}, unfinished
    return None, []


_IMPROVE = re.compile(r"how i will improve", re.I)


def _improvement(entries: list[str]) -> dict | None:
    """The newest Sleep or Reflection journal entry's "How I will improve" plan, up to its consolidation actions,
    dated. Mirrors template/shaft/prompts.py improvement_plan (kept in step by hand: the supervisor never imports
    agent code)."""
    for entry in reversed(entries):
        title = entry.splitlines()[0].lstrip("# ").strip()
        what = title.partition(" — ")[2]
        if not what.startswith(("Sleep", "Reflection")):
            continue
        m = _IMPROVE.search(entry)
        if not m:
            continue
        start = entry.rfind("\n", 0, m.start()) + 1
        plan = entry[start:].split("*Consolidation actions:*")[0].strip()
        cut = f"{plan[:2000]} […]" if len(plan) > 2000 else plan
        return {"ts": title[:16], "text": cut}
    return None


def _local_now(now: datetime | None) -> datetime:
    now = now or datetime.now()
    return now.astimezone().replace(tzinfo=None) if now.tzinfo else now


def _queue(root: Path, now: datetime | None) -> dict:
    """The instance's task queue (notes/queue.json): open rows, overdue first (an empty due_at is due now), then
    soonest, at most 10; done rows are left out. Three states: rows, none (no queue), unreadable (a link, a bad
    shape, or JSON that does not read)."""
    text = core.safe_read(root, "notes/queue.json", "", errors="replace")
    if not text.strip():
        return {"state": "none", "open": 0, "overdue": 0, "error": "", "rows": []}
    try:
        data = json.loads(text)
    except ValueError as e:
        return {"state": "unreadable", "open": 0, "overdue": 0, "error": f"ValueError: {e}", "rows": []}
    if not isinstance(data, list):
        return {"state": "unreadable", "open": 0, "overdue": 0,
                "error": "notes/queue.json is not a list of tasks", "rows": []}
    try:
        rows = [_queue_row(r) for r in data]
    except (ValueError, TypeError) as e:
        return {"state": "unreadable", "open": 0, "overdue": 0, "error": f"{type(e).__name__}: {e}", "rows": []}
    now = _local_now(now)
    open_rows = [r for r in rows if not r["done"]]
    ordered = sorted(open_rows, key=lambda r: _due_key(r["due_at"], now))
    return {"state": "rows", "open": len(open_rows),
            "overdue": sum(1 for r in open_rows if _is_overdue(r["due_at"], now)), "error": "",
            "rows": [{"id": r["id"], "task": r["task"][:200], "due_at": r["due_at"],
                      "overdue": _is_overdue(r["due_at"], now)} for r in ordered[:10]]}


def _queue_row(r) -> dict:
    if not isinstance(r, dict):
        raise ValueError("a queue row is an object")
    rid, task = r.get("id"), r.get("task")
    if not isinstance(rid, int) or isinstance(rid, bool) or not isinstance(task, str):
        raise ValueError("a queue row needs an integer id and a string task")
    due = r.get("due_at")
    due = due if isinstance(due, str) and due.strip() else None
    return {"id": rid, "task": task, "due_at": due, "done": bool(r.get("done"))}


def _due_key(due: str | None, now: datetime) -> tuple:
    """A sort key: overdue first (an empty due_at is due now), then soonest, and a due_at that does not parse last."""
    if due is None:
        return 0, now
    try:
        at = datetime.fromisoformat(due)
    except ValueError:
        return 2, due
    at = at.astimezone().replace(tzinfo=None) if at.tzinfo else at
    return (0, at) if at <= now else (1, at)


def _is_overdue(due: str | None, now: datetime) -> bool:
    if due is None:
        return True
    try:
        at = datetime.fromisoformat(due)
    except ValueError:
        return False
    at = at.astimezone().replace(tzinfo=None) if at.tzinfo else at
    return at <= now
