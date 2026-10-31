"""Model bake-offs: which cloud model should an instance run on? (docs/plans/REVIEW-thelos-model-2026-10-30.md)

A trial compares two versions of an instance's code on one model; a bake-off compares models on one version of its
code, its committed one. For each chosen past live cycle it rebuilds the state the cycle began from and replays the
cycle once per model (trial.replay_arm: scratch copy, ReplayBroker — nothing settled, committed, delivered or sent),
the baseline first so that its web answers are shared with the rest (one cache per state), then the candidates
(--arm-parallel at a time).
A judge who is not told which model ran which compares each candidate with the baseline (the first model) in both
orders (trial.judge_pair, trial.decide), effectiveness first; each candidate's verdict is the trials' sign test.

A model is "id" or "id@effort" (LLM_REASONING_EFFORT for that arm, one of the instance's own choices). Only cloud
models the broker allows (SHAFT_CLOUD_MODELS): a bake-off never takes an instance's time on the local model. Every
replay is billed as "<name>@bakeoff:<id>:…", under the default per-name cap, and logged to the trials' ledger.
Results are saved as they come (.supervisor/bakeoffs/<name>-<id>.json); the same id again resumes, with the code
frozen at creation (code_rev). A state whose arms did not all finish is replayed again in full on resume (its shared
web answers are not kept on disk), and a pair the judge failed on is judged again. One process at a time (a lock).

The judge can also be Claude Code subagents instead of the API (no API cost): replay with --replay-only, export the
blind cases (export_cases: model names redacted, both orders as separate cases), have one subagent judge each case
into verdicts/<case>.json, then import_verdicts decides each pair as the API judge's would. Past bake-offs, what
they found and what to reuse: docs/bakeoffs/README.md.
"""
from __future__ import annotations

import fcntl
import json
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import trial
from .core import INSTANCES, PROJECT, SUP_DIR, cloud_model, log

DIR = SUP_DIR / "bakeoffs"
DEFAULT_JUDGE = "claude-sonnet-5-5"   # not a candidate's: Haiku shares its family (said in the report)
SUBAGENT = " (Claude Code subagent)"   # suffix of a judge that answered through import_verdicts, not the API


class BakeoffError(Exception):
    pass


def parse_arm(spec: str, efforts: tuple[str, ...] | None) -> dict:
    """'claude-haiku-5-5@xhigh' -> {'label', 'model', 'env'}."""
    from .cloudllm import CLOUD_MODELS
    model, _, effort = spec.partition("@")
    try:
        model = cloud_model(model)
    except ValueError as e:
        raise BakeoffError(f"{spec}: {e}") from None
    if model not in CLOUD_MODELS:
        raise BakeoffError(f"{model} is not allowed for instances (SHAFT_CLOUD_MODELS={','.join(CLOUD_MODELS)})")
    if effort and efforts is not None and effort not in efforts:
        raise BakeoffError(f"{spec}: the instance's code accepts LLM_REASONING_EFFORT {', '.join(efforts)}, "
                           f"not {effort!r}")
    return {"label": spec, "model": model, "env": {"LLM_REASONING_EFFORT": effort} if effort else {}}


def path(name: str, bid: str) -> Path:
    return DIR / f"{name}-{bid}.json"


