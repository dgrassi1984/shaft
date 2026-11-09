"""Search over everything I remember — journal, facts, notes, skills, conversations.

Two rankings fused (reciprocal rank fusion): BM25 over words, and the meaning of each chunk as a vector from
SHAFT's local embedding service (SHAFT_EMBED_URL), so a memory written in other words — or in the other
language — is still found. Vectors are cached per chunk in .tmp/memory-index.bin and computed only for new
chunks. Without the service, search is BM25 only. Every search_memory result says which it was (mode). Each hit carries its source
(journal entry heading, file, or conversation timestamp) so I can cite it.
"""
from __future__ import annotations

import base64
import hashlib
import heapq
import fcntl
import json
import math
import os
import operator
import re
import sys
import struct
import tempfile
import urllib.request
from array import array
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import clock, surprises

VERDICTS = "memory/verdicts.jsonl"   # the judge's verdicts, as my sleeps received them

WORD = re.compile(r"[a-zA-ZÀ-ÿ0-9]{2,}")
STOP = set("the and for that this with you are was have not but from they his her she him its our "
           "what when who will would there their about which into can all one had has were been "
           "di che il la le lo gli una per con non sono del della".split())
CHUNK = 900
INDEX = Path(".tmp") / "memory-index.json"   # derived from my memory; rebuilt if missing
BINARY_INDEX = INDEX.with_suffix(".bin")
CORPUS_CACHE = Path(".tmp/memory-corpus")
_VECTOR_RECORD = struct.Struct("<20sI")  # SHA1 bytes, number of native float32 values
RRF_K = 60                                    # reciprocal rank fusion constant (the usual 60)
semantic_error = ""                           # why the last search was words-only, if it was
mode = ""                                     # how the last search ranked: "words + meaning", "words only: <why>"


@dataclass
class Hit:
    source: str
    text: str
    score: float


def _tokens(text: str) -> list[str]:
    return [w for w in (m.lower() for m in WORD.findall(text)) if w not in STOP]


def _chunks(text: str) -> list[str]:
    paras, out, cur = re.split(r"\n\s*\n", text), [], ""
    for p in paras:
        if cur and len(cur) + len(p) > CHUNK:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return [c.strip() for c in out if c.strip()]


def verdict_rows(root: Path) -> list[dict]:
    """The judge's verdicts my sleeps received from the supervisor's record (memory/verdicts.jsonl, appended by
    sleep.py): joined to my cycles by their time, so a recalled cycle carries the judge's word, not mine (A3)."""
    path = root / VERDICTS
    rows = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("episode_ts"), str):
            rows.append(row)
    return rows


@dataclass
class _File:
    revision: tuple
    docs: list[tuple[str, str]]
    rows: list[dict] | None
    words: tuple
    fold_rows: list[dict]


# One instance per process; changing roots drops the previous instance's derived state.
_corpus_root: Path | None = None
_files: dict[Path, _File] = {}
_surprise_revision: tuple = ()
_surprise_docs: list[tuple[str, str]] = []
_surprise_expiry: datetime | None = None
_vector_root: Path | None = None
_vector_revision: tuple | None = None
_snapshot: tuple = ()
_snapshot_docs: list[tuple[str, str]] = []
_snapshot_expiry: datetime | None = None
_lexical_docs = None
_lexical = None
_decoded: dict[str, array] = {}
_vector_end = 0
_vector_records = 0
_vector_version = 0
_vector_texts: list[str] = []
_vector_keys: list[str] = []
_vector_live: set[str] = set()
_vector_ordered: list[array] = []


def _revision(path: Path) -> tuple:
    stat = path.stat()
    return path, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino


