"""#138: sleep notices dated context and untouched Active goals without a model.

Dates are ISO or day + English/Italian month (full name or abbreviation), optionally
preceded by a weekday. Missing years mean the most recent occurrence, at local midnight.
Only dates strictly older than STALE_LINE_HOURS count. A goal is a bullet under a
## Active heading, through the next level-1/2 heading. A touch is its normalized
text, or any six consecutive words, in a live intent, finish, or tool's arguments
(never its results). Short goals require all words. We report the latest touch;
no recorded touch means unknown, not a guessed creation date. Flag only when no
touch falls within STALE_GOAL_HOURS and an operator user turn falls in that window.
These are review candidates, not claims that the text is false or the goal obsolete.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import surprises

MONTHS = {}
for number, names in enumerate((
        "january jan gennaio gen", "february feb febbraio", "march mar marzo",
        "april apr aprile", "may maggio mag", "june jun giugno giu",
        "july jul luglio lug", "august aug agosto ago", "september sep sept settembre set",
        "october oct ottobre ott", "november nov novembre", "december dec dicembre dic"), 1):
    MONTHS.update(dict.fromkeys(names.split(), number))
DATE = re.compile(r"\b(?:(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\s+("
                  + "|".join(sorted(MONTHS, key=len, reverse=True))
                  + r")\b\.?\s*(\d{4})?)", re.I)


def _dates(line: str, now: datetime):
    for match in DATE.finditer(line):
        year, month, day, named_day, named_month, named_year = match.groups()
        try:
            at = datetime(int(year or named_year or now.year),
                          int(month) if month else MONTHS[named_month.lower()], int(day or named_day))
            if not (year or named_year) and at > now:
                at = at.replace(year=at.year - 1)
            yield at
        except ValueError:
            continue


MAX_ROWS, LINE_CHARS = 30, 200


def _cut(line: str) -> str:
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS] + " […]"


def _words(text: str) -> str:
    return " " + " ".join(re.findall(r"\w+", text.casefold())) + " "


def _text(value) -> str:
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return str(value or "")


def scan(root: Path, now: datetime, line_hours: int = 24, goal_hours: int = 48) -> str:
    """#138: return review lines; reverting loses the deterministic sleep warning."""
    if line_hours <= 0 and goal_hours <= 0:
        return "Staleness checks disabled."
    now = surprises._at(now.isoformat())
    rows, goals = [], []
    for name in ("self/goals.md", "self/self.md"):
        path = root / name
        active = False
        for number, line in enumerate(path.read_text().splitlines() if path.exists() else [], 1):
            if line_hours > 0 and (dates := list(_dates(line, now))):
                at = max(dates)   # one row per line, by its newest date: a line still current by one date is not stale
                age = (now - at).total_seconds() / 3600
                if age > line_hours:
                    rows.append(f"- {name}:{number}: date {at:%Y-%m-%d}, age {age:.1f} h: {_cut(line.strip())}")
            if re.match(r"^#{1,2}\s", line):
                active = bool(re.match(r"^##\s+Active\b", line, re.I))
            if name == "self/goals.md" and active and re.match(r"^\s*[-*+]\s+", line):
                text = re.sub(r"^\s*[-*+]\s+(?:\[[ xX]\]\s*)?", "", line).strip()
                if text:
                    goals.append((number, text))
    if goal_hours > 0 and goals:
        episodes = surprises.episodes(root)
        since = now - timedelta(hours=goal_hours)
        conversed = any(ep.get("kind") == "talk" and ep.get("role") == "user" and not ep.get("speaker")
                        and (at := surprises._at(ep.get("ts"))) is not None and since <= at <= now
                        for ep in episodes)
        if conversed:
            live = []
            for ep in episodes:
                at = surprises._at(ep.get("ts"))
                if ep.get("kind") == "live" and at is not None and at <= now:
                    args = []
                    for tool in ep.get("tools", []):
                        raw = tool.get("args")
                        try:
                            args.append(json.loads(raw) if isinstance(raw, str) else raw)
                        except ValueError:   # a truncated log still contains usable words
                            args.append(raw)
                    live.append((at, _words(_text([ep.get("intent"), ep.get("finish"), args]))))
            for number, text in goals:
                words = _words(text).split()
                size = min(6, len(words))
                parts = [" " + " ".join(words[i:i + size]) + " " for i in range(len(words) - size + 1)]
                last = max((at for at, body in live if any(part in body for part in parts)), default=None)
                if last is None or last < since:
                    touch = f"{last.isoformat()}, age {(now - last).total_seconds() / 3600:.1f} h" if last else \
                        f"no recorded touch; none in the last {goal_hours} h"
                    rows.append(f"- self/goals.md:{number}: Active goal, last touch {touch}: {_cut(text)}")
    if len(rows) > MAX_ROWS:   # Thelos's 159 KB goals.md gave 504 rows (313,000 characters) uncapped, 2026-10-28
        rows = rows[:MAX_ROWS] + [f"- … and {len(rows) - MAX_ROWS} more, in file order: fix these, then the "
                                  "next sleep lists the rest"]
    return "\n".join(rows) or "No stale dated lines or untouched Active goals found (disabled checks skipped)."


def block(root: Path, now: datetime, line_hours: int = 24, goal_hours: int = 48) -> str:
    try:
        return scan(root, now, line_hours, goal_hours)
    except Exception as e:   # a broken record must not prevent sleep
        return f"(staleness scan could not be read: {type(e).__name__}: {e})"
