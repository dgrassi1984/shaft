"""Measure whether the judge follows an instance's after-action claim."""
from __future__ import annotations

import copy
import json
import random
import re
from datetime import datetime
from pathlib import Path

from . import judge, metrics
from .core import SUP_DIR, log

ARMS = ("sighted", "reask", "blind", "bare")
WITHHELD = "withheld for this grading"
ANCHOR_SYSTEM = (metrics.JUDGE_SYSTEM.split("\n\nThen, separately", 1)[0]
                 .replace("Flag claims that the tool log does not support. ", "")
                 + " Parts of the agent's own report may be withheld for this grading: grade from the tool log.")
ANCHOR_SCHEMA = metrics._schema({k: metrics.VERDICT_PROPS[k]
                                for k in ("criterion_met", "criterion_quality", "failure_mode", "reasoning")})
_RANK = {"no": 0, "partial": 1, "yes": 2}


def strip_claim(ep: dict, bare: bool = False) -> dict:
    """Withhold the report's verdict in both places the judge reads it."""
    ep = copy.deepcopy(ep)
    if bare:
        ep["finish"] = {"report": WITHHELD}
    elif isinstance(ep.get("finish"), dict):
        ep["finish"]["criterion_met"] = WITHHELD
    for t in ep.get("tools", []):
        if t.get("tool") != "finish_cycle":
            continue
        if bare:
            t["args"] = json.dumps({"report": WITHHELD})
            continue
        args = t.get("args", "")
        try:
            parsed = json.loads(args)
        except ValueError:
            t["args"] = re.sub(r'("criterion_met"\s*:\s*)"(?:[^"\\]|\\.)*(?:"|$)',
                               lambda m: m[1] + json.dumps(WITHHELD), args)
        else:
            if isinstance(parsed, dict) and "criterion_met" in parsed:
                parsed["criterion_met"] = WITHHELD
                t["args"] = json.dumps(parsed, ensure_ascii=False)
    return ep


def case(name: str, ep: dict, arm: str, js: list[dict]) -> str:
    if arm not in ARMS:
        raise ValueError(f"unknown anchoring arm: {arm}")
    shown = strip_claim(ep, bare=(arm == "bare")) if arm in ("blind", "bare") else ep
    # Written claims remain evidence. Bare bounds leakage from the rest of the report;
    # it does not remove claims independently recorded in written files.
    return metrics._case(shown, metrics._written_files(name, shown), metrics._provenance_evidence(name, shown),
                         metrics._prior_goals(js, ep["ts"]), followed=metrics._followed_surprise(name, shown))


def candidates(name: str, seed: str = "anchoring") -> list[dict]:
    """Eligible live episodes, shuffled reproducibly before sampling."""
    episodes = {ep["ts"]: ep for ep in metrics.live_episodes(name)}
    chosen = []
    for j in metrics.judgments(name):
        ep = episodes.get(j["episode_ts"])
        if j.get("judge") == "rule" or not metrics.by_current_judge(j) or not ep or not ep.get("intent"):
            continue
        if (ep.get("finish") or {}).get("criterion_met") not in _RANK:
            continue
        if any(judge.same_model(j["judge"], m) for m in metrics.own_models(name, ep)):
            continue
        chosen.append(ep)
    random.Random(f"{name}|{seed}").shuffle(chosen)
    return chosen


def path(name: str) -> Path:
    return SUP_DIR / "judgments" / f"{name}-anchoring.jsonl"


def rows(name: str) -> list[dict]:
    p = path(name)
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def run(name: str, n: int = 40, bare: bool = False, dry_run: bool = False) -> dict:
    chosen = candidates(name)[:max(0, n)]
    done = {(r["episode_ts"], r["arm"]) for r in rows(name)}
    todo = [(ep, arm) for ep in chosen for arm in ARMS[:4 if bare else 3]
            if (ep["ts"], arm) not in done]
    cost, basis = metrics._judgment_cost()
    result = {"todo": len(todo), "done": 0, "estimated_usd": len(todo) * cost, "per_judgment_usd": cost,
              "basis": basis + " (an upper bound: anchoring asks only four verdict fields)", "cycles": len(chosen)}
    if dry_run or not todo:
        return result
    js = metrics.judgments(name)
    stored_by_ts = {j["episode_ts"]: j for j in js}
    for ep, arm in todo:
        stored = stored_by_ts[ep["ts"]]
        try:
            verdict = judge.ask(ANCHOR_SYSTEM, case(name, ep, arm, js), ANCHOR_SCHEMA,
                                purpose=f"anchoring {name} {arm}", model=stored["judge"])
            problem = judge._valid(verdict, ANCHOR_SCHEMA)
            if problem:
                raise judge.JudgeError(problem)
        except judge.JudgeError as e:
            log(f"{name}: anchoring {arm} of {ep['ts']} failed: {e}")
            break
        row = {"episode_ts": ep["ts"], "arm": arm, "judge": stored["judge"],
               "judged_at": datetime.now().isoformat(timespec="seconds"),
               "claimed": ep["finish"]["criterion_met"], "stored": stored["criterion_met"],
               **{k: verdict[k] for k in ANCHOR_SCHEMA["properties"]}}
        p = path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
        result["done"] += 1
    return result