def _save(b: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path(b["name"], b["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(b, ensure_ascii=False, indent=1))
    tmp.replace(path(b["name"], b["id"]))


def create(name: str, arms: list[str], episodes: list[str], judge: str = DEFAULT_JUDGE,
           bid: str = "", project: Path = PROJECT) -> dict:
    """A new bake-off, or the saved one with this id (to resume: its arms, states and judge are kept)."""
    from .opconfig import effort_choices
    if bid and path(name, bid).exists():
        b = json.loads(path(name, bid).read_text())
        if (arms and arms != [a["label"] for a in b["arms"]]) or (episodes and episodes != [s["ts"] for s in b["states"]]):
            raise BakeoffError(f"bake-off {bid} has other arms or cycles; give only --id to resume it")
        return b
    if not bid:   # a new id, never an existing one (two launches in the same second)
        bid = datetime.now().strftime("b-%Y%m%d-%H%M%S")
        bid = next(f"{bid}{s}" for s in ("", *(f"-{i}" for i in range(2, 100))) if not path(name, f"{bid}{s}").exists())
    if len(arms) < 2:
        raise BakeoffError("a bake-off needs a baseline and at least one candidate")
    parsed = [parse_arm(a, effort_choices(INSTANCES / name)) for a in arms]
    if len({a["label"] for a in parsed}) != len(parsed):
        raise BakeoffError("the same arm twice")
    judge = cloud_model(judge)
    if any(a["model"] == judge for a in parsed):
        raise BakeoffError(f"the judge {judge} is also a candidate: a model never judges its own runs")
    eps = {e["ts"]: e for e in trial.live_episodes(INSTANCES / name)}
    starts = trial._starts(name)
    states = []
    for ts in episodes:
        if ts not in eps:
            raise BakeoffError(f"no live episode of {name} ended at {ts}")
        st = trial.state_of(project, name, eps[ts], starts)
        if not st:
            raise BakeoffError(f"cannot find when the cycle that ended {ts} started")
        states.append({**st, "arms": {}, "outcomes": {}})
    code_rev = trial._git(project, "rev-parse", trial.branch(name)).stdout.strip()   # every arm, every resume
    b = {"id": bid, "name": name, "created": datetime.now().isoformat(timespec="seconds"), "judge": judge,
         "code_rev": code_rev, "arms": parsed, "states": states}
    _save(b)
    return b


def _arm_record(rec: dict) -> dict:
    """What the report and the judge need of a replay (its text is rebuilt from the same fields)."""
    return {k: rec.get(k) for k in ("exit", "ended", "seconds", "model", "episode", "trace", "diff", "usage", "usd",
                                    "completed", "output_tail")}


def _replay_state(project: Path, b: dict, i: int, lock: threading.Lock, arm_parallel: int = 1) -> None:
    """One state on every arm: the baseline first (its web answers fill the cache), then the candidates,
    `arm_parallel` at a time, reading the same cache."""
    st = b["states"][i]
    if len(st["arms"]) == len(b["arms"]):
        return
    if st["arms"]:   # cut off part-way: the arms done had web answers the rest could no longer share
        with lock:
            st["arms"], st["outcomes"] = {}, {}
            _save(b)
    code = trial.committed_code(project, b["name"], b["code_rev"])
    cache, cache_lock = {}, threading.Lock()   # the first arm's web answers, the same for the others

    def one(arm: dict) -> None:
        billed = f"{b['name']}@bakeoff:{b['id']}:s{i}:{arm['label']}"
        try:
            rec = trial.replay_arm(project, b["name"], st, code, model=arm["model"], cache=cache,
                                   cache_lock=cache_lock, billed=billed, env=arm["env"])
        except trial.TrialError as e:
            rec = {"exit": None, "ended": f"not replayed: {e}", "seconds": 0, "model": arm["model"], "episode": None,
                   "trace": [], "diff": "", "usage": {}, "usd": 0.0, "completed": False, "output_tail": ""}
        trial._ledger({"instance": b["name"], "trial": b["id"], "by": "operator", "kind": "bakeoff",
                       "model": arm["label"], "usd": rec["usd"], "seconds": rec["seconds"], "ended": rec["ended"],
                       "tokens_in": (rec["usage"] or {}).get("prompt_tokens"),
                       "tokens_out": (rec["usage"] or {}).get("completion_tokens")})
        with lock:
            st["arms"][arm["label"]] = _arm_record(rec)
            _save(b)
        log(f"bakeoff {b['id']} s{i} {arm['label']}: {rec['ended']}, completed {rec['completed']}, ${rec['usd']}")

    one(b["arms"][0])
    with ThreadPoolExecutor(max_workers=arm_parallel) as pool:
        list(pool.map(one, b["arms"][1:]))


def _judge_state(project: Path, b: dict, i: int, lock: threading.Lock) -> None:
    st = b["states"][i]
    base = b["arms"][0]["label"]
    if base not in st["arms"]:
        return
    context = None
    for arm in b["arms"][1:]:
        label = arm["label"]
        before = st["outcomes"].get(label)
        if (before and before["outcome"] != "unjudged") or label not in st["arms"]:
            continue
        A, B = st["arms"][base], st["arms"][label]
        judgments, costs = [], (before or {}).get("judge_usd", 0.0)   # a failed attempt's cost stays counted
        if A["completed"] and B["completed"]:   # otherwise decide() settles it without a judge
            context = context or trial.state_context(project, b["name"], st)
            for order, (first, second) in (("AB", (A, B)), ("BA", (B, A))):
                purpose = f"bakeoff {b['name']} {b['id']} s{i} {label} {order}"
                try:
                    judgments.append(trial.judge_pair(context, first, second, purpose, model=b["judge"]))
                except Exception as e:   # judge.JudgeError or a network failure: the state is reported unjudged
                    log(f"bakeoff {b['id']} s{i} {label} {order}: judge failed: {e}")
                    judgments.append(None)
                costs += trial._judge_usd(purpose)
        else:
            judgments = [None, None]
        outcome, why = trial.decide({"A": A, "B": B}, judgments)
        with lock:
            st["outcomes"][label] = {"outcome": outcome, "why": why, "judgments": judgments,
                                     "judge_usd": round(costs, 6)}
            _save(b)


def run(b: dict, parallel: int = 3, project: Path = PROJECT, arm_parallel: int = 1, replay_only: bool = False) -> dict:
    """Replay every state on every arm, then judge (unless `replay_only`): `parallel` states at a time; within a
    state the baseline first, then `arm_parallel` candidates at once."""
    lock = threading.Lock()
    todo = range(len(b["states"]))
    DIR.mkdir(parents=True, exist_ok=True)
    with path(b["name"], b["id"]).with_suffix(".lock").open("w") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BakeoffError(f"bake-off {b['id']} is already running in another process") from None
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            list(pool.map(lambda i: _replay_state(project, b, i, lock, arm_parallel), todo))
        if replay_only or b["judge"].endswith(SUBAGENT):   # subagents judge it: import_verdicts, not the API
            return b
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            list(pool.map(lambda i: _judge_state(project, b, i, lock), todo))
    return b


# A model's name in a run's text (its config printed, its own logs) would tell the judge which run is which, or tilt
# it: the arms' own ids and these families are replaced before a case is written. One DeepSeek run printed its config.
_FAMILIES = (r"(?i)\b(?:openrouter/)?(?:[\w.-]+/)?(?:mimo|deepseek|haiku|sonnet|opus|minimax|nemotron|hy\d|hunyuan|"
             r"step-3[\w.-]*|glm-[\w.-]+|claude-[\w.-]+|gemini|gpt-[\w.-]+)[\w.@/-]*")


def _redactor(b: dict):
    names = sorted({n for a in b["arms"] for n in (a["model"], a["model"].rsplit("/", 1)[-1])}, key=len, reverse=True)
    pattern = re.compile("|".join([_FAMILIES, *(re.escape(n) for n in names)]), re.IGNORECASE)
    return lambda text: pattern.sub("[model]", text)


_CASE = """# System prompt
{system}

# Instructions
You are the judge. Read the whole case below, then write ONLY a JSON object matching this schema (no prose, no code \
fence) to the output file named in your task:

{schema}

`first`/`second` mean Run 1 and Run 2 as they appear below. Judge effectiveness first, as the system prompt says.

{case}"""


def export_cases(b: dict, out: Path, project: Path = PROJECT) -> list[str]:
    """Every judgeable pair of `b` in both orders as blind cases out/cases/<id>.md (random ids, model names redacted,
    the order hidden), recorded in out/mapping.json (id -> bake-off, state, arm, order). Pairs already exported are
    kept; several bake-offs can share `out`. Returns the cases still without a valid verdict in out/verdicts/."""
    cases, verdicts, mapping_path = out / "cases", out / "verdicts", out / "mapping.json"
    cases.mkdir(parents=True, exist_ok=True)
    verdicts.mkdir(exist_ok=True)
    mapping = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
    have = {(m["bid"], m["state"], m["arm"], m["order"]) for m in mapping.values()}
    redact, rng, base = _redactor(b), random.SystemRandom(), b["arms"][0]["label"]
    schema = json.dumps(trial.JUDGE_SCHEMA, indent=1)
    for i, st in enumerate(b["states"]):
        context = None
        for arm in b["arms"][1:]:
            label = arm["label"]
            if base not in st["arms"] or label not in st["arms"]:
                continue
            A, B = st["arms"][base], st["arms"][label]
            if not (A["completed"] and B["completed"]):
                continue   # decide() settles it without a judge
            for order, (first, second) in (("AB", (A, B)), ("BA", (B, A))):
                if (b["id"], i, label, order) in have:
                    continue
                context = context or trial.state_context(project, b["name"], st)
                cid = next(c for c in iter(lambda: f"c{rng.getrandbits(40):010x}", None) if c not in mapping)
                text = _CASE.format(system=trial.JUDGE_SYSTEM, schema=schema,
                                    case=redact(trial.judge_case(context, first, second)))
                (cases / f"{cid}.md").write_text(text)
                mapping[cid] = {"bid": b["id"], "state": i, "arm": label, "order": order, "chars": len(text)}
    mapping_path.write_text(json.dumps(mapping, indent=1))
    return [c for c, m in mapping.items() if m["bid"] == b["id"] and verdict(out, c) is None]


def verdict(out: Path, cid: str) -> dict | None:
    """Case `cid`'s verdict, or None when it is missing or not exactly the judge's schema (text around the JSON
    included: such a case is judged again from scratch, never repaired)."""
    from .judge import _valid
    p = out / "verdicts" / f"{cid}.json"
    try:
        v = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if _valid(v, trial.JUDGE_SCHEMA) or _valid(v["first"], trial._ARM) or _valid(v["second"], trial._ARM):
        return None
    return v


def import_verdicts(b: dict, out: Path, judge: str = "claude-opus-5-5") -> list[str]:
    """Decide every pair of `b` from the verdicts in `out` (trial.decide: both orders must agree; a run that did not
    complete loses without a judge) and save it with `judge` + SUBAGENT as its judge. Refused over another judge's
    verdicts: one bake-off, one judge. Returns the cases whose verdict file is there but invalid (judge them again)."""
    judged_by = judge + SUBAGENT
    if b["judge"] != judged_by and any(j for s in b["states"] for o in s["outcomes"].values() for j in o["judgments"]):
        raise BakeoffError(f"bake-off {b['id']} was judged by {b['judge']}: verdicts of {judged_by} would mix judges")
    mapping = json.loads((out / "mapping.json").read_text())
    pairs = {}
    for cid, m in mapping.items():
        if m["bid"] == b["id"]:
            pairs.setdefault((m["state"], m["arm"]), {})[m["order"]] = cid
    b["judge"] = judged_by
    base = b["arms"][0]["label"]
    for i, st in enumerate(b["states"]):
        for arm in b["arms"][1:]:
            label = arm["label"]
            if base not in st["arms"] or label not in st["arms"]:
                continue
            cids = pairs.get((i, label), {})
            judgments = [verdict(out, cids[o]) if o in cids else None for o in ("AB", "BA")]
            outcome, why = trial.decide({"A": st["arms"][base], "B": st["arms"][label]}, judgments)
            st["outcomes"][label] = {"outcome": outcome, "why": why, "judgments": judgments, "judge_usd": 0.0}
    _save(b)
    return [c for c, m in mapping.items() if m["bid"] == b["id"] and (out / "verdicts" / f"{c}.json").exists()
            and verdict(out, c) is None]


def report(b: dict) -> str:
    """Each arm's cost, completion and supported claims; each candidate's verdict against the baseline."""
    base = b["arms"][0]["label"]
    lines = [f"bake-off {b['id']} on {b['name']}: {len(b['states'])} past cycles, judge {b['judge']}, "
             f"baseline {base}", "",
             "| arm | replayed | completed | $/cycle | tokens in/out per cycle | claim supported (judge) |",
             "|---|---|---|---|---|---|"]
    for arm in b["arms"]:
        label = arm["label"]
        recs = [s["arms"][label] for s in b["states"] if label in s["arms"]]
        done = [r for r in recs if r["completed"]]
        supported, seen = 0, 0
        for s in b["states"]:   # the judge grades both runs of each judged pair: count this arm's side
            for cand, o in s["outcomes"].items():
                if label not in (base, cand):
                    continue
                for j, first in zip(o["judgments"], (base, cand)):
                    if j:
                        side = j["first"] if first == label else j["second"]
                        seen += 1
                        supported += bool(side.get("claim_supported"))
        n = len(recs) or 1
        tin = sum((r["usage"] or {}).get("prompt_tokens", 0) for r in recs) / n
        tout = sum((r["usage"] or {}).get("completion_tokens", 0) for r in recs) / n
        lines.append(f"| {label} | {len(recs)} | {len(done)} | {sum(r['usd'] for r in recs) / n:.4f} | "
                     f"{tin:,.0f} / {tout:,.0f} | {f'{supported}/{seen}' if seen else '—'} |")
    lines += ["", "| candidate vs baseline | wins | losses | ties | unjudged | verdict | p |", "|---|---|---|---|---|---|---|"]
    for arm in b["arms"][1:]:
        outcomes = [s["outcomes"][arm["label"]]["outcome"] for s in b["states"] if arm["label"] in s["outcomes"]]
        v = trial.verdict(outcomes)
        lines.append(f"| {arm['label']} | {v['W']} | {v['L']} | {v['T']} | {v['unjudged']} | {v['label']} | {v['p']} |")
    judge_usd = sum(o["judge_usd"] for s in b["states"] for o in s["outcomes"].values())
    replay_usd = sum(r["usd"] for s in b["states"] for r in s["arms"].values())
    lines += ["", f"spent: replays ${replay_usd:.2f}, judging ${judge_usd:.2f}", "",
              "Per cycle, each candidate against the baseline (W win · L loss · T tie · ? unjudged · – not replayed):", "",
              "| cycle (episode ts) | " + " | ".join(a["label"] for a in b["arms"][1:]) + " |",
              "|---|" + "---|" * (len(b["arms"]) - 1)]
    mark = {"win": "W", "loss": "L", "tie": "T", "unjudged": "?"}
    for s in b["states"]:
        lines.append(f"| {s['ts']} | " + " | ".join(mark[s["outcomes"][a["label"]]["outcome"]]
                                                   if a["label"] in s["outcomes"] else "–" for a in b["arms"][1:]) + " |")
    return "\n".join(lines)
