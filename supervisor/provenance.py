"""Check observation claims against tool results, apart from the instance's own prose."""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from . import core, metrics

SHINGLE = 8
MIN_WORDS = 3   # a fact shorter than this has no shingle worth matching: n/a, not "unmatched" (Grok's review)
FACT = re.compile(r"^- \[([^\]]+)\] (.+)$")
SOURCE = re.compile(r" \(src: (.+?)\)(?=$| \(superseded by \[)")
JOURNAL_BLOCK = re.compile(r"(?m)^## (\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)[^\n]*\n")
JOURNAL_HEAD_BYTES = re.compile(rb"(?m)^## (\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)[^\n]*\n")
EPOCH = datetime(1970, 1, 1)
INDEX_VERSION = 1


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


def _shingles(text: str, n: int = SHINGLE) -> frozenset[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return frozenset(tuple(words[i:i + n]) for i in range(len(words) - n + 1))


def _usec(at: datetime) -> int:
    delta = at - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _hash(shingle: tuple[str, ...]) -> int:
    return int.from_bytes(hashlib.blake2b("\x1f".join(shingle).encode(), digest_size=8).digest(), "big", signed=True)


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


def _add_shingles(at: datetime | None, text: str, sizes: set[int], target: dict[int, dict[int, int]]) -> None:
    if at is None or not text:
        return
    us = _usec(at)
    for size in sizes:
        bucket = target[size]
        for shingle in _shingles(text, size):
            h = _hash(shingle)
            old = bucket.get(h)
            if old is None or us < old:
                bucket[h] = us


def _index_episode(ep: dict, sizes: set[int], obs: dict[int, dict[int, int]], wr: dict[int, dict[int, int]]) -> None:
    ts = ep.get("ts", "")
    date = _date(ts)
    intent, tools, finish = ep.get("intent"), ep.get("tools"), ep.get("finish")
    started = _date(intent.get("at") if isinstance(intent, dict) else None) or date
    for call in tools if isinstance(tools, list) else []:
        if isinstance(call, dict):
            _add_shingles(started, _text(call.get("result", "")), sizes, obs)
    finish = finish if isinstance(finish, dict) else {}
    for field in ("what_happened", "learned"):
        _add_shingles(date, _text(finish.get(field, "")), sizes, wr)


def _ingest_episode(raw: bytes, prev: tuple[int, str, int, str] | None, sizes: set[int],
                    obs: dict[int, dict[int, int]], wr: dict[int, dict[int, int]]) -> tuple[int, str, int, str]:
    old_offset, _, old_indexed, _ = prev if prev is not None else (0, "", 0, "")
    start = old_indexed + 1 if old_indexed > old_offset and raw[old_indexed:old_indexed + 1] == b"\n" else old_offset
    nl = raw.rfind(b"\n")
    new_offset = nl + 1 if nl >= 0 else 0
    if new_offset > start:
        for line in core.jsonl_lines(raw[start:new_offset].decode("utf-8", errors="replace")):
            try:
                ep = json.loads(line)
            except ValueError:
                continue
            if isinstance(ep, dict):
                _index_episode(ep, sizes, obs, wr)
    tail = raw[new_offset:]
    new_indexed = new_offset
    if tail:
        try:
            ep = json.loads(tail.decode("utf-8", errors="replace").removesuffix("\r"))
        except ValueError:
            ep = None
        if isinstance(ep, dict):
            _index_episode(ep, sizes, obs, wr)
            new_indexed = len(raw)
    digest = hashlib.sha256(raw[:new_offset]).hexdigest()
    indexed_digest = digest if new_indexed == new_offset else hashlib.sha256(raw[:new_indexed]).hexdigest()
    return new_offset, digest, new_indexed, indexed_digest


def _ingest_journal(raw: bytes, prev: tuple[int, str, int, str] | None, sizes: set[int],
                    wr: dict[int, dict[int, int]]) -> tuple[int, str, int, str]:
    old_offset = prev[0] if prev is not None else 0
    tail = raw[old_offset:]
    matches = list(JOURNAL_HEAD_BYTES.finditer(tail))
    new_offset = old_offset + matches[-1].start() if matches else old_offset
    blocks = JOURNAL_BLOCK.split(tail.decode("utf-8", errors="replace"))
    for stamp, body in zip(blocks[1::2], blocks[2::2]):
        _add_shingles(_date(stamp), body, sizes, wr)
    new_indexed = len(raw)
    digest = hashlib.sha256(raw[:new_offset]).hexdigest()
    indexed_digest = digest if new_indexed == new_offset else hashlib.sha256(raw[:new_indexed]).hexdigest()
    return new_offset, digest, new_indexed, indexed_digest


class _Index:
    """Kept SQLite index of earliest observation and prose timestamps by blake2b-64 shingle hash."""
    def __init__(self, name: str):
        self.name = name
        self.root = core.INSTANCES / name
        self.path = core.SUP_DIR / "provenance" / f"{name}.sqlite"
        self.synced = False
        self.ready: set[int] = set()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _remove_db(self) -> None:
        self.synced = False
        self.ready.clear()
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    def _raw_sources(self) -> dict[str, bytes]:
        files = [f"memory/episodes/{stamp[0]}" for stamp in metrics._episode_key(self.root)]
        files.append("memory/journal.md")
        return {rel: core.safe_read(self.root, rel, "", errors="replace").encode("utf-8") for rel in files}

    def _write_batch(self, conn: sqlite3.Connection, raw_files: dict[str, bytes],
                     recorded: dict[str, tuple[int, str, int, str]], sizes: set[int], full: bool) -> None:
        for size in sizes:
            for kind in ("obs", "wr"):
                conn.execute(f"CREATE TABLE IF NOT EXISTS {kind}_{size} "
                             "(h INTEGER PRIMARY KEY, at INTEGER NOT NULL) WITHOUT ROWID")
        obs: dict[int, dict[int, int]] = {size: {} for size in sizes}
        wr: dict[int, dict[int, int]] = {size: {} for size in sizes}
        rows = []
        for file, raw in raw_files.items():
            prev = None if full else recorded.get(file)
            if prev is not None and len(raw) == prev[2]:
                continue
            if file == "memory/journal.md":
                state = _ingest_journal(raw, prev, sizes, wr)
            else:
                state = _ingest_episode(raw, prev, sizes, obs, wr)
            rows.append((file, *state))
        for size in sizes:
            for kind, table in (("obs", obs[size]), ("wr", wr[size])):
                if table:
                    conn.executemany(
                        f"INSERT INTO {kind}_{size}(h, at) VALUES (?, ?) "
                        "ON CONFLICT(h) DO UPDATE SET at=MIN(at, excluded.at)",
                        table.items(),
                    )
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO sources(file, offset, sha256, indexed, indexed_sha256) VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def _ensure(self, conn: sqlite3.Connection, size: int) -> None:
        if self.synced and size in self.ready:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall()) if "meta" in tables else {}
            existing = {
                int(t[4:]) for t in tables
                if t.startswith("obs_") and t[4:].isdigit() and f"wr_{t[4:]}" in tables
            }
            need_reset = ("sources" not in tables or meta.get("version") != str(INDEX_VERSION)
                          or meta.get("root") != str(self.root))
            raw_files = None
            recorded = {}
            if not need_reset and not self.synced:
                raw_files = self._raw_sources()
                recorded = {
                    r[0]: (int(r[1]), str(r[2]), int(r[3]), str(r[4]))
                    for r in conn.execute("SELECT file, offset, sha256, indexed, indexed_sha256 FROM sources")
                }
                for file, (offset, digest, indexed, indexed_digest) in recorded.items():
                    raw = raw_files.get(file)
                    # An episode line indexed without its "\n" must end there: one that grew on the same line would
                    # keep the old line's shingles, which a build from scratch would not have.
                    grown_tail = (raw is not None and file != "memory/journal.md" and indexed > offset
                                  and len(raw) > indexed and raw[indexed:indexed + 1] != b"\n")
                    if (raw is None or offset < 0 or indexed < offset or len(raw) < indexed or grown_tail
                            or hashlib.sha256(raw[:offset]).hexdigest() != digest
                            or hashlib.sha256(raw[:indexed]).hexdigest() != indexed_digest):
                        need_reset = True
                        break
            if need_reset:
                for t in list(tables):
                    conn.execute(f'DROP TABLE IF EXISTS "{t}"')
                conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID")
                conn.execute("INSERT INTO meta(key, value) VALUES ('version', ?), ('root', ?)",
                             (str(INDEX_VERSION), str(self.root)))
                conn.execute("CREATE TABLE sources (file TEXT PRIMARY KEY, offset INTEGER NOT NULL, "
                             "sha256 TEXT NOT NULL, indexed INTEGER NOT NULL, indexed_sha256 TEXT NOT NULL) "
                             "WITHOUT ROWID")
                existing = set()
                recorded = {}
                self.ready.clear()
            if raw_files is None:
                raw_files = self._raw_sources()
            if not existing:
                self._write_batch(conn, raw_files, {}, {size}, full=True)
                existing.add(size)
            else:
                if not self.synced:
                    self._write_batch(conn, raw_files, recorded, existing, full=False)
                if size not in existing:
                    self._write_batch(conn, raw_files, {}, {size}, full=True)
                    existing.add(size)
            conn.execute("COMMIT")
            self.synced = True
            self.ready.update(existing)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def earliest(self, kind: str, size: int, shingles: frozenset[tuple[str, ...]]) -> datetime | None:
        hashes = [_hash(s) for s in shingles]
        for attempt in range(2):
            try:
                with contextlib.closing(self._connect()) as conn:
                    self._ensure(conn, size)
                    best = None
                    for i in range(0, len(hashes), 900):
                        chunk = hashes[i:i + 900]
                        marks = ",".join("?" * len(chunk))
                        row = conn.execute(f"SELECT MIN(at) FROM {kind}_{size} WHERE h IN ({marks})", chunk).fetchone()
                        if row and row[0] is not None and (best is None or row[0] < best):
                            best = row[0]
                    return EPOCH + timedelta(microseconds=best) if best is not None else None
            except sqlite3.DatabaseError:
                if attempt:
                    raise
                self._remove_db()
        return None