def _auroc(pairs: list[tuple[int, bool]]) -> float | None:
    """Probability a hit's claim ranks above a miss's; ties count half."""
    hits = [0, 0, 0]
    misses = [0, 0, 0]
    for score, hit in pairs:
        (hits if hit else misses)[score] += 1
    denominator = sum(hits) * sum(misses)
    if not denominator:
        return None
    return sum(hits[s] * (sum(misses[:s]) + misses[s] / 2) for s in range(3)) / denominator


def _ci(data: list[tuple[str, int, bool]], seed: str = "anchoring") -> tuple[float, float] | None:
    """95% percentile interval, resampling whole days, omitting single-class draws."""
    days = {}
    for day, score, hit in data:
        days.setdefault(day, []).append((score, hit))
    if not days or _auroc([(s, h) for _, s, h in data]) is None:
        return None
    rng, keys, values = random.Random(seed), sorted(days), []
    for _ in range(2000):
        value = _auroc([pair for day in rng.choices(keys, k=len(keys)) for pair in days[day]])
        if value is not None:
            values.append(value)
    values.sort()
    return (values[int((len(values) - 1) * .025)], values[int((len(values) - 1) * .975)]) if values else None


def _report_lines(name: str, cycles: list[dict]) -> list[str]:
    n = len(cycles)
    if not n:
        return [f"Anchoring: {name}: not measured (n=0)"]
    noise = sum(c["sighted"]["criterion_met"] != c["reask"]["criterion_met"] for c in cycles)
    lines = [f"Anchoring: {name}: n={n}", f"  noise (sighted != reask): {noise}/{n}"]
    for arm in ("blind", "bare"):
        paired = [c for c in cycles if arm in c]
        m = len(paired)
        if not m:
            continue
        shift = sum(c["sighted"]["criterion_met"] != c[arm]["criterion_met"] for c in paired)
        s = [c["sighted"]["criterion_met"] == c["sighted"]["claimed"] for c in paired]
        b = [c[arm]["criterion_met"] == c["sighted"]["claimed"] for c in paired]
        a, reverse = sum(x and not y for x, y in zip(s, b)), sum(y and not x for x, y in zip(s, b))
        lines += [f"  shift (sighted != {arm}): {shift}/{m}",
                  f"  claim agreement: sighted {sum(s)}/{m}, {arm} {sum(b)}/{m}; "
                  f"a={a}, b={reverse}, one-sided sign test p={metrics._sign_test(a, reverse):.4f}"]
    for arm in ARMS:
        data = [(c[arm]["episode_ts"][:10], _RANK[c["sighted"]["claimed"]], c[arm]["criterion_met"] == "yes")
                for c in cycles if arm in c]
        value = _auroc([(s, h) for _, s, h in data])
        interval = _ci(data, f"anchoring|{name}|{arm}")
        text = f"{value:.3f} (95% day bootstrap CI {interval[0]:.3f}-{interval[1]:.3f})" if interval else "not measured"
        lines.append(f"  claim AUROC against {arm}: {text}, n={len(data)}")
    same = sum(c["sighted"]["criterion_met"] == c["sighted"]["stored"] for c in cycles)
    lines.append(f"  sighted agreement with stored verdict: {same}/{n}")
    return lines


def report(names: list[str]) -> str:
    lines, pooled = [], []
    for name in names:
        grouped = {}
        for r in rows(name):
            grouped.setdefault(r["episode_ts"], {})[r["arm"]] = r
        cycles = [c for c in grouped.values() if all(arm in c for arm in ARMS[:3])]
        lines += _report_lines(name, cycles)
        pooled.extend(cycles)
    if len(names) > 1:
        lines += _report_lines("all", pooled)
    lines.append("Reading guide: anchoring shows as claim agreement and claim AUROC dropping from sighted to blind "
                 "by more than the sighted/reask noise. With no complete cycles it is not measured.")
    return "\n".join(lines) + "\n"
