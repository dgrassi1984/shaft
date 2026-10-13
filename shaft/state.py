"""SHAFT's on-disk state: self-model, goals, journal, episodes.

Everything is plain text. The journal is append-only; the self-model is
replaced only through an explicit, validated edit (see selfedit.py).
"""
from __future__ import annotations

import json
import re
import os
import tempfile
from datetime import datetime
from pathlib import Path

ENTRY_SEPARATOR = "\n---\n"


DATED_HEADING = re.compile(r"\A#{1,6}[ \t]*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}[^\n]*\n+")


class State:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.self_md = self.root / "self" / "self.md"
        self.goals_md = self.root / "self" / "goals.md"
        self.journal = self.root / "memory" / "journal.md"
        self.episodes_dir = self.root / "memory" / "episodes"

    # -- reading --------------------------------------------------------------
    def read(self, path: Path) -> str:
        return path.read_text() if path.exists() else ""

    def journal_entries(self) -> list[str]:
        """Entries are the `---`-separated blocks that start with a `## ` heading."""
        text = self.read(self.journal)
        return [block.strip() for block in text.split(ENTRY_SEPARATOR)
                if block.strip().startswith("## ")]

    def recent_journal(self, max_chars: int) -> tuple[str, int, int]:
        """Newest entries that fit in max_chars, oldest first.

        Returns (text, entries_included, entries_total). The newest entry is
        always included — cut from the front with a marker if it alone is too big.
        """
        entries = self.journal_entries()
        picked: list[str] = []
        used = 0
        for entry in reversed(entries):
            if picked and used + len(entry) > max_chars:
                break
            if not picked and len(entry) > max_chars:
                entry = "[...start of entry cut for length...]\n" + entry[-max_chars:]
            picked.append(entry)
            used += len(entry)
        picked.reverse()
        return ENTRY_SEPARATOR.join(picked), len(picked), len(entries)

    def last_episode(self) -> dict | None:
        files = sorted(self.episodes_dir.glob("*.jsonl"))
        for path in reversed(files):
            lines = [l for l in path.read_text().splitlines() if l.strip()]
            if lines:
                return json.loads(lines[-1])
        return None

    # -- writing --------------------------------------------------------------
    def append_journal(self, title: str, body: str, when: datetime | None = None) -> None:
        when = when or datetime.now()
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        # A model sometimes opens its text with a dated heading of its own ("## 2026-10-13 10:09 — ..."):
        # the journal already writes one, so drop the copy (section headings like "## What happened" stay).
        body = DATED_HEADING.sub("", body.strip())
        entry = f"{ENTRY_SEPARATOR}## {when:%Y-%m-%d %H:%M} — {title}\n\n{body.strip()}\n"
        with self.journal.open("a") as f:
            f.write(entry)

    def append_episode(self, record: dict) -> None:
        record = {"ts": datetime.now().isoformat(timespec="seconds"), **record}
        self.episodes_dir.mkdir(parents=True, exist_ok=True)
        path = self.episodes_dir / f"{datetime.now():%Y-%m-%d}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_self_model(self, text: str) -> None:
        _atomic_write(self.self_md, text.strip() + "\n")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
