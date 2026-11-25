"""Night replay (#5): in the night window, the day's worst judged cycles are replayed with the judge's first error shown.

For each of up to CAP cycles of the last 24 hours whose judge located a first error (`first_error_step` > 0; a step
of 0 means no step was at fault, so there is nothing to show), failed first, then partial, then lowest value: the
cycle is replayed from the state it began in, on the instance's own code and model, with one addition: a note from the
supervisor in its inbox naming the earlier run's first error. The trial judge then compares the original run and the
replay in both orders (trial.judge_pair, trial.decide; the replay is B). The instance reads the comparison through
`record` kind `night_replays`; what it makes of it is its own decision.

Operator decision 2026-11-24: only instances on a cloud model (a replay never takes the local model's time, so an
instance on the local or a self-hosted model would replay on another model and a "win" could be the model's); no
control arm. Replays follow the trial rules: nothing is settled, committed, delivered or sent (ReplayBroker).

Spending cap per night: COST_FACTOR × what the picked originals cost (tokens from the instance's own call log, and
dollars where the model is priced), checked before each replay and recorded with every row.
"""
from __future__ import annotations

import json
import random
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import core

CAP = 3                 # replays per instance per night (operator, 2026-11-24)
COST_FACTOR = 2         # the night's spending cap: this many times what the picked originals cost
CRITERION_ORDER = {"no": 0, "partial": 1}
HINT_FILE = "from-supervisor-replay-hint.md"


def path(name: str) -> Path:
    return core.SUP_DIR / "night-replay" / f"{name}.jsonl"


def rows(name: str) -> list[dict]:
    try:
        lines = core.jsonl_lines(path(name).read_text())
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _append(name: str, row: dict) -> None:
    p = path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def night_of(now: datetime) -> str:
    """The night a moment belongs to: the date its window opened (battery.window opens after midnight)."""
    from . import battery
    start, _ = battery.window(now)
    return (start if now >= start else start - timedelta(days=1)).date().isoformat()


def eligible(name: str) -> str:
    """'' when the instance runs on a cloud model, else why not."""
    from . import selfhosted, trial
    cfg = trial._config(core.PROJECT, name)
    model = cfg.get("LLM_MODEL", "")
    if cfg.get("LLM_BASE_URL", "") != "broker":
        return "not eligible: runs on the local model"
    if not model:
        return "not eligible: no LLM_MODEL configured"
    if selfhosted.serves(model):
        return f"not eligible: runs on a self-hosted model ({model})"
    return ""


def due(name: str, now: datetime) -> bool:
    """Inside the night window and not yet run (or recorded as having nothing to run) tonight."""
    from . import battery
    start, end = battery.window(now)
    if not start <= now < end:
        return False
    night = night_of(now)
    return not any(r.get("night") == night for r in rows(name))


def picks(name: str, now: datetime) -> list[dict]:
    """The judged cycles of the last 24 h with a located first error, not replayed before, worst first."""
    from . import metrics
    done = {r.get("episode_ts") for r in rows(name) if r.get("kind") == "replay"}
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    found = []
    for j in metrics.judgments(name):
        ts, step = j.get("episode_ts"), j.get("first_error_step")
        if (isinstance(ts, str) and since <= ts <= now.isoformat(timespec="seconds") and ts not in done
                and type(step) is int and step > 0 and str(j.get("first_error") or "").strip()):
            found.append(j)
    value = lambda j: j["value"] if type(j.get("value")) is int else 9
    found.sort(key=lambda j: (CRITERION_ORDER.get(j.get("criterion_met"), 2), value(j), j["episode_ts"]))
    return found[:CAP]


def _episode(name: str, ts: str) -> dict | None:
    from . import metrics
    return next((ep for ep in metrics.live_episodes(name) if ep.get("ts") == ts), None)


