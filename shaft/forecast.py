"""Resource forecasts before I choose what to work on (docs/plans/PLAN-consciousness.md, M9/D1).

My prompt shows the budget I have left, never what a goal of this kind has usually cost me. When I declare an
intent, the comparable past cycles say what that kind of goal took: how many turns, minutes and supervisor calls
they used, and how many of them ran out of turns or time. Seeing that before I commit lets me size the goal; the
fixed reserve rule is the control, and it is kept if it does as well.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import memory, surprises

MODES = ("on", "reserve", "off")
RESERVE = "Keep at least 2 turns and 15 minutes for checking your work and calling finish_cycle."
COMPARABLE = 10
MIN_COMPARABLE = 3
RAN_OUT_SHARE = 0.3
CONTROLS = ("declare_intent", "confirm_confidence", "settle_commitment", "finish_cycle")


def _at(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _scored(root: Path, goal: str, now: datetime) -> list[tuple[float, datetime, dict]]:
    """My live episodes that ended before `now` and declared an intent, with the word overlap of their goal with
    `goal` (Jaccard over memory._tokens), when there is any. A malformed episode is skipped, never an error."""
    goal_words = set(memory._tokens(goal))
    out: list[tuple[float, datetime, dict]] = []
    for ep in surprises.episodes(root):
        if not isinstance(ep, dict) or ep.get("kind") != "live":
            continue
        intent = ep.get("intent")
        if not isinstance(intent, dict):
            continue
        past_goal = intent.get("goal")
        if not isinstance(past_goal, str) or not past_goal.strip():
            continue
        at = _at(ep.get("ts"))
        if at is None or at >= now:
            continue
        overlap = _jaccard(goal_words, set(memory._tokens(past_goal)))
        if overlap > 0:
            out.append((overlap, at, ep))
    return out


def comparable(root: Path, goal: str, origin: str, now: datetime, k: int = COMPARABLE) -> list[dict]:
    """My live episodes that ended before `now` and declared an intent, ranked by the word overlap of their goal
    with `goal`, the same origin breaking ties, then newest; the top `k` with any overlap."""
    scored = [(overlap, ((ep.get("intent") or {}).get("origin") or "") == origin, at, ep)
              for overlap, at, ep in _scored(root, goal, now)]
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [t[3] for t in scored[:k]]


# One earlier goal shown on its own (a judged value on the goal slate, a first error at declare_intent) must be
# similar, not merely overlapping: at least a fifth of the words shared. The medians above take any overlap, as ten
# of them average a weak match out; one shown alone does not.
SIMILAR = 0.2
FIRST_ERROR_CHARS = 300


def judged_similar(root: Path, goal: str, now: datetime) -> list[tuple[dict, dict]]:
    """My earlier live goals at least SIMILAR to `goal` that the judge graded, newest first, each with its verdict as
    my sleeps received it (memory/verdicts.jsonl: the latest row of each cycle)."""
    verdicts = {row["episode_ts"]: row for row in memory.verdict_rows(root) if not row.get("left_out")}
    hits = sorted(((at, ep) for overlap, at, ep in _scored(root, goal, now)
                   if overlap >= SIMILAR and ep.get("ts") in verdicts), key=lambda t: t[0], reverse=True)
    return [(ep, verdicts[ep["ts"]]) for _, ep in hits]


def first_error(root: Path, goal: str, now: datetime) -> str:
    """At most two lines for declare_intent (review G5): where the judge found the newest similar goal that was not
    met first went wrong (its first_error). Never how often such goals were met: see text() below."""
    for ep, row in judged_similar(root, goal, now):
        error = " ".join(str(row.get("first_error") or "").split())
        if row.get("criterion_met") in ("no", "partial") and error:
            goal_then = " ".join(str(ep["intent"]["goal"]).split())
            return (f"The judge's first error on my newest similar goal that fell short ({str(ep['ts'])[:16]}: "
                    f"{goal_then[:120]}{'…' if len(goal_then) > 120 else ''}):\n"
                    f"{error[:FIRST_ERROR_CHARS]}{'…' if len(error) > FIRST_ERROR_CHARS else ''}")
    return ""


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    half = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[half]
    return (ordered[half - 1] + ordered[half]) / 2


def summarise(eps: list[dict], root: Path) -> dict:
    """What a set of comparable cycles usually costs: the median turns, minutes and supervisor calls (their tool
    log minus the controls), how many ran out of turns or time, and how many of their judge's verdicts found the
    criterion met. Ran out: no finish (whatever ended the loop — steps, repeating), or a forced deadline wrap-up. A
    last-step wrap-up that still finished did not run out, nor did a yield: counting it put Thelos's cut-offs at 23%
    where its judge attributed 5% (2026-10-31)."""
    turns: list[float] = []
    minutes: list[float] = []
    calls: list[float] = []
    ran_out = 0
    for ep in eps:
        steps = ep.get("steps")
        if isinstance(steps, int) and not isinstance(steps, bool):
            turns.append(steps)
        ts, at = _at(ep.get("ts")), _at((ep.get("intent") or {}).get("at"))
        if ts is not None and at is not None and ts >= at:
            minutes.append((ts - at).total_seconds() / 60)
        tools = ep.get("tools")
        if isinstance(tools, list):
            calls.append(sum(1 for e in tools if isinstance(e, dict) and e.get("tool") not in CONTROLS))
        if not ep.get("finish") or ep.get("wrapped_up") == "deadline":
            ran_out += 1
    verdicts = {row["episode_ts"]: row for row in memory.verdict_rows(root)}
    met = judged = 0
    for ep in eps:
        row = verdicts.get(ep.get("ts"))
        if row is not None and "criterion_met" in row:
            judged += 1
            if row.get("criterion_met") == "yes":
                met += 1
    return {"n": len(eps), "turns": _median(turns), "minutes": _median(minutes),
            "tool_calls": _median(calls), "ran_out": ran_out, "met": met, "judged": judged}


def _join(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def text(mode: str, root: Path, goal: str, origin: str, now: datetime, *, turns_left: int | None = None,
         minutes_left: float | None = None, calls_left: int | None = None) -> str:
    """What declare_intent shows: what comparable past cycles cost, against what this cycle has left. `off` (or an
    unknown mode) shows nothing; `reserve` shows the fixed rule."""
    if mode not in MODES or mode == "off":
        return ""
    if mode == "reserve":
        return RESERVE
    eps = comparable(root, goal, origin, now)
    if len(eps) < MIN_COMPARABLE:   # nothing to say yet: no line at every declare of a young instance
        return ""
    s = summarise(eps, root)
    medians = [f"{round(v)} {unit}" for v, unit in ((s["turns"], "turns"), (s["minutes"], "minutes"),
                                                     (s["tool_calls"], "tool calls")) if v is not None]
    left = []
    if turns_left is not None:
        left.append(f"{round(turns_left)} turns")
    if minutes_left is not None:
        left.append(f"about {round(minutes_left)} minutes")
    if calls_left is not None:
        left.append(f"{round(calls_left)} supervisor calls")
    out = (f"Forecast from my {s['n']} most comparable past cycles: "
           + (f"median {', '.join(medians)}; " if medians else "")
           + f"{s['ran_out']} of {s['n']} ran out of turns or time.")
    # Costs only, never how often those goals were met: this sits in declare_intent's result, which the v3 confirm
    # step reads — a success rate here would be a second base rate, outside the paired test that measures the first.
    if left:
        out += f" This cycle has {_join(left)} left."
    warn = s["ran_out"] >= RAN_OUT_SHARE * s["n"]   # a share, not a count: 3 of 4 matters more than 3 of 10
    if turns_left is not None and s["turns"] is not None and s["turns"] > 0.8 * turns_left:
        warn = True
    if minutes_left is not None and s["minutes"] is not None and s["minutes"] > 0.8 * minutes_left:
        warn = True
    if warn:
        out += " Size the goal so a checkable piece is done well within that."
    return out
