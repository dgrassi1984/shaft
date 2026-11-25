"""Report resemblance chains among sampled, judged cycles without changing their work."""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from . import metrics

REGULAR_MIN_CYCLES = 5     # A round minimum avoids calling a short coincidence a schedule.
REGULAR_GAP_SPREAD = 0.5   # Deliberately loose: ordinary scheduler jitter should still count.


def _minute(ts: str) -> str:
    return ts[:16].replace("T", " ")


def _changes(name: str, groups: list[dict], as_of: datetime | None) -> None:
    """Attribute each kept change to the family of the cycle it was made in (by the cycle's run window, so a change
    made in an unjudged cycle belongs to no family), and count it survived only when its probation passed."""
    changes = metrics.change_outcomes(name, as_of)
    if not changes or not groups:
        return
    owners = {ts: group for group in groups for ts in group["members"]}
    windows = []
    previous = None
    episodes = [ep for ep in metrics.live_episodes(name) if metrics._ts(ep.get("ts")) is not None]
    for ep in sorted(episodes, key=lambda ep: metrics._ts(ep["ts"])):
        end = metrics._ts(ep["ts"])
        start = metrics._ts(ep["run_started"]) if ep.get("run_started") else previous
        if ep.get("run_started") and start is None:
            continue
        windows.append((start, end + timedelta(minutes=2), owners.get(ep["ts"])))
        previous = end
    for change in changes:
        when = metrics._ts(change.get("ts"))
        if when is None:
            continue
        for start, end, group in windows:
            if start is not None and start < when <= end:
                if group is not None:
                    group["changes_kept"] += 1
                    group["changes_survived"] += change["outcome"] == "passed"
                break


def families(name: str, as_of: datetime | None = None) -> list[dict]:
    """Union judged resemblance links; keep singletons and exclude cycles at or after as_of."""
    rows = sorted((r for r in metrics.judgments(name)
                   if (at := metrics._ts(r.get("episode_ts"))) is not None
                   and ("judged_at" not in r or metrics._ts(r["judged_at"]) is not None)
                   and (as_of is None or at < as_of)),
                  key=lambda r: metrics._ts(r["episode_ts"]))
    parents = list(range(len(rows)))

    def root(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    earlier, minutes = {}, {}
    for i, row in enumerate(rows):
        resembles = (row.get("resembles") or "").replace("T", " ")
        if len(resembles) == 16:
            candidates = minutes.get(resembles, [])
            target = candidates[0] if len(candidates) == 1 else None
        else:
            target = earlier.get(resembles)
        if target is not None and metrics._ts(rows[target]["episode_ts"]) < metrics._ts(row["episode_ts"]):
            parents[root(i)] = root(target)
        earlier[row["episode_ts"].replace("T", " ")] = i
        minutes.setdefault(_minute(row["episode_ts"]), []).append(i)
    chains = {}
    for i, row in enumerate(rows):
        chains.setdefault(root(i), []).append(row)
    groups = []
    for members in chains.values():
        stamps = [r["episode_ts"] for r in members]
        valued = [r for r in members if type(r.get("value")) is int]
        deliveries = [r["episode_ts"] for r in members if r.get("delivery_supported") == "yes"]
        new = [r["episode_ts"] for r in members if (r.get("novelty") or 0) >= 3]
        times = [metrics._ts(ts) for ts in stamps]
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
        interval = median(gaps) if len(members) >= REGULAR_MIN_CYCLES else None
        groups.append({
            "id": stamps[0][:16], "goal": members[-1].get("goal", "")[:120],
            "cycles": len(members), "first": stamps[0], "latest": stamps[-1],
            "mean_value": sum(r["value"] for r in valued) / len(valued) if valued else None,
            "deliveries": len(deliveries), "last_delivery": deliveries[-1] if deliveries else None,
            "used": sum(bool((r.get("use") or {}).get("used")) for r in members),
            "last_new": new[-1] if new else None,
            "capability": sum(r.get("value_for") in ("own_capability", "own_record") for r in valued)
                          > len(valued) / 2,
            "changes_kept": 0, "changes_survived": 0, "interval_minutes": interval,
            "regular": interval is not None and interval > 0
                       and max(gaps) - min(gaps) <= REGULAR_GAP_SPREAD * interval,
            "members": stamps,
        })
    _changes(name, groups, as_of)
    return sorted(groups, key=lambda g: (g["cycles"], metrics._ts(g["latest"])), reverse=True)


def no_yield_share(name: str, since: datetime, until: datetime | None = None) -> dict:
    """Count windowed judged cycles in repeating families without yield.

    Capability families must lack passed probation, deliveries and later use.
    Other families must lack deliveries and have a known mean value below 2.
    """
    judged = no_yield = 0
    for group in families(name, as_of=until):
        count = sum(since <= metrics._ts(ts) for ts in group["members"])
        judged += count
        empty = group["deliveries"] == 0 and (
            group["changes_survived"] == 0 and group["used"] == 0 if group["capability"] else
            group["mean_value"] is not None and group["mean_value"] < 2)
        if group["cycles"] >= 2 and empty:
            no_yield += count
    return {"judged": judged, "in_no_yield_families": no_yield, "share": no_yield / judged if judged else None}


def _description(group: dict) -> str:
    text = f"{group['goal']} ({group['cycles']} cycles; {group['first']} to {group['latest']})"
    if group["capability"]:
        text += (f"; read on its own yardstick: {group['changes_kept']} changes kept / "
                 f"{group['changes_survived']} survived probation; work used later: {group['used']}; "
                 f"deliveries: {group['deliveries']}; last delivery: {group['last_delivery'] or 'none'}")
    else:
        value = "unknown" if group["mean_value"] is None else f"{group['mean_value']:.1f}"
        text += (f"; mean value: {value}; deliveries: {group['deliveries']}; "
                 f"last delivery: {group['last_delivery'] or 'none'}; "
                 f"last time something new came of it: {group['last_new'] or 'none'}")
    if group["regular"]:
        minutes = group["interval_minutes"]
        interval = f"{minutes / 60:g} h" if minutes >= 60 else f"{minutes:g} min"
        text += (f"; recurs about every {interval} ({group['cycles']} cycles): "
                 "a scheduled job could run this without a model")
    return text


def record_text(name: str, limit: int = 20, as_of: datetime | None = None) -> str:
    """Describe repeated families and state that only sampled, judged cycles are included."""
    groups = families(name, as_of=as_of)
    total = sum(g["cycles"] for g in groups)
    header = (f"Goal families over my {total} judged cycles (the judge samples cycles; unjudged ones are not here). "
              "A family is the chain of goals the judge said resemble each other.")
    repeated = [g for g in groups if g["cycles"] >= 2]
    blocks = [_description(g) for g in repeated[:max(0, limit)]]
    if not repeated:
        blocks.append("(no family of two or more cycles yet)")
    return "\n\n".join([header, *blocks])


def stats_lines(name: str) -> list[str]:
    """Show the top three repeating families and any other regular families on their own yardsticks."""
    groups = [g for g in families(name) if g["cycles"] >= 2]
    if not groups:
        return []
    shown = groups[:3] + [g for g in groups[3:] if g["regular"]]
    return ["## My goal families (top 3 by cycles)", *["- " + _description(g) for g in shown], ""]
