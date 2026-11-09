"""Model bake-offs: which cloud model should an instance run on? (docs/bakeoffs/README.md)

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
import hashlib
import json
import math
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import trial
from .core import INSTANCES, PROJECT, SUP_DIR, cloud_model, instance_model, log

DIR = SUP_DIR / "bakeoffs"
DEFAULT_JUDGE = "claude-sonnet-5-5"   # not a candidate's: Haiku shares its family (said in the report)
SUBAGENT = " (Claude Code subagent)"   # suffix of a judge that answered through import_verdicts, not the API


class BakeoffError(Exception):
    pass


def parse_arm(spec: str, efforts: tuple[str, ...] | None) -> dict:
    """'claude-haiku-5-5@xhigh' -> {'label', 'model', 'env'}."""
    from .cloudllm import CLOUD_MODELS
    from . import planchannel
    model, _, effort = spec.partition("@")
    try:
        model = instance_model(model)
    except ValueError as e:
        raise BakeoffError(f"{spec}: {e}") from None
    if model not in CLOUD_MODELS and model not in planchannel.models():
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
           bid: str = "", project: Path = PROJECT, drawn: dict | None = None) -> dict:
    """A new bake-off, or the saved one with this id (to resume: its arms, states and judge are kept). `drawn` is the
    draw() the episodes came from: its window and the kinds' counts are kept for the report."""
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
    from .judge import same_model
    if any(same_model(a["model"], judge) for a in parsed):
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
        states.append({**st, "stratum": stratum(eps[ts]), "arms": {}, "outcomes": {}})
    code_rev = trial._git(project, "rev-parse", trial.branch(name)).stdout.strip()   # every arm, every resume
    b = {"id": bid, "name": name, "created": datetime.now().isoformat(timespec="seconds"), "judge": judge,
         "code_rev": code_rev, "arms": parsed, "states": states,
         **({"drawn": {k: v for k, v in drawn.items() if k != "episodes"}} if drawn else {})}
    _save(b)
    return b


# -- drawing a fresh set of cycles ---------------------------------------------------------------------------
# Kinds of live cycle, so a set holds each kind the instance runs and its result can be read per kind. A cycle the
# operator asked for; one that wrote or sent something; one that only looked (read, searched, checked). In the 10-28
# sets 4% of cycles only looked; on 11-08, 74% did (review 2026-11-08). By the names of the tools a cycle called: a
# heuristic, the same for every arm.
STRATA = ("operator", "writes", "check")
_WRITES = re.compile(r"write|append|send|deliver|record|remember|commit|settle|update|create|delete|edit|post|notify|"
                     r"reply|task_queue")
LOOP_PATHS = ("shaft", "config.env")   # the instance's loop and its configuration: not its plugins, which it grows
MIN_PER_KIND = 4


def stratum(ep: dict) -> str:
    if (ep.get("intent") or {}).get("origin") == "operator":
        return "operator"
    return "writes" if any(_WRITES.search(str(t.get("tool") or "")) for t in ep.get("tools") or []) else "check"


def last_loop_change(name: str, project: Path = PROJECT) -> str:
    """When the instance's loop or configuration last changed on its branch (ISO time), or ''."""
    return trial._git(project, "log", "-1", "--first-parent", "--format=%cI", trial.branch(name), "--",
                      *LOOP_PATHS, check=False).stdout.strip()


