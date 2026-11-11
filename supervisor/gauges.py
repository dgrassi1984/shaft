"""Gauges: a few readings an instance chooses to see first in self/stats.md, each a gap the supervisor computes.

A drive stated in words has no error signal. The supervisor already measures most of what one would need (promises
open, claims the log did not support, failures that came back, value, capability against a control) and prints it low
on a long page. A gauge is one of those figures stated as a gap. The instance picks up to MAX_CHOSEN of them from the
MENU (the broker's `gauges` tool), or the operator does (`shaft gauges`); the chosen ones are the first lines of the
page, largest gap first. They are readings only: nothing here instructs, refuses or gates, and nothing is shown until a
choice is made.

No gauge can be set by the instance: each is recomputed whenever the page is built. Most read the supervisor's own
record (judgments, tool logs, battery, checks). Three read a ledger the instance keeps in its folder, as the
supervisor folds and re-checks it (promises: commitments.py; corrections; plan: projects.py): those are as true as
that ledger, and the menu says which they are.
The choice is a row of SUP_DIR/gauges/<name>.jsonl, append-only {ts, by, chosen}; the last row is the choice in force
and the earlier rows are the history of how it changed. A trial replay writes nothing here: its ReplayBroker keeps the
choice it made and passes it in as `extra`.

To order gauges in different units, each states its gap as a share of its own total (open of made, unsupported of
judged, recurred of kinds): the one ordering with no weights to tune.
"""
from __future__ import annotations

import fcntl
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import core
from .core import jsonl_lines

MAX_CHOSEN = 4
WINDOW_DAYS = 14       # the period a rate is read over: long enough for a sampled judge, short enough to move
LAST_VALUED = 20       # value is read over the last cycles, as the value section does
BY = ("instance", "operator")


@dataclass
class Reading:
    text: str                  # one line, as the instance reads it
    gap: float | None = None   # the gap as a share of its own total, 0..1; None: nothing to measure yet


def _share(part: float, whole: float) -> float | None:
    return max(0.0, min(1.0, part / whole)) if whole else None


def _days(since: datetime, now: datetime) -> str:
    d = (now - since).total_seconds() / 86400
    return f"{d:.0f} days" if d >= 2 else f"{d * 24:.0f} h"


# -- the readings: each from records the supervisor already keeps -------------------------------------------------
def _promises(name: str, now: datetime) -> Reading:
    from . import commitments
    items = [it for it in commitments.ledger(name, now).values() if it["made_to"] == "operator"]
    if not items:
        return Reading("Promises to the operator: none recorded")
    pending = [it for it in items if it["status"] in ("open", "overdue")]
    overdue = sum(it["status"] == "overdue" for it in pending)
    made = [at for it in pending if (at := commitments._at(it.get("made_at")))]
    recent = [it for it in items if (at := commitments._at(it.get("made_at"))) and now - at <= timedelta(days=WINDOW_DAYS)]
    text = f"Promised to the operator and still open: {len(pending)} (overdue {overdue})"
    if made:
        text += f", oldest made {_days(min(made), now)} ago"
    return Reading(text, _share(len(pending), len({it["id"] for it in pending + recent})))


def _recent(name: str, now: datetime) -> list[dict]:
    from . import metrics
    return metrics._window(metrics.judgments_for_stats(name), now - timedelta(days=WINDOW_DAYS), now)


def _unasked(name: str, now: datetime) -> Reading:
    own = [j for j in _recent(name, now) if j.get("origin") == "self" and isinstance(j.get("anticipated"), bool)]
    if not own:
        return Reading(f"Work I chose myself that anticipated a need: no judged self-chosen cycle in {WINDOW_DAYS} days")
    hit = sum(j["anticipated"] for j in own)
    return Reading(f"Work I chose myself that the judge found anticipated a need: {hit} of {len(own)} judged "
                   f"self-chosen cycles ({WINDOW_DAYS} days)", _share(len(own) - hit, len(own)))


def _claims(name: str, now: datetime) -> Reading:
    from . import metrics
    js = [j for j in _recent(name, now) if metrics.by_current_judge(j) and j.get("judge") != "rule"]
    if not js:
        return Reading(f"Claims the tool log did not support: no judged cycle in {WINDOW_DAYS} days")
    bad = sum(1 for j in js if j.get("claim_supported") is False)
    return Reading(f"Claims the tool log did not support: {bad} of {len(js)} judged cycles ({WINDOW_DAYS} days)",
                   _share(bad, len(js)))


def _corrections(name: str, now: datetime) -> Reading:
    from . import corrections
    items = list(corrections._fold(core.safe_read(core.INSTANCES / name, corrections.FILE, "")).values())
    if not items:
        return Reading("Corrections from the operator: none recorded")
    opened = [it for it in items if it["status"] == "open"]
    since = [at for it in opened if (at := corrections._at(it.get("when")))]
    text = f"Corrections from the operator still open: {len(opened)} of {len(items)}"
    if since:
        text += f", oldest {_days(min(since), now)} old"
    return Reading(text, _share(len(opened), len(items)))


