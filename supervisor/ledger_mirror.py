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

A promise copied during its run, or at the commit of a cycle paused for a conversation, is judged at the next mind
commit that ends a run, once the run's episode (its source) is written: the copy is add-only, so the verdict is a second
record of the same row, and a reader folds the two.

<supervisor dir>/ledger_mirror/<name>/commitments.jsonl: {seen, row[, checked][, judge, strict][, judged]}, row being the
ledger's line as it was parsed and checked the promise as its source was judged. A promise copied without judging has
judge true (and strict: judged as a row made after the source rule; history: from a first backfill, its verdict
counting from its `seen` as a backfill's always did); its verdict record has judge false and judged, when, or unjudged
true for a row the source check could not read (left out of every reading). A cycle paused for a conversation holds
the copy (`held`, its runner's pid): no commit judges until the cycle ends.
"""
from __future__ import annotations

import fcntl
import json
import os
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


# How often a run's broker looks at the ledger between requests (observe): a row written and taken back by one tool
# step, with no request in between, is copied when the two are further apart than this.
POLL_S = 2.0

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
        for field in ("checked", "judge", "unjudged"):
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


def _checked_batch(name: str, new: list[dict], strict: set[int] = frozenset()) -> dict[int, dict]:
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


def _checked(name: str, new: list[dict], strict: set[int] = frozenset()) -> tuple[dict[int, dict], set[int]]:
    """_checked_batch, and the promises that could not be judged at all (a row whose fields break the source check,
    such as a time no clock can hold): those cost only themselves, never the verdicts of the rest."""
    try:
        return _checked_batch(name, new, strict), set()
    except Exception:
        pass
    verdicts, failed = {}, set()
    for i, row in enumerate(new):
        try:
            verdicts.update({i: v for v in _checked_batch(name, [row], {0} if i in strict else set()).values()})
        except Exception:
            failed.add(i)
    return verdicts, failed


def _held(name: str, project: Path | None) -> bool:
    """A cycle of this instance is paused for a conversation (hold): its episode, the source of what it promised, is
    not written yet, so no commit judges until it ends. A hold whose runner is gone holds nothing."""
    mark = _dir(name, project) / "held"
    try:
        pid = int(mark.read_text().strip())
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        mark.unlink(missing_ok=True)
        return False
    except (OSError, ValueError):
        return False


def hold(name: str, held: bool, project: Path | None = None) -> None:
    """Mark (held=True, by this process) or clear the pause of a cycle of `name` (runner._pause)."""
    mark = _dir(name, project) / "held"
    if held:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(f"{os.getpid()}\n")
    else:
        mark.unlink(missing_ok=True)


def sync(name: str, root: Path, project: Path | None = None, now: datetime | None = None, judge: bool = True) -> int:
    """Copy new commitment rows and return their count. Backfill the mind store's history on the first sync.

    `judge` (the mind commit at a run's end): judge each new promise's source now, and every promise waiting for it.
    False (observe during a run, or the commit of a cycle paused for a conversation: the run's episode, which holds
    a promise's source, is not written yet): copy without judging, marking each new promise to be judged at the next
    judging sync. A promise not judged yet is read strictly (merged), and its verdict is a later record of the row."""
    from .commitments import FILE
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    p = path(name, "commitments", project)
    p.parent.mkdir(parents=True, exist_ok=True)
    done = p.with_name("commitments.backfilled")
    with p.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        current = core.safe_read(root, FILE, "", errors="replace")   # first: what the ledger holds as this sync began
        first = not done.exists()
        recs = records(name, "commitments", project)
        seen = {_key(rec["row"]) for rec in recs}
        texts = (_history(name, FILE, project) if first else []) + [(current, None)]
        new, strict, history = [], set(), set()
        stamps = []
        for n, (text, committed) in enumerate(texts):
            for row in parse(text)[0]:
                if (k := _key(row)) not in seen:
                    seen.add(k)
                    if n < len(texts) - 1:
                        history.add(len(new))       # a first sync's backfill
                    elif not first:
                        strict.add(len(new))
                    new.append(row)
                    stamps.append(min(committed, stamp) if committed else stamp)
        if judge and _held(name, project):
            judge = False      # a paused cycle's promises wait for the commit that ends it
        if judge:
            checked, failed = _checked(name, new, strict)
            out = [{"seen": stamps[i], "row": row, **({"checked": checked[i]} if i in checked else {}),
                    **({"judge": False, "unjudged": True} if i in failed else {})} for i, row in enumerate(new)]
            waiting = [rec for rec in recs if rec.get("judge") is True and "checked" not in rec]
            verdicts, failed = _checked(name, [rec["row"] for rec in waiting],
                                        {i for i, rec in enumerate(waiting) if rec.get("strict", True)})
            # A history row's verdict counts from its `seen`, as a first sync's backfill always recorded it.
            out += [{"seen": rec["seen"], "row": rec["row"], **({"checked": verdicts[i]} if i in verdicts else {}),
                     **({"unjudged": True} if i in failed else {}), "judge": False,
                     "judged": rec["seen"] if rec.get("history") else stamp} for i, rec in enumerate(waiting)]
        else:
            out = [{"seen": stamps[i], "row": row,
                    **({"judge": True, "strict": i in strict, **({"history": True} if i in history else {})}
                       if row.get("event") == "made" else {})}
                   for i, row in enumerate(new)]
        jsonl.append(f, [json.dumps(rec) + "\n" for rec in out])   # ASCII: a lone surrogate cannot stop the copy
        if first:
            done.write_text(stamp + "\n")    # only once the backfill is written: an interrupted one is redone
        return len(new)


def observe(name: str, root: Path, project: Path | None = None, now: datetime | None = None) -> int:
    """Copy the rows the ledger holds now, during a run (#21): what its broker calls on each request and poll. Rows
    are only copied, never judged here (see sync); a first sync's backfill is done here too, so a fresh instance's
    first run is copied as it goes."""
    return sync(name, root, project, now, judge=False)


def _strict(rec: dict) -> dict:
    """A promise not judged yet is read as one made after the source rule (it was): an operator promise needs its
    source, whatever the row says about `sourced`."""
    row = rec["row"]
    if rec.get("strict", True) and row.get("event") == "made" and row.get("made_to") == "operator":
        return {**row, "sourced": True}
    return row


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
    kept = [rec for rec in every if not rec.get("unjudged")]   # a row no source check could read: no reader can either
    known = {_key(rec["row"]) for rec in kept}
    now = {_key(r) for r in current}
    ids_now = {r.get("id") for r in current if isinstance(r.get("id"), str)}
    gone = {rec["row"].get("id") for rec in kept
            if _key(rec["row"]) not in now and isinstance(rec["row"].get("id"), str)}
    out = [{**rec["checked"], CHECKED: True} if isinstance(rec.get("checked"), dict)
           else _strict(rec) if rec.get("judge") is True else rec["row"] for rec in kept]
    return (out + [r for r in current if _key(r) not in known],
            {"removed": {i for i in gone if i not in ids_now}, "altered": {i for i in gone if i in ids_now}})
