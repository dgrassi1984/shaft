"""Night replay (#5): in the night window, the day's worst judged cycles are replayed with the judge's first error shown.

For each of up to CAP cycles of the last 24 hours whose judge located a first error (`first_error_step` > 0; a step
of 0 means no step was at fault, so there is nothing to show), failed first, then partial, then lowest value: the
cycle is replayed from the state it began in, on the instance's own code and model, with one addition: a note from the
supervisor in its inbox naming the earlier run's first error. The judge that graded the original then compares the
original run and the replay in both orders (trial.judge_pair, trial.decide; the replay is B), each from its own cycle
record (no file diffs: nothing ties a branch commit to the cycle that made it). The instance reads the comparison through
`record` kind `night_replays`; what it makes of it is its own decision.

Operator decision 2026-11-24: only instances on a cloud model (a replay never takes the local model's time, so an
instance on the local or a self-hosted model would replay on another model and a "win" could be the model's); no
control arm. Replays follow the trial rules: nothing is settled, committed, delivered or sent (ReplayBroker).

Spending cap per night: COST_FACTOR × what the supervisor booked for the picked originals (tokens, and dollars where
the model is priced), checked before each replay and every 30 s during one (a replay over it is stopped, unjudged);
the judges' cost is added to what the night spent. Recorded with every row and shown by `record`.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import core

CAP = 3                 # replays per instance per night (operator, 2026-11-24)
COST_FACTOR = 4         # the night's spending cap: this many times what the picked originals cost. The first real
#                         replay (2026-11-25) booked 3.3× its original (22 calls against 18: it did the work the
#                         original skipped), so 2× would stop exactly the replays worth having.
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
    """Inside the night window and not yet started (or recorded as having nothing to run) tonight: a run writes its
    first row before any paid call, so a worker killed mid-replay is not started again the same night."""
    from . import battery
    start, end = battery.window(now)
    if not start <= now < end:
        return False
    night = night_of(now)
    return not any(r.get("night") == night for r in rows(name))


def picks(name: str, now: datetime) -> list[dict]:
    """The judged cycles of the last 24 h with a located first error, not replayed before, worst first."""
    from . import metrics
    done = {r.get("episode_ts") for r in rows(name) if r.get("kind") in ("replay", "started")}
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


LATE_USAGE_S = 120   # a plan channel books a turn's usage when the turn ends (a late_usage row), after the reply


def original_cost(name: str, ep: dict) -> dict:
    """Tokens and dollars the supervisor booked for the instance while the cycle ran (start to end, plus late usage
    rows): the same record a replay's own cost is read from (trial._cloud_tokens), so the cap compares like with
    like. The instance's own call log counts differently (cached prompt tokens), and read 237k where the supervisor
    booked 789k for its replay (2026-11-25)."""
    from . import cloudllm
    from .pushes import _time
    end, start = _time(ep.get("ts")), _time(ep.get("run_started"))
    out = {"tokens": 0, "usd": 0.0}
    if not end or not start:
        return out
    end += timedelta(seconds=LATE_USAGE_S)
    for line in core.jsonl_lines(cloudllm.USAGE.read_text()) if cloudllm.USAGE.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        at = _time(r.get("ts")) if isinstance(r, dict) and r.get("instance") == name else None
        if at and start <= at <= end:
            out["tokens"] += int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
            out["usd"] += float(r.get("usd") or 0.0)
    return out


class Meter:
    """What a replay has booked so far under its `billed` label, read incrementally from the supervisor's usage file
    (it grows by megabytes a day; only what was appended since the night began is read)."""

    def __init__(self, billed: str):
        from . import cloudllm
        self.billed, self.path = billed, cloudllm.USAGE
        self.offset = self.path.stat().st_size if self.path.exists() else 0
        self.tokens, self.usd, self.checked = 0, 0.0, 0.0

    def read(self) -> dict:
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except FileNotFoundError:
            chunk = b""
        whole = chunk[:chunk.rfind(b"\n") + 1]
        self.offset += len(whole)
        for line in whole.decode(errors="replace").split("\n"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("instance") == self.billed:
                self.tokens += int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
                self.usd += float(r.get("usd") or 0.0)
        return {"tokens": self.tokens, "usd": round(self.usd, 6)}

    def over(self, spent: dict, cap: dict, every_s: float = 30.0) -> bool:
        """Whether the night's spending with this replay's so far reaches the cap; read at most every `every_s`."""
        if time.monotonic() - self.checked < every_s:
            return False
        self.checked = time.monotonic()
        now = self.read()
        return (spent["tokens"] + now["tokens"] >= cap["tokens"]
                or bool(cap["usd"]) and spent["usd"] + now["usd"] >= cap["usd"])


def hint_text(judgment: dict) -> str:
    """The error comes first: an inbox preview shows only the head of a message."""
    return (f"Earlier attempt's first error (step {judgment['first_error_step']}): "
            f"{' '.join(str(judgment['first_error']).split())}\n\n"
            "A note from the supervisor: you worked on this cycle once before, from this same starting point, and "
            "the judge of that earlier run located the error above. Nothing else from that run is shown to you.\n")


# Why each comparison says what it says, in the night replay's words (trial.decide speaks of a proposed change).
WHY_WORDS = (("the proposed version's", "the replay's"), ("the current version's", "the original's"),
             ("the proposed version", "the replay"), ("the current version", "the original"),
             ("the proposed one", "the replay"), ("the current one", "the original"),
             ("proposed version", "replay"), ("current version", "original"), ("current one", "original"))


def _words(why: str) -> str:
    for old, new in WHY_WORDS:
        why = why.replace(old, new)
    return why


def judge_model(name: str, judgment: dict, ep: dict) -> str:
    """The judge that graded the original when it is a model, so the comparison is that same judge's; else one from
    the rotation that is not the instance's own model."""
    from . import judge, metrics
    by = judgment.get("judge")
    if isinstance(by, str) and by and by != "rule":
        return by
    return judge.pick(metrics.own_models(name, ep))