def _value(name: str, now: datetime) -> Reading:
    from . import metrics
    rows = metrics.valued([j for j in metrics.judgments_for_stats(name)
                           if (at := metrics._ts(j["episode_ts"])) and at <= now])[-LAST_VALUED:]
    if not rows:
        return Reading("Judged value of my cycles: none graded yet")
    mean = sum(j["value"] for j in rows) / len(rows)
    delivered = sum(1 for j in rows if j.get("delivery_supported") == "yes")
    return Reading(f"Judged value per cycle, last {len(rows)}: {mean:.1f} of 5 (1 = nobody would miss it); "
                   f"delivered to the operator in {delivered}", _share(5 - mean, 5))


def _recurrence(name: str, now: datetime) -> Reading:
    from . import surprise
    from . import metrics
    rec = surprise._p2([e for e in surprise.events(name) if (at := metrics._ts(str(e.get("episode")))) and at <= now])
    if not rec["classes"]:
        return Reading("Kinds of failed result that came back in a later cycle: no failed result recorded")
    return Reading(f"Kinds of failed result that came back in a later cycle: {rec['recurred']} of {rec['classes']}",
                   _share(rec["recurred"], rec["classes"]))


def _battery(name: str, now: datetime) -> Reading:
    from . import battery, metrics
    try:
        e = battery.era(name)
    except FileNotFoundError:
        return Reading("Capability on fixed tasks: no battery items for me")
    rows = [r for r in battery.results(name) if battery._same_era(r, e) and r.get("status") == "scored"
            and (at := metrics._ts(str(r.get("at", "")))) is not None and at <= now]
    parts, gaps = [], []
    for cat in battery.CATEGORIES:
        mine = battery._per_item(rows, cat, "instance")
        if not mine:
            continue
        score = sum(sum(v[-3:]) / len(v[-3:]) for v in mine.values()) / len(mine)
        control = battery._per_item(rows, cat, "control")
        if control:
            c = sum(sum(v[-3:]) / len(v[-3:]) for v in control.values()) / len(control)
            parts.append(f"{cat} {score:.1f} vs control {c:.1f}")
            gaps.append(max(0.0, c - score) / 10)
        elif cat in ("self", "recall"):       # no control can know my record: the gap is to the full score
            parts.append(f"{cat} {score:.1f} of 10")
            gaps.append((10 - score) / 10)
    if not parts:
        return Reading("Capability on fixed tasks: no scored item in this era yet")
    return Reading("Capability on fixed tasks I never see (0-10; the control is an empty mind on my model): "
                   + ", ".join(parts), max(gaps))


def _unrecorded(name: str, now: datetime) -> Reading:
    from . import unrecorded
    return Reading(*unrecorded.reading(name, now))


def _plan(name: str, now: datetime) -> Reading:
    """How stale the instance's plan is: its open projects, or how long since it last opened or closed one. The plan
    is the instance's own (memory/projects.jsonl); the supervisor only reads it."""
    from . import projects
    items = list(projects._fold(core.safe_read(core.INSTANCES / name, projects.FILE, "")).values())
    opened = [it for it in items if it["status"] == "open"]
    if opened:
        late = sum(1 for it in opened if (at := projects._at(it.get("deadline"))) and at <= projects._at(now.isoformat()))
        return Reading(f"My plan: {len(opened)} open project(s), {late} past its date", _share(late, len(opened)))
    stamps = [at for it in items for key in ("closed_at", "at") if (at := projects._at(it.get(key)))]
    if not stamps:
        return Reading("My plan: no open project; none ever opened", 1.0)
    idle = now - max(stamps).replace(tzinfo=None)
    return Reading(f"My plan: no open project; the last one was opened or closed {_days(now - idle, now)} ago",
                   min(1.0, idle / timedelta(days=WINDOW_DAYS)))


MENU = {   # key: (what it reads, in the menu's words; its reading)
    "promises": ("promises to the operator that are open or overdue", _promises),
    "unasked_used": ("self-chosen work the judge found anticipated a need", _unasked),
    "claims": ("claims the tool log did not support", _claims),
    "corrections": ("the operator's corrections still open", _corrections),
    "value": ("judged value of my recent cycles", _value),
    "recurrence": ("kinds of failed result that came back", _recurrence),
    "battery": ("capability on fixed tasks against an empty-mind control", _battery),
    "unrecorded_orders": ("orders the operator gave for later that I did not record", _unrecorded),
    "plan": ("how stale my plan is: open projects, or days since the last", _plan),
}


def reading(name: str, key: str, now: datetime | None = None) -> Reading:
    """One gauge's reading. A record that cannot be read is said, never hidden and never a reason to drop the line."""
    try:
        return MENU[key][1](name, now or datetime.now())
    except (Exception, SystemExit) as e:   # one unreadable record must not cost the instance its other gauges
        return Reading(f"{MENU[key][0]}: could not be read ({type(e).__name__}: {e})"[:200])


