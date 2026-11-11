"""Repeated work across live cycles, as evidence for the instance's own tooling choices.

Bare tool names say nothing on a real record ("run -> write_file" is in every cycle), so each call is named by what
it touched: the script a `run` started, the file a file tool read or wrote. Only what names something specific is
reported: the same call made in many cycles, and sequences of two or more such calls."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from . import commitments, metrics, surprise

MIN_CYCLES = 3
RUNNERS = ("python3", "python", "bash", "sh", "zsh", "uv", "node")
FILE_TOOLS = ("read_file", "write_file", "append_file")


def _arg(call: dict, key: str) -> str:
    args = call.get("args", "")
    if isinstance(args, dict):
        value = args.get(key)
        return value if isinstance(value, str) else ""
    return metrics._arg(args, key) or ""


def signature(call: dict) -> str:
    """The call named by what it touched: run[python3 tools/check.py], read_file[skills/x.md]; a bare tool name when
    the call names nothing that would recur (an inline script, a one-off shell line)."""
    tool = call["tool"]
    if tool == "run":
        words = _arg(call, "command").split()
        for i, word in enumerate(words[:-1]):
            target = words[i + 1]
            if word in RUNNERS and not target.startswith(("-", "<", "'", '"')) and ("/" in target or "." in target):
                return f"run[{word} {target[:80]}]"
        return tool
    if tool in FILE_TOOLS and (path := _arg(call, "path")):
        return f"{tool}[{path[:80]}]"
    return tool


def _specific(sig: str) -> bool:
    return sig.endswith("]")


def repeated_calls(name: str, days: int = 14, as_of: datetime | None = None) -> list[dict]:
    """The same specific call (same script, same file) made in MIN_CYCLES or more live cycles of the window, most
    cycles first: {"call", "cycles", "last"}."""
    cycles = defaultdict(set)
    for ts, sigs in _cycles(name, days, as_of):
        for sig in set(sigs):
            if _specific(sig):
                cycles[sig].add(ts)
    rows = [{"call": sig, "cycles": len(stamps), "last": max(stamps, key=commitments._at)}
            for sig, stamps in cycles.items() if len(stamps) >= MIN_CYCLES]
    return sorted(rows, key=lambda r: (-r["cycles"], r["call"]))


def _cycles(name: str, days: int, as_of: datetime | None):
    """(episode ts, the cycle's successful, non-bookkeeping calls by signature, consecutive repeats collapsed)."""
    end = commitments._local(as_of or datetime.now())
    start = end - timedelta(days=days)
    for ep in metrics.live_episodes(name):
        at = commitments._at(ep.get("ts"))
        if at is None or not start <= at < end:
            continue
        sigs = []
        for call in ep.get("tools") or []:
            if not isinstance(call, dict):
                continue
            tool = call.get("tool")
            if (not isinstance(tool, str) or not tool or tool in commitments.BOOKKEEPING
                    or str(call.get("result") or "").startswith(commitments.FAILED)):
                continue
            sig = signature(call)
            if not sigs or sigs[-1] != sig:
                sigs.append(sig)
        yield ep["ts"], sigs


def sequences(name: str, days: int = 14, as_of: datetime | None = None) -> list[dict]:
    """Sequences of 2 to 4 calls that occur in MIN_CYCLES or more live cycles and include at least two specific calls,
    counted once per cycle; of sequences with the same count the longest is kept."""
    cycles = defaultdict(set)
    for ts, sigs in _cycles(name, days, as_of):
        for size in range(2, 5):
            for i in range(len(sigs) - size + 1):
                seq = tuple(sigs[i:i + size])
                if len(set(seq)) > 1 and sum(map(_specific, seq)) >= 2:
                    cycles[seq].add(ts)
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
    calls = repeated_calls(name, as_of=end)[:max(0, limit)]
    lines = ["The same call I made in many cycles (last 14 days; a call is named by the script it ran or the file it "
             "touched)"]
    lines += [f"{r['call']}: in {r['cycles']} cycles, last {r['last'][:16]}" for r in calls]
    lines += ["", "Tool sequences I repeated across cycles (last 14 days)"]
    lines += [f"{' -> '.join(r['tools'])}: in {r['cycles']} cycles, last {r['last'][:16]}" for r in rows]
    lines += ["", "Kinds of failed call that came back"]
    lines += [f"{kind}: in {len(stamps)} cycles, last {', '.join(stamps[-3:])}" for kind, stamps in failures]
    if not calls and not rows and not failures:
        lines.append("(nothing repeated yet)")
    lines += ["", "A call or sequence repeated by hand is a candidate for a plugin or a scheduled job; this is "
              "information only."]
    return "\n".join(lines)
