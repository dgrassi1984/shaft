"""Fingerprints of the tool results the broker served: what a cited tool-log step is checked against.

A kept commitment's evidence and a prediction's outcome cite a step of an episode, and the episode is the instance's
own file: the tool, its arguments and the result text are its word. The broker's log line says a call happened (time,
tool, ok or error) but keeps nothing of the result, and the result may hold the operator's mail, Drive or Chat. So at
each supervisor-tool call the broker keeps a fingerprint of the result, never the result: its length and the sha256
of its text, and the sha256 of the part an episode keeps of a long result (the template's tool log keeps the first
EPISODE_CUT characters of a result longer than its limit, followed by a "[... truncated, N chars total" marker).

A cited step of a supervisor tool counts only when the record holds a successful call of that tool by that instance
inside the cited cycle's run window whose fingerprint matches the episode's text. Built-in and plugin calls never
reach the broker: they cannot be checked this way, and the output says so. Cycles that started before the record
began are read as they were before it (`since`).

SUP_DIR/served/<name>.jsonl: one row per successful call, {ts, tool, len, sha256, cut}. SUP_DIR/served/since: when
the record began. A trial replay keeps none (its calls are not the instance's).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import core

DIR = "served"
# The template's tool log (shaft/tools.py): a result over MAX_RESULT_CHARS (6000) is kept as its first
# MAX_RESULT_CHARS - READ_MARKER_ROOM characters and a marker naming the full length.
EPISODE_CUT = 5600
CUT_MARK = re.compile(r"\n\[\.\.\. truncated, (\d+) chars total")
SLACK = timedelta(seconds=2)       # the record's and the episode's stamps keep whole seconds
NOT_CHECKED = "built-in and plugin calls never reach the supervisor and are not checked against its record"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def fingerprint(text: str) -> dict:
    """What is kept of a result: never the text."""
    return {"len": len(text), "sha256": _sha(text), "cut": _sha(text[:EPISODE_CUT]) if len(text) > EPISODE_CUT else None}


def matches(row: dict, text) -> bool:
    """The episode's text is the served result, whole or as the template's tool log cuts it."""
    if not isinstance(text, str):
        return False
    if len(text) == row.get("len") and _sha(text) == row.get("sha256"):
        return True
    m = CUT_MARK.match(text, EPISODE_CUT)
    return bool(m and row.get("cut") and int(m[1]) == row.get("len") and _sha(text[:EPISODE_CUT]) == row["cut"])


def _dir(base: Path | None = None) -> Path:
    return (base if base is not None else core.SUP_DIR) / DIR


def begin(at: datetime, base: Path | None = None) -> Path:
    """Mark when the record began, once: cycles that started before it are read as they were before it."""
    folder = _dir(base)
    folder.mkdir(parents=True, exist_ok=True)
    since = folder / "since"
    if not since.exists():
        since.write_text(at.isoformat(timespec="seconds") + "\n")
    return folder


def keep(name: str, tool: str, result, at: datetime | None = None, base: Path | None = None) -> None:
    """Append the fingerprint of one successful call's result (a non-text result is kept as its JSON)."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    at = at or datetime.now()
    folder = begin(at, base)
    with (folder / f"{name}.jsonl").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({"ts": at.isoformat(timespec="seconds"), "tool": tool, **fingerprint(text)}) + "\n")


def _at(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return at.astimezone().replace(tzinfo=None) if at is not None and at.tzinfo else at


class Record:
    """One instance's record, read once: check(ep, call) -> None (not checkable), "" (served and matching) or the
    reason it does not count."""

    def __init__(self, name: str, base: Path | None = None):
        folder = _dir(base)
        try:
            self.since = _at((folder / "since").read_text().strip())
        except OSError:
            self.since = None
        self.rows: list[dict] = []
        try:
            text = (folder / f"{name}.jsonl").read_text()
        except OSError:
            text = ""
        for line in core.jsonl_lines(text):
            try:
                row = json.loads(line)
            except ValueError:
                continue       # a torn line must not hide the rows around it
            if isinstance(row, dict) and (at := _at(row.get("ts"))) is not None:
                self.rows.append({**row, "at": at})

    def check(self, ep: dict, call: dict) -> str | None:
        from .broker import SPECS
        tool = call.get("tool")
        if tool not in SPECS:
            return None
        end = _at(ep.get("ts"))
        start = _at(ep.get("run_started"))
        if end is None:
            return "the cited cycle has no end time"
        if self.since is None or (start or end) < self.since:
            return None       # before the record began: read as it was then
        start = start if start is not None else end - timedelta(hours=2)
        served = [r for r in self.rows if r.get("tool") == tool and start - SLACK <= r["at"] <= end + SLACK]
        if not served:
            return f"the supervisor served no successful {tool} call in that cycle"
        if any(matches(r, call.get("result")) for r in served):
            return ""
        return "result differs from the supervisor's record"
