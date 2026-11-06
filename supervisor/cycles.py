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


def note_lines(value, fields: tuple[str, ...]) -> list[str]:
    """Instance-written notes in the same words for the web page and native apps."""
    if value is None or value == "" or value == [] or value == {}:
        return []
    items = value if isinstance(value, list) else [value]
    return [
        "; ".join(f"{key.replace('_', ' ')}: {item[key]}" if len(fields) > 1 else str(item[key]) for key in fields)
        if isinstance(item, dict) and all(key in item for key in fields) else str(item)
        for item in items]


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
        return {j["episode_ts"]: j for j in reversed(panel.judgments(name))}, ""
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
        if not shown and not skipped:
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


def cycle(name: str, ts: str) -> dict | None:
    """One finished cycle's plan, action, result and remainder, without its tool log."""
    if not isinstance(ts, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}", ts):
        return None
    try:
        end = datetime.fromisoformat(ts)
    except ValueError:
        return None
    date = ts[:10]
    lines, _ = _lines(name, date)
    for line, record in lines:
        if record.get("ts") != ts:
            continue
        raw_kind = record.get("kind")
        kind = panel.CYCLE_KINDS.get(raw_kind) if isinstance(raw_kind, str) else None
        if not kind:
            continue
        intent = record.get("intent")
        finish = record.get("finish")
        finish = finish if isinstance(finish, dict) else {}
        usage = record.get("project_usage")
        tools = record.get("tools")
        try:
            duration = int((end - datetime.fromisoformat(record.get("run_started"))).total_seconds())
        except (TypeError, ValueError):
            duration = None
        out = {
            "id": ts, "ts": ts, "kind": kind, "where": f"memory/episodes/{date}.jsonl:{line}",
            "content": record.get("content") if kind != "cycle" else None,
            "planned": None,
            "did": {"what_happened": finish.get("what_happened"), "duration_s": duration,
                    "usd": usage.get("usd") if isinstance(usage, dict) else None,
                    "tool_calls": len(tools) if isinstance(tools, list) else 0,
                    "ended_by": record.get("ended_by"), "wrapped_up": record.get("wrapped_up"),
                    "delivered_via": finish.get("delivered_via")},
            "result": None, "left": None,
        }
        if kind == "cycle":
            out["planned"] = {k: intent.get(k) for k in (
                "goal", "success_criterion", "origin", "origin_ref", "confidence", "predictions", "attention"
            )} if isinstance(intent, dict) else None
            verdicts, judge_error = _verdicts(name)
            judge = verdicts.get(ts)
            second = judge.get("second") if isinstance(judge, dict) else None
            out["result"] = {"claim": finish.get("criterion_met"), "surprises": finish.get("surprises"),
                             "doubts": finish.get("doubts"), "judge_error": judge_error,
                             "judge": _judge_detail(judge), "second": _judge_detail(second)}
            unfinished = finish.get("unfinished")
            nxt = finish.get("next_action")
            out["left"] = {"unfinished": [s for s in unfinished if isinstance(s, str)]
                           if isinstance(unfinished, list) else [],
                           "next_action": nxt if isinstance(nxt, dict) else None,
                           "next_goal": _next_goal(name, date, line, lines)}
        return out
    return None


def _judge_detail(row: dict | None) -> dict | None:
    if not isinstance(row, dict):
        return None
    return {"verdict": row.get("criterion_met"), "claim_supported": row.get("claim_supported"),
            "first_error": row.get("first_error"), "reasoning": row.get("reasoning"), "model": row.get("judge"),
            "error": row.get("error") if isinstance(row.get("error"), str) else ""}


