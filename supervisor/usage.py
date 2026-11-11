"""Use of instance-owned plugins and skills, counted from live tool logs rather than job runs."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from . import commitments, core, metrics


def _ledger(name: str, folder: str, end: datetime) -> list[dict]:
    """Read complete ledger rows visible before the replay boundary, ignoring torn lines."""
    text = core.safe_read(core.SUP_DIR, f"{folder}/{name}.jsonl", "", errors="replace")
    rows = []
    for line in core.jsonl_lines(text):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and (at := commitments._at(row.get("ts"))) is not None and at < end:
            rows.append(row)
    return rows


def _history(name: str, end: datetime) -> tuple[list[dict], dict]:
    episodes = [ep for ep in metrics.live_episodes(name)
                if (at := commitments._at(ep.get("ts"))) is not None and at < end]
    values = {}
    for row in metrics.judgments(name):
        at = commitments._at(row.get("episode_ts"))
        judged = commitments._at(row.get("judged_at"))
        if at is not None and at < end and (judged is None or judged < end):
            values[row["episode_ts"]] = row.get("value")
    return episodes, values


def _counts(calls: list[tuple[str, dict]], values: dict, last_key: str) -> dict:
    stamps = {ts for ts, _ in calls}
    judged = [values[ts] for ts in stamps if isinstance(values.get(ts), (int, float))]
    return {"cycles": len(stamps), last_key: max(stamps, key=commitments._at) if stamps else None,
            "mean_value": sum(judged) / len(judged) if judged else None}


def _sort(row: dict) -> tuple:
    last = row.get("last_call", row.get("last_read"))
    at = commitments._at(last or row.get("first_seen")) or datetime.min
    return (last is not None, at, row.get("plugin", row.get("file", "")))


def plugins(name: str, as_of: datetime | None = None) -> list[dict]:
    """Report current plugin manifests and their use in live cycles visible before as_of."""
    end = commitments._local(as_of or datetime.now())
    root = core.INSTANCES / name
    episodes, values = _history(name, end)
    changes = _ledger(name, "code_changes", end)
    rows = []
    for plugin in core.list_beneath(root, "plugins"):
        try:
            spec = json.loads(core.safe_read(root, f"plugins/{plugin}/plugin.json", errors="replace"))
        except (OSError, core.UnsafePath, ValueError):
            continue
        if not isinstance(spec, dict):
            continue
        tool = spec.get("name") or plugin
        seen = [r["ts"] for r in changes if any(p.startswith(f"plugins/{plugin}/") for p in r.get("files", []))]
        calls = [(ep["ts"], c) for ep in episodes for c in ep.get("tools") or [] if c.get("tool") == tool]
        rows.append({"plugin": plugin, "tool": tool,
                     "first_seen": min(seen, key=commitments._at) if seen else None, "calls": len(calls),
                     "failed": sum(not isinstance(c.get("result"), str) or c["result"].startswith(commitments.FAILED)
                                   for _, c in calls),
                     "scheduled": isinstance(spec.get("schedule"), dict), **_counts(calls, values, "last_call")})
    return sorted(rows, key=_sort)


def _skill_files(root) -> list[str]:
    """Walk skills without following links, including files in nested folders."""
    pending, files = ["skills"], []
    while pending:
        folder = pending.pop()
        for entry in core.list_beneath(root, folder):
            rel = f"{folder}/{entry}"
            try:
                fd = core.dir_beneath(root, PurePosixPath(rel).parts)
            except (OSError, core.UnsafePath):
                try:
                    core.safe_read(root, rel, errors="replace")
                except (OSError, core.UnsafePath, ValueError):
                    continue
                files.append(rel)
            else:
                os.close(fd)
                pending.append(rel)
    return files


def skills(name: str, as_of: datetime | None = None) -> list[dict]:
    """Count successful read_file calls for each current skill file, once per cycle for judged value."""
    end = commitments._local(as_of or datetime.now())
    episodes, values = _history(name, end)
    reads = {}
    for ep in episodes:
        for call in ep.get("tools") or []:
            if (call.get("tool") != "read_file" or not isinstance(call.get("result"), str)
                    or call["result"].startswith(commitments.FAILED)):
                continue
            args = call.get("args", "")
            path = args.get("path", "") if isinstance(args, dict) else metrics._arg(args, "path")
            if not isinstance(path, str) or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
                continue
            reads.setdefault(str(PurePosixPath(path)), []).append((ep["ts"], call))
    rows = []
    for file in _skill_files(core.INSTANCES / name):
        calls = reads.get(file, [])
        rows.append({"file": file, "reads": len(calls), **_counts(calls, values, "last_read")})
    return sorted(rows, key=_sort)


def record_text(name: str, limit: int = 40, as_of: datetime | None = None) -> str:
    """Describe usage and make clear that scheduled runs do not count as cycle calls."""
    end = as_of or datetime.now()
    lines = ["Calls are counted from my cycles' tool logs.", "", "My plugins"]
    mine = plugins(name, end)[:max(0, limit)]
    for row in mine:
        built = row["first_seen"][:10] if row["first_seen"] else "unknown date"
        detail = _detail(row, "calls", "last_call") if row["calls"] else "never called since"
        line = f"{row['plugin']} (tool {row['tool']}): built {built}, {detail}"
        if row["scheduled"]:
            line += " · runs as a scheduled job (job runs are not cycle calls)"
        lines.append(line)
    if not mine:
        lines.append("(no plugins)")
    lines += ["", "My skill files"]
    files = skills(name, end)[:max(0, limit)]
    lines += [f"{r['file']}: {_detail(r, 'reads', 'last_read') if r['reads'] else 'never read'}" for r in files]
    if not files:
        lines.append("(no skill files)")
    return "\n".join(lines)


def _detail(row: dict, count: str, last: str) -> str:
    value = f"{row['mean_value']:.1f}" if row["mean_value"] is not None else "not judged"
    stamp = row[last][:16].replace("T", " ")
    return f"{row[count]} {count} in {row['cycles']} cycles, last {stamp}, mean value of those cycles {value}"


def kept_changes_line(name: str, days: int = 14, now: datetime | None = None) -> str | None:
    """One line for self/stats.md: the kept changes of the period, how many a trial tested first, and what probation
    made of them (metrics.change_outcomes: the supervisor's own record of each change)."""
    end = commitments._local(now or datetime.now())
    start = end - timedelta(days=days)
    changes = [c for c in metrics.change_outcomes(name, end) if (at := commitments._at(c["ts"])) and at >= start]
    if not changes:
        return None
    count = lambda outcome: sum(c["outcome"] == outcome for c in changes)   # noqa: E731
    return (f"Kept changes to my code in the last {days} days: {len(changes)}, of which tested by a trial first: "
            f"{sum(bool(c['tested']) for c in changes)}; probation: passed {count('passed')}, reverted "
            f"{count('reverted')}, still running {count('on probation')}, too little use to judge "
            f"{count('kept, not judged')}")


def kept_lines(name: str) -> list[str]:
    """kept_changes_line as a stats section: [] when nothing was kept in the period."""
    line = kept_changes_line(name)
    return ["## My code changes, tested and kept", f"- {line}",
            "- record(kind='usage') shows which plugins and skill files I built are used; record(kind='habits') what I "
            "keep doing by hand", ""] if line else []