# -- the choice ---------------------------------------------------------------------------------------------------
def path(name: str) -> Path:
    return core.SUP_DIR / "gauges" / f"{name}.jsonl"


def history(name: str, as_of: datetime | None = None, extra=()) -> list[dict]:
    """Every choice made, oldest first; `as_of`: only those made by then; `extra`: a replay's own, never written."""
    p = path(name)
    rows = []
    for line in jsonl_lines(p.read_text()) if p.exists() else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue       # a torn line must not hide the choices around it
        if isinstance(row, dict) and isinstance(row.get("chosen"), list) and isinstance(row.get("ts"), str):
            rows.append(row)
    if as_of is not None:
        cut = as_of.isoformat(timespec="seconds")
        rows = [r for r in rows if r["ts"] <= cut]
    return rows + list(extra)


def choice(name: str, as_of: datetime | None = None, extra=()) -> dict | None:
    """The choice in force ({ts, by, chosen}), or None when none was made or the last one cleared it."""
    rows = history(name, as_of, extra)
    return rows[-1] if rows and rows[-1]["chosen"] else None


def checked(names) -> list[str]:
    """The names as a valid choice: from the menu, no repeats, at most MAX_CHOSEN (an empty list clears the choice)."""
    if not isinstance(names, (list, tuple)) or not all(isinstance(n, str) for n in names):
        raise ValueError("choose is a list of gauge names, e.g. [\"promises\", \"claims\"]; [] shows none")
    names = [n.strip() for n in names]
    unknown = [n for n in names if n not in MENU]
    if unknown:
        raise ValueError(f"no such gauge: {', '.join(unknown)}. The menu: {', '.join(MENU)}")
    if len(set(names)) != len(names):
        raise ValueError("a gauge is chosen once")
    if len(names) > MAX_CHOSEN:
        raise ValueError(f"at most {MAX_CHOSEN} gauges: they are the first lines of the page, kept few on purpose")
    return names


def row(names, by: str, now: datetime | None = None) -> dict:
    if by not in BY:
        raise ValueError(f"by must be one of {', '.join(BY)}")
    return {"ts": (now or datetime.now()).isoformat(timespec="seconds"), "by": by, "chosen": checked(names)}


def choose(name: str, names, by: str, now: datetime | None = None) -> dict:
    """Record a choice (the last row is the one in force). Returns the row written."""
    new = row(names, by, now)
    p = path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(new) + "\n")
    return new


# -- what is shown ------------------------------------------------------------------------------------------------
def head_lines(name: str, now: datetime | None = None, as_of: datetime | None = None, extra=()) -> list[str]:
    """The chosen gauges for the head of self/stats.md, largest gap first; [] until a choice is made."""
    c = choice(name, as_of, extra)
    if not c:
        return []
    known = [k for k in c["chosen"] if k in MENU]   # a gauge since removed from the menu is no longer shown
    now = as_of or now          # a replay reads the gauges as they stood at its cycle's start
    readings = sorted(((reading(name, k, now), i) for i, k in enumerate(known)),
                      key=lambda ri: (-(ri[0].gap if ri[0].gap is not None else -1.0), ri[1]))
    who = "me" if c["by"] == "instance" else "the operator"
    return ([f"## My gauges (chosen by {who} on {c['ts'][:10]}; largest gap first; readings only, the `gauges` tool "
             "changes the choice)"] + [f"- {r.text}" for r, _ in readings] + [""])


def menu_text(name: str, now: datetime | None = None, as_of: datetime | None = None, extra=()) -> str:
    """What the `gauges` tool returns with no argument: every gauge with its reading now, and the choice in force."""
    c = choice(name, as_of, extra)
    now = as_of or now          # a replay reads the gauges as they stood at its cycle's start
    chosen = c["chosen"] if c else []
    lines = ["Gauges: readings the supervisor computes about you each time it writes self/stats.md. Choose up to "
             f"{MAX_CHOSEN} with gauges(choose=[...]) and they become the first lines of that page, largest gap "
             "first. They are readings only: nothing is asked of you and nothing is refused. You cannot set a "
             "reading: each is recomputed from the record (promises, corrections and plan from your own ledgers as "
             "the supervisor re-checks them, the others from the supervisor's own record). You can change the "
             "choice at any time.", ""]
    for key, (what, _) in MENU.items():
        lines.append(f"- {key}{' [chosen]' if key in chosen else ''}: {reading(name, key, now).text}")
    lines += ["", (f"Chosen now: {', '.join(chosen)} (by {'you' if c['by'] == 'instance' else 'the operator'}, "
                   f"{c['ts'][:16].replace('T', ' ')})" if c else "Chosen now: none, so no gauge is shown.")]
    return "\n".join(lines)
