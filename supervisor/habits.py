"""Repeated work across live cycles, as evidence for the instance's own tooling choices."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from . import commitments, metrics, surprise

MIN_CYCLES = 3


def sequences(name: str, days: int = 14, as_of: datetime | None = None) -> list[dict]:
    """Count contiguous tool sequences once per live cycle, keeping the longest equal-count patterns."""
    end = commitments._local(as_of or datetime.now())
    start = end - timedelta(days=days)
    cycles = defaultdict(set)
    for ep in metrics.live_episodes(name):
        at = commitments._at(ep.get("ts"))
        if at is None or not start <= at < end:
            continue
        tools = []
        for call in ep.get("tools") or []:
            tool = call.get("tool")
            if not tool or tool in commitments.BOOKKEEPING or (call.get("result") or "").startswith(commitments.FAILED):
                continue
            if not tools or tools[-1] != tool:
                tools.append(tool)
        for size in range(2, 5):
            for i in range(len(tools) - size + 1):
                seq = tuple(tools[i:i + size])
                if len(set(seq)) > 1:
                    cycles[seq].add(ep["ts"])
    repeated = {seq: stamps for seq, stamps in cycles.items() if len(stamps) >= MIN_CYCLES}
    rows = []
    for seq, stamps in repeated.items():
        if any(len(other) > len(seq) and len(others) == len(stamps)
               and any(other[i:i + len(seq)] == seq for i in range(len(other) - len(seq) + 1))
               for other, others in repeated.items()):
            continue
        rows.append({"tools": list(seq), "cycles": len(stamps), "last": max(stamps, key=commitments._at)})
    return sorted(rows, key=lambda r: (-r["cycles"], -len(r["tools"]), r["tools"]))


def record_text(name: str, limit: int = 20, as_of: datetime | None = None) -> str:
    """Describe repeated work and recurring failure classes without prescribing a change."""
    end = commitments._local(as_of or datetime.now())
    rows = sequences(name, as_of=end)[:max(0, limit)]
    events = [dict(e, **{"class": surprise.p2_class(e)}) for e in surprise.events(name)
              if e.get("episode_kind") in ("live", "talk")
              and (at := commitments._at(e.get("episode"))) is not None and at < end]
    recurring = surprise.recurrence(events)["recurring"]
    failures = sorted(recurring.items(), key=lambda item: (-len(item[1]), item[0]))[:max(0, limit)]
    lines = ["Tool sequences I repeated across cycles (last 14 days)"]
    lines += [f"{' -> '.join(r['tools'])}: in {r['cycles']} cycles, last {r['last'][:16]}" for r in rows]
    lines += ["", "Kinds of failed call that came back"]
    lines += [f"{kind}: {', '.join(stamps)}" for kind, stamps in failures]
    if not rows and not failures:
        lines.append("(nothing repeated yet)")
    lines += ["", "A repeated sequence is a candidate for a plugin or a scheduled job; this is information only."]
    return "\n".join(lines)