def original_cost(name: str, ep: dict) -> dict:
    """Tokens (and dollars where priced) of the cycle's own model calls, from its call log: start to end."""
    from . import cloudllm, metrics
    from .pushes import _time
    end, start = _time(ep.get("ts")), _time(ep.get("run_started"))
    out = {"tokens": 0, "usd": 0.0}
    if not end or not start:
        return out
    for line in core.jsonl_lines(metrics._instance_text(name, "logs/llm_usage.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        at = _time(r.get("ts")) if isinstance(r, dict) else None
        if at and start <= at <= end and r.get("purpose") == "live":
            out["tokens"] += int(r.get("prompt_tokens") or 0) + int(r.get("completion_tokens") or 0)
            out["usd"] += cloudllm.usd(r.get("model"), r) or 0.0
    return out


def hint_text(judgment: dict) -> str:
    return ("A note from the supervisor.\n\n"
            "You worked on this cycle once before, from this same starting point. The judge of that earlier run "
            f"located its first error at step {judgment['first_error_step']} of its tool log:\n\n"
            f"> {' '.join(str(judgment['first_error']).split())}\n\n"
            "Nothing else from that run is shown to you.\n")


def _after_state(name: str, ep: dict) -> dict | None:
    """The instance as it stood just after the original cycle: the first commit on its branch within ten minutes of
    the cycle's end (the run commits when it ends)."""
    from . import trial
    end = datetime.fromisoformat(ep["ts"])
    r = trial._git(core.PROJECT, "log", "--first-parent", "--reverse", "--format=%H %cI",
                   f"--after={end - timedelta(seconds=1):%Y-%m-%dT%H:%M:%S}",
                   f"--before={end + timedelta(minutes=10):%Y-%m-%dT%H:%M:%S}", trial.branch(name), check=False)
    first = (r.stdout or "").split("\n")[0].split()
    if len(first) != 2:
        return None
    at = datetime.fromisoformat(first[1])
    at = at.astimezone().replace(tzinfo=None) if at.tzinfo else at
    return {"ts": ep["ts"], "t0": (at + timedelta(seconds=1)).isoformat(timespec="seconds"),
            "start_commit": first[0], "inbox": []}


def original_diff(name: str, state: dict, ep: dict, code: dict) -> str | None:
    """What the original cycle changed in the instance's files, computed like a replay's diff; None when the state
    after it cannot be rebuilt."""
    from . import trial
    after = _after_state(name, ep)
    if not after:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="shaft-nightreplay-")).resolve()
    try:
        before = trial._files_now(trial.reconstruct(core.PROJECT, name, state, code, tmp / "before"))
        then = trial._files_now(trial.reconstruct(core.PROJECT, name, after,
                                                  trial.committed_code(core.PROJECT, name, after["start_commit"]),
                                                  tmp / "after"))
        return trial.file_diff(before, then)
    except trial.TrialError:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _judge(name: str, state: dict, original: dict, replay: dict, purpose: str) -> tuple[str, str, list]:
    from . import trial
    context = trial.state_context(core.PROJECT, name, state)
    judgments = []
    for first, second in ((original, replay), (replay, original)):
        try:
            judgments.append(trial.judge_pair(context, first, second, purpose))
        except Exception as e:   # one failed judgment leaves the comparison unjudged, never takes the night down
            core.log(f"{name}: night replay judgment failed ({type(e).__name__}: {e})")
            judgments.append(None)
    outcome, why = trial.decide({"A": original, "B": replay}, judgments)
    return outcome, why, judgments


def run(name: str, until: float | None = None, dry_run: bool = False, now: datetime | None = None,
        stop=lambda: False) -> int:
    """Tonight's replays for `name`. Returns how many were replayed. Every outcome, including none, is recorded."""
    from . import trial
    now = now or datetime.now()
    night = night_of(now)
    base = {"night": night, "at": now.isoformat(timespec="seconds")}
    if why := eligible(name):
        if not dry_run:
            _append(name, {**base, "kind": "none", "why": why})
        print(f"{name}: {why}")
        return 0
    chosen = picks(name, now)
    if not chosen:
        why = "nothing qualified: no judged cycle of the last 24 h with a located first error"
        if not dry_run:
            _append(name, {**base, "kind": "none", "why": why})
        print(f"{name}: {why}")
        return 0
    starts = trial._starts(name)
    planned = []
    for j in chosen:
        ep = _episode(name, j["episode_ts"])
        state = trial.state_of(core.PROJECT, name, ep, starts) if ep else None
        planned.append((j, ep, state))
    costs = [original_cost(name, ep) if ep else {"tokens": 0, "usd": 0.0} for _, ep, _ in planned]
    cap = {"tokens": COST_FACTOR * sum(c["tokens"] for c in costs),
           "usd": round(COST_FACTOR * sum(c["usd"] for c in costs), 4)}
    model = trial._config(core.PROJECT, name).get("LLM_MODEL", "")
    print(f"{name}: night {night}, {len(planned)} cycle(s) to replay on {model}; cap {cap['tokens']} tokens"
          + (f", ${cap['usd']:.2f}" if cap["usd"] else ""))
    for j, ep, state in planned:
        print(f"  {j['episode_ts']} criterion {j.get('criterion_met')} value {j.get('value')} "
              f"first error at step {j['first_error_step']}: {' '.join(str(j['first_error']).split())[:160]}"
              + ("" if state else " — its starting state cannot be rebuilt"))
    if dry_run:
        return 0
    spent = {"tokens": 0, "usd": 0.0}
    done = 0
    for i, (j, ep, state) in enumerate(planned):
        row = {**base, "kind": "replay", "episode_ts": j["episode_ts"], "criterion": j.get("criterion_met"),
               "value": j.get("value"), "first_error_step": j["first_error_step"], "first_error": j["first_error"],
               "model": model, "cap": cap}
        if until and time.time() >= until or stop():
            _append(name, {**row, "outcome": "not run", "why": "the night window closed"})
            break
        if spent["tokens"] >= cap["tokens"] > 0 or (cap["usd"] and spent["usd"] >= cap["usd"]):
            _append(name, {**row, "outcome": "not run", "why": f"the night's cap was reached ({spent})"})
            break
        if not state:
            _append(name, {**row, "outcome": "not run", "why": "the cycle's starting state cannot be rebuilt"})
            continue
        code = trial.committed_code(core.PROJECT, name, state["start_commit"])
        stamp = datetime.fromisoformat(state["t0"]) - timedelta(seconds=1)

        def prepare(dest: Path, j=j, stamp=stamp) -> None:
            core.safe_write(dest, f"inbox/{stamp:%Y%m%d-%H%M%S-%f}-{HINT_FILE}", hint_text(j))
        billed = f"{name}@night-replay:{night}:{i}"
        try:
            rec = trial.replay_arm(core.PROJECT, name, state, code, model="local", cache={},
                                   cache_lock=threading.Lock(), billed=billed, prepare=prepare,
                                   should_stop=lambda: stop() or bool(until and time.time() >= until))
        except trial.TrialError as e:
            _append(name, {**row, "outcome": "not run", "why": f"replay refused: {e}"})
            continue
        tokens = int(rec["usage"].get("prompt_tokens") or 0) + int(rec["usage"].get("completion_tokens") or 0)
        spent = {"tokens": spent["tokens"] + tokens, "usd": round(spent["usd"] + rec["usd"], 6)}
        diff = original_diff(name, state, ep, code)
        hidden = "(not shown for either run: the original's file changes could not be rebuilt)"
        original = {"episode": ep, "trace": [], "diff": diff if diff is not None else hidden, "exit": 0,
                    "ended": "ok", "completed": True}
        replay = {**rec, "trace": [], "diff": rec["diff"] if diff is not None else hidden}
        if rec.get("ended") == "preempted":
            outcome, why, judgments = "unjudged", "the replay was cut short by the night window", []
        else:
            outcome, why, judgments = _judge(name, state, original, replay, f"night-replay:{name}:{night}:{i}")
        reasons = [(jd or {}).get("reasoning", "") for jd in judgments]
        _append(name, {**row, "outcome": outcome, "why": why, "completed": rec["completed"],
                       "ended": rec["ended"], "seconds": rec["seconds"], "usd": rec["usd"], "tokens": tokens,
                       "spent": spent, "replay_goal": ((rec.get("episode") or {}).get("intent") or {}).get("goal"),
                       "judges": reasons})
        print(f"  {j['episode_ts']}: {outcome} — {why}")
        done += 1
    return done


