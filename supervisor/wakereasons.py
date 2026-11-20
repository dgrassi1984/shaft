"""Two reasons for a timed wake that are about an intention, not an event: a project past a boundary, and an order
the operator gave that has had no result for a day.

The wake gate knew an unread message, a commitment coming due, a job result and idleness. A plan could pass its date
and an order could sit unanswered without anything waking the instance for it. Both are facts the supervisor already
holds (projects.py, commitments.py); this only lets them start a wake. Each item wakes once, then at most once per
PACE, so a project nobody closes does not wake the instance at every tick: the times are kept in
SUP_DIR/wake_reasons/<name>.json. Nothing here tells the instance what to do with the wake.

"An order with no result": a commitment made to the operator (commitments.ledger keeps only those with a source: a
conversation, a message of his or one delivered to him, #238), still open STALE after it was made and due within
DUE_WINDOW (#242: two promises due in December woke the instance 13 times in a week, with nothing to deliver yet). One
past its due time already wakes by the existing rules.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import core

PACE = timedelta(hours=24)
STALE = timedelta(hours=24)
DUE_WINDOW = timedelta(days=3)


def _path(name: str) -> Path:
    return core.SUP_DIR / "wake_reasons" / f"{name}.json"


def _woken(name: str) -> dict:
    try:
        data = json.loads(_path(name).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def reasons(name: str, now: datetime) -> list[tuple[str, str]]:
    """(key, why) for every such reason that holds now, projects first. The key names the item, so its wakes are
    paced per item."""
    from . import commitments, metrics, projects
    out = []
    items = projects._fold(core.safe_read(core.INSTANCES / name, projects.FILE, ""))
    if any(it["status"] == "open" for it in items.values()):
        for row in projects.lines(items, metrics.live_episodes(name), metrics.judgments_for_stats(name), now):
            m = re.match(r"- (\S+) (.*?): past boundary: ([^;]+);", row)
            if m:
                out.append((f"project:{m[1]}", f"project {m[1]} is past its boundary ({m[3]})"))
    for item in commitments.ledger(name, now).values():
        made, due_at = commitments._at(item.get("made_at")), commitments._at(item.get("due"))
        if (item["made_to"] == "operator" and item["status"] == "open" and made is not None and due_at is not None
                and now - made >= STALE and due_at - now <= DUE_WINDOW):
            # no elapsed time in the text: the scheduler marks the wake by this text a moment after reading it
            out.append((f"order:{item['id']}", f"the operator's order {item['id']} of {made:%Y-%m-%d %H:%M}, due "
                                               f"{due_at:%Y-%m-%d %H:%M}, has had no result for over a day"))
    return out


def due(name: str, now: datetime) -> tuple[str, str]:
    """(key, why) of the first reason that has not woken the instance within PACE, or ("", "")."""
    woken = _woken(name)
    for key, why in reasons(name, now):
        last = woken.get(key)
        try:
            recent = last is not None and now - datetime.fromisoformat(last) < PACE
        except (TypeError, ValueError):
            recent = False
        if not recent:
            return key, why
    return "", ""


def mark(name: str, why: str, now: datetime) -> None:
    """Record that the wake started for `why` (as the scheduler logged it) used up that item's turn; reasons that no
    longer hold are forgotten, so the file stays as small as what is open."""
    current = dict(reasons(name, now))
    key = next((k for k, text in current.items() if text == why), None)
    if key is None:
        return
    woken = {k: v for k, v in _woken(name).items() if k in current}
    woken[key] = now.isoformat(timespec="seconds")
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(woken, indent=1))
    tmp.replace(p)
