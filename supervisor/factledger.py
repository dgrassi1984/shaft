"""Show current facts, their checked sources, and later restatements in live cycles."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime

from . import core, provenance

MATCH_MIN_SHARE = 0.3  # One shared window can be chance; require a share of the fact's shingles.

SOURCES = {
    "self-generated": "first appears in my own prose, not in a tool result",
    "unmatched": "cites a log or page that does not show it",
    "n/a": "no log or web source to check",
    "matched": "matches the log or page it cites",
}


def _prose(episode: dict) -> list[str]:
    """Keep prose fields separate so a shingle cannot cross their boundaries."""
    finish = episode.get("finish")
    texts = [finish.get(key) for key in ("what_happened", "learned")] if isinstance(finish, dict) else []
    tools = episode.get("tools")
    for call in tools if isinstance(tools, list) else []:
        if not isinstance(call, dict) or call.get("tool") != "send_message" or call.get("ok") is False:
            continue
        result = call.get("result")
        if not isinstance(result, str) or not result or result.startswith("ERROR"):
            continue
        args = call.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                continue
        if isinstance(args, dict):
            texts.extend(args.get(key) for key in ("text", "content"))
    return [text for text in texts if isinstance(text, str) and text]


def rows(name: str, as_of: datetime | None = None) -> list[dict]:
    """Current facts with source checks and distinct later live cycles that restated them."""
    facts = [fact for fact in provenance.facts(name) if fact["file"] == "memory/facts.md"]
    episodes = provenance._episodes(name)
    journal = core.safe_read(core.INSTANCES / name, "memory/journal.md", "", errors="replace")
    evidence = provenance._Evidence(episodes, journal)
    pairs = [(fact, evidence.check(fact)) for fact in facts]
    sizes = {provenance._size(fact["text"]) for fact, _ in pairs} - set(range(provenance.MIN_WORDS))
    cutoff = provenance._date(as_of.isoformat()) if as_of is not None else None
    # Index once per required size: short facts use their whole text, as in provenance.
    index = {size: defaultdict(set) for size in sizes}
    stamps = {}
    for episode in episodes:
        at = provenance._date(episode.get("ts"))
        if episode.get("kind") != "live" or at is None or (cutoff is not None and at >= cutoff):
            continue
        stamps[at] = episode["ts"]
        texts = _prose(episode)
        for size in sizes:
            shingles = set().union(*(provenance._shingles(text, size) for text in texts))
            for shingle in shingles:
                index[size][shingle].add(at)
    out = []
    for fact, status in pairs:
        size, at = provenance._size(fact["text"]), provenance._date(fact["date"])
        uses, last_use = None, None
        if size >= provenance.MIN_WORDS and at is not None:
            candidates = set().union(*(index[size].get(shingle, set())
                                       for shingle in provenance._shingles(fact["text"], size)))
            later = {stamp for stamp in candidates
                     if (stamp.date() > at.date() if len(fact["date"]) == 10 else stamp > at)}
            uses = len(later)
            last_use = stamps[max(later)] if later else None
        out.append({"date": fact["date"], "text": fact["text"], "src": fact["src"], "status": status,
                    "uses": uses, "last_use": last_use})
    return out


def record_text(name: str, limit: int = 40, offset: int = 0, status: str = "", as_of: datetime | None = None) -> str:
    """Page current facts with source totals and restatement counts for record(kind='facts')."""
    if status and status not in SOURCES:
        raise ValueError("status must be one of: " + ", ".join(SOURCES))
    facts = rows(name, as_of)
    counts = Counter(fact["status"] for fact in facts)
    order = {key: i for i, key in enumerate(SOURCES)}
    facts = sorted((fact for fact in facts if not status or fact["status"] == status),
                   key=lambda fact: (order[fact["status"]], -(fact["uses"] or 0)))
    offset, limit = max(0, offset), max(0, min(limit, 200))
    page = facts[offset:offset + limit]
    start, end = (offset + 1, offset + len(page)) if page else (0, 0)
    lines = ["Sources: " + "; ".join(f"{key}: {counts[key]}" for key in SOURCES),
             f"{len(facts)} facts; showing {start}–{end}"]
    for fact in page:
        if fact["uses"] is None:
            use = ("restatements not counted (invalid fact date)" if provenance._date(fact["date"]) is None
                   else "restatements not counted (fact too short)")
        elif fact["uses"]:
            unit = "cycle" if fact["uses"] == 1 else "cycles"
            use = f"restated in {fact['uses']} later {unit}, last {fact['last_use'][:16]}"
        else:
            use = "never restated since"
        text = " ".join(fact["text"].split())[:160]
        lines.append(f"[{fact['date']}] {text} — source: {SOURCES[fact['status']]}; {use}")
    lines.append("A fact often restated with no checked source is the one worth checking or retiring. "
                 "Information only.")
    return "\n".join(lines)


def match_claim(name: str, quote: str, as_of: datetime | None = None, rows=None) -> dict | None:
    """Find an unchecked fact with enough shared shingles; supplied rows reuse an existing rebuild."""
    quotes = {}
    best, best_key = None, (0, datetime.min)
    for fact in globals()["rows"](name, as_of) if rows is None else rows:
        size = provenance._size(fact["text"])
        if fact["status"] == "matched" or size < provenance.MIN_WORDS:
            continue
        if size not in quotes:
            quotes[size] = provenance._shingles(quote, size)
        shingles = provenance._shingles(fact["text"], size)
        overlap = len(shingles & quotes[size])
        key = overlap, provenance._date(fact["date"]) or datetime.min
        if shingles and overlap / len(shingles) >= MATCH_MIN_SHARE and key > best_key:
            best, best_key = fact, key
    return best
