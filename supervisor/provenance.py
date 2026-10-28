"""Check observation claims against tool results, apart from the instance's own prose."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from functools import lru_cache

from . import core, metrics

SHINGLE = 8
MIN_WORDS = 3   # a fact shorter than this has no shingle worth matching: n/a, not "unmatched" (Grok's review)
FACT = re.compile(r"^- \[([^\]]+)\] (.+)$")
SOURCE = re.compile(r" \(src: (.+?)\)(?=$| \(superseded by \[)")


def facts(name: str) -> list[dict]:
    out = []
    for file in ("memory/facts.md", "memory/facts-archive.md"):
        for line in core.safe_read(core.INSTANCES / name, file, "", errors="replace").splitlines():
            if m := FACT.match(line):
                tag = SOURCE.search(m[2])
                out.append({"date": m[1], "text": m[2][:tag.start()] if tag else m[2],
                            "src": tag[1] if tag else None, "file": file})
    return out


def _episodes(name: str) -> list[dict]:
    root = core.INSTANCES / name
    out = []
    for file in core.list_beneath(root, "memory/episodes"):
        if not file.endswith(".jsonl"):
            continue
        for line in core.safe_read(root, f"memory/episodes/{file}", "", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _date(stamp: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(stamp)
        return at.astimezone().replace(tzinfo=None) if at.tzinfo else at
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=20000)   # every fact is checked against every tool result: each result is split once
def _shingles(text: str, n: int = SHINGLE) -> frozenset[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return frozenset(tuple(words[i:i + n]) for i in range(len(words) - n + 1))


def _size(text: str) -> int:
    """The shingle length for a fact: SHINGLE words, or the whole fact when it is shorter — a short fact copied
    verbatim from a tool result must still match it."""
    return min(SHINGLE, len(re.findall(r"\w+", text.lower())))


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _urls(value) -> set[str]:
    return {re.sub(r"^https?://", "", url).rstrip("/")
            for url in re.findall(r"https?://[^\s\"'<>]+", _text(value))}


def check(name: str, fact: dict, episodes: list[dict] | None = None, journal: str | None = None) -> str:
    """`episodes` and `journal`: read once by a caller checking many facts (they are read here otherwise)."""
    source = fact.get("src") or ""
    if not source.startswith(("log:", "web:")):
        return "n/a"
    at = _date(fact.get("date"))
    size = _size(fact.get("text", ""))
    if size < MIN_WORDS:
        return "n/a"
    shingles = _shingles(fact.get("text", ""), size)
    if at is None or not shingles:
        return "unmatched"
    episodes = _episodes(name) if episodes is None else episodes
    earlier = []
    stamp, sep, step = source[4:].partition("#") if source.startswith("log:") else ("", "", "")
    observed = False
    for ep in episodes:
        ts = ep.get("ts", "")
        date = _date(ts)
        if date is not None and date < at:
            earlier.append(ep)   # its report was written before the fact: prose the fact may have copied
        # A cycle that had begun by the fact's minute may have fetched it — the cycle that remembered it included: its
        # episode is written at its end, after the fact (Grok's review: it could never match its own fetch).
        started = _date((ep.get("intent") or {}).get("at")) or date
        before = started is not None and started <= at + timedelta(seconds=59)
        cited = (source.startswith("log:") and _date(stamp) is not None and isinstance(ts, str)
                 and (ts.replace("T", " ") == stamp.replace("T", " ") or
                      len(stamp) == 16 and ts[:16].replace("T", " ") == stamp.replace("T", " ")))
        for i, call in enumerate(ep.get("tools") or [], 1):
            if not isinstance(call, dict):
                continue
            matches = bool(shingles & _shingles(_text(call.get("result", "")), size))
            observed |= matches and before
            if source.startswith("log:"):
                eligible = cited and (not sep or step == str(i))
            else:
                eligible = (before and call.get("tool") in ("fetch_page", "http_request", "run")
                            and re.sub(r"^https?://", "", source[4:]).rstrip("/") in _urls(call.get("args", "")))
            if eligible and matches:
                return "matched"
    if observed:
        return "unmatched"   # evidence elsewhere does not validate the cited source
    for ep in earlier:
        finish = ep.get("finish") or {}
        for field in ("what_happened", "learned"):
            if shingles & _shingles(_text(finish.get(field, "")), size):
                return "self-generated"
    if journal is None:
        journal = core.safe_read(core.INSTANCES / name, "memory/journal.md", "", errors="replace")
    blocks = re.split(r"(?m)^## (\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)[^\n]*\n", journal)
    for stamp, body in zip(blocks[1::2], blocks[2::2]):
        date = _date(stamp)
        if date is not None and date < at and shingles & _shingles(body, size):
            return "self-generated"
    return "unmatched"


def checked(name: str) -> list[tuple[dict, str]]:
    """Every fact with its check, the episodes and journal read once (stats.md is written after every cycle)."""
    episodes = _episodes(name)
    journal = core.safe_read(core.INSTANCES / name, "memory/journal.md", "", errors="replace")
    return [(row, check(name, row, episodes, journal)) for row in facts(name)]


def _surfaced(ep: dict, suspect: set[str] = frozenset()) -> bool:
    """Whether the cycle's tool results showed it a fact of the kind the kill test is about: inferred, untagged, or
    one whose observation claim the record does not bear out (`suspect`: self-generated facts' texts)."""
    for call in ep.get("tools") or []:
        if not isinstance(call, dict):
            continue
        if call.get("tool") == "read_file":
            try:
                args = call.get("args") or {}
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                continue
            if not isinstance(args, dict) or args.get("path") != "memory/facts.md":
                continue
        elif call.get("tool") != "search_memory":
            continue
        for line in _text(call.get("result", "")).splitlines():
            # Search adds a source label; a file read returns the stored bullet unchanged.
            line = re.sub(r"^\[[^\n]*?\] ", "", line)
            if FACT.match(line) or re.match(r"^\[\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?\] ", line):
                tag = SOURCE.search(line)
                if tag is None or tag[1] == "inferred" or any(text and text in line for text in suspect):
                    return True
    return False


def reliance(name: str, suspect: set[str] | None = None) -> dict:
    if suspect is None:
        suspect = {row["text"] for row, status in checked(name) if status == "self-generated"}
    episodes = {ep.get("ts"): ep for ep in metrics.live_episodes(name)}
    groups = {key: {"n": 0, "unsupported": 0, "share": None} for key in ("exposed", "other")}
    for judgment in metrics.judgments(name):
        if not metrics.by_current_judge(judgment) or not isinstance(judgment.get("claim_supported"), bool):
            continue
        ep = episodes.get(judgment.get("episode_ts"))
        if ep is None:
            continue   # a missing tool log cannot establish either exposure group
        group = groups["exposed" if _surfaced(ep, suspect) else "other"]
        group["n"] += 1
        group["unsupported"] += judgment["claim_supported"] is False
    for group in groups.values():
        if group["n"]:
            group["share"] = group["unsupported"] / group["n"]
    return groups


def section(name: str) -> list[str]:
    rows = facts(name)
    if not any(row["src"] for row in rows):
        return []
    counts = Counter((row["src"] or "unknown").split(":", 1)[0] for row in rows)
    checks = Counter(status for _, status in checked(name))
    return ["## Where my facts come from", "",
            "Sources: " + ", ".join(f"{key} {n}" for key, n in sorted(counts.items())) + ".",
            f"Observation claims: {checks['matched']} matched the record; "
            f"{checks['self-generated']} self-generated; {checks['unmatched']} unmatched."]


def report(names: list[str]) -> str:
    lines = []
    for name in names:
        rows = facts(name)
        pairs = checked(name)
        counts = Counter(status for _, status in pairs)
        lines += [f"## {name}", "", "| Source | Facts |", "| --- | ---: |"]
        sources = Counter((row["src"] or "unknown").split(":", 1)[0] for row in rows)
        lines += [f"| {key} | {n} |" for key, n in sorted(sources.items())]
        lines += ["", f"Matched {counts['matched']}; self-generated {counts['self-generated']}; "
                  f"unmatched {counts['unmatched']}; n/a {counts['n/a']}."]
        suspect = {row["text"] for row, status in pairs if status == "self-generated"}
        for key, group in reliance(name, suspect).items():
            share = f"{group['share']:.1%}" if group["share"] is not None else "n/a"
            lines.append(f"Unsupported claims, {key}: {group['unsupported']}/{group['n']} ({share}).")
        for row, status in pairs:
            if status in ("self-generated", "unmatched"):
                lines.append(f"- {status}: [{row['date']}] {row['text']} (src: {row['src']}; {row['file']})")
        lines.append("")
    return "\n".join(lines)