def _next_goal(name: str, date: str, line: int, lines: list[tuple[int, dict]]) -> str | None:
    """The first later live goal, walking onward in this file and then across later day files."""
    def goal_after(rows: list[tuple[int, dict]], after: int) -> str | None:
        for n, record in rows:
            if n <= after or record.get("kind") != "live":
                continue
            intent = record.get("intent")
            goal = intent.get("goal") if isinstance(intent, dict) else None
            if isinstance(goal, str) and goal.strip():
                return goal
        return None

    goal = goal_after(lines, line)
    if goal is not None:
        return goal
    for path in sorted(panel._episode_files(name), key=lambda p: p.name):
        if path.stem <= date:
            continue
        goal = goal_after(_lines(name, path.stem)[0], 0)
        if goal is not None:
            return goal
    return None


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

    root = core.instance_root(name)
    commitments_error = ""
    try:
        ledger = panel.cached(f"cycles-commitments:{name}", [root / "memory/commitments.jsonl"],
                              lambda: commitments.ledger(name, now), ttl=60)
    except Exception as e:
        ledger, commitments_error = {}, f"{type(e).__name__}: {e}"
    items = [{"id": it["id"], "promise": it["promise"], "due": it["due"], "status": it["status"]}
             for it in ledger.values()
             if it["status"] in ("open", "overdue") and it.get("made_to") not in ("self", "own", name)]
    items.sort(key=lambda it: it["due"])

    queue = _queue(root, now)
    try:
        goals = core.safe_read(root, "self/goals.md", errors="replace")
        goals_error = ""
    except FileNotFoundError:
        goals, goals_error = "", "no goals file"
    except (core.UnsafePath, core.TooBig, OSError) as e:
        goals, goals_error = "", f"could not read self/goals.md: {e}"

    def read_improvement() -> dict | None:
        text = core.safe_read(root, "memory/journal.md")
        entries = [b.strip() for b in text.split("\n---\n") if b.strip().startswith("## ")]
        return _improvement(entries)

    improvement_error = ""
    try:
        improvement = panel.cached(f"cycles-improvement:{name}", [root / "memory/journal.md"], read_improvement)
        improvement = dict(improvement) if improvement is not None else None
    except FileNotFoundError:
        improvement = None
    except (core.UnsafePath, core.TooBig, OSError) as e:
        improvement = None
        improvement_error = f"could not read memory/journal.md: {e}"

    return {"now": now_block, "check": check, "next_step": next_step, "unfinished": unfinished,
            "commitments": items, "commitments_error": commitments_error, "queue": queue, "goals": goals,
            "goals_error": goals_error, "improvement": improvement, "improvement_error": improvement_error}


def _next_step(name: str) -> tuple[dict | None, list[str]]:
    """The newest live cycle with a next action, walking back over at most the two newest day files, and what that
    cycle left unfinished. A next_action that is not an object, or with no action, is skipped."""
    def read(date: str) -> tuple[dict | None, list[str]]:
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
            raw = fin.get("unfinished")
            unfinished = [str(s) for s in raw if isinstance(s, str)] if isinstance(raw, list) else []
            return {"ts": r.get("ts") or "", "action": action, "revise_if": revise}, unfinished
        return None, []

    dates = sorted((f.stem for f in panel._episode_files(name)), reverse=True)[:2]
    for date in dates:
        step, unfinished = panel.cached(f"cycles-next:{name}:{date}", [_day_path(name, date)], lambda: read(date))
        if step is not None:
            return dict(step), list(unfinished)
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
    soonest, at most 10; done rows are left out. Three states: rows, none (no queue file), unreadable (a link, an
    unreadable or oversized file, or a shape that is not a list of tasks)."""
    try:
        text = core.safe_read(root, "notes/queue.json", errors="replace")
    except FileNotFoundError:
        return _queue_state("none")
    except (core.UnsafePath, core.TooBig, OSError) as e:
        return _queue_state("unreadable", f"could not read notes/queue.json: {e}")
    if not text.strip():
        return _queue_state("none")
    try:
        data = json.loads(text)
    except ValueError as e:
        return _queue_state("unreadable", f"could not read notes/queue.json: {e}")
    if not isinstance(data, list):
        return _queue_state("unreadable", "could not read notes/queue.json: not a list of tasks")
    try:
        rows = [_queue_row(r) for r in data]
    except (ValueError, TypeError) as e:
        return _queue_state("unreadable", f"could not read notes/queue.json: {e}")
    now = _local_now(now)
    open_rows = [r for r in rows if not r["done"]]
    classified = [(r, _due(r["due_at"], now)) for r in open_rows]
    ordered = sorted(classified, key=lambda pair: pair[1][0])
    return {"state": "rows", "open": len(open_rows),
            "overdue": sum(1 for _, (_, overdue) in classified if overdue), "error": "",
            "rows": [{"id": r["id"], "task": r["task"][:200], "due_at": r["due_at"], "overdue": overdue}
                     for r, (_, overdue) in ordered[:10]]}


def _queue_state(state: str, error: str = "") -> dict:
    return {"state": state, "open": 0, "overdue": 0, "error": error, "rows": []}


def _queue_row(r) -> dict:
    if not isinstance(r, dict):
        raise ValueError("a queue row is an object")
    rid, task = r.get("id"), r.get("task")
    if not isinstance(rid, int) or isinstance(rid, bool) or not isinstance(task, str):
        raise ValueError("a queue row needs an integer id and a string task")
    due = r.get("due_at")
    due = due if isinstance(due, str) and due.strip() else None
    return {"id": rid, "task": task, "due_at": due, "done": r.get("status", "open") != "open"}


def _due(due: str | None, now: datetime) -> tuple[tuple, bool]:
    """(sort key, overdue?) for one due_at, the single rule for ordering, counts and row flags: overdue first (an
    empty due_at is due now), then soonest, and a due_at that does not parse last and is not overdue."""
    if due is None:
        return (0, now), True
    try:
        at = datetime.fromisoformat(due)
    except ValueError:
        return (2, due), False
    at = at.astimezone().replace(tzinfo=None) if at.tzinfo else at
    return (0, at) if at <= now else (1, at), at <= now
