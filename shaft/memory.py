"""Search over everything I remember — journal, facts, notes, skills, conversations.

Plain BM25 over text chunks, no dependencies. Each hit carries its source
(journal entry heading, file, or conversation timestamp) so I can cite it.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

WORD = re.compile(r"[a-zA-ZÀ-ÿ0-9]{2,}")
STOP = set("the and for that this with you are was have not but from they his her she him its our "
           "what when who will would there their about which into can all one had has were been "
           "di che il la le lo gli una per con non sono del della".split())
CHUNK = 900


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
    facts = root / "memory" / "facts.md"
    if facts.exists():
        for line in facts.read_text().splitlines():
            if line.startswith("- "):
                docs.append(("fact", line[2:]))
    for folder in ("notes", "skills", "memory/people"):
        base = root / folder
        for path in sorted(base.rglob("*.md")) if base.is_dir() else []:
            for chunk in _chunks(path.read_text(errors="replace")):
                docs.append((path.relative_to(root).as_posix(), chunk))
    episodes = root / "memory" / "episodes"
    for path in sorted(episodes.glob("*.jsonl")) if episodes.is_dir() else []:
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "talk" and rec.get("content"):
                who = "Daniele" if rec.get("role") == "user" else "me"
                docs.append((f"conversation {rec.get('ts', '')} ({who})", rec["content"][:CHUNK * 2]))
    return docs


def search(root: Path, query: str, k: int = 5) -> list[Hit]:
    docs = corpus(Path(root))
    q = _tokens(query)
    if not docs or not q:
        return []
    toks = [_tokens(text) for _, text in docs]
    n, avg = len(docs), sum(map(len, toks)) / len(docs) or 1
    df = Counter(w for t in toks for w in set(t))
    k1, b = 1.5, 0.75
    scored = []
    for (source, text), t in zip(docs, toks):
        tf, score = Counter(t), 0.0
        for w in q:
            if tf[w]:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                score += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / avg))
        if score > 0:
            scored.append(Hit(source, text[:1200], round(score, 2)))
    return sorted(scored, key=lambda h: -h.score)[:k]