def _paused(name: str) -> list[datetime]:
    """When a live cycle of `name` was paused for a conversation (supervisor.log): a replay cannot pause the same way."""
    path, mark, out = SUP_DIR / "supervisor.log", f"] {name}: live paused", []
    if path.exists():
        with path.open(errors="replace") as f:
            for line in f:
                if mark in line and line.startswith("["):
                    out.append(datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S"))
    return out


def _local(stamp: str) -> datetime:
    """An ISO time as the naive local time episodes are stamped in: an offset is converted, not dropped."""
    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def eligible(name: str, since: str, until: str = "", project: Path = PROJECT) -> list[dict]:
    """The live cycles a replay can start from as they began, ended after `since` (and by `until`): a known start,
    ended by the loop's own control, no pause for a conversation, every inbox message still in the mind store."""
    from . import mind
    starts, pauses = trial._starts(name), _paused(name)
    lo, hi = _local(since), _local(until) if until else datetime.max
    out = []
    for ep in sorted(trial.live_episodes(INSTANCES / name), key=lambda e: e["ts"]):
        end = datetime.fromisoformat(ep["ts"]).replace(tzinfo=None)
        if not lo < end <= hi or ep.get("ended_by") != "control":
            continue
        t0 = trial.start_time(name, ep, starts)
        if not t0 or any(t0 <= p <= end for p in pauses):
            continue
        if any("/" in m or m.startswith(".") or mind.show(name, "mind", f"memory/messages/{m}", project) is None
               for m in ep.get("inbox") or []):
            continue
        out.append(ep)
    return out


def _spaced(items: list, k: int) -> list:
    """`k` of `items`, evenly spaced, the first and last included."""
    if k >= len(items):
        return list(items)
    if k == 1:
        return [items[len(items) // 2]]
    return [items[round(j * (len(items) - 1) / (k - 1))] for j in range(k)]


def draw(name: str, n: int, since: str = "", until: str = "", project: Path = PROJECT) -> dict:
    """A fresh set of `n` cycles from the eligible ones after `since` (default: the loop's last change): of each kind
    there is, MIN_PER_KIND or as many as it has (fewer each when `n` is too small for all kinds), the rest by each
    kind's share, evenly spaced in time within a kind.
    Returns {since, until, population: {kind: eligible count}, episodes: [ts]}."""
    if n < 1:
        raise BakeoffError(f"--draw takes a number of cycles, not {n}")
    since = since or last_loop_change(name, project)
    if not since:
        raise BakeoffError(f"no change to {name}'s loop found: give --since")
    pool = eligible(name, since, until, project)
    by = {k: [e for e in pool if stratum(e) == k] for k in STRATA}
    if len(pool) < n:
        raise BakeoffError(f"{len(pool)} eligible live cycles of {name} after {since}, fewer than {n}: give an "
                           f"earlier --since or a smaller --draw")
    take = {k: min(len(v), MIN_PER_KIND, n // max(1, sum(bool(v) for v in by.values()))) for k, v in by.items()}
    while sum(take.values()) < n:   # the rest by share: each seat to the kind furthest below its share
        k = max((k for k in STRATA if take[k] < len(by[k])), key=lambda k: len(by[k]) / len(pool) * n - take[k])
        take[k] += 1
    chosen = sorted(e["ts"] for k in STRATA for e in _spaced(by[k], take[k]))
    return {"since": since, "until": until or pool[-1]["ts"], "population": {k: len(v) for k, v in by.items()},
            "episodes": chosen}


def _arm_record(rec: dict) -> dict:
    """What the report and the judge need of a replay (its text is rebuilt from the same fields)."""
    return {k: rec.get(k) for k in ("exit", "ended", "seconds", "model", "episode", "trace", "diff", "usage", "usd",
                                    "completed", "output_tail", "last_call_failed")}


def _replay_state(project: Path, b: dict, i: int, lock: threading.Lock, arm_parallel: int = 1) -> None:
    """One state on every arm: the baseline first (its web answers fill the cache), then the candidates,
    `arm_parallel` at a time, reading the same cache."""
    st = b["states"][i]
    failed = [label for label, rec in st["arms"].items() if trial.infra_failure(rec)]
    if len(st["arms"]) == len(b["arms"]) and not failed:
        return
    if st["arms"]:   # cut off part-way, or an arm failed on its provider: the arms done had web answers the rest
        if failed:   # could no longer share, so the state is replayed in full
            log(f"bakeoff {b['id']} s{i}: replaying again, {', '.join(failed)} failed on the provider")
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
                try:   # blinded as the subagents' cases are: the API judge saw model names until 2026-11-08
                    judgments.append(trial.judge_pair(context, first, second, purpose, model=b["judge"],
                                                      redact=_redactor(b)))
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
# it: the arms' own ids, also as spelled out ("Claude Haiku 5.5" for claude-haiku-5-5), and these families are replaced
# before a case is written. One DeepSeek run printed its config.
_FAMILIES = (r"\b(?:(?:openrouter|codex|grok)/)?(?:[\w.-]+/)?(?:codex|mimo|deepseek|haiku|sonnet|opus|minimax|nemotron|hy\d|hunyuan|"
             r"step-3[\w.-]*|glm-[\w.-]+|claude-[\w.-]+|gemini|gpt-[\w.-]+|"
             r"grok[\w.-]*|(?:Space)?xai\b|muse-spark[\w.-]*|Muse Spark|"
             r"kimi(?![a-z])[\w.-]*|moonshot(?:ai)?\b|mercury-\d[\w.-]*|Mercury \d|particle-\d[\w.-]*)[\w.@/-]*")


def _redactor(b: dict):
    from . import planchannel
    channels = r"\b(?:" + "|".join(map(re.escape, planchannel.PROVIDERS)) + r")[\w.@/-]*"
    names = sorted({n for a in b["arms"] for n in (a["model"], a["model"].rsplit("/", 1)[-1])}, key=len, reverse=True)
    spelled = [r"[\s._/-]*".join(map(re.escape, re.split(r"[\s._/-]+", n))) for n in names]
    pattern = re.compile("|".join([*(rf"\b{n}\b" for n in spelled), channels, _FAMILIES]), re.IGNORECASE)   # the arms first
    return lambda text: pattern.sub("[model]", text)


# What a judge is shown changes with trial._run_text and JUDGE_SYSTEM: a case written before (2026-11-08: cache tags
# and seconds still in it; 2026-11-09: no word on the replay's clock) is stale, and so is its verdict.
CASE_VERSION = 3


def _runs(A: dict, B: dict) -> str:
    """The pair's two runs as exported, and how they were shown: a case judged on other runs (the state replayed
    again since) or written by an older _run_text is stale."""
    return hashlib.sha256(json.dumps([A, B], sort_keys=True, ensure_ascii=False).encode()
                          + (f":v{CASE_VERSION}" if CASE_VERSION > 1 else "").encode()).hexdigest()[:16]


def _pair_cases(mapping: dict, b: dict, i: int, label: str, runs: str) -> dict:
    """{order: case id} of this pair's current runs in `mapping`."""
    return {m["order"]: c for c, m in mapping.items() if (m.get("name"), m["bid"], m["state"], m["arm"], m.get("runs"))
            == (b["name"], b["id"], i, label, runs)}


_CASE = """# System prompt
{system}

# Instructions
You are the judge. Read the whole case below, then write ONLY a JSON object matching this schema (no prose, no code \
fence) to the output file named in your task:

{schema}

`first`/`second` mean Run 1 and Run 2 as they appear below. Judge effectiveness first, as the system prompt says.

{case}"""


def mapping_path(out: Path) -> Path:
    """Where the key of the cases exported to `out` is kept: under the supervisor's folder, never beside the cases. In
    `out` it told any judge that listed the folder which run was which (review 2026-11-08)."""
    return DIR / "exports" / f"{hashlib.sha256(str(out.resolve()).encode()).hexdigest()[:16]}.json"


def _mapping(out: Path) -> dict:
    """The key of `out`'s cases; one written beside them by an older export moves to mapping_path(out)."""
    key, legacy = mapping_path(out), out / "mapping.json"
    mapping = json.loads(key.read_text()) if key.exists() else {}
    if legacy.exists():
        mapping = {**json.loads(legacy.read_text()), **mapping}
        _write_mapping(out, mapping)
        legacy.unlink()
    return mapping


def _write_mapping(out: Path, mapping: dict) -> None:
    key = mapping_path(out)
    key.parent.mkdir(parents=True, exist_ok=True)
    tmp = key.with_suffix(".tmp")
    tmp.write_text(json.dumps({**mapping}, indent=1))
    tmp.replace(key)


def export_cases(b: dict, out: Path, project: Path = PROJECT) -> list[str]:
    """Every judgeable pair of `b` in both orders as blind cases out/cases/<id>.md (random ids, model names redacted,
    the order hidden), recorded in mapping_path(out) (id -> instance, bake-off, state, arm, order, runs). Pairs already
    exported on the same runs are kept; several bake-offs can share `out`. Returns, shuffled (their order would tell
    AB from BA), the current cases still without a valid verdict in out/verdicts/."""
    cases, verdicts = out / "cases", out / "verdicts"
    cases.mkdir(parents=True, exist_ok=True)
    verdicts.mkdir(exist_ok=True)
    mapping = _mapping(out)
    current = []
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
            runs = _runs(A, B)
            have = _pair_cases(mapping, b, i, label, runs)
            current += have.values()
            for order, (first, second) in (("AB", (A, B)), ("BA", (B, A))):
                if order in have:
                    continue
                context = context or trial.state_context(project, b["name"], st)
                cid = next(c for c in iter(lambda: f"c{rng.getrandbits(40):010x}", None) if c not in mapping)
                text = _CASE.format(system=trial.JUDGE_SYSTEM, schema=schema,
                                    case=redact(trial.judge_case(context, first, second)))
                (cases / f"{cid}.md").write_text(text)
                mapping[cid] = {"name": b["name"], "bid": b["id"], "state": i, "arm": label, "order": order,
                                "runs": runs, "chars": len(text)}
                current.append(cid)
    _write_mapping(out, mapping)
    todo = [c for c in current if verdict(out, c) is None]
    rng.shuffle(todo)
    return todo


def verdict(out: Path, cid: str) -> dict | None:
    """Case `cid`'s verdict, or None when it is missing or not exactly the judge's schema (text around the JSON or a
    field it does not have included: such a case is judged again from scratch, never repaired)."""
    from .judge import _valid
    p = out / "verdicts" / f"{cid}.json"
    try:
        v = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if _valid(v, trial.JUDGE_SCHEMA) or set(v) - set(trial.JUDGE_SCHEMA["properties"]):
        return None
    if any(_valid(v[side], trial._ARM) or set(v[side]) - set(trial._ARM["properties"]) for side in ("first", "second")):
        return None
    return v


def import_verdicts(b: dict, out: Path, judge: str = "claude-opus-5-5") -> list[str]:
    """Decide every pair of `b` from the verdicts in `out` (trial.decide: both orders must agree; a run that did not
    complete loses without a judge) and save it with `judge` + SUBAGENT as its judge. Refused over another judge's
    verdicts: one bake-off, one judge. Returns the cases whose verdict file is there but invalid (judge them again)."""
    judged_by = judge + SUBAGENT
    if b["judge"] != judged_by and any(j for s in b["states"] for o in s["outcomes"].values() for j in o["judgments"]):
        raise BakeoffError(f"bake-off {b['id']} was judged by {b['judge']}: verdicts of {judged_by} would mix judges")
    mapping = _mapping(out)
    b["judge"] = judged_by
    base, bad = b["arms"][0]["label"], []
    for i, st in enumerate(b["states"]):
        for arm in b["arms"][1:]:
            label = arm["label"]
            if base not in st["arms"] or label not in st["arms"]:
                continue
            A, B = st["arms"][base], st["arms"][label]
            cids = _pair_cases(mapping, b, i, label, _runs(A, B))   # never a verdict on runs replayed since
            judgments = [verdict(out, cids[o]) if o in cids else None for o in ("AB", "BA")]
            outcome, why = trial.decide({"A": A, "B": B}, judgments)
            st["outcomes"][label] = {"outcome": outcome, "why": why, "judgments": judgments, "judge_usd": 0.0}
            bad += [c for c in cids.values() if (out / "verdicts" / f"{c}.json").exists() and verdict(out, c) is None]
    _save(b)
    return bad


def _outcome(b: dict, st: dict, label: str) -> str | None:
    """The pair's outcome by today's rules (trial.decide on its stored judgments): a record saved before splits and
    provider failures were told apart reads the same as a new one."""
    base, o = b["arms"][0]["label"], st["outcomes"].get(label)
    if not o:
        return None
    if base not in st["arms"] or label not in st["arms"]:
        return o["outcome"]
    return trial.decide({"A": st["arms"][base], "B": st["arms"][label]}, o["judgments"])[0]


def _claims(b: dict, label: str) -> tuple[int, int, list[float]]:
    """The judge's claim_supported calls on `label`'s runs: (supported, calls, each run's supported share). A baseline
    run is judged once per candidate and order, so the pooled share weighs it many times; the per-run shares do not."""
    base, supported, seen, per_run = b["arms"][0]["label"], 0, 0, []
    for s in b["states"]:
        calls = []
        for cand, o in s["outcomes"].items():
            if label not in (base, cand):
                continue
            for j, first in zip(o["judgments"], (base, cand)):
                if j:
                    calls.append(bool((j["first"] if first == label else j["second"]).get("claim_supported")))
        supported, seen = supported + sum(calls), seen + len(calls)
        if calls:
            per_run.append(sum(calls) / len(calls))
    return supported, seen, per_run


def report(b: dict) -> str:
    """Each arm's cost, completion and supported claims; each candidate's verdict against the baseline, with ties
    (both orders said so), splits (the orders disagreed) and the outcomes no judge decided shown apart; per stratum
    when the cycles were drawn by kind."""
    base = b["arms"][0]["label"]
    lines = [f"bake-off {b['id']} on {b['name']}: {len(b['states'])} past cycles, judge {b['judge']}, "
             f"baseline {base}, instance code {b.get('code_rev', '')[:10]}", "",
             "| arm | replayed | completed | failed on provider | $/cycle | s/cycle | tokens in/out per cycle "
             "| claim supported (judge calls) | per run |",
             "|---|---|---|---|---|---|---|---|---|"]
    for arm in b["arms"]:
        label = arm["label"]
        recs = [s["arms"][label] for s in b["states"] if label in s["arms"]]
        done = [r for r in recs if r["completed"]]
        supported, seen, per_run = _claims(b, label)
        n = len(recs) or 1
        tin = sum((r["usage"] or {}).get("prompt_tokens", 0) for r in recs) / n
        tout = sum((r["usage"] or {}).get("completion_tokens", 0) for r in recs) / n
        lines.append(f"| {label} | {len(recs)} | {len(done)} | {sum(bool(trial.infra_failure(r)) for r in recs)} | "
                     f"{sum(r['usd'] for r in recs) / n:.4f} | {sum(r['seconds'] or 0 for r in recs) / n:.0f} | "
                     f"{tin:,.0f} / {tout:,.0f} | {f'{supported}/{seen}' if seen else '—'} | "
                     f"{f'{sum(per_run) / len(per_run):.0%} of {len(per_run)}' if per_run else '—'} |")
    lines += ["", "T: both orders found the runs equal. S: the two orders disagreed. Neither counts in the sign test. "
              "No judge: wins and losses settled because one run did not complete.", "",
              "| candidate vs baseline | wins | losses | ties | splits | unjudged | no judge | verdict | p |",
              "|---|---|---|---|---|---|---|---|---|"]
    for arm in b["arms"][1:]:
        label = arm["label"]
        states = [s for s in b["states"] if label in s["outcomes"]]
        v = trial.verdict([_outcome(b, s, label) for s in states])
        auto = sum(_outcome(b, s, label) in ("win", "loss") and not any(s["outcomes"][label]["judgments"])
                   for s in states)
        lines.append(f"| {label} | {v['W']} | {v['L']} | {v['T']} | {v['S']} | {v['unjudged']} | {auto} | "
                     f"{v['label']} | {v['p']} |")
    lines += _strata_lines(b)
    judge_usd = sum(o["judge_usd"] for s in b["states"] for o in s["outcomes"].values())
    replay_usd = sum(r["usd"] for s in b["states"] for r in s["arms"].values())
    lines += ["", f"spent: replays ${replay_usd:.2f}, judging ${judge_usd:.2f}", "",
              "Per cycle, each candidate against the baseline (W win · L loss · T tie · S split · ? unjudged · "
              "– not replayed):", "",
              "| cycle (episode ts) | " + " | ".join(a["label"] for a in b["arms"][1:]) + " |",
              "|---|" + "---|" * (len(b["arms"]) - 1)]
    mark = {"win": "W", "loss": "L", "tie": "T", "split": "S", "unjudged": "?", None: "–"}
    for s in b["states"]:
        kind = f" ({s['stratum']})" if b.get("drawn") and s.get("stratum") else ""
        lines.append(f"| {s['ts']}{kind} | " + " | ".join(mark[_outcome(b, s, a["label"])] for a in b["arms"][1:]) + " |")
    return "\n".join(lines)


def _strata_lines(b: dict) -> list[str]:
    """W–L–T–S per kind of cycle, and the net wins per cycle reweighted to the kinds' share of the instance's cycles
    when they were drawn: a set that over-samples a rare kind would otherwise speak for it."""
    if not b.get("drawn"):   # a set chosen by hand: its kinds were not balanced, so no table
        return []
    kinds = [k for k in STRATA if any(s.get("stratum") == k for s in b["states"])]
    population = b["drawn"].get("population") or {}
    total = sum(population.get(k, 0) for k in kinds)
    head = " | ".join(f"{k} ({population.get(k, 0)} of {total})" if total else k for k in kinds)
    lines = ["", "Per kind of cycle (W–L–T–S; in brackets the kind's count among the cycles the set was drawn from):",
             "", f"| candidate | {head} | net per cycle, reweighted |", "|---|" + "---|" * (len(kinds) + 1)]
    for arm in b["arms"][1:]:
        label, cells, net = arm["label"], [], 0.0
        for k in kinds:
            o = [_outcome(b, s, label) for s in b["states"] if s.get("stratum") == k and label in s["outcomes"]]
            cells.append(f"{o.count('win')}–{o.count('loss')}–{o.count('tie')}–{o.count('split')}")
            judged = [x for x in o if x != "unjudged"]
            if judged and total:
                net += population.get(k, 0) / total * (judged.count("win") - judged.count("loss")) / len(judged)
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {f'{net:+.2f}' if total else '—'} |")
    return lines


# -- the operator as judge ------------------------------------------------------------------------------------------
# The LLM judges grade by a rubric; whether it is the operator's is a question only the operator can answer. Each pair
# is shown to them blind (the two runs as A and B, sides drawn per pair) with what the cycle started from and what each
# run would have put in front of them: its messages, its closing account and the files it delivered. Their verdicts
# are kept apart from the LLM judges' (one record per bake-off) and set beside them in the report.
OPERATOR_CHOICES = ("A", "B", "tie", "both bad")
_INTERNAL = ("memory/", "self/", "logs/", ".tmp/", "inbox/")


def operator_path(b: dict) -> Path:
    return DIR / "operator" / f"{b['name']}-{b['id']}.jsonl"


def operator_verdicts(b: dict) -> dict[tuple[int, str], dict]:
    """(state, candidate) -> the operator's last verdict on the pair's current runs."""
    out, base = {}, b["arms"][0]["label"]
    p = operator_path(b)
    for line in p.read_text().splitlines() if p.exists() else []:
        try:
            v = json.loads(line)
        except ValueError:
            continue
        st = b["states"][v["state"]] if 0 <= v.get("state", -1) < len(b["states"]) else None
        if st and base in st["arms"] and v.get("arm") in st["arms"] and \
                v.get("runs") == _runs(st["arms"][base], st["arms"][v["arm"]]):
            out[(v["state"], v["arm"])] = v
    return out


def _judgeable(b: dict, i: int, label: str) -> bool:
    st, base = b["states"][i], b["arms"][0]["label"]
    return (base in st["arms"] and label in st["arms"] and st["arms"][base]["completed"]
            and st["arms"][label]["completed"])


def _flipped(b: dict, i: int, label: str) -> bool:
    """Whether the candidate is shown as A: drawn per pair, the same on every visit."""
    return hashlib.sha256(f"{b['name']}:{b['id']}:{b['created']}:{i}:{label}".encode()).digest()[0] & 1 == 1


def operator_queue(b: dict, labels: list[str]) -> list[tuple[int, str]]:
    """The pairs of these candidates against the baseline the operator has not judged yet, in an order that mixes
    candidates and cycles (the same order on every visit)."""
    done = operator_verdicts(b)
    todo = [(i, label) for i in range(len(b["states"])) for label in labels
            if _judgeable(b, i, label) and (i, label) not in done]
    return sorted(todo, key=lambda p: hashlib.sha256(f"{b['id']}:{p[0]}:{p[1]}".encode()).hexdigest())


def _diff_files(diff: str) -> list[dict]:
    """trial.file_diff's text as one record per file: {path, added, removed, created, deleted, binary, cut}. Parsed by
    headers and hunk lengths, so a content line that starts with '--- ' or '+++ ' stays content."""
    files, cur, old_left, new_left = [], None, 0, 0
    lines = diff.splitlines()
    k = 0
    while k < len(lines):
        line = lines[k]
        if old_left > 0 or new_left > 0:   # inside a hunk: content until its lengths are used up, or a cut mark
            if line.startswith("[... this file's diff cut here") or line.startswith("[... diff cut here"):
                cur["cut"], old_left, new_left = True, 0, 0
            elif line.startswith("+"):
                cur["added"].append(line[1:])
                new_left -= 1
            elif line.startswith("-"):
                cur["removed"] += 1
                old_left -= 1
            else:
                old_left, new_left = old_left - 1, new_left - 1
            k += 1
            continue
        m = re.match(r"\(binary file (.+) (created|deleted|changed)\)$", line)
        if m:
            files.append({"path": m[1], "added": [], "removed": 0, "created": m[2] == "created",
                          "deleted": m[2] == "deleted", "binary": True, "cut": False})
            cur = None
        elif line.startswith("--- ") and k + 1 < len(lines) and lines[k + 1].startswith("+++ "):
            old, new = line[4:].strip(), lines[k + 1][4:].strip()
            cur = {"path": (new if new != "/dev/null" else old).removeprefix("b/").removeprefix("a/"), "added": [],
                   "removed": 0, "created": old == "/dev/null", "deleted": new == "/dev/null", "binary": False,
                   "cut": False}
            files.append(cur)
            k += 1
        elif (h := re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)) and cur is not None:
            old_left, new_left = int(h[1] if h[1] is not None else 1), int(h[2] if h[2] is not None else 1)
        elif line.startswith("[... this file's diff cut here") and cur is not None:
            cur["cut"] = True
        k += 1
    return files


def _facing(rec: dict, redact) -> dict:
    """What one run would have put in front of the operator: its messages (and whether they were let through), its
    closing account, the files it delivered with what it added to them, and which internal files it touched. Every
    string shown goes through `redact`: a file or an attachment can carry a model's name too."""
    sent = []
    for c in rec.get("trace") or []:
        if c.get("tool") != "send_message":
            continue
        try:
            args = json.loads(c["args"]) if isinstance(c["args"], str) else dict(c["args"] or {})
        except ValueError:
            args = {"text": str(c["args"])}
        held = "not sent" in str(c.get("result", "")) or not c.get("ok", True)
        sent.append({"to": redact(str(args.get("to", ""))), "kind": redact(str(args.get("kind", ""))),
                     "text": redact(str(args.get("text", ""))),
                     "attachments": [redact(str(a)) for a in args.get("attachments") or []],
                     "held": redact(str(c.get("result", ""))[:300]) if held else ""})
    files, internal = [], []
    for f in _diff_files(rec.get("diff") or ""):
        if f["path"].startswith(_INTERNAL):
            internal.append(redact(f["path"]))
            continue
        added = "\n".join(f["added"])
        note = ("binary file " + ("created" if f["created"] else "deleted" if f["deleted"] else "changed")
                if f["binary"] else "deleted" if f["deleted"] else
                f"only removals ({f['removed']} lines)" if not added else "")
        files.append({"path": redact(f["path"]), "added": redact(added[:6000]), "note": note,
                      "cut": f["cut"] or len(added) > 6000})
    ep = rec.get("episode") or {}
    finish = ep.get("finish") or {}
    account = finish.get("what_happened") if isinstance(finish, dict) else finish
    return {"goal": redact(str((ep.get("intent") or {}).get("goal", ""))), "sent": sent,
            "account": redact(str(account or "")), "files": files, "internal": sorted(set(internal)),
            "turns": ep.get("steps")}


def _pending(b: dict, st: dict, redact, project: Path = PROJECT) -> dict:
    """What was waiting when the cycle began, from the mind as of its start: the queue's open and waiting tasks and the
    promises made and not yet settled, each with its due time and whether it was already past. A cycle with an empty
    inbox is judged by these: was there something to do, and did the run do it (operator, 2026-11-09)."""
    from . import mind
    try:
        rev = trial.mind_rev(project, b["name"], st)
    except trial.TrialError:
        return {"queue": [], "commitments": [], "backlog": 0}
    t0 = st["t0"][:16]
    try:
        q = json.loads(mind.show(b["name"], rev, "notes/queue.json", project) or "[]")
    except ValueError:
        q = []
    q = [x for x in q if isinstance(x, dict)]
    live = sorted((x for x in q if x.get("status") in ("open", "waiting")), key=lambda x: x.get("due_at") or "9")
    queue = [{"id": x.get("id"), "status": x.get("status", ""), "due": str(x.get("due_at") or ""),
              "late": bool(x.get("due_at")) and str(x["due_at"])[:16] <= t0, "task": redact(str(x.get("task", ""))[:400])}
             for x in live[:20]]
    made, settled = {}, set()
    for line in (mind.show(b["name"], rev, "memory/commitments.jsonl", project) or "").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "made":
            made[e.get("id")] = e
        elif e.get("event") == "settled":
            settled.add(e.get("id"))
    open_ = sorted((e for k, e in made.items() if k not in settled), key=lambda e: e.get("due") or "9")
    commitments = [{"due": str(e.get("due") or ""), "late": bool(e.get("due")) and str(e["due"])[:16] <= t0,
                    "to": str(e.get("made_to", "")), "promise": redact(str(e.get("promise", ""))[:400])}
                   for e in open_[:15]]
    return {"queue": queue, "commitments": commitments,
            "backlog": sum(1 for x in q if x.get("status") == "backlog"), "more": max(0, len(live) - 20)}


def operator_case(b: dict, i: int, label: str, project: Path = PROJECT) -> dict:
    """One pair as the operator sees it: the cycle's start (goals and inbox, as the judges get it) and the two runs
    as A and B."""
    st, base = b["states"][i], b["arms"][0]["label"]
    redact = _redactor(b)
    runs = {"baseline": _facing(st["arms"][base], redact), "candidate": _facing(st["arms"][label], redact)}
    flipped = _flipped(b, i, label)
    return {"state": i, "arm": label, "runs": _runs(st["arms"][base], st["arms"][label]), "ts": st["ts"],
            "t0": st["t0"], "replayed": b["created"][:10],
            "kind": st.get("stratum", ""), "context": redact(trial.state_context(project, b["name"], st)),
            "pending": _pending(b, st, redact, project),
            "A": runs["candidate" if flipped else "baseline"], "B": runs["baseline" if flipped else "candidate"]}


def operator_record(b: dict, i: int, label: str, choice: str, note: str = "", by: str = "", runs: str = "") -> dict:
    """Keep the operator's verdict on a pair: A, B, tie or both bad, read back as candidate win, loss, tie or both bad.
    `runs` is the pair the page showed: refused when the pair was replayed since, so a verdict never lands on runs
    the operator did not see."""
    if choice not in OPERATOR_CHOICES:
        raise BakeoffError(f"choose one of {', '.join(OPERATOR_CHOICES)}")
    if not (0 <= i < len(b["states"])) or label not in [a["label"] for a in b["arms"][1:]] or not _judgeable(b, i, label):
        raise BakeoffError("no such pair to judge")
    st, base = b["states"][i], b["arms"][0]["label"]
    if runs and runs != _runs(st["arms"][base], st["arms"][label]):
        raise BakeoffError("this pair was replayed since the page was opened: reload it")
    flipped = _flipped(b, i, label)
    outcome = {"tie": "tie", "both bad": "both bad"}.get(choice) or \
        ("win" if (choice == "A") == flipped else "loss")
    row = {"at": datetime.now().isoformat(timespec="seconds"), "by": by, "state": i, "arm": label, "choice": choice,
           "flipped": flipped, "outcome": outcome, "note": note.strip()[:2000],
           "runs": _runs(st["arms"][base], st["arms"][label])}
    p = operator_path(b)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


ELO = 400 / math.log(10)


def elo(outcomes: list[str]) -> dict | None:
    """A candidate's Elo against the baseline (fixed at 0) from its pair outcomes: win 1, tie and both bad ½, loss 0;
    splits and unjudged pairs left out. Every candidate meets only the baseline, so the Bradley-Terry fit is each
    one's own log-odds; half a win and half a loss are added so that 5-0 stays finite. 90% range from the normal
    approximation. None with no pair."""
    w, l = outcomes.count("win"), outcomes.count("loss")
    t = outcomes.count("tie") + outcomes.count("both bad")
    n = w + l + t
    if not n:
        return None
    share = (w + t / 2 + 0.5) / (n + 1)
    se = 1 / math.sqrt((n + 1) * share * (1 - share))
    theta = math.log(share / (1 - share))
    return {"elo": round(theta * ELO), "lo": round((theta - 1.645 * se) * ELO), "hi": round((theta + 1.645 * se) * ELO),
            "n": n}


def ranking(b: dict) -> list[dict]:
    """Every arm by the operator's Elo (the baseline at 0), with the LLM judges' Elo on all the pairs they settled
    beside it. Arms the operator has not judged yet come last."""
    done = operator_verdicts(b)
    base = b["arms"][0]["label"]
    rows = [{"arm": base, "baseline": True, "you": {"elo": 0, "lo": 0, "hi": 0, "n": 0}, "llm": {"elo": 0, "lo": 0,
                                                                                                "hi": 0, "n": 0}}]
    for arm in b["arms"][1:]:
        label = arm["label"]
        mine = [v["outcome"] for (i, l), v in done.items() if l == label]
        theirs = [o for o in (_outcome(b, s, label) for s in b["states"]) if o in ("win", "loss", "tie")]
        rows.append({"arm": label, "baseline": False, "you": elo(mine), "llm": elo(theirs)})
    return sorted(rows, key=lambda r: (r["you"] is None, -(r["you"] or {"elo": 0})["elo"]))


def operator_summary(b: dict) -> list[dict]:
    """Per candidate: the operator's W–L–T and both-bad, and how often the LLM judges' outcome on the same pairs
    agreed with theirs (a split or an unjudged pair does not count as agreement or disagreement)."""
    done = operator_verdicts(b)
    rows = []
    for arm in b["arms"][1:]:
        label = arm["label"]
        mine = {i: v["outcome"] for (i, l), v in done.items() if l == label}
        llm = {i: _outcome(b, b["states"][i], label) for i in mine}
        compared = [i for i in mine if llm[i] in ("win", "loss", "tie") and mine[i] in ("win", "loss", "tie")]
        o = list(mine.values())
        rows.append({"arm": label, "judged": len(o), "left": sum(_judgeable(b, i, label) for i in range(len(b["states"])))
                     - len(o), "W": o.count("win"), "L": o.count("loss"), "T": o.count("tie"),
                     "bad": o.count("both bad"), "p": trial.verdict(o)["p"] if o else None,
                     "agree": sum(mine[i] == llm[i] for i in compared), "compared": len(compared)})
    return rows


def saved() -> list[dict]:
    """Every saved bake-off as {name, id, modified}, newest first: from the file names only (records run to 30 MB).
    An instance name may hold a hyphen: the longest known instance name that prefixes the file name is its owner."""
    known = sorted((p.name for p in INSTANCES.iterdir() if p.is_dir()), key=len, reverse=True) \
        if INSTANCES.exists() else []
    out = []
    for p in sorted(DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        name = next((n for n in known if p.stem.startswith(n + "-")), p.stem.partition("-")[0])
        bid = p.stem[len(name) + 1:]
        if name and bid:
            out.append({"name": name, "id": bid, "modified": datetime.fromtimestamp(p.stat().st_mtime)})
    return out


_LOADED: dict[Path, tuple[float, dict]] = {}
_LOADED_LOCK = threading.Lock()


def load(name: str, bid: str) -> dict | None:
    """A saved bake-off, or None; parsed again only when its file changed. One record is held at a time, under a lock:
    the web server answers requests on several threads."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not re.fullmatch(r"[A-Za-z0-9_-]+", bid):
        return None
    p = path(name, bid)
    try:
        mtime = p.stat().st_mtime
        with _LOADED_LOCK:
            held = _LOADED.get(p)
            if held and held[0] == mtime:
                return held[1]
        b = json.loads(p.read_text())
        with _LOADED_LOCK:
            _LOADED.clear()
            _LOADED[p] = (mtime, b)
        return b
    except (OSError, ValueError):
        return None