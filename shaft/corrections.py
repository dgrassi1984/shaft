"""Operator corrections, recorded and closed as append-only events."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .tools import _spec

FILE = "memory/corrections.jsonl"
CLOSED_BY = ("fact", "code", "test", "not_accepted")
ID = re.compile(r"k\d{4}-\d{4}-[1-9]\d*")
FIELDS = ("what_was_wrong", "what_operator_said", "when", "conversation")
RECORD = _spec("record_correction", "Record one fact the operator corrected or complaint about my behaviour. "
               "For the same correction within 7 days, name its previous id in reopens.", {
    "what_was_wrong": {"type": "string"}, "what_operator_said": {"type": "string"},
    "when": {"type": "string", "description": "Timestamp of the operator's correction in the transcript."},
    "reopens": {"type": ["string", "null"], "description": "The earlier correction's id (kMMDD-HHMM-n), or null for a "
                "new correction."}})
CLOSE = _spec("close_correction", "Close an open correction with a change or an explicit reason not to accept it.", {
    "id": {"type": "string"}, "closed_by": {"type": "string", "enum": list(CLOSED_BY)},
    "evidence": {"type": "string", "description": "Name the episode, commit or fact; for not_accepted, also say why."}})


def _at(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def fold(root: Path) -> dict[str, dict]:
    path = Path(root) / FILE
    items = {}
    for line in path.read_text().splitlines() if path.is_file() else []:
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
                          "status": "open", "closed_by": None, "evidence": None}
        elif rec.get("event") == "closed":
            evidence = rec.get("evidence")
            if (cid in items and items[cid]["status"] == "open" and rec.get("closed_by") in CLOSED_BY
                    and isinstance(evidence, str) and evidence.strip()):
                items[cid].update(status="closed", closed_by=rec["closed_by"], evidence=evidence)
    return items


def _append(root: Path, event: dict) -> None:
    path = Path(root) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(event, ensure_ascii=False) + "\n")


def record(root: Path, args: dict, conversation: str, now: datetime) -> str:
    """Each refusal names the field and the value it got, and says what to send: on mimo (2026-10-28..30) every
    call was refused — reopens "null", or when copied with the transcript's brackets — and the writeups went on as
    if recorded. The writeup logs every result (recovery.log_corrections)."""
    item = {k: args.get(k) for k in FIELDS}
    item["conversation"] = conversation
    if isinstance(item["when"], str):   # copied from a transcript label: "[2026-10-29T14:08:39]"
        item["when"] = item["when"].strip().removeprefix("[").removesuffix("]").strip()
    missing = [k for k in FIELDS if not isinstance(item[k], str) or not item[k].strip()]
    if missing:
        return f"ERROR: not recorded: {', '.join(missing)} must be non-empty text"
    if _at(item["when"]) is None:
        return (f"ERROR: not recorded: when {item['when'][:60]!r} is not a timestamp; give the transcript's, "
                "like 2026-10-29T14:08:39")
    items = fold(root)
    for it in items.values():   # a writeup retried after interruption records the same turn only once
        if all(it[k] == item[k] for k in ("conversation", "what_operator_said", "when", "what_was_wrong")):
            return f"correction {it['id']} already recorded"
    previous = args.get("reopens")
    if previous is not None and (not isinstance(previous, str) or previous not in items
            or not timedelta(0) <= _at(item["when"]) - _at(items[previous]["when"]) <= timedelta(days=7)):
        recent = [cid for cid, it in items.items()
                  if timedelta(0) <= _at(item["when"]) - _at(it["when"]) <= timedelta(days=7)]
        return (f"ERROR: not recorded: reopens {str(previous)[:60]!r} is not a correction recorded within the 7 "
                f"days before {item['when']} (those are: {', '.join(recent) or 'none'}). For a new correction "
                "leave reopens out or pass JSON null, not the text \"null\".")
    prefix = f"k{_at(item['when']):%m%d-%H%M}-"
    n = 1
    while prefix + str(n) in items:
        n += 1
    cid = prefix + str(n)
    _append(root, {"event": "recorded", "id": cid, **item, "reopens": previous, "at": now.isoformat()})
    return f"correction {cid} recorded"


def close(root: Path, args: dict, now: datetime) -> str:
    cid, by, evidence = args.get("id"), args.get("closed_by"), args.get("evidence")
    item = fold(root).get(cid) if isinstance(cid, str) else None
    if item is None or item["status"] != "open":
        return "ERROR: id must name an open correction"
    if by not in CLOSED_BY:
        return f"ERROR: closed_by must be one of {', '.join(CLOSED_BY)}"
    if not isinstance(evidence, str) or not evidence.strip():
        return "ERROR: evidence must name an episode, commit or fact, with a reason for not_accepted"
    _append(root, {"event": "closed", "id": cid, "closed_by": by,
                   "evidence": evidence.strip(), "at": now.isoformat()})
    return f"closed correction {cid} ({by})"


def block(root: Path, now: datetime) -> str:
    try:
        items = list(fold(root).values())
        recent = [it for it in items if timedelta(0) <= _at(now.isoformat()) - _at(it["when"]) <= timedelta(days=7)]
        counts = f"reopened {sum(it['reopens'] is not None for it in recent)} of {len(recent)} in 7 days"
        rows = [f"- {it['id']} ({it['when']}, conversation {it['conversation']}): {it['what_was_wrong']}"
                f"; operator said: {it['what_operator_said']}" for it in items if it["status"] == "open"]
        return counts + "\n" + ("\n".join(rows) or "(none open)")
    except Exception as e:   # a broken record must not prevent live or sleep
        return f"(my corrections could not be read: {type(e).__name__}: {e})"
