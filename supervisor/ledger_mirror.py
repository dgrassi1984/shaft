"""The supervisor's add-only copy of the instance's commitments ledger.

Each mind commit (`core.commit_state`) copies new rows from `memory/commitments.jsonl`. The promises
section of the statistics folds the supervisor's copy together with rows it has yet to copy. It counts rows the
instance later alters or removes and reports those changes.

A promise to the operator is checked against its source (the conversation, the delivered message or the operator
message it answers: commitments._source_rows) when it is copied, and the copy keeps that verdict: removing the source
later does not turn a recorded promise into one made to itself.

The first sync backfills the copy from the instance's mind store history. A `commitments.backfilled` marker records
completion. The next sync retries an interrupted backfill. Statistics use the copy and the current file.

<supervisor dir>/ledger_mirror/<name>/commitments.jsonl: {seen, row[, checked]}, row being the ledger's line as it was
parsed and checked the promise as its source was judged when it was copied.
"""
from __future__ import annotations

import fcntl
import json
from datetime import datetime
from pathlib import Path

from . import core, jsonl
from .served import _at

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


def _history(name: str, rel: str, project: Path | None) -> list[tuple[str, str | None]]:
    """Every committed state of the file in the mind store, oldest first, with when it was committed (naive local:
    a backfilled row is `seen` then, so a replay of an earlier cycle still reads it); [] without a store."""
    from . import mind
    if not mind.exists(name, project) or not mind.head(name, project):
        return []
    out = []
    for line in mind.log(name, "--format=%H %cI", "--reverse", mind.BRANCH, "--", rel, project=project).splitlines():
        rev, _, when = line.partition(" ")
        at = _at(when.strip())
        # As bytes, decoded leniently: one old blob that is not UTF-8 must not stop every backfill.
        r = mind.git(name, "show", f"{rev}:{rel}", project=project, binary=True, check=False)
        out.append((r.stdout.decode("utf-8", "replace") if r.returncode == 0 else "",
                    at.isoformat(timespec="seconds") if at else None))
    return out


def _checked(name: str, new: list[dict], strict: set[int] = frozenset()) -> dict[int, dict]:
    """The source verdict of each new promise row, judged now (index in `new` -> the row as checked). A row in
    `strict` (one that first appeared after the backfill, so after the source rule) is checked as a sourced promise
    whatever it says: an operator promise without `sourced` would otherwise be kept as made, as rows older than the
    rule are."""
    from . import commitments
    made = [i for i, row in enumerate(new) if row.get("event") == "made"]
    if not made or not commitments.NAME.fullmatch(name):
        return {}
    rows = [{**new[i], "sourced": True} if i in strict and new[i].get("made_to") == "operator" else new[i]
            for i in made]
    checked = commitments._source_rows(rows, commitments._episodes(name),
                                       lambda file: bool(commitments.operator_message(name, file)), name,
                                       commitments._received(name))
    return dict(zip(made, checked))


def sync(name: str, root: Path, project: Path | None = None, now: datetime | None = None) -> int:
    """Copy new commitment rows and return their count. Backfill the mind store's history on the first sync."""
    from .commitments import FILE
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    p = path(name, "commitments", project)
    p.parent.mkdir(parents=True, exist_ok=True)
    done = p.with_name("commitments.backfilled")
    with p.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        seen = {_key(r) for r in rows(name, "commitments", project)}
        first = not done.exists()
        texts = _history(name, FILE, project) if first else []
        texts.append((core.safe_read(root, FILE, "", errors="replace"), None))
        new, strict = [], set()
        stamps = []
        for n, (text, committed) in enumerate(texts):
            for row in parse(text)[0]:
                if (k := _key(row)) not in seen:
                    seen.add(k)
                    if not first and n == len(texts) - 1:
                        strict.add(len(new))
                    new.append(row)
                    stamps.append(min(committed, stamp) if committed else stamp)
        checked = _checked(name, new, strict)
        jsonl.append(f, [json.dumps({"seen": stamps[i], "row": row, **({"checked": checked[i]} if i in checked else {})})
                         + "\n" for i, row in enumerate(new)])   # ASCII: a lone surrogate cannot stop the copy
        if first:
            done.write_text(stamp + "\n")    # only once the backfill is written: an interrupted one is redone
        return len(new)


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
        current = [r for r in current if _key(r) not in later and ((at := _at(r.get("at"))) is None or at <= as_of)]
    kept = every
    known = {_key(rec["row"]) for rec in kept}
    now = {_key(r) for r in current}
    ids_now = {r.get("id") for r in current if isinstance(r.get("id"), str)}
    gone = {rec["row"].get("id") for rec in kept
            if _key(rec["row"]) not in now and isinstance(rec["row"].get("id"), str)}
    out = [{**rec["checked"], CHECKED: True} if isinstance(rec.get("checked"), dict) else rec["row"] for rec in kept]
    return (out + [r for r in current if _key(r) not in known],
            {"removed": {i for i in gone if i not in ids_now}, "altered": {i for i in gone if i in ids_now}})
