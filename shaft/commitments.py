"""My commitments, recorded as append-only events and settled against my episodes."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import broker, surprises
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

COMMIT_TO = _spec("commit_to", "Record a promise to the operator or another agent. "
                  "Operator promises need this conversation or a delivered operator message in this run; "
                  "otherwise they are recorded as self with a note. My own plans belong in goals/notes.", {
    "promise": {"type": "string", "description": "What I will do."},
    "due": {"type": "string", "description": 'YYYY-MM-DD or YYYY-MM-DD HH:MM, or "today"/"tomorrow".'},
    "criterion": {"type": "string", "description": "How someone else could check it was done."},
    "made_to": {"type": "string", "description": '"operator", or a peer name from list_instances. '
                "No promises to self."}})
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
    items, self_ids = {}, set()
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
            items[cid].update({k: rec[k] for k in ("talk_id", "message_file", "note") if k in rec})
            if item["made_to"] in ("self", "own") or rec.get("self_commitment"):
                self_ids.add(cid)
        elif rec.get("event") == "settled":
            item, evidence = items.get(cid), rec.get("evidence")
            if (item and item["status"] == "open" and rec.get("status") in STATUSES
                    and not (cid in self_ids and at - _at(item["due"]) >= timedelta(hours=24))
                    and isinstance(evidence, str) and evidence.strip() and at >= _at(item["made_at"])):
                item.update(status=rec["status"], settled_at=rec["at"], evidence=evidence)
    for item in items.values():
        if item["status"] == "open":
            if item["id"] in self_ids and _local(now) - _at(item["due"]) >= timedelta(hours=24):
                expires = _at(item["due"]) + timedelta(hours=24)
                item.update(status="expired", settled_at=expires.isoformat(), evidence="self commitment expired 24h after due")
            elif _at(item["due"]) < _local(now):
                item["status"] = "overdue"
    return items


def message_file(name: str) -> re.Pattern:
    """The file name the broker gives a message I delivered to the operator (send_message's result)."""
    return re.compile(r"\d{8}-\d{6}-\d{6}-from-" + re.escape(name) + r"\.md")


def _source_rows(rows: list[dict], episodes: list[dict], delivered, own: str) -> list[dict]:
    """An operator promise needs the conversation it was made in, or a message I delivered to the operator (review
    I10). `delivered(file)` confirms a delivered message; a note in my own folder is not one."""
    talks = {ep["talk_id"] for ep in episodes if ep.get("kind") == "talk" and ep.get("role") == "user"
             and not ep.get("speaker") and isinstance(ep.get("talk_id"), str) and ep["talk_id"]}
    out = []
    for row in rows:
        row = dict(row)
        if row.get("event") == "made":
            if row.get("made_to") == "operator" and row.get("sourced"):   # older rows predate the rule: kept as made
                talk, message = row.get("talk_id"), row.get("message_file")
                valid = isinstance(talk, str) and talk in talks
                if not valid and isinstance(message, str) and message_file(own).fullmatch(message):
                    valid = delivered(message)
                if not valid:
                    row.update(made_to="self", note="operator source missing or invalid; recorded as self")
            if row.get("made_to") == own:
                row["self_commitment"] = True
        out.append(row)
    return out


def _delivered(message: str) -> bool:
    """Asked of the broker, which sees the operator's inbox; a broker failure is raised, never read as "no"."""
    return bool(re.search(r"(?m)^To: operator$", broker.call("record", kind="commitment_source", cycle=message)))


def message_source(log: list[dict]) -> dict:
    """The last message delivered to the operator in this run (this conversation or cycle), as send_message
    reported it."""
    for call in reversed(log):
        if call.get("tool") == "send_message" and isinstance(call.get("result"), str):
            match = re.search(r"; message_file (\d{8}-\d{6}-\d{6}-from-[a-z0-9_-]+\.md)", call["result"])
            if match and call["result"].startswith("delivered to the operator"):
                return {"message_file": match[1]}
    return {}


def fold(root: Path, now: datetime) -> dict[str, dict]:
    path = Path(root) / FILE
    rows = _rows(path.read_text(errors="replace")) if path.is_file() else []
    # add() asked the broker before recording a message source; the supervisor's fold checks the inbox again.
    return _fold(_source_rows(rows, surprises.episodes(root), lambda _: True, Path(root).name), now)


def _append(root: Path, event: dict) -> None:
    path = Path(root) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(event, ensure_ascii=False) + "\n")


def _to_self(root: Path, item: dict) -> bool:
    return item["made_to"] in ("self", "own", Path(root).name)


def _recipient_error(root: Path, item: dict) -> str:
    rule = "the ledger is for promises to the operator or another agent; my own plans belong in goals/notes"
    if _to_self(root, item):
        return f"ERROR: {rule}"
    if item["made_to"] == "operator":
        return ""
    try:
        peers = broker.call("list_instances")
    except broker.BrokerError:
        return f"ERROR: cannot check peer names with list_instances; {rule}"
    names = peers.removeprefix("instances: ").split(", ") if peers.startswith("instances: ") else []
    return "" if item["made_to"] in names else f"ERROR: unknown peer; use list_instances; {rule}"


def add(root: Path, args: dict, source: str, now: datetime) -> str:
    item = _entry(args)
    if item is None:
        return f"ERROR: need promise and criterion (at most {FIELD_CHARS} characters each), due and made_to (a name)"
    error = _recipient_error(root, item)
    if error:
        return error
    due = parse_due(item["due"], now)
    if due is None:
        return 'ERROR: due must be YYYY-MM-DD, YYYY-MM-DD HH:MM, "today" or "tomorrow"'
    if due < _local(now):
        return "ERROR: due is in the past"
    if source not in ("cycle", "talk"):
        return "ERROR: source must be cycle or talk"
    refs = {k: args[k] for k in ("talk_id", "message_file") if isinstance(args.get(k), str) and args[k]}
    event = {"event": "made", **item, **refs, "sourced": True}
    try:
        checked = _source_rows([event], surprises.episodes(root), _delivered, Path(root).name)[0]
    except broker.BrokerError as e:   # an outage is not a missing source: nothing is recorded
        return f"ERROR: commitment not recorded: could not check the operator message ({e}); try again later"
    item["made_to"] = checked["made_to"]
    if "note" in checked:
        refs["note"] = checked["note"]
    items = fold(root, now)
    prefix = f"c{_local(now):%m%d-%H%M}-"
    n = 1
    while prefix + str(n) in items:
        n += 1
    cid = prefix + str(n)
    _append(root, {"event": "made", "id": cid, **item, **refs, "due": due.isoformat(),
                   "source": source, "at": _local(now).isoformat(), "sourced": True})
    return f"commitment {cid} recorded, due {item['due']}" + (f"; {refs['note']}" if "note" in refs else "")


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
    if item is None or _to_self(root, item) or item["status"] not in ("open", "overdue"):
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
    return sorted((it for it in fold(root, now).values()
                   if not _to_self(root, it) and it["status"] in ("open", "overdue")),
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
