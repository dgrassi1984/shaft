"""My commitments, recorded as append-only events and settled against my episodes."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import surprises
from .tools import _spec

FILE = "memory/commitments.jsonl"
STATUSES = ("kept", "broken", "superseded", "void")
OPEN_SHOWN = 6
FIELD_CHARS = surprises.FIELD_CHARS
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
ID = re.compile(r"c\d{4}-\d{4}-[1-9]\d*")
FAILED = ("ERROR", "(repeat: not run)", "(held, not run yet)")
FIELDS = ("promise", "due", "criterion", "made_to")
# Calls that record or report rather than do: commit_to's own "recorded" made any committing cycle its own evidence
# (Grok's review). Evidence is a call that did something.
BOOKKEEPING = ("commit_to", "settle_commitment", "declare_intent", "confirm_confidence", "finish_cycle",
               "close_surprise")

COMMIT_TO = _spec("commit_to", "Record a commitment I will keep and someone else can check.", {
    "promise": {"type": "string", "description": "What I will do."},
    "due": {"type": "string", "description": 'YYYY-MM-DD or YYYY-MM-DD HH:MM, or "today"/"tomorrow".'},
    "criterion": {"type": "string", "description": "How someone else could check it was done."},
    "made_to": {"type": "string", "description": '"operator", "self", or another instance\'s name.'}})
SETTLE = _spec("settle_commitment", "Settle one of my open commitments with evidence.", {
    "id": {"type": "string"}, "status": {"type": "string", "enum": list(STATUSES)},
    "evidence": {"type": "string", "description": "For kept: an episode ts, optionally #step (1-based tool-log "
                 "step). For the others: one sentence why."}})


def _at(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _local(at)


def _local(at: datetime) -> datetime:
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def parse_due(text: str, now: datetime) -> datetime | None:
    """My deadline in local time; a date alone includes the whole day."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    now = _local(now)
    if text in ("today", "tomorrow"):
        day = now + timedelta(days=text == "tomorrow")
        return day.replace(hour=23, minute=59, second=59, microsecond=999999)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?", text):
        return None
    try:
        due = datetime.strptime(text, "%Y-%m-%d" if len(text) == 10 else "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return due.replace(hour=23, minute=59, second=59, microsecond=999999) if len(text) == 10 else due


def _entry(value) -> dict | None:
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) for k in FIELDS):
        return None
    item = {k: " ".join(value[k].split()) for k in FIELDS}
    if any(not item[k] for k in FIELDS) or any(len(item[k]) > FIELD_CHARS for k in ("promise", "criterion")):
        return None
    return item if NAME.fullmatch(item["made_to"]) else None


def clean(value) -> list[dict]:
    """Malformed promises in my cycle report are dropped, never an error."""
    entries = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    return [it for it in map(_entry, entries) if it and parse_due(it["due"], datetime(2000, 1, 1)) is not None]


def _rows(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _fold(rows: list[dict], now: datetime) -> dict[str, dict]:
    # The supervisor keeps its own copy; shared fixtures pin the event contract.
    items = {}
    for rec in rows:
        cid, at = rec.get("id"), _at(rec.get("at"))
        if not isinstance(cid, str) or not ID.fullmatch(cid) or at is None:
            continue
        if rec.get("event") == "made":
            item, due = _entry(rec), _at(rec.get("due"))
            if item is None or due is None or due < at or rec.get("source") not in ("cycle", "talk") or cid in items:
                continue
            items[cid] = {"id": cid, **item, "source": rec["source"], "made_at": rec["at"],
                          "status": "open", "settled_at": None, "evidence": None}
        elif rec.get("event") == "settled":
            item, evidence = items.get(cid), rec.get("evidence")
            if (item and item["status"] == "open" and rec.get("status") in STATUSES
                    and isinstance(evidence, str) and evidence.strip() and at >= _at(item["made_at"])):
                item.update(status=rec["status"], settled_at=rec["at"], evidence=evidence)
    for item in items.values():
        if item["status"] == "open" and _at(item["due"]) < _local(now):
            item["status"] = "overdue"
    return items


def fold(root: Path, now: datetime) -> dict[str, dict]:
    path = Path(root) / FILE
    return _fold(_rows(path.read_text(errors="replace")) if path.is_file() else [], now)


def _append(root: Path, event: dict) -> None:
    path = Path(root) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(event, ensure_ascii=False) + "\n")


