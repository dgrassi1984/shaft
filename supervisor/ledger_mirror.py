"""The supervisor's add-only copy of the instance's commitments ledger.

Each mind commit (`core.commit_state`) copies new rows from `memory/commitments.jsonl`, and so does the instance's
broker during a run (`observe`: before every POST it serves, the model's steps included, and every POLL_S seconds), so
a row present at any of those looks is copied even if it is taken back within the run (#21). The promises section of the statistics folds the
supervisor's copy together with rows it has yet to copy. It counts rows the instance later alters or removes and
reports those changes.

A promise to the operator is checked against its source (the conversation, the delivered message or the operator
message it answers: commitments._source_rows) when it is copied, and the copy keeps that verdict: removing the source
later does not turn a recorded promise into one made to itself.

The first sync backfills the copy from the instance's mind store history. A `commitments.backfilled` marker records
completion. The next sync retries an interrupted backfill. Statistics use the copy and the current file.

A promise copied during its run is judged at the run's mind commit, once the run's episode (its source) is written:
the copy is add-only, so the verdict is a second record of the same row, and a reader folds the two.

<supervisor dir>/ledger_mirror/<name>/commitments.jsonl: {seen, row[, checked][, judge]}, row being the ledger's line as
it was parsed, checked the promise as its source was judged, and judge true on a promise copied during its run and not
judged yet (false once judging found nothing to judge).
"""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, timedelta
from pathlib import Path

from . import core, jsonl
from .served import _at

RELABELLED = "operator source missing or invalid; recorded as self"   # commitments.RELABELLED (a test pins it)

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


# How often a run's broker looks at the ledger between requests (observe): a row written and taken back by one tool
# step, with no request in between, is copied when the two are further apart than this.
POLL_S = 2.0
# A promise whose source is not found yet stays waiting this long: the run that made it (a cycle paused for a
# conversation is committed before its episode is written; a conversation lasts up to served.OPEN_TALK) has written
# its episode by then. Found, it is recorded at once; still missing after this, it is recorded as the instance's own.
SOURCE_WAIT = timedelta(hours=24)


def records(name: str, kind: str, project: Path | None = None, as_of: datetime | None = None) -> list[dict]:
    """The copy's records ({seen, row[, checked][, judge]}), one per row, oldest first; [] before the first sync. A
    later record of the same row (its verdict, judged after it was copied) adds to the first, `seen` staying the
    first's; with `as_of`, only a verdict judged by then."""
    out: dict[str, dict] = {}
    for rec in jsonl.rows(path(name, kind, project)):
        if not isinstance(rec.get("row"), dict):
            continue
        k = _key(rec["row"])
        if k not in out:
            out[k] = dict(rec)
            continue
        if as_of is not None and ((judged := _at(rec.get("judged"))) is None or judged > as_of):
            continue
        for field in ("checked", "judge"):
            if field in rec:
                out[k][field] = rec[field]
    return list(out.values())


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


def _verdicts(name: str, rows: list[dict], seen: list[str], strict: set[int], now: datetime) -> dict[int, dict]:
    """The verdict to record now for each promise in `rows` (index -> the row as checked); a promise whose source is not
    found while its run may still be writing the episode that holds it (seen less than SOURCE_WAIT ago) gets none and
    waits."""
    out = {}
    for i, checked in _checked(name, rows, strict).items():
        missing = checked.get("made_to") != rows[i].get("made_to") and checked.get("note") == RELABELLED
        if missing and (at := _at(seen[i])) is not None and now - at < SOURCE_WAIT:
            continue
        out[i] = checked
    return out


