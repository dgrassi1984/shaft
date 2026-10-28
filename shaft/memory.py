"""Search over everything I remember — journal, facts, notes, skills, conversations.

Two rankings fused (reciprocal rank fusion): BM25 over words, and the meaning of each chunk as a vector from
SHAFT's local embedding service (SHAFT_EMBED_URL), so a memory written in other words — or in the other
language — is still found. Vectors are cached per chunk in .tmp/memory-index.json and computed only for new
chunks. Without the service, search is BM25 only. Every search_memory result says which it was (mode). Each hit carries its source
(journal entry heading, file, or conversation timestamp) so I can cite it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sys
import urllib.request
from array import array
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import clock, surprises

VERDICTS = "memory/verdicts.jsonl"   # the judge's verdicts, as my sleeps received them

WORD = re.compile(r"[a-zA-ZÀ-ÿ0-9]{2,}")
STOP = set("the and for that this with you are was have not but from they his her she him its our "
           "what when who will would there their about which into can all one had has were been "
           "di che il la le lo gli una per con non sono del della".split())
CHUNK = 900
INDEX = Path(".tmp") / "memory-index.json"   # derived from my memory; rebuilt if missing
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


def corpus(root: Path) -> list[tuple[str, str]]:
    docs: list[tuple[str, str]] = []
    journal = root / "memory" / "journal.md"
    if journal.exists():
        for block in journal.read_text().split("\n---\n"):
            block = block.strip()
            if block.startswith("## "):
                head, _, body = block.partition("\n")
                for chunk in _chunks(body) or [""]:
                    docs.append((f"journal {head[3:].strip()}", f"{head}\n{chunk}"))
    for name in ("facts.md", "facts-archive.md"):
        path = root / "memory" / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.startswith("- "):
                continue
            tag = re.search(r" \(src: (.+)\)$", line)
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
    path = root / "memory" / "digests.md"
    if path.exists():
        for chunk in _chunks(path.read_text(errors="replace")):
            docs.append(("memory/digests.md", chunk))
    for folder in ("notes", "skills", "memory/people"):
        base = root / folder
        for path in sorted(base.rglob("*.md")) if base.is_dir() else []:
            try:
                text = path.read_text(errors="replace")
            except OSError as e:   # an unreadable note (including my briefing) must not stop automatic recall
                text = f"(could not be read: {type(e).__name__}: {e})"
            for chunk in _chunks(text):
                docs.append((path.relative_to(root).as_posix(), chunk))
    verdicts = {row["episode_ts"]: row for row in verdict_rows(root)}
    episodes = root / "memory" / "episodes"
    for path in sorted(episodes.glob("*.jsonl")) if episodes.is_dir() else []:
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
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
    return docs + surprises.docs(root)   # what surprised me, and what became of it


def _bm25(docs: list[tuple[str, str]], query: str) -> list[int]:
    """Indexes of the docs with any query word, best first."""
    q = _tokens(query)
    if not docs or not q:
        return []
    toks = [_tokens(text) for _, text in docs]
    n, avg = len(docs), sum(map(len, toks)) / len(docs) or 1
    df = Counter(w for t in toks for w in set(t))
    k1, b = 1.5, 0.75
    scored = []
    for i, t in enumerate(toks):
        tf, score = Counter(t), 0.0
        for w in q:
            if tf[w]:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                score += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / avg))
        if score > 0:
            scored.append((score, i))
    return [i for _, i in sorted(scored, reverse=True)]


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


def _vectors(root: Path, texts: list[str]) -> list[array]:
    """One vector per text, from the cache or the embedding service (only new texts are sent)."""
    path = root / INDEX
    try:
        cache = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        cache = {}
    keys = [hashlib.sha1(t.encode()).hexdigest() for t in texts]
    missing = sorted({k: t for k, t in zip(keys, texts) if k not in cache}.items())
    if missing:
        for (k, _), v in zip(missing, _embed([t for _, t in missing], query=False)):
            cache[k] = base64.b64encode(array("f", v).tobytes()).decode()
        path.parent.mkdir(parents=True, exist_ok=True)
        live = set(keys)
        path.write_text(json.dumps({k: v for k, v in cache.items() if k in live}))   # drop chunks gone
    vecs = []
    for k in keys:
        a = array("f")
        a.frombytes(base64.b64decode(cache[k]))
        vecs.append(a)
    return vecs


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
    sims = sorted(((sum(a * b for a, b in zip(qv, v)), i) for i, v in enumerate(vecs)), reverse=True)
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