def add(root: Path, args: dict, source: str, now: datetime) -> str:
    item = _entry(args)
    if item is None:
        return f"ERROR: need promise and criterion (at most {FIELD_CHARS} characters each), due and made_to (a name)"
    due = parse_due(item["due"], now)
    if due is None:
        return 'ERROR: due must be YYYY-MM-DD, YYYY-MM-DD HH:MM, "today" or "tomorrow"'
    if due < _local(now):
        return "ERROR: due is in the past"
    if source not in ("cycle", "talk"):
        return "ERROR: source must be cycle or talk"
    items = fold(root, now)
    prefix = f"c{_local(now):%m%d-%H%M}-"
    n = 1
    while prefix + str(n) in items:
        n += 1
    cid = prefix + str(n)
    _append(root, {"event": "made", "id": cid, **item, "due": due.isoformat(),
                   "source": source, "at": _local(now).isoformat()})
    return f"commitment {cid} recorded, due {item['due']}"


def _citation(evidence: str) -> tuple[str, int | None] | None:
    ts, sep, step = evidence.partition("#")
    if sep and (not re.fullmatch(r"[1-9]\d{0,8}", step)):
        return None
    if _at(ts) is None:
        return None
    return ts, int(step) if sep else None


def _matches(ts, cited: str) -> bool:
    return isinstance(ts, str) and (ts == cited or len(cited) == 16 and ts[:16].replace("T", " ") == cited.replace("T", " "))


def _evidence(episodes: list[dict], item: dict, evidence: str) -> tuple[str, dict | None]:
    citation = _citation(evidence)
    if citation is None:
        return "cite an episode ts, optionally #step", None
    ts, step = citation
    matches = [ep for ep in episodes if _matches(ep.get("ts"), ts)]
    if not matches:
        return "no such episode", None
    if len(matches) != 1:
        return "episode citation is ambiguous; use the exact ts", None
    ep = matches[0]
    ended, made = _at(ep.get("ts")), _at(item.get("made_at"))
    if ended is None or made is None or ended <= made:
        return "cited episode did not end after my commitment", ep
    calls = ep.get("tools")
    calls = calls if isinstance(calls, list) else []
    if step is not None:
        if step > len(calls):
            return "no such tool-log step", ep
        calls = [calls[step - 1]]
    good = any(isinstance(c, dict) and isinstance(c.get("tool"), str) and bool(c["tool"])
               and c["tool"] not in BOOKKEEPING
               and isinstance(c.get("result"), str) and not c["result"].startswith(FAILED) for c in calls)
    return ("" if good else "cited evidence has no successful tool call"), ep


def settle(root: Path, args: dict, now: datetime) -> str:
    cid, status = args.get("id"), args.get("status")
    item = fold(root, now).get(cid) if isinstance(cid, str) else None
    if item is None or item["status"] not in ("open", "overdue"):
        return "ERROR: id must name one of my open commitments"
    if status not in STATUSES:
        return f"ERROR: status must be one of {', '.join(STATUSES)}"
    evidence = args.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return "ERROR: evidence must cite an episode for kept, or say why for the others"
    evidence = " ".join(evidence.split())
    if _local(now) < _at(item["made_at"]):
        return "ERROR: cannot settle before my commitment was made"
    if status == "kept":
        reason, _ = _evidence(surprises.episodes(root), item, evidence)
        if reason:
            return f"ERROR: {reason}"
    _append(root, {"event": "settled", "id": cid, "status": status, "evidence": evidence,
                   "at": _local(now).isoformat()})
    return f"settled {cid} as {status} (evidence {evidence})"


def _open(root: Path, now: datetime) -> list[dict]:
    return sorted((it for it in fold(root, now).values() if it["status"] in ("open", "overdue")),
                  key=lambda it: (it["status"] != "overdue", _at(it["due"]), it["id"]))


def overdue(root: Path, now: datetime) -> list[dict]:
    return [it for it in _open(root, now) if it["status"] == "overdue"]


def _row(item: dict, now: datetime) -> str:
    due = _at(item["due"])
    shown = due.strftime("%Y-%m-%d" if due.hour == 23 and due.minute == 59 and due.second == 59 else "%Y-%m-%d %H:%M")
    late = ""
    if item["status"] == "overdue":
        days = (_local(now).date() - due.date()).days
        late = (f" — overdue since {due:%H:%M}" if days < 1 else f" — overdue by {days} day{'s' if days != 1 else ''}")
    return (f"- {item['id']} (to {item['made_to']}, due {shown}{late}): {item['promise']}"
            f" — check: {item['criterion']}")


def block(root: Path, now: datetime) -> str:
    return "\n".join(_row(it, now) for it in _open(root, now)[:OPEN_SHOWN]) or "(none)"


def sleep_text(root: Path, now: datetime) -> str:
    return "\n".join(_row(it, now) for it in overdue(root, now)) or "(none overdue)"
