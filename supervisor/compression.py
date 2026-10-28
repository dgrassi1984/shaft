"""#113: offline two-part compression and exact held-out fact recall.

Sleep commits and their parents supply facts/digests, never today's rewritten files.
The archive is checked for recall only. Missing history is not reconstructed from
current memory. Commit time bounds the unseen window conservatively: a live run
must start after the snapshot and end within 24 hours. Legacy runs without a start
are excluded. Logged (possibly historically truncated) tool payloads are all we score.
"""
from __future__ import annotations

import json
import re
import zlib
from datetime import datetime, timedelta, timezone
from statistics import median

from . import metrics, mind


def _time(value: str) -> datetime:
    when = datetime.fromisoformat(value)
    return when.astimezone(timezone.utc)


def _size(data: bytes, memory: bytes = b"") -> int:
    compressor = zlib.compressobj(zdict=memory[-32768:]) if memory else zlib.compressobj()
    return len(compressor.compress(data) + compressor.flush())


def score(before: str, after: str, logs: bytes) -> dict:
    """#113: positive delta means fewer bits after sleep, including the full memory cost."""
    old, new = before.encode(), after.encode()
    baseline = _size(logs)
    old_cost = _size(old) + _size(logs, old) - baseline
    new_cost = _size(new) + _size(logs, new) - baseline
    return {"delta_bits": 8 * (old_cost - new_cost),
            "conditional_gain_bits": 8 * (_size(logs, old) - _size(logs, new))}


def probes(facts: str) -> list[str]:
    """#113: deterministic exact fact-text probes, without dates or provenance tags."""
    out = []
    for line in facts.splitlines():
        if not line.startswith("- "):
            continue
        text = re.sub(r"^\[(\d{4}-\d{2}-\d{2})[^\]]*\]\s*", "", line[2:])
        text = re.sub(r" \(src: .+\)$", "", text).strip()
        if text and text not in out:
            out.append(text)
    return out


def assay(name: str, since: str = "") -> list[dict]:
    """#113: read sleep snapshots and live episodes; never init, backfill or write a store."""
    cutoff = _time(since) if since else None
    if not mind.exists(name):
        raise mind.MindError(f"{name}: no mind history; cannot reconstruct sleep memory")
    history = mind.log(name, "--first-parent", "--reverse", "--format=%H%x09%P%x09%cI%x09%s", mind.BRANCH)
    episodes = metrics.live_episodes(name)
    rows = []
    for line in history.splitlines():
        rev, parents, stamp, subject = line.split("\t", 3)
        match = re.fullmatch(re.escape(name) + r": sleep (\d{4}-\d{2}-\d{2}) \d{2}:\d{2}", subject)
        if not match:
            continue
        when = _time(stamp)
        if cutoff and when < cutoff:
            continue
        parent = parents.split()[0] if parents else None
        # A parentless snapshot cannot tell us what the sleep started with.
        if parent is None:
            rows.append({"date": stamp, "verdict": "missing pre-sleep history"})
            continue
        def read(commit, path):
            return mind.show(name, commit, f"memory/{path}") or ""

        if when + timedelta(hours=24) > datetime.now(timezone.utc):
            rows.append({"date": stamp, "verdict": "held-out day incomplete"})
            continue
        before_facts = read(parent, "facts.md")
        after_facts = read(rev, "facts.md")
        before = before_facts + "\n" + read(parent, "digests.md")
        after = after_facts + "\n" + read(rev, "digests.md")
        tools = []
        for ep in episodes:
            try:
                start, end = _time(ep["run_started"]), _time(ep["ts"])
            except (KeyError, ValueError, TypeError):
                continue
            if when < start <= end <= when + timedelta(hours=24):
                tools.extend({k: call.get(k) for k in ("tool", "args", "result")} for call in ep.get("tools", []))
        if not tools:
            rows.append({"date": stamp, "verdict": "no held-out tool logs"})
            continue
        logs = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        result = score(before, after, logs)
        held_out = probes(before_facts)
        searchable = after + "\n" + read(rev, "facts-archive.md")
        recall = sum(p in searchable for p in held_out) / len(held_out) if held_out else None
        gain = result["delta_bits"] > 0
        verdict = "forgetting, not progress" if gain and recall is not None and recall < 1 else (
            "gain" if gain and recall == 1 else "gain; recall untested" if gain else "no gain")
        rows.append({"date": stamp, **result, "recall": recall, "probes": len(held_out), "verdict": verdict})
    return rows


def report(name: str, since: str = "") -> str:
    """#113: a read-only table; positive delta is a two-part compression gain."""
    rows = assay(name, since)
    if not rows:
        return f"{name}: no sleeps in the recorded history for this window."
    lines = [f"{name}: speculative offline assay; positive Δbits = fewer bits after sleep",
             "sleep date                 Δbits two-part  recall kept  verdict"]
    for row in rows:
        delta = str(row["delta_bits"]) if "delta_bits" in row else "n/a"
        recall = f"{row['recall']:.0%} ({row['probes']})" if row.get("recall") is not None else "n/a"
        lines.append(f"{row['date']:25}  {delta:>14}  {recall:>11}  {row['verdict']}")
    values = [row["delta_bits"] for row in rows if "delta_bits" in row]
    lines.append(f"median Δbits two-part: {median(values):g} ({len(values)} sleeps)" if values else
                 "median Δbits two-part: n/a (no scored sleeps)")
    return "\n".join(lines)
