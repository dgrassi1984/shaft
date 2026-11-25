"""Self-improvement graded on use (#15, the operator's decision of 2026-11-20).

The judge caps a cycle whose value is for the instance's own capability at 3 unless the same cycle delivered that work
to the operator — and a new plugin loads only from the next run, so it rarely could. That grade stays as the
provisional one. Seven days after the cycle it is graded again, without a model, on what became of it:

- kept: the files the cycle built are still there — code it wrote passed the gate (a code_changes row of its run) and
  was not reverted by probation; any other file it wrote still exists;
- used: a later cycle or conversation within the seven days called the plugin, ran or read a file the cycle built
  (its path, or its module name, in the call), or a scheduled job ran the plugin; a file in notes/docs/ is delivered
  where the operator reads it; the agent's own runtime code and config.env run in every later cycle.

Kept and used: the judge's grade without the cap (value_uncapped). Not kept, or not used: 1, the scale's "nothing
anyone would miss". Nothing to trace (no file written): the provisional grade stands. Only verdicts that carry
value_uncapped are re-graded: an older one could only fall (its uncapped grade was never asked), which would lower the
record before the change and flatter every comparison across it. Both grades are kept: the provisional in the
judgments file, the re-grade in use_regrade/<name>.jsonl; metrics.judgments() shows the re-graded value and keeps the
other as value_provisional.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import metrics
from .core import TEMPLATE, UnsafePath, jsonl_lines, safe_read

# The supervisor's folders are read from metrics at each call (metrics.SUP_DIR, metrics.INSTANCES): the records this
# module reads and writes sit beside the judgments metrics keeps, wherever those are.

WINDOW = timedelta(days=7)
UNUSED_VALUE = 1         # the scale's 1: nothing anyone would miss — seven days and nobody used it
BENEFICIARY = "own_capability"
NOT_TRACED = ("memory/", "self/", "tests/", "inbox/", ".tmp/")   # bookkeeping, tests and scratch: never the capability
CODE_ROOTS = ("shaft", "plugins", "config.env")
SETTLE_S = 900           # a run's code is settled after its episode is written: the gate, then the commit (stage limit 300)
WRITES = ("write_file", "append_file")   # naming a file in what is written is not using it


def path(name: str, sup: Path | None = None) -> Path:
    # its own folder: judgments/<name>-use.jsonl would be the judgments file of an instance named "<name>-use"
    return (sup or metrics.SUP_DIR) / "use_regrade" / f"{name}.jsonl"


def rows(name: str, sup: Path | None = None) -> dict[str, dict]:
    """The re-grades by episode_ts (the first one recorded for a cycle wins: it is graded once)."""
    p, out = path(name, sup), {}
    for line in jsonl_lines(p.read_text()) if p.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("episode_ts"), str) and type(r.get("value")) is int:
            out.setdefault(r["episode_ts"], r)
    return out


def _at(stamp) -> datetime | None:
    """A naive local time, or None: an offset-aware stamp is converted, so no comparison mixes the two."""
    try:
        at = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def _clean(rel: str) -> str:
    return rel.strip().removeprefix("./")


def _traced(rel: str) -> bool:
    rel = _clean(rel)
    return bool(rel) and not rel.startswith("/") and ".." not in Path(rel).parts and not rel.startswith(NOT_TRACED)


def _code(rel: str) -> bool:
    return Path(rel).parts[0] in CODE_ROOTS


def _written(ep: dict) -> set[str]:
    """Files the cycle wrote with a file tool, successfully."""
    out = set()
    for t in _tools(ep):
        if t.get("tool") in WRITES and not str(t.get("result", "")).startswith("ERROR"):
            rel = metrics._arg(str(t.get("args") or ""), "path")
            if _traced(rel):
                out.add(_clean(rel))
    return out


def _tools(ep: dict) -> list[dict]:
    tools = ep.get("tools")
    return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []


def _run_changes(ep: dict, next_start: datetime | None, changes: list[dict]) -> list[dict]:
    """The kept code changes of this cycle's run. An episode's ts is when it ended; its run started at run_started.
    A change settled from its start (a pause for a conversation settles what is written so far) to SETTLE_S after its
    end, and before the next live run started. A change row names no run, so this is a window, never a guess
    beyond it."""
    end = _at(ep.get("ts"))
    start = _at(ep.get("run_started")) or end
    if end is None:
        return []
    stop = end + timedelta(seconds=SETTLE_S)
    if next_start is not None:
        stop = min(stop, next_start)
    return [c for c in changes if (at := _at(c.get("ts"))) and start <= at <= stop]


def _mentions(rel: str) -> re.Pattern:
    """What a call that uses this file carries in its arguments, as a whole name: plugins/example never matches
    plugins/example-other."""
    parts = Path(rel).parts
    out = [rel]
    if parts[0] == "shaft" and rel.endswith(".py"):
        mod = rel[:-3]
        out += [mod.replace("/", "."), mod]
    if parts[0] == "plugins" and len(parts) > 1:
        out.append("plugins/" + parts[1])
    return re.compile(r"(?<![\w./-])(?:" + "|".join(map(re.escape, out)) + r")(?![\w-])")


def _runs_every_cycle(rel: str) -> bool:
    """The agent's own runtime (a module the template ships) and config.env run in every cycle; a module the instance
    added runs only when something calls it."""
    return rel == "config.env" or (rel.startswith("shaft/") and (TEMPLATE / rel).is_file())


def _job_runs(name: str, start: datetime, end: datetime) -> set[str]:
    p = metrics.SUP_DIR / "jobs" / f"{name}.jsonl"
    out = set()
    for line in jsonl_lines(p.read_text()) if p.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        # a job that ran and exited 0: one skipped or refused before launch, timed out or failed is no use
        if (isinstance(r, dict) and isinstance(r.get("plugin"), str) and r.get("exit") == 0 and not r.get("timed_out")
                and not r.get("refused") and not r.get("skipped") and (at := _at(r.get("ts"))) and start < at <= end):
            out.add(r["plugin"])
    return out


def trace(name: str, ep: dict, later: list[dict], next_ts: datetime | None, changes: list[dict],
          outcomes: dict[str, str], jobs: set[str]) -> dict:
    """{artifacts, kept, used, basis} for one cycle. `later`: the cycles and conversations in its window, itself
    excluded; `outcomes`: each code change's probation outcome by its ts; `jobs`: the plugins a job ran in the window."""
    run = _run_changes(ep, next_ts, changes)
    gated = {_clean(f) for c in run for f in c.get("files") or [] if isinstance(f, str) and _traced(f)}
    reverted = {_clean(f) for c in run if outcomes.get(c.get("ts")) == "reverted" for f in c.get("files") or []
                if isinstance(f, str)}
    artifacts = sorted(_written(ep) | gated)
    if not artifacts:
        return {"artifacts": [], "kept": [], "used": [], "basis": "nothing to trace: no file written"}
    kept = []
    for rel in artifacts:
        try:   # still there, never read through a link (core.safe_read)
            safe_read(metrics.INSTANCES / name, rel, errors="replace")
            there = True
        except (OSError, ValueError, UnsafePath):   # missing, a link, or too big to read (TooBig: a ValueError)
            there = False
        if there and (not _code(rel) or (rel in gated and rel not in reverted)):
            kept.append(rel)
    used = []
    for rel in kept:
        plugin = Path(rel).parts[1] if Path(rel).parts[0] == "plugins" and len(Path(rel).parts) > 2 else None
        if rel.startswith("notes/docs/"):
            used.append(f"{rel}: delivered as a web document")
        elif _runs_every_cycle(rel) and later:
            used.append(f"{rel}: runs in every cycle ({len(later)} since)")
        elif plugin and plugin in jobs:
            used.append(f"{rel}: a job ran {plugin}")
        else:
            named = _mentions(rel)
            for other in later:
                hit = next((t for t in _tools(other) if not str(t.get("result", "")).startswith("ERROR")
                            and ((plugin and t.get("tool") == plugin)
                                 or (t.get("tool") not in WRITES and named.search(str(t.get("args") or ""))))), None)
                if hit:
                    used.append(f"{rel}: {hit.get('tool')} in {other.get('kind')} {str(other.get('ts'))[:16]}")
                    break
    basis = ("kept and used" if used else "kept, not used within 7 days" if kept else "not kept")
    return {"artifacts": artifacts, "kept": kept, "used": used, "basis": basis}


