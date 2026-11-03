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
    return metrics._parsed_episodes(core.INSTANCES / name)


def _date(stamp: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(stamp)
        return at.astimezone().replace(tzinfo=None) if at.tzinfo else at
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=20000)   # reuse unchanged results across rebuilds
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


class _Result:
    def __init__(self, text):
        self.text, self.sizes = text, {}

    def shingles(self, size):
        if size not in self.sizes:
            self.sizes[size] = _shingles(self.text, size)
        return self.sizes[size]


class _Evidence:
    """Tool results by citation, earliest observation and prose dates by shingle."""
    def __init__(self, episodes, journal):
        self.logs, self.urls = {}, {}
        self.results, self.prose = [], []
        self.observed, self.written = {}, {}
        for ep in episodes:
            ts = ep.get("ts", "")
            date = _date(ts)
            # A malformed field in one episode must not stop the check of a fact another episode bears out.
            intent, tools, finish = ep.get("intent"), ep.get("tools"), ep.get("finish")
            started = _date(intent.get("at") if isinstance(intent, dict) else None) or date
            for i, call in enumerate(tools if isinstance(tools, list) else [], 1):
                if not isinstance(call, dict):
                    continue
                result = _Result(_text(call.get("result", "")))
                self.results.append((started, result))
                if isinstance(ts, str):
                    for stamp in {ts.replace("T", " "), ts[:16].replace("T", " ")}:
                        self.logs.setdefault(stamp, []).append((i, result))
                if call.get("tool") in ("fetch_page", "http_request", "run"):
                    for url in _urls(call.get("args", "")):
                        self.urls.setdefault(url, []).append((started, result))
            finish = finish if isinstance(finish, dict) else {}
            for field in ("what_happened", "learned"):
                self.prose.append((date, _Result(_text(finish.get(field, "")))))
        blocks = re.split(r"(?m)^## (\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)[^\n]*\n", journal)
        for stamp, body in zip(blocks[1::2], blocks[2::2]):
            self.prose.append((_date(stamp), _Result(body)))

    def _earliest(self, index, results, size):
        if size not in index:
            dates = {}
            for at, result in results:
                if at is not None:
                    for shingle in result.shingles(size):
                        old = dates.get(shingle)
                        if old is None or at < old:
                            dates[shingle] = at
            index[size] = dates
        return index[size]

    def check(self, fact):
        source = fact.get("src") or ""
        if not source.startswith(("log:", "web:")):
            return "n/a"
        at, size = _date(fact.get("date")), _size(fact.get("text", ""))
        if size < MIN_WORDS:
            return "n/a"
        shingles = _shingles(fact.get("text", ""), size)
        if at is None or not shingles:
            return "unmatched"
        before = at + timedelta(seconds=59)
        if source.startswith("log:"):
            stamp, sep, step = source[4:].partition("#")
            if _date(stamp) is not None:
                for i, result in self.logs.get(stamp.replace("T", " "), []):
                    if (not sep or step == str(i)) and shingles & result.shingles(size):
                        return "matched"
        else:
            url = re.sub(r"^https?://", "", source[4:]).rstrip("/")
            for started, result in self.urls.get(url, []):
                if started is not None and started <= before and shingles & result.shingles(size):
                    return "matched"
        observed = self._earliest(self.observed, self.results, size)
        if any((seen := observed.get(s)) is not None and seen <= before for s in shingles):
            return "unmatched"
        written = self._earliest(self.written, self.prose, size)
        if any((seen := written.get(s)) is not None and seen < at for s in shingles):
            return "self-generated"
        return "unmatched"


def check(name: str, fact: dict, episodes: list[dict] | None = None, journal: str | None = None) -> str:
    """`episodes` and `journal`: read once by a caller checking many facts (they are read here otherwise)."""
    if not (fact.get("src") or "").startswith(("log:", "web:")) or _size(fact.get("text", "")) < MIN_WORDS:
        return "n/a"
    episodes = _episodes(name) if episodes is None else episodes
    if journal is None:
        journal = core.safe_read(core.INSTANCES / name, "memory/journal.md", "", errors="replace")
    return _Evidence(episodes, journal).check(fact)


def checked(name: str) -> list[tuple[dict, str]]:
    """Every fact with its check; index the episode results once for the whole rebuild (review C1)."""
    rows = facts(name)
    journal = core.safe_read(core.INSTANCES / name, "memory/journal.md", "", errors="replace")
    evidence = _Evidence(_episodes(name), journal)
    return [(row, evidence.check(row)) for row in rows]


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
