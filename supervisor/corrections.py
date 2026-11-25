"""Read-only correction alerts; the event contract matches shaft/corrections.py."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .core import jsonl_lines
from . import core, metrics

FILE = "memory/corrections.jsonl"
CLOSED_BY = ("fact", "code", "test", "not_accepted")
ID = re.compile(r"k\d{4}-\d{4}-[1-9]\d*")
FIELDS = ("what_was_wrong", "what_operator_said", "when", "conversation")


def _at(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def _fold(text: str) -> dict[str, dict]:
    items = {}
    for line in jsonl_lines(text):
        rec = json.loads(line)
        if not isinstance(rec, dict):
            raise ValueError("correction event must be an object")
        cid = rec.get("id")
        if not isinstance(cid, str) or not ID.fullmatch(cid):
            continue
        if rec.get("event") == "recorded":
            if (cid in items or any(not isinstance(rec.get(k), str) or not rec[k].strip() for k in FIELDS)
                    or _at(rec["when"]) is None):
                continue
            previous = rec.get("reopens")
            if previous is not None and (not isinstance(previous, str) or previous not in items
                    or not timedelta(0) <= _at(rec["when"]) - _at(items[previous]["when"]) <= timedelta(days=7)):
                continue
            items[cid] = {"id": cid, **{k: rec[k] for k in FIELDS}, "reopens": previous,
                          "status": "open", "closed_by": None, "evidence": None, "what_failed": None}
        elif rec.get("event") == "closed":
            evidence = rec.get("evidence")
            if (cid in items and items[cid]["status"] == "open" and rec.get("closed_by") in CLOSED_BY
                    and isinstance(evidence, str) and evidence.strip()):
                failed = rec.get("what_failed")   # what in the fix it reopened did not hold (#11)
                items[cid].update(status="closed", closed_by=rec["closed_by"], evidence=evidence,
                                  what_failed=failed if isinstance(failed, str) and failed.strip() else None)
    for it in items.values():   # the later corrections that reopened this one
        it["reopened_by"] = [later["id"] for later in items.values() if later["reopens"] == it["id"]]
    return items


def alerts(name: str, cycles: int, now: datetime) -> list[dict]:
    if cycles <= 0:
        return []
    try:
        items = _fold(core.safe_read(core.INSTANCES / name, FILE, ""))
        opened = [it for it in items.values() if it["status"] == "open"]
        if not opened:
            return []
        ends = [at for ep in metrics.live_episodes(name) if (at := _at(ep.get("ts"))) is not None
                and at <= _at(now.isoformat())]
        out = []
        for it in opened:
            count = sum(at > _at(it["when"]) for at in ends)
            if count > cycles:
                out.append({"id": f"correction:{name}:{it['id']}",
                            "text": f"{name}: correction {it['id']} open for {count} live cycles: "
                                    + it["what_was_wrong"]})
        return out
    except (OSError, ValueError, TypeError):   # an unreadable or malformed instance file raises no alert
        return []
