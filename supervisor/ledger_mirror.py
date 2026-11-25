"""The supervisor's add-only copy of two ledgers the instance keeps: its promises and the operator's corrections (#7).

memory/commitments.jsonl and memory/corrections.jsonl are the instance's files: it appends to them through its own
tools, and nothing stops it from rewriting them. The `promises` and `corrections` gauges, the promises section of its
statistics and the claim lint are meant to be readings the instance cannot argue with, so they are not read from those
files alone. Each time the supervisor commits the instance's mind (core.commit_state, after every run) it appends to
its own copy every row of the two ledgers it has not seen before; a row is never taken out of the copy. Readers fold
the copy together with the rows of the ledger that are not in it yet, so a row later altered or removed in the
instance's file is still counted, and the reading names the change. Nothing is refused: the instance may still edit
its files; the reading only stops following the edit. (The plan, memory/projects.jsonl, is the instance's own account
and is read as it is.)

A promise to the operator is checked against its source (the conversation, the delivered message or the operator
message it answers: commitments._source_rows) when it is copied, and the copy keeps that verdict: removing the source
later does not turn a recorded promise into one made to itself.

The first sync of an instance backfills the copy from its mind store's history (every committed state of the two
files), once: a `<kind>.backfilled` marker says it was done, so an empty ledger is not walked again and an
interrupted first copy is. A statistics build never walks git: it reads the copy and the current file. A replay reads
the copy as it stood at its moment (`seen`).

<supervisor dir>/ledger_mirror/<name>/<kind>.jsonl: {seen, row[, checked]}, row being the ledger's line as it was
parsed and checked the promise as its source was judged when it was copied.
"""
from __future__ import annotations

import fcntl
import json
from datetime import datetime
from pathlib import Path

from . import core, jsonl
from .served import _at

KINDS = {"commitments": "memory/commitments.jsonl", "corrections": "memory/corrections.jsonl"}
# What marks a row of the copy as checked when copied: a key no JSON row can carry, so the instance cannot claim it.
CHECKED = object()


def _dir(name: str, project: Path | None = None) -> Path:
    from . import mind
    return mind.minds_dir(project).parent / "ledger_mirror" / name


def path(name: str, kind: str, project: Path | None = None) -> Path:
    return _dir(name, project) / f"{kind}.jsonl"


def _key(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def parse(text: str) -> tuple[list[dict], int]:
    """The ledger's rows that are JSON objects, in order, and how many lines were not (a torn or malformed line is the
    instance's to explain; it is never copied, and it costs only itself)."""
    rows, bad = [], 0
    for line in core.jsonl_lines(text or ""):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def records(name: str, kind: str, project: Path | None = None) -> list[dict]:
    """The copy's records ({seen, row[, checked]}), oldest first; [] before the first sync."""
    return [rec for rec in jsonl.rows(path(name, kind, project)) if isinstance(rec.get("row"), dict)]


def rows(name: str, kind: str, project: Path | None = None) -> list[dict]:
    """The copy's rows as the ledger had them, oldest first."""
    return [rec["row"] for rec in records(name, kind, project)]


def _history(name: str, rel: str, project: Path | None) -> list[str]:
    """Every committed state of the file in the mind store, oldest first ([] without a store)."""
    from . import mind
    if not mind.exists(name, project) or not mind.head(name, project):
        return []
    revs = mind.log(name, "--format=%H", "--reverse", mind.BRANCH, "--", rel, project=project).split()
    return [mind.show(name, rev, rel, project) or "" for rev in revs]


def _checked(name: str, new: list[dict]) -> dict[int, dict]:
    """The source verdict of each new promise row, judged now (index in `new` -> the row as checked)."""
    from . import commitments
    made = [i for i, row in enumerate(new) if row.get("event") == "made"]
    if not made or not commitments.NAME.fullmatch(name):
        return {}
    checked = commitments._source_rows([new[i] for i in made], commitments._episodes(name),
                                       lambda file: bool(commitments.operator_message(name, file)), name,
                                       commitments._received(name))
    return dict(zip(made, checked))


def sync(name: str, root: Path, project: Path | None = None, now: datetime | None = None) -> int:
    """Append to the copy every row of the two ledgers in `root` it does not hold yet; the first sync of an instance
    also takes every row its mind store's history holds. Returns the number of rows added."""
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    added = 0
    for kind, rel in KINDS.items():
        p = path(name, kind, project)
        p.parent.mkdir(parents=True, exist_ok=True)
        done = p.with_name(f"{kind}.backfilled")
        with p.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            seen = {_key(r) for r in rows(name, kind, project)}
            first = not done.exists()
            texts = _history(name, rel, project) if first else []
            texts.append(core.safe_read(root, rel, "", errors="replace"))
            new = []
            for text in texts:
                for row in parse(text)[0]:
                    if (k := _key(row)) not in seen:
                        seen.add(k)
                        new.append(row)
            checked = _checked(name, new) if kind == "commitments" else {}
            jsonl.append(f, [json.dumps({"seen": stamp, "row": row, **({"checked": checked[i]} if i in checked else {})})
                             + "\n" for i, row in enumerate(new)])   # ASCII: a lone surrogate cannot stop the copy
            if first:
                done.write_text(stamp + "\n")    # only once the backfill is written: an interrupted one is redone
            added += len(new)
    return added


def _time(row: dict) -> datetime | None:
    """When the instance stamped a row: `at` (every event the template writes), else a correction's `when`."""
    return _at(row.get("at")) or _at(row.get("when"))


def merged(name: str, kind: str, current: list[dict], project: Path | None = None,
           as_of: datetime | None = None) -> tuple[list[dict], dict]:
    """The rows a reader folds: the copy's (as checked when copied, marked with CHECKED), then the current ledger's not
    in it yet; with `as_of`, only what was known then (copied by then; a current row stamped later is left out). And
    the ids whose recorded rows the ledger no longer holds as they were: {"removed": ids no longer in it at all,
    "altered": ids still in it with a row changed or taken out}."""
    every = records(name, kind, project)
    if as_of is not None:
        as_of = _at(as_of.isoformat())     # naive local, as the copy's stamps are
        later = {_key(rec["row"]) for rec in every if (seen := _at(rec.get("seen"))) is None or seen > as_of}
        every = [rec for rec in every if _key(rec["row"]) not in later]
        # Not yet known then: a row copied after the moment, or stamped after it.
        current = [r for r in current if _key(r) not in later and ((at := _time(r)) is None or at <= as_of)]
    kept = every
    known = {_key(rec["row"]) for rec in kept}
    now = {_key(r) for r in current}
    ids_now = {r.get("id") for r in current if isinstance(r.get("id"), str)}
    gone = {rec["row"].get("id") for rec in kept
            if _key(rec["row"]) not in now and isinstance(rec["row"].get("id"), str)}
    out = [{**rec["checked"], CHECKED: True} if isinstance(rec.get("checked"), dict) else rec["row"] for rec in kept]
    return (out + [r for r in current if _key(r) not in known],
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