def _judge(name: str, state: dict, original: dict, replay: dict, purpose: str, model: str,
           stop=lambda: False) -> tuple[str, str, list]:
    from . import trial
    context = trial.state_context(core.PROJECT, name, state)
    judgments = []
    for first, second in ((original, replay), (replay, original)):
        if stop():
            return "unjudged", "the night window closed before the comparison", judgments
        try:
            judgments.append(trial.judge_pair(context, first, second, purpose, model=model))
        except Exception as e:   # one failed judgment leaves the comparison unjudged, never takes the night down
            core.log(f"{name}: night replay judgment failed ({type(e).__name__}: {e})")
            judgments.append(None)
    outcome, why = trial.decide({"A": original, "B": replay}, judgments)
    return outcome, _words(why), judgments


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
    stamp_now = lambda: datetime.now().isoformat(timespec="seconds")
    started_tonight = sum(r.get("kind") == "started" for r in rows(name) if r.get("night") == night)
    chosen = chosen[:max(0, CAP - started_tonight)]   # a second run the same night never exceeds CAP in all
    if not chosen:
        why = f"tonight's {CAP} replays have already started"
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
          + (f", ${cap['usd']:.2f}" if cap["usd"] else "") + f" ({COST_FACTOR} × what the originals cost)")
    for (j, ep, state), cost in zip(planned, costs):
        print(f"  {j['episode_ts']} criterion {j.get('criterion_met')} value {j.get('value')} cost {cost['tokens']} "
              f"tokens; first error at step {j['first_error_step']}: {' '.join(str(j['first_error']).split())[:160]}"
              + ("" if state else " — its starting state cannot be rebuilt"))
    if dry_run:
        return 0
    if not cap["tokens"]:
        _append(name, {**base, "kind": "none", "why": "not run: the supervisor booked no model usage for the "
                       "picked cycles, so the night has no spending cap to hold to"})
        return 0
    spent = {"tokens": 0, "usd": 0.0}
    done = 0
    closed = lambda: stop() or bool(until and time.time() >= until)
    for i, (j, ep, state) in enumerate(planned):
        row = {"night": night, "kind": "replay", "episode_ts": j["episode_ts"], "criterion": j.get("criterion_met"),
               "value": j.get("value"), "first_error_step": j["first_error_step"], "first_error": j["first_error"],
               "model": model, "cap": cap}
        if closed():
            _append(name, {**row, "at": stamp_now(), "outcome": "not run", "why": "the night window closed"})
            break
        if spent["tokens"] >= cap["tokens"] or (cap["usd"] and spent["usd"] >= cap["usd"]):
            _append(name, {**row, "at": stamp_now(), "outcome": "not run",
                           "why": f"the night's cap was reached ({spent['tokens']} of {cap['tokens']} tokens)"})
            break
        if not state:
            _append(name, {**row, "at": stamp_now(), "outcome": "not run",
                           "why": "the cycle's starting state cannot be rebuilt"})
            continue
        code = trial.committed_code(core.PROJECT, name, state["start_commit"])
        stamp = datetime.fromisoformat(state["t0"]) - timedelta(seconds=1)

        def prepare(dest: Path, j=j, stamp=stamp) -> None:
            core.safe_write(dest, f"inbox/{stamp:%Y%m%d-%H%M%S-%f}-{HINT_FILE}", hint_text(j))
        billed = f"{name}@night-replay:{night}:{started_tonight + i}"
        meter = Meter(billed)
        judge_by = judge_model(name, j, ep)
        # Written before any paid call: a worker killed during the replay is not started again tonight, and this
        # cycle is never picked again.
        _append(name, {"night": night, "at": stamp_now(), "kind": "started", "episode_ts": j["episode_ts"]})
        try:
            # The instance's configured cloud model, named explicitly: it wins over whatever the cycle's own
            # config.env said then (trial._replay_cycle), so a cycle from before a switch off the local model never
            # reaches the local model or a self-hosted one.
            rec = trial.replay_arm(core.PROJECT, name, state, code, model=model, cache={},
                                   cache_lock=threading.Lock(), billed=billed, prepare=prepare,
                                   should_stop=lambda meter=meter: closed() or meter.over(spent, cap))
        except trial.TrialError as e:
            _append(name, {**row, "at": stamp_now(), "outcome": "not run", "why": f"replay refused: {e}"})
            continue
        booked = meter.read()
        tokens = booked["tokens"] or int(rec["usage"].get("prompt_tokens") or 0) + int(
            rec["usage"].get("completion_tokens") or 0)
        spent = {"tokens": spent["tokens"] + tokens, "usd": round(spent["usd"] + max(booked["usd"], rec["usd"]), 6)}
        # File changes are shown for neither run: nothing ties a commit on the instance's branch to the cycle that
        # made it (the next cycle's commit can fall in the same minutes), and an unfair diff for one side biases the
        # judge. Both runs are judged on their own cycle record: intent, claim, every tool call and its result.
        hidden = "(not shown for either run)"
        original = {"episode": ep, "trace": [], "diff": hidden, "exit": 0,
                    "ended": "ok" if not ep.get("llm_error") else "its model call failed",
                    "completed": bool((ep.get("intent") or {}).get("goal")) and not ep.get("llm_error")}
        replay = {**rec, "trace": [], "diff": hidden}
        purpose = f"night-replay:{name}:{night}:{started_tonight + i}"
        if rec.get("ended") == "preempted":
            outcome, why, judgments = "unjudged", ("the replay was stopped: the night's cap was reached"
                                                   if not closed() else "the replay was cut short by the night window"), []
        else:
            outcome, why, judgments = _judge(name, state, original, replay, purpose, judge_by, stop=closed)
        judge_usd = round(trial._judge_usd(purpose), 6)
        spent["usd"] = round(spent["usd"] + judge_usd, 6)
        reasons = [(jd or {}).get("reasoning", "") for jd in judgments]
        _append(name, {**row, "at": stamp_now(), "outcome": outcome, "why": why, "completed": rec["completed"],
                       "ended": rec["ended"], "seconds": rec["seconds"], "usd": rec["usd"], "tokens": tokens,
                       "judge": judge_by, "judge_usd": judge_usd, "spent": spent,
                       "replay_goal": ((rec.get("episode") or {}).get("intent") or {}).get("goal"),
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
        if r.get("kind") == "started":
            continue
        if r.get("kind") == "none":
            out.append(f"- night {r.get('night')}: {r.get('why')}")
            continue
        line = (f"- night {r.get('night')}, cycle {str(r.get('episode_ts', ''))[:16].replace('T', ' ')} "
                f"(criterion {r.get('criterion')}, value {r.get('value')}), replayed on {r.get('model')} with the "
                f"judge's first error shown (step {r.get('first_error_step')}: {r.get('first_error')}): "
                f"{OUTCOME_TEXT.get(r.get('outcome'), r.get('outcome'))} — {r.get('why')}")
        if isinstance(r.get("cap"), dict) and isinstance(r.get("spent"), dict):
            line += (f"\n  spent so far tonight {r['spent'].get('tokens')} of {r['cap'].get('tokens')} tokens"
                     + (f", ${r['spent'].get('usd', 0):.2f}" if r["spent"].get("usd") else ""))
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