def _atomic_write(path: Path, write) -> tuple:
    """Readers see one complete generation, including with concurrent writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as f:
        temporary = Path(f.name)
        try:
            write(f)
            f.flush()
            temporary.replace(path)
            stat = os.fstat(f.fileno())
            return path, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino
        finally:
            temporary.unlink(missing_ok=True)


def _word_index(docs):
    lengths, postings = [], {}
    for i, (_, text) in enumerate(docs):
        tokens = _tokens(text)
        lengths.append(len(tokens))
        for word, count in Counter(tokens).items():
            postings.setdefault(word, []).append((i, count))
    return lengths, postings


def _file_docs(root: Path, path: Path, verdicts: dict, rows: list[dict]) -> list[tuple[str, str]]:
    docs = []
    rel = path.relative_to(root).as_posix()
    name = path.name
    if rel == "memory/journal.md":
        for block in path.read_text().split("\n---\n"):
            block = block.strip()
            if block.startswith("## "):
                head, _, body = block.partition("\n")
                for chunk in _chunks(body) or [""]:
                    docs.append((f"journal {head[3:].strip()}", f"{head}\n{chunk}"))
    elif rel in ("memory/facts.md", "memory/facts-archive.md"):
        for line in path.read_text().splitlines():
            if not line.startswith("- "):
                continue
            tag = re.search(r" \(src: (.+?)\)(?=$| \(superseded by \[)", line)
            label = "fact" if name == "facts.md" else "memory/facts-archive.md"
            label += f", src {tag[1] if tag else 'unknown'}"
            date = re.match(r"- \[(\d{4}-\d{2}-\d{2})(?: \d{2}:\d{2})?\]", line)
            if date:
                try:
                    age = (clock.now().date() - datetime.fromisoformat(date[1]).date()).days
                    label += f", {age} days old"
                except ValueError:
                    pass
            docs.append((label, line[2:]))
    elif rel == "memory/digests.md":
        for chunk in _chunks(path.read_text(errors="replace")):
            docs.append(("memory/digests.md", chunk))
    elif rel.startswith("memory/episodes/"):
        for rec in rows:
            if rec.get("kind") == "talk" and rec.get("content"):
                who = (rec.get("speaker") or "the operator") if rec.get("role") == "user" else "me"
                docs.append((f"conversation {rec.get('ts', '')} ({who})", rec["content"][:CHUNK * 2]))
            elif rec.get("kind") == "live":
                # Never re-index finish.what_happened or finish.learned: my prose can repeat confabulation.
                intent = rec.get("intent") or {}
                verdict = verdicts.get(rec.get("ts"))
                judged = "Not judged yet"
                if verdict:
                    if verdict.get("left_out"):
                        judged = "Not judged: left out by the judge's sampling"
                    else:
                        judged = (f"Judge: criterion met {verdict.get('criterion_met')}; "
                                  f"claim supported {verdict.get('claim_supported')}; "
                                  f"failure: {verdict.get('failure_mode')}")
                        if verdict.get("second_criterion_met") is not None:
                            judged += f"; second judge: {verdict['second_criterion_met']}"
                lines = [f"Goal: {str(intent.get('goal', ''))[:400]}",
                         f"Success criterion: {str(intent.get('success_criterion', ''))[:400]}",
                         judged,
                         f"Ended by: {rec.get('ended_by', '')}; steps: {rec.get('steps', '')}"]
                for entry in rec.get("tools", []):
                    result = str(entry.get("result", ""))
                    # agent.NOT_RUN, kept literal to avoid importing the agent through its toolbox.
                    if result.startswith("ERROR") or result == "(repeat: not run)":
                        lines.append(f"Flagged/refused call {entry.get('tool', '?')}: {result[:160]}")
                docs.append((f"cycle {rec.get('ts', '')}", "\n".join(lines)[:CHUNK * 2]))
    else:
        try:
            text = path.read_text(errors="replace")
        except OSError as e:   # an unreadable note (including my briefing) must not stop automatic recall
            text = f"(could not be read: {type(e).__name__}: {e})"
        for chunk in _chunks(text):
            docs.append((path.relative_to(root).as_posix(), chunk))
    return docs


def corpus(root: Path) -> list[tuple[str, str]]:
    global _corpus_root, _surprise_revision, _surprise_docs, _surprise_expiry
    global _snapshot, _snapshot_docs, _snapshot_expiry, _lexical_docs, _lexical
    root = root.resolve()
    if root != _corpus_root:
        _files.clear()
        _surprise_revision = ()
        _corpus_root = root
    paths = [root / "memory" / name for name in ("journal.md", "facts.md", "facts-archive.md", "digests.md")]
    for folder in ("notes", "skills", "memory/people"):
        paths.extend(sorted((root / folder).rglob("*.md")))
    episodes = sorted((root / "memory" / "episodes").glob("*.jsonl"))
    paths.extend(episodes)
    episode_paths = set(episodes)
    revisions = {}
    for path in paths:
        try:
            revisions[path] = _revision(path)
        except FileNotFoundError:
            pass
    verdict_path = root / VERDICTS
    verdict_revision = _revision(verdict_path) if verdict_path.exists() else None
    now = clock.now()
    day = now.date()
    # Include all sources, their order, verdicts and the day. Expiry can happen within a day.
    snapshot = (str(root), repr((revisions, verdict_revision, day)))
    if snapshot == _snapshot and (_snapshot_expiry is None or now <= _snapshot_expiry):
        return _snapshot_docs.copy()
    verdicts = None
    docs = []
    for path, revision in revisions.items():
        is_episode = path in episode_paths
        # Verdicts annotate cycles; fact ages and surprise expiry depend on the day as well as the files.
        key = (revision, verdict_revision if is_episode else None,
               day if path.name in ("facts.md", "facts-archive.md") else None)
        cached = _files.get(path)
        shard = root / CORPUS_CACHE / (hashlib.sha1(str(path.relative_to(root)).encode()).hexdigest() + ".json")
        if cached is None:
            try:
                saved = json.loads(shard.read_text())
            except (OSError, ValueError):
                saved = {}
            if saved.get("revision") == repr(key):
                cached = _File(key, [tuple(doc) for doc in saved["docs"]], None,
                               saved["words"], saved["fold_rows"])
                _files[path] = cached
        if cached is None or cached.revision != key:
            rows = []
            if is_episode:
                if cached and cached.rows is not None and cached.revision[0] == revision:
                    rows = cached.rows
                else:
                    for line in path.read_text(errors="replace").splitlines():
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(rec, dict):
                            rows.append(rec)
                if verdicts is None:
                    verdicts = {row["episode_ts"]: row for row in verdict_rows(root)}
            file_docs = _file_docs(root, path, verdicts or {}, rows)
            # The surprise fold ignores talk records and never reads tool payloads.
            fold_rows = [{k: row[k] for k in ("kind", "ts", "intent", "finish", "closed") if k in row}
                         for row in rows if row.get("kind") in ("live", "sleep")]
            cached = _File(key, file_docs, rows, _word_index(file_docs), fold_rows)
            _files[path] = cached
            saved = {"revision": repr(key), "docs": cached.docs,
                     "words": cached.words, "fold_rows": cached.fold_rows}
            try:
                _atomic_write(shard, lambda f: f.write(json.dumps(saved).encode()))
            except OSError:
                pass  # A read-only .tmp must not prevent corpus reading.
        docs.extend(cached.docs)
    for path in _files.keys() - revisions.keys():
        del _files[path]
    surprise_revision = (tuple(revisions[p] for p in episodes), day)
    if (surprise_revision != _surprise_revision
            or _surprise_expiry is not None and now > _surprise_expiry):
        items = surprises.fold([row for p in episodes for row in _files[p].fold_rows], now)
        _surprise_docs = [(f"surprise {it['id']} ({it['opened'][:16]})", surprises.text(it)) for it in items.values()]
        _surprise_revision = surprise_revision
        _surprise_expiry = min((it["opened_at"] + timedelta(days=surprises.EXPIRE_DAYS)
                                for it in items.values() if it["status"] == "open"), default=None)
    _snapshot_docs = docs + _surprise_docs
    _snapshot, _snapshot_expiry = snapshot, _surprise_expiry
    _lexical_docs = _snapshot_docs
    _lexical = [*(_files[p].words for p in revisions), _word_index(_surprise_docs)]
    return _snapshot_docs.copy()


def _bm25(docs: list[tuple[str, str]], query: str) -> list[int]:
    """Indexes of the docs with any query word, best first."""
    q = _tokens(query)
    if not docs or not q:
        return []
    global _lexical_docs, _lexical
    if docs is not _lexical_docs and docs != _lexical_docs:
        _lexical_docs, _lexical = docs, [_word_index(docs)]
    n, avg = len(docs), sum(sum(lengths) for lengths, _ in _lexical) / len(docs) or 1
    k1, b = 1.5, 0.75
    scored = {}
    for w in q:
        df = sum(len(postings.get(w, ())) for _, postings in _lexical)
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        offset = 0
        for lengths, postings in _lexical:
            for i, tf in postings.get(w, ()):
                score = idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * lengths[i] / avg))
                index = i + offset
                scored[index] = scored.get(index, 0.0) + score
            offset += len(lengths)
    return [i for _, i in sorted(((score, i) for i, score in scored.items() if score > 0), reverse=True)]


def _embed(texts: list[str], query: bool) -> list[list[float]]:
    url = os.environ.get("SHAFT_EMBED_URL")
    if not url:
        raise RuntimeError("no embedding service (SHAFT_EMBED_URL is not set)")
    out: list[list[float]] = []
    for i in range(0, len(texts), 32):
        req = urllib.request.Request(url, data=json.dumps({"input": texts[i:i + 32],
                                                           "input_type": "query" if query else "document"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            out += [d["embedding"] for d in sorted(json.load(resp)["data"], key=lambda d: d["index"])]
    return out


def _read_vectors(path: Path) -> None:
    """Read a generation or just its appended records; ignore an incomplete final record."""
    global _vector_revision, _vector_end, _vector_records, _vector_version
    revision = _revision(path) if path.exists() else None
    if revision == _vector_revision:
        return
    incremental = (_vector_version == 2 and revision and _vector_revision
                   and revision[4] == _vector_revision[4] and revision[2] > _vector_end)
    if not incremental:
        _decoded.clear()
        _vector_end = _vector_records = _vector_version = 0
    if revision:
        with path.open("rb") as f:
            # Bind the revision to this descriptor, not a concurrently replaced pathname.
            stat = os.fstat(f.fileno())
            revision = (path, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino)
            if incremental and revision[4] != _vector_revision[4]:
                _decoded.clear()
                _vector_end = _vector_records = _vector_version = 0
                incremental = False
            if incremental:
                f.seek(_vector_end)
            else:
                header = json.loads(f.readline())
                if header["byteorder"] != sys.byteorder or header["version"] not in (1, 2):
                    raise ValueError("unsupported memory index format")
                _vector_version = header["version"]
                if _vector_version == 1:
                    for key, size in header["vectors"]:
                        vec = array("f")
                        vec.fromfile(f, size)
                        _decoded[key] = vec
                    _vector_records = len(_decoded)
                    _vector_end = f.tell()
                    _vector_revision = revision
                    return
                _vector_end = f.tell()
            while _vector_end + _VECTOR_RECORD.size <= stat.st_size:
                record = f.read(_VECTOR_RECORD.size)
                if len(record) != _VECTOR_RECORD.size:
                    break
                raw_key, size = _VECTOR_RECORD.unpack(record)
                end = _vector_end + _VECTOR_RECORD.size + size * 4
                if end > stat.st_size:
                    break
                payload = f.read(size * 4)
                if len(payload) != size * 4:
                    break
                vec = array("f")
                vec.frombytes(payload)
                _decoded[raw_key.hex()] = vec
                _vector_end = end
                _vector_records += 1
    _vector_revision = revision


def _load_vectors(path: Path) -> None:
    global _vector_revision, _vector_end, _vector_records, _vector_version
    try:
        _read_vectors(path)
    except (OSError, ValueError, EOFError, KeyError, TypeError):
        _decoded.clear()
        _vector_end = _vector_records = _vector_version = 0
        _vector_revision = _revision(path) if path.exists() else None


def _write_vector(f, key: str, vec: array) -> None:
    f.write(_VECTOR_RECORD.pack(bytes.fromhex(key), len(vec)))
    f.write(vec.tobytes())


def _vectors(root: Path, texts: list[str]) -> list[array]:
    """Reuse known vectors; serialize migration, append and compaction with one stable lockfile."""
    global _vector_root, _vector_revision, _vector_end, _vector_records, _vector_version
    global _vector_texts, _vector_keys, _vector_live, _vector_ordered
    root = root.resolve()
    path, legacy = root / BINARY_INDEX, root / INDEX
    if root != _vector_root:
        _decoded.clear()
        _vector_root, _vector_revision = root, None
        _vector_end = _vector_records = _vector_version = 0
        _vector_texts, _vector_keys, _vector_live, _vector_ordered = [], [], set(), []
    revision = _revision(path) if path.exists() else None
    if (revision == _vector_revision and _vector_version == 2 and texts == _vector_texts
            and len(_vector_ordered) == len(texts)):
        return _vector_ordered.copy()
    _vector_ordered = []
    if texts != _vector_texts:
        _vector_texts = texts.copy()
        _vector_keys = [hashlib.sha1(t.encode()).hexdigest() for t in texts]
        _vector_live = set(_vector_keys)
    _load_vectors(path)
    missing = _vector_live - _decoded.keys()
    dead = _vector_records - len(_vector_live & _decoded.keys())
    if missing or _vector_version != 2 or dead > _vector_records * 0.25 or legacy.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Lock a separate inode: compaction atomically replaces the data file.
        try:
            with path.with_suffix(".lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                _load_vectors(path)  # another writer may have supplied the missing keys
                if _vector_version == 0:
                    try:
                        encoded = json.loads(legacy.read_text()) if legacy.exists() else {}
                    except (OSError, ValueError):
                        encoded = {}
                    for key, value in encoded.items():
                        if key not in _vector_live:
                            continue
                        vec = array("f")
                        vec.frombytes(base64.b64decode(value))
                        _decoded[key] = vec
                missing = sorted({k: t for k, t in zip(_vector_keys, texts) if k not in _decoded}.items())
                added = {k: array("f", v) for (k, _), v in
                         zip(missing, _embed([t for _, t in missing], query=False))} if missing else {}
                _decoded.update(added)
                # A short embedding response must fail before replacing or deleting a cache.
                _vector_ordered = [_decoded[k] for k in _vector_keys]
                # Dead keys are retained between compactions, including temporarily removed chunks.
                dead = _vector_records - len(_vector_live & (_decoded.keys() - added.keys()))
                if _vector_version != 2 or dead > _vector_records * 0.25:
                    kept = {k: v for k, v in _decoded.items() if k in _vector_live}
                    def write(f):
                        f.write(json.dumps({"version": 2, "byteorder": sys.byteorder}).encode() + b"\n")
                        for key, vec in kept.items():
                            _write_vector(f, key, vec)
                        f.flush()
                        os.fsync(f.fileno())
                    _vector_revision = _atomic_write(path, write)
                    _decoded.clear()
                    _decoded.update(kept)
                    _vector_version, _vector_records = 2, len(kept)
                    _vector_end = _vector_revision[2]
                elif added or _vector_end != _vector_revision[2]:
                    with path.open("r+b") as f:
                        f.truncate(_vector_end)  # discard a crashed writer's incomplete final record
                        f.seek(_vector_end)
                        for key, vec in added.items():
                            _write_vector(f, key, vec)
                        f.flush()
                        os.fsync(f.fileno())
                        stat = os.fstat(f.fileno())
                        _vector_revision = (path, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino)
                        _vector_end = stat.st_size
                        _vector_records += len(added)
                # Only a complete, flushed new generation permits removing the migration source.
                legacy.unlink(missing_ok=True)
        except Exception:
            # Do not serve vectors from a failed append or replacement on a later retry.
            _decoded.clear()
            _vector_ordered = []
            _vector_revision = None
            _vector_end = _vector_records = _vector_version = 0
            raise
    _vector_ordered = [_decoded[k] for k in _vector_keys]
    return _vector_ordered.copy()


def _semantic(root: Path, docs: list[tuple[str, str]], query: str, top: int) -> list[int]:
    """Indexes of the docs closest in meaning to the query, best first (vectors are unit length: dot = cosine)."""
    global semantic_error
    try:
        qv = _embed([query], query=True)[0]
        vecs = _vectors(root, [text for _, text in docs])
    except Exception as e:   # the service down or broken: words only, and say so
        semantic_error = f"{type(e).__name__}: {e}"
        print(f"memory search: words only — semantic search unavailable ({semantic_error})", file=sys.stderr)
        return []
    semantic_error = ""
    sims = [(math.sumprod(qv, v) if len(qv) == len(v) else sum(map(operator.mul, qv, v)), i)
            for i, v in enumerate(vecs)]
    if 0 < top < len(sims):
        cutoff = heapq.nlargest(top, sims)[-1][0]
        sims = [(score, i) for score, i in sims if score >= cutoff - 1e-12]
    sims.sort(reverse=True)
    # sumprod keeps extended-precision products. Resolve rounding-level ties with the
    # original float products so tie-breaking stays identical for unit vectors.
    close = set()
    for (left, i), (right, j) in zip(sims, sims[1:]):
        if abs(left - right) <= 1e-12:
            close.update((i, j))
    if close:
        sims = sorted(((sum(map(operator.mul, qv, vecs[i])) if i in close else score, i)
                       for score, i in sims), reverse=True)
    return [i for _, i in sims[:top]]


def search(root: Path, query: str, k: int = 5) -> list[Hit]:
    global mode
    mode = ""                                 # nothing searched: no claim either way
    root = Path(root)
    docs = corpus(root)
    if not docs or not query.strip():
        return []
    fused: dict[int, float] = {}
    for ranking in (_bm25(docs, query)[:50], _semantic(root, docs, query, 50)):
        for rank, i in enumerate(ranking):
            fused[i] = fused.get(i, 0.0) + 1 / (RRF_K + rank + 1)
    mode = f"words only: semantic search is unavailable — {semantic_error}" if semantic_error else "words + meaning"
    best = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
    return [Hit(docs[i][0], docs[i][1][:1200], round(score * 1000, 2)) for i, score in best]


def recall(root: Path, query: str, shown: str = "", k: int = 4, max_chars: int = 1800) -> str:
    """What my memory holds about `query` that is not already in my context — added to a message or a cycle's
    prompt without my having to think of searching (a small model rarely does). Empty if nothing is found."""
    picked, used = [], 0
    for h in search(Path(root), query, k=k + 8):
        probe = h.text.strip()[:120]
        if probe and probe in shown:   # already in my context (recent journal, facts, notes shown)
            continue
        piece = " ".join(h.text.split())
        piece = piece[:400] + ("…" if len(piece) > 400 else "")
        if used + len(piece) > max_chars:
            break
        picked.append(f"- [{h.source}] {piece}")
        used += len(piece)
        if len(picked) >= k:
            break
    if not picked:
        return ""
    return ("\n\n---\n(Recalled automatically from my memory for this — possibly relevant, possibly not; "
            "search_memory finds more:)\n" + "\n".join(picked))