def sync(name: str, root: Path, project: Path | None = None, now: datetime | None = None, judge: bool = True) -> int:
    """Copy new commitment rows and return their count. Backfill the mind store's history on the first sync.

    `judge` (the mind commit): judge each new promise's source now, and the promises still waiting for it. False
    (observe, during a run): copy the ledger's current rows without judging them (their source, the run's episode,
    is written when the run ends), marking each promise to be judged; the history of a first sync is judged either way.
    A promise not judged yet is read strictly (merged), and its verdict is a later record of the same row."""
    from .commitments import FILE
    now = (now or datetime.now()).replace(microsecond=0)
    stamp = now.isoformat(timespec="seconds")
    p = path(name, "commitments", project)
    p.parent.mkdir(parents=True, exist_ok=True)
    done = p.with_name("commitments.backfilled")
    with p.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        first = not done.exists()
        recs = records(name, "commitments", project)
        seen = {_key(rec["row"]) for rec in recs}
        texts = _history(name, FILE, project) if first else []
        texts.append((core.safe_read(root, FILE, "", errors="replace"), None))
        new, stamps, current = [], [], set()
        for n, (text, committed) in enumerate(texts):
            for row in parse(text)[0]:
                if (k := _key(row)) not in seen:
                    seen.add(k)
                    if n == len(texts) - 1:
                        current.add(len(new))
                    new.append(row)
                    stamps.append(min(committed, stamp) if committed else stamp)
        # Judged now: the history (committed after its runs); the current rows only at a commit. A current row of a
        # sync after the backfill, or of one during a run, first appeared after the source rule: judged strictly.
        now_judged = [i for i in range(len(new)) if judge or i not in current]
        strict = {j for j, i in enumerate(now_judged) if i in current and not first}
        verdicts = _verdicts(name, [new[i] for i in now_judged], [stamps[i] for i in now_judged], strict, now)
        checked = {now_judged[j]: v for j, v in verdicts.items()}
        waits = {i for i in range(len(new)) if new[i].get("event") == "made" and i not in checked
                 and (i in current and (not judge or not first)) and commitments_name(name)}
        out = [{"seen": stamps[i], "row": row, **({"checked": checked[i]} if i in checked else {}),
                **({"judge": True} if i in waits else {})} for i, row in enumerate(new)]
        if judge:   # the promises waiting for their source
            waiting = [rec for rec in recs if rec.get("judge") is True and "checked" not in rec]
            found = _verdicts(name, [rec["row"] for rec in waiting], [rec["seen"] for rec in waiting],
                              set(range(len(waiting))), now)
            out += [{"seen": rec["seen"], "row": rec["row"], "checked": found[i], "judge": False, "judged": stamp}
                    for i, rec in enumerate(waiting) if i in found]
        jsonl.append(f, [json.dumps(rec) + "\n" for rec in out])   # ASCII: a lone surrogate cannot stop the copy
        if first:
            done.write_text(stamp + "\n")    # only once the backfill is written: an interrupted one is redone
        return len(new)


def commitments_name(name: str) -> bool:
    from . import commitments
    return bool(commitments.NAME.fullmatch(name))


def observe(name: str, root: Path, project: Path | None = None, now: datetime | None = None) -> int:
    """Copy the rows the ledger holds now, during a run (#21): what its broker calls on each request and poll. The
    current rows are only copied, never judged here (see sync); a first sync's backfill is done here too, so a fresh
    instance's first run is copied as it goes."""
    return sync(name, root, project, now, judge=False)


def _strict(row: dict) -> dict:
    """A promise not judged yet is read as one made after the source rule (it was): an operator promise needs its
    source, whatever the row says about `sourced`."""
    return {**row, "sourced": True} if row.get("event") == "made" and row.get("made_to") == "operator" else row


def merged(name: str, kind: str, current: list[dict], project: Path | None = None,
           as_of: datetime | None = None) -> tuple[list[dict], dict]:
    """The rows a reader folds: the copy's (as checked when copied, marked with CHECKED), then the current ledger's not
    in it yet; with `as_of`, only what was known then (copied by then; a current row stamped later is left out). And
    the ids whose recorded rows the ledger no longer holds as they were: {"removed": ids no longer in it at all,
    "altered": ids still in it with a row changed or taken out}."""
    if as_of is not None:
        as_of = _at(as_of.isoformat())
    every = records(name, kind, project, as_of=as_of)
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
    out = [{**rec["checked"], CHECKED: True} if isinstance(rec.get("checked"), dict)
           else _strict(rec["row"]) if rec.get("judge") is True else rec["row"] for rec in kept]
    return (out + [r for r in current if _key(r) not in known],
            {"removed": {i for i in gone if i not in ids_now}, "altered": {i for i in gone if i in ids_now}})
