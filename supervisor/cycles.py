"""Read-only cycle history and next-wake plan for the Cycles screen (web and /app/v1).

Reads episode logs one day's file at a time through panel._episode_files and panel._episode_text, caching only compact
row fields per day file so tool logs do not stay in memory.
"""
from __future__ import annotations

import json
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
