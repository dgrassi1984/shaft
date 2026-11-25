"""The supervisor's add-only copy of two ledgers the instance keeps: its promises and the operator's corrections (#7).

memory/commitments.jsonl and memory/corrections.jsonl are the instance's files: it appends to them through its own
tools, and nothing stops it from rewriting them. The `promises` and `corrections` gauges are meant to be readings the
instance cannot argue with, so they are not read from those files alone. Each time the supervisor commits the
instance's mind (core.commit_state, after every run) it appends to its own copy every row of the two ledgers it has
not seen before; a row is never taken out of the copy. The gauges fold the copy together with the rows of the ledger
that are not in it yet, so a row later altered or removed in the instance's file is still counted, and the reading
names the change. Nothing is refused: the instance may still edit its files; the reading only stops following the
edit. (The plan, memory/projects.jsonl, is the instance's own account and is read as it is.)

The first sync of an instance backfills the copy from its mind store's history (every committed state of the two
files), once. A statistics build never walks git: it reads the copy and the current file.

<supervisor dir>/ledger_mirror/<name>/<kind>.jsonl: {seen, row}, row being the ledger's line as it was parsed.
"""
from __future__ import annotations

import fcntl
import json
from datetime import datetime
from pathlib import Path

from . import core

KINDS = {"commitments": "memory/commitments.jsonl", "corrections": "memory/corrections.jsonl"}


def _dir(name: str, project: Path | None = None) -> Path:
    from . import mind
    return mind.minds_dir(project).parent / "ledger_mirror" / name


def path(name: str, kind: str, project: Path | None = None) -> Path:
    return _dir(name, project) / f"{kind}.jsonl"


def _key(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def parse(text: str) -> list[dict]:
    """The ledger's rows that are JSON objects, in order (a torn or malformed line is the instance's to explain; it
    is never copied)."""
    rows = []
    for line in core.jsonl_lines(text or ""):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def rows(name: str, kind: str, project: Path | None = None) -> list[dict]:
    """The copy's rows, oldest first; [] before the first sync."""
    try:
        text = path(name, kind, project).read_text()
    except OSError:
        return []
    out = []
    for line in core.jsonl_lines(text):
        try:
            rec = json.loads(line)
        except ValueError:
            continue       # a torn line must not hide the rows around it
        if isinstance(rec, dict) and isinstance(rec.get("row"), dict):
            out.append(rec["row"])
    return out


def _history(name: str, rel: str, project: Path | None) -> list[str]:
    """Every committed state of the file in the mind store, oldest first ([] without a store)."""
    from . import mind
    if not mind.exists(name, project) or not mind.head(name, project):
        return []
    revs = mind.log(name, "--format=%H", "--reverse", mind.BRANCH, "--", rel, project=project).split()
    return [mind.show(name, rev, rel, project) or "" for rev in revs]


def sync(name: str, root: Path, project: Path | None = None, now: datetime | None = None) -> int:
    """Append to the copy every row of the two ledgers in `root` it does not hold yet; the first sync of an instance
    also takes every row its mind store's history holds. Returns the number of rows added."""
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    added = 0
    for kind, rel in KINDS.items():
        p = path(name, kind, project)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            first = not f.read(1)
            seen = {_key(r) for r in rows(name, kind, project)}
            texts = _history(name, rel, project) if first else []
            texts.append(core.safe_read(root, rel, "", errors="replace"))
            lines = []
            for text in texts:
                for row in parse(text):
                    if (k := _key(row)) not in seen:
                        seen.add(k)
                        lines.append(json.dumps({"seen": stamp, "row": row}, ensure_ascii=False) + "\n")
            f.seek(0, 2)
            f.write("".join(lines))
            added += len(lines)
    return added


def merged(name: str, kind: str, current: list[dict], project: Path | None = None) -> tuple[list[dict], dict]:
    """The rows a gauge folds: the copy's, then the current ledger's not in it yet. And the ids whose recorded rows
    the ledger no longer holds as they were: {"removed": ids no longer in it at all, "altered": ids still in it with
    a row changed or taken out}."""
    kept = rows(name, kind, project)
    known = {_key(r) for r in kept}
    now = {_key(r) for r in current}
    ids_now = {r.get("id") for r in current}
    gone = {r.get("id") for r in kept if _key(r) not in now and isinstance(r.get("id"), str)}
    return (kept + [r for r in current if _key(r) not in known],
            {"removed": {i for i in gone if i not in ids_now}, "altered": {i for i in gone if i in ids_now}})


def changed_text(changes: dict, ids, noun: str) -> str:
    """'; 1 correction removed from the ledger since it was recorded' (only of the ids the reading counts)."""
    parts = []
    for what, verb in (("removed", "removed from"), ("altered", "altered in")):
        n = len(changes[what] & set(ids))
        if n:
            parts.append(f"{n} {noun}{'' if n == 1 else 's'} {verb} the ledger since "
                         f"{'it was' if n == 1 else 'they were'} recorded")
    return "".join(f"; {p}" for p in parts) + (" (still counted)" if parts else "")
