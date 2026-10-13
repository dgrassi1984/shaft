"""My personality, in two layers, every trait tied to the experience that formed it.

self/personality.md has a `## Core` section (values, temperament — slow) and a
`## Surface` section (interests, opinions, moods — fast). Each bullet must cite at
least one real journal entry, e.g. "[2026-10-12 16:54]", so the personality is
grown from what actually happened rather than written from nowhere.
The supervisor enforces how often the core may change.
"""
from __future__ import annotations

import re
from pathlib import Path

LAYERS = {"core": "Core", "surface": "Surface"}
CITE = re.compile(r"\[(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}))?\]")
HEAD = re.compile(r"^## (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})", re.M)
TEMPLATE = """# Personality

*Grown from experience. Every trait cites the journal entry that supports it.*

## Core
*(values and temperament — change rarely)*

(nothing yet)

## Surface
*(current interests, opinions, moods)*

(nothing yet)
"""


def path(root: Path) -> Path:
    return Path(root) / "self" / "personality.md"


def read(root: Path) -> str:
    p = path(root)
    return p.read_text() if p.exists() else TEMPLATE


def section(text: str, layer: str) -> str:
    title = LAYERS[layer]
    m = re.search(rf"^## {title}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def update(root: Path, layer: str, text: str) -> str:
    if layer not in LAYERS:
        return f"ERROR: layer must be one of {sorted(LAYERS)}"
    journal = Path(root) / "memory" / "journal.md"
    entries = HEAD.findall(journal.read_text()) if journal.exists() else []
    days = {d for d, _ in entries}
    stamps = {f"{d} {t}" for d, t in entries}
    bullets = [line for line in text.splitlines() if line.lstrip().startswith(("- ", "* "))]
    if not bullets:
        return "ERROR: write the layer as bullet points ('- trait — why [YYYY-MM-DD HH:MM]')"
    for line in bullets:
        cites = CITE.findall(line)
        if not cites:
            return f"ERROR: this trait cites no journal entry: {line.strip()!r}"
        for day, hm in cites:
            if (hm and f"{day} {hm}" not in stamps) or (not hm and day not in days):
                stamp = f"{day} {hm}".strip()
                return f"ERROR: [{stamp}] is not a journal entry I have (in: {line.strip()!r})"
    current = read(root)
    title = LAYERS[layer]
    note = "*(values and temperament — change rarely)*" if layer == "core" else \
        "*(current interests, opinions, moods)*"
    # Only this layer's own lines: headings (`# Personality`, `## Core` …) and captions I sometimes paste in
    # once put a second Core block inside Surface, and the file then had two Cores (wren, 2026-10-13).
    text = "\n".join(l for l in text.strip().splitlines()
                     if not l.lstrip().startswith("#") and l.strip() not in (note, ""))
    body = f"## {title}\n{note}\n\n{text.strip()}\n\n"
    path(root).parent.mkdir(parents=True, exist_ok=True)
    path(root).write_text(_rebuild(current, title, body).rstrip() + "\n")
    return f"{layer} personality updated ({len(bullets)} traits)"


def _rebuild(current: str, title: str, body: str) -> str:
    """The file with exactly one block per section: the first `## X` of each title is kept, later copies are
    dropped, and the layer being written is replaced (or appended if missing)."""
    parts = re.split(r"(?m)^(?=## )", current)
    preamble, blocks, seen = parts[0], [], set()
    for block in parts[1:]:
        name = block.splitlines()[0][3:].strip()
        if name in seen:
            continue
        seen.add(name)
        blocks.append(body if name == title else block.rstrip() + "\n\n")
    if title not in seen:
        blocks.append(body)
    return preamble + "".join(blocks)