def record_failure(name: str, error: str, now: datetime | None = None) -> None:
    now = now or datetime.now()
    _append(name, {"night": night_of(now), "at": now.isoformat(timespec="seconds"), "kind": "none",
                   "why": f"the night replay failed: {error}"})


OUTCOME_TEXT = {"win": "the replay did better", "loss": "the original did better", "tie": "equally effective",
                "split": "the two judge orders disagreed", "unjudged": "not judged", "not run": "not run"}


def record_text(name: str, limit: int = 20, as_of: datetime | None = None) -> str:
    """What `record` kind night_replays shows: each night's replays, newest last."""
    cut = as_of.isoformat(timespec="seconds") if as_of else None
    shown = [r for r in rows(name) if not cut or str(r.get("at", "")) <= cut][-max(1, limit):]
    out = []
    for r in shown:
        if r.get("kind") == "none":
            out.append(f"- night {r.get('night')}: {r.get('why')}")
            continue
        line = (f"- night {r.get('night')}, cycle {str(r.get('episode_ts', ''))[:16].replace('T', ' ')} "
                f"(criterion {r.get('criterion')}, value {r.get('value')}), replayed on {r.get('model')} with the "
                f"judge's first error shown (step {r.get('first_error_step')}: {r.get('first_error')}): "
                f"{OUTCOME_TEXT.get(r.get('outcome'), r.get('outcome'))} — {r.get('why')}")
        if r.get("replay_goal"):
            line += f"\n  the replay's goal: {r['replay_goal']}"
        for reason in [x for x in r.get("judges") or [] if x][:1]:
            line += f"\n  judge: {reason}"
        out.append(line)
    return "\n".join(out)


def stats_lines(name: str) -> list[str]:
    """The last night's replays, one line, for the review."""
    rs = rows(name)
    if not rs:
        return []
    night = rs[-1].get("night")
    tonight = [r for r in rs if r.get("night") == night]
    if all(r.get("kind") == "none" for r in tonight):
        return [f"night replay {night}: {tonight[-1].get('why')}"]
    reps = [r for r in tonight if r.get("kind") == "replay"]
    counts = {}
    for r in reps:
        counts[r.get("outcome")] = counts.get(r.get("outcome"), 0) + 1
    return [f"night replay {night}: {len(reps)} cycle(s) with the first error shown — "
            + ", ".join(f"{k} {v}" for k, v in counts.items())]