class _Evidence:
    """Tool results by citation, earliest observation and prose dates by shingle."""
    def __init__(self, episodes, journal: str = "", earliest=None):
        self.logs, self.urls = {}, {}
        self.results, self.prose = [], []
        self.observed, self.written = {}, {}
        self._custom_earliest = earliest
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
                if earliest is None:
                    self.results.append((started, result))
                if isinstance(ts, str):
                    for stamp in {ts.replace("T", " "), ts[:16].replace("T", " ")}:
                        self.logs.setdefault(stamp, []).append((i, result))
                if call.get("tool") in ("fetch_page", "http_request", "run"):
                    for url in _urls(call.get("args", "")):
                        self.urls.setdefault(url, []).append((started, result))
            if earliest is None:
                finish = finish if isinstance(finish, dict) else {}
                for field in ("what_happened", "learned"):
                    self.prose.append((date, _Result(_text(finish.get(field, "")))))
        if earliest is None and journal:
            blocks = JOURNAL_BLOCK.split(journal)
            for stamp, body in zip(blocks[1::2], blocks[2::2]):
                self.prose.append((_date(stamp), _Result(body)))

    def _earliest(self, kind: str, size: int, shingles: frozenset[tuple[str, ...]]) -> datetime | None:
        if self._custom_earliest is not None:
            return self._custom_earliest(kind, size, shingles)
        index, results = (self.observed, self.results) if kind == "obs" else (self.written, self.prose)
        if size not in index:
            dates = {}
            for at, result in results:
                if at is not None:
                    for shingle in result.shingles(size):
                        old = dates.get(shingle)
                        if old is None or at < old:
                            dates[shingle] = at
            index[size] = dates
        dates = index[size]
        return min((dates[s] for s in shingles if s in dates), default=None)

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
        if (seen := self._earliest("obs", size, shingles)) is not None and seen <= before:
            return "unmatched"
        if (seen := self._earliest("wr", size, shingles)) is not None and seen < at:
            return "self-generated"
        return "unmatched"


def _evidence(name: str, episodes: list[dict] | None = None) -> _Evidence:
    return _Evidence(_episodes(name) if episodes is None else episodes, earliest=_Index(name).earliest)


def check(name: str, fact: dict, episodes: list[dict] | None = None, journal: str | None = None) -> str:
    """`episodes` and `journal`: read once by a caller checking many facts (they are read here otherwise)."""
    if not (fact.get("src") or "").startswith(("log:", "web:")) or _size(fact.get("text", "")) < MIN_WORDS:
        return "n/a"
    if episodes is None and journal is None:
        return _evidence(name).check(fact)
    episodes = _episodes(name) if episodes is None else episodes
    if journal is None:
        journal = core.safe_read(core.INSTANCES / name, "memory/journal.md", "", errors="replace")
    return _Evidence(episodes, journal).check(fact)


def checked(name: str) -> list[tuple[dict, str]]:
    """Every fact with its check; index the episode results once for the whole rebuild (review C1)."""
    rows = facts(name)
    evidence = _evidence(name)
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
    for judgment in metrics.judgments_for_stats(name):
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