def regraded_value(row: dict, traced: dict) -> int:
    provisional = row["value"]
    if not traced["artifacts"]:
        return provisional
    if traced["used"]:
        uncapped = row["value_uncapped"]
        return max(provisional, uncapped) if 0 <= uncapped <= 5 else provisional
    return min(provisional, UNUSED_VALUE)


def run(name: str, now: datetime | None = None) -> int:
    """Re-grade every own_capability cycle whose seven days have passed and that has no re-grade yet. Model-free;
    returns how many were recorded."""
    now = now or datetime.now()
    done = rows(name)
    due = [j for j in metrics.judgments(name) if j.get("value_for") == BENEFICIARY and type(j.get("value")) is int
           and type(j.get("value_uncapped")) is int and j["episode_ts"] not in done and (at := _at(j["episode_ts"])) and at + WINDOW <= now]
    if not due:
        return 0
    episodes = [ep for ep in metrics._episode_records(name) if ep.get("kind") in ("live", "talk") and _at(ep.get("ts"))]
    episodes.sort(key=lambda ep: _at(ep["ts"]))
    by_ts = {ep["ts"]: ep for ep in episodes if ep.get("kind") == "live"}
    lives = [ep for ep in episodes if ep.get("kind") == "live"]
    cp = metrics.SUP_DIR / "code_changes" / f"{name}.jsonl"
    changes = []
    for line in jsonl_lines(cp.read_text()) if cp.exists() else []:
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if isinstance(c, dict) and isinstance(c.get("ts"), str):
            changes.append(c)
    outcomes = {c["ts"]: c["outcome"] for c in metrics.change_outcomes(name)}
    written = 0
    for j in due:
        ep = by_ts.get(j["episode_ts"])
        if ep is None:
            continue   # its episode is gone (a deleted log): nothing to trace, and never guessed
        try:   # one malformed record skips its own cycle (logged), never the others
            start = _at(ep["ts"])
            end = start + WINDOW
            nxt = next(((_at(o.get("run_started")) or _at(o["ts"])) for o in lives if _at(o["ts"]) > start), None)
            later = [o for o in episodes if o is not ep and start < _at(o["ts"]) <= end]
            traced = trace(name, ep, later, nxt, changes, outcomes, _job_runs(name, start, end))
        except Exception as e:
            metrics.log(f"{name}: use re-grade of {j['episode_ts']} skipped: {type(e).__name__}: {e}")
            continue
        rec = {"episode_ts": j["episode_ts"], "regraded_at": now.isoformat(timespec="seconds"),
               "provisional": j.get("value_provisional", j["value"]), "value": regraded_value(j, traced), **traced}
        p = path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        written += 1
    return written
