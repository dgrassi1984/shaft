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
    "evidence": {"type": "string", "description": "Name the episode, commit or fact; for not_accepted, also say why."},
    "what_failed": {"type": "string", "description": "Required when it reopens an earlier correction: what in the "
                    "earlier fix did not hold, and why this change will."}}, ["id", "closed_by", "evidence"])
SEARCH = _spec("search_corrections", "Search every correction the operator made, open and closed: what was wrong, "
               "what he said, how it was closed, and what reopened it. Run it on the topic before asking him anything "
               "or suggesting something, and say what it found.", {
    "query": {"type": "string", "description": "The topic: what you are about to ask or suggest."},
    "k": {"type": "integer", "description": "how many, default 3"}}, ["query"])
NONE_RECORDED = "No corrections recorded yet."
HOLD_LINE = "Before this goes to the operator, my corrections closest to it"
K = 3              # corrections shown per search: the closest few, ranked like search_memory
FIELD_CHARS = 300  # each text field, shown cut


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
                          "status": "open", "closed_by": None, "evidence": None, "what_failed": None}
        elif rec.get("event") == "closed":
            evidence = rec.get("evidence")
            if (cid in items and items[cid]["status"] == "open" and rec.get("closed_by") in CLOSED_BY
                    and isinstance(evidence, str) and evidence.strip()):
                failed = rec.get("what_failed")
                items[cid].update(status="closed", closed_by=rec["closed_by"], evidence=evidence,
                                  what_failed=failed if isinstance(failed, str) and failed.strip() else None)
    for it in items.values():   # the later corrections that reopened this one
        it["reopened_by"] = [later["id"] for later in items.values() if later["reopens"] == it["id"]]
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
    """A correction that reopens an earlier one closes only with what_failed: closing the new one alone let the same
    mistake come back (#11: 6 of 23 corrections in a week reopened an earlier one, each closed in its turn)."""
    cid, by, evidence = args.get("id"), args.get("closed_by"), args.get("evidence")
    items = fold(root)
    item = items.get(cid) if isinstance(cid, str) else None
    if item is None or item["status"] != "open":
        return "ERROR: id must name an open correction"
    if by not in CLOSED_BY:
        return f"ERROR: closed_by must be one of {', '.join(CLOSED_BY)}"
    if not isinstance(evidence, str) or not evidence.strip():
        return "ERROR: evidence must name an episode, commit or fact, with a reason for not_accepted"
    failed = args.get("what_failed")
    failed = failed.strip() if isinstance(failed, str) else ""
    earlier = items.get(item["reopens"]) if item["reopens"] else None
    if earlier is not None and not failed:
        return (f"ERROR: not closed: {cid} reopens {earlier['id']}, closed by {earlier['closed_by'] or 'nothing yet'}"
                f" ({_cut(earlier['evidence'] or '')}). Give what_failed: what in that fix did not hold, and why this "
                "change will.")
    _append(root, {"event": "closed", "id": cid, "closed_by": by, "evidence": evidence.strip(),
                   **({"what_failed": failed} if failed else {}), "at": now.isoformat()})
    return f"closed correction {cid} ({by})"


def _cut(text: str, n: int = FIELD_CHARS) -> str:
    text = " ".join(str(text).split())
    return text[:n] + ("…" if len(text) > n else "")


def line(it: dict) -> str:
    """One correction as search_corrections, recall and the briefing show it: what, what he said, how it ended."""
    if it["status"] == "open":
        state = "open"
    else:
        state = f"closed by {it['closed_by']}: {_cut(it['evidence'])}"
        if it.get("what_failed"):
            state += f"; what failed before: {_cut(it['what_failed'])}"
    links = ([f"reopens {it['reopens']}"] if it["reopens"] else []) + (
        [f"reopened by {', '.join(it['reopened_by'])}"] if it.get("reopened_by") else [])
    return (f"{it['id']} ({it['when'][:16]}, {state}){' [' + '; '.join(links) + ']' if links else ''}: "
            f"what was wrong: {_cut(it['what_was_wrong'])}; operator said: {_cut(it['what_operator_said'])}")


def doc(it: dict) -> tuple[str, str]:
    """A correction as a memory document: search_memory, automatic recall and search_corrections find it, open or
    closed. Before #11 a closed correction was in no prompt and no search, so the next mistake of its kind met none."""
    return f"correction {it['id']}", "Operator correction " + line(it)


def search(root: Path, query: str, k: int = K) -> list[dict]:
    """The corrections closest to `query`, best first, ranked as search_memory ranks (words + meaning)."""
    from . import memory   # memory reads this module's ledger for its corpus
    items = fold(root)
    if not items or not str(query).strip():
        return []
    hits = memory.search(root, query, k=max(1, int(k)), where=lambda source: source.startswith("correction "))
    return [items[cid] for h in hits if (cid := h.source.removeprefix("correction ")) in items]


def found(root: Path, query: str, k: int = K) -> str:
    """search_corrections' answer. Says when there is none: the instance is to say what it found, nothing included."""
    total = len(fold(root))
    if not total:
        return NONE_RECORDED
    hits = search(root, query, k)
    if not hits:
        return f"None of my {total} corrections matches {_cut(query, 80)!r}."
    return (f"The {len(hits)} of my {total} corrections closest to {_cut(query, 80)!r}, best first "
            "(closest is not always on topic: judge each):\n" + "\n".join("- " + line(it) for it in hits))


def closest(root: Path, query: str, k: int = K) -> str:
    """Added to each operator message, as automatic recall is: the corrections closest to it, open and closed, so the
    search runs before every reply that could ask or suggest (#11). Empty when no correction is recorded."""
    try:
        if not fold(root):
            return ""
        hits = search(root, query, k)
    except Exception as e:   # a broken ledger or search must not stop a reply
        return f"\n\n---\n(My corrections could not be searched: {type(e).__name__}: {e})"
    body = "\n".join("- " + line(it) for it in hits) or "(none matches this message)"
    return ("\n\n---\n(The operator's earlier corrections closest to this, open and closed. If one covers what you "
            "are about to ask, suggest or claim, follow it; when you ask or suggest, say what you found:)\n" + body)


def block(root: Path, now: datetime) -> str:
    try:
        items = list(fold(root).values())
        recent = [it for it in items if timedelta(0) <= _at(now.isoformat()) - _at(it["when"]) <= timedelta(days=7)]
        counts = f"reopened {sum(it['reopens'] is not None for it in recent)} of {len(recent)} in 7 days"
        by_id = {it["id"]: it for it in items}
        rows = []
        for it in items:
            if it["status"] != "open":
                continue
            rows.append(f"- {it['id']} ({it['when']}, conversation {it['conversation']}): {it['what_was_wrong']}"
                        f"; operator said: {it['what_operator_said']}")
            if (earlier := by_id.get(it["reopens"])) is not None:   # side by side: what the earlier fix was (#11)
                rows.append(f"  - reopens {line(earlier)}")
        return counts + "\n" + ("\n".join(rows) or "(none open)")
    except Exception as e:   # a broken record must not prevent live or sleep
        return f"(my corrections could not be read: {type(e).__name__}: {e})"
