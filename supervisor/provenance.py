"""Check observation claims against tool results, apart from the instance's own prose."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime

from . import core, metrics

SHINGLE = 8
FACT = re.compile(r"^- \[([^\]]+)\] (.+)$")
SOURCE = re.compile(r" \(src: (.+)\)$")


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


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _urls(value) -> set[str]:
    return {re.sub(r"^https?://", "", url).rstrip("/")
            for url in re.findall(r"https?://[^\s\"'<>]+", _text(value))}


def check(name: str, fact: dict) -> str:
    source = fact.get("src") or ""
    if not source.startswith(("log:", "web:")):
        return "n/a"
    at = _date(fact.get("date"))
    shingles = _shingles(fact.get("text", ""))
    if at is None or not shingles:
        return "unmatched"
    episodes = _episodes(name)
    earlier = []
    stamp, sep, step = source[4:].partition("#") if source.startswith("log:") else ("", "", "")
    observed = False
    for ep in episodes:
        ts = ep.get("ts", "")
        date = _date(ts)
        before = date is not None and date < at
        if before:
            earlier.append(ep)
        cited = (source.startswith("log:") and _date(stamp) is not None and isinstance(ts, str)
                 and (ts.replace("T", " ") == stamp.replace("T", " ") or
                      len(stamp) == 16 and ts[:16].replace("T", " ") == stamp.replace("T", " ")))
        for i, call in enumerate(ep.get("tools") or [], 1):
            if not isinstance(call, dict):
                continue
            matches = bool(shingles & _shingles(_text(call.get("result", ""))))
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
            if shingles & _shingles(_text(finish.get(field, ""))):
                return "self-generated"
    journal = core.safe_read(core.INSTANCES / name, "memory/journal.md", "", errors="replace")
    blocks = re.split(r"(?m)^## (\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)[^\n]*\n", journal)
    for stamp, body in zip(blocks[1::2], blocks[2::2]):
        date = _date(stamp)
        if date is not None and date < at and shingles & _shingles(body):
            return "self-generated"
    return "unmatched"


def _surfaced(ep: dict) -> bool:
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
                if tag is None or tag[1] == "inferred":
                    return True
    return False


def reliance(name: str) -> dict:
    episodes = {ep.get("ts"): ep for ep in metrics.live_episodes(name)}
    groups = {key: {"n": 0, "unsupported": 0, "share": None} for key in ("exposed", "other")}
    for judgment in metrics.judgments(name):
        if not metrics.by_current_judge(judgment) or not isinstance(judgment.get("claim_supported"), bool):
            continue
        ep = episodes.get(judgment.get("episode_ts"))
        if ep is None:
            continue   # a missing tool log cannot establish either exposure group
        group = groups["exposed" if _surfaced(ep) else "other"]
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
    checks = Counter(check(name, row) for row in rows)
    return ["## Where my facts come from", "",
            "Sources: " + ", ".join(f"{key} {n}" for key, n in sorted(counts.items())) + ".",
            f"Observation claims: {checks['matched']} matched the record; "
            f"{checks['self-generated']} self-generated; {checks['unmatched']} unmatched."]


def report(names: list[str]) -> str:
    lines = []
    for name in names:
        rows = facts(name)
        checked = [(row, check(name, row)) for row in rows]
        counts = Counter(status for _, status in checked)
        lines += [f"## {name}", "", "| Source | Facts |", "| --- | ---: |"]
        sources = Counter((row["src"] or "unknown").split(":", 1)[0] for row in rows)
        lines += [f"| {key} | {n} |" for key, n in sorted(sources.items())]
        lines += ["", f"Matched {counts['matched']}; self-generated {counts['self-generated']}; "
                  f"unmatched {counts['unmatched']}; n/a {counts['n/a']}."]
        for key, group in reliance(name).items():
            share = f"{group['share']:.1%}" if group["share"] is not None else "n/a"
            lines.append(f"Unsupported claims, {key}: {group['unsupported']}/{group['n']} ({share}).")
        for row, status in checked:
            if status in ("self-generated", "unmatched"):
                lines.append(f"- {status}: [{row['date']}] {row['text']} (src: {row['src']}; {row['file']})")
        lines.append("")
    return "\n".join(lines)
