"""Confidence discrimination, separate from bias, from recorded verdicts only.

Never writes files or feeds probation, base rates or rewards. Bootstrap intervals are
seeded and clustered by day. These measurements make no claim about experience.
"""
from __future__ import annotations

import math
import json
import random
from bisect import bisect_left, bisect_right
from functools import lru_cache
from datetime import datetime, timedelta

from . import metrics

MIN_V3, MIN_FAILURES, MARGIN = 40, 12, -0.05
BINS = 10
MIN_CLASS = 5   # met and missed cycles each, before stats.md shows a number: one miss in 13 gave wisp "AUROC 0.12"
MIN_DAYS = 5    # days a day-clustered interval resamples: three gave shaft "AUROC 0.60 [0.17, 0.64]" (2026-10-27)


def auroc(pos: list[float], neg: list[float]) -> float | None:
    """P(a positive scores above a negative), counting ties half; None without both."""
    if not pos or not neg:
        return None
    ordered = sorted(neg)
    return sum((bisect_left(ordered, p) + bisect_right(ordered, p)) / 2 for p in pos) / (len(pos) * len(neg))


def _bins(pairs, bins):
    grouped = {}
    for p, y in pairs:
        grouped.setdefault(min(int(p * bins), bins - 1), []).append((p, y))
    return list(grouped.values())


def brier_decomposition(pairs: list[tuple[float, float]], bins: int = BINS) -> dict | None:
    """Murphy reliability, resolution and uncertainty, using each bin's mean forecast.

    Brier = reliability - resolution + uncertainty is exact when each bin contains
    one forecast value, approximate otherwise. Brier itself uses the raw forecasts.
    """
    if not pairs:
        return None
    n = len(pairs)
    rate = sum(y for _, y in pairs) / n
    reliability = resolution = 0.0
    for group in _bins(pairs, bins):
        p = sum(p for p, _ in group) / len(group)
        y = sum(y for _, y in group) / len(group)
        reliability += len(group) / n * (p - y) ** 2
        resolution += len(group) / n * (y - rate) ** 2
    return {"reliability": reliability, "resolution": resolution, "uncertainty": rate * (1 - rate),
            "brier": sum((p - y) ** 2 for p, y in pairs) / n, "n": n}


def meta_i(pairs, bins: int = BINS) -> dict | None:
    """Binned mutual information in bits, with the Miller-Madow bias correction.

    Subtract (occupied confidence bins - 1) * (occupied outcomes - 1) /
    (2*n*ln(2)), then clip at zero. The share uses empirical outcome entropy;
    it is undefined when all outcomes are alike.
    """
    if not pairs:
        return None
    n = len(pairs)
    groups = _bins(pairs, bins)
    counts = [sum(y == value for _, y in pairs) for value in (0, 1)]
    entropy = -sum(c / n * math.log2(c / n) for c in counts if c)
    info = 0.0
    for group in groups:
        for value, total in enumerate(counts):
            count = sum(y == value for _, y in group)
            if count:
                info += count / n * math.log2(count * n / (len(group) * total))
    info = max(0.0, info - (len(groups) - 1) * (sum(c > 0 for c in counts) - 1) / (2 * n * math.log(2)))
    return {"bits": info, "share": info / entropy if entropy else None, "n": n}


def cluster_ci(rows: list[dict], stat, *, key=lambda r: r["episode_ts"][:10], n_boot: int = 2000,
               seed: str = "metacog") -> tuple[float, float] | None:
    """95% percentile interval from resampled days; none from fewer than MIN_DAYS days (three days have only ten
    distinct resamples, so the "interval" is a handful of values), and none over 10% undefined draws.

    Percentiles interpolate linearly between adjacent sorted bootstrap estimates.
    """
    clusters = {}
    for row in rows:
        clusters.setdefault(key(row), []).append(row)
    if len(clusters) < MIN_DAYS or n_boot < 1:
        return None
    groups = list(clusters.values())
    rng = random.Random(seed)
    values = []
    for _ in range(n_boot):
        sample = [row for group in rng.choices(groups, k=len(groups)) for row in group]
        value = stat(sample)
        if value is not None:
            values.append(value)
    if n_boot - len(values) > n_boot * 0.1 or not values:
        return None
    values.sort()

    def percentile(p):
        at = (len(values) - 1) * p
        lo, hi = math.floor(at), math.ceil(at)
        return values[lo] + (values[hi] - values[lo]) * (at - lo)

    return percentile(0.025), percentile(0.975)


def domain(ep: dict) -> str:
    """Most frequent tool family, ignoring the instance's labels and unknown tools."""
    counts = {"coding": 0, "research": 0, "upkeep": 0, "social": 0}
    for call in ep.get("tools", []):
        tool = call.get("tool")
        family = None
        if tool in ("run", "delegate_coding"):
            family = "coding"
        elif tool in ("web_search", "fetch_page", "http_request", "decide"):
            family = "research"
        elif tool in ("send_message", "list_instances"):
            family = "social"
        elif tool in ("remember", "rewrite_facts", "write_digest", "update_self_model", "update_personality",
                      "record", "history", "search_memory", "read_file", "list_files"):
            family = "upkeep"
        elif tool in ("write_file", "append_file"):
            path = metrics._arg(call.get("args") or "", "path")
            while path.startswith("./"):
                path = path[2:]
            if path.startswith(("shaft/", "plugins/", "tests/")):
                family = "coding"
            elif path.startswith(("self/", "memory/", "notes/", "skills/")):
                family = "upkeep"
        if family:
            counts[family] += 1
    top = max(counts.values())
    winners = [family for family, count in counts.items() if count == top]
    return "none" if not top else winners[0] if len(winners) == 1 else "mixed"


def _auc(rows, field="confidence"):
    return auroc([r[field] for r in rows if r["criterion_met"] == "yes"],
                 [r[field] for r in rows if r["criterion_met"] != "yes"])


def _claim_auc(rows):
    return _auc([{**r, "claim_score": metrics.SCORE[r["claimed"]] * 2} for r in rows], "claim_score")


def claim_matrix(rows: list[dict]) -> dict:
    """#98: all judged cycles with both labels; the sampled second judge sees the claim too, unlike A0."""
    counts = {claim: {verdict: 0 for verdict in metrics.SCORE} for claim in metrics.SCORE}
    n = 0
    for r in rows:
        if r.get("claimed") in counts and r.get("criterion_met") in metrics.SCORE:
            counts[r["claimed"]][r["criterion_met"]] += 1
            n += 1
    return {"source": "all judged cycles", "counts": counts, "n": n, "excluded": len(rows) - n}


def doubt_alignment(rows: list[dict], episodes: dict[str, dict]) -> dict:
    """#98: share of cycles with both a located error and doubts where any doubt is at or before the error."""
    matched = n = 0
    for r in rows:
        error = r.get("first_error_step")
        doubts = (episodes.get(r["episode_ts"], {}).get("finish") or {}).get("doubts")
        steps = [d["step"] for d in doubts if isinstance(d, dict) and type(d.get("step")) is int
                 and d["step"] >= 1] if isinstance(doubts, list) else []
        if type(error) is int and error >= 1 and steps:
            n += 1
            matched += any(step <= error for step in steps)
    return {"matched": matched, "n": n, "rate": matched / n if n else None}


@lru_cache(maxsize=32)
def _cached_ci(content: str, stat) -> tuple[float, float] | None:
    return cluster_ci(json.loads(content), stat)


def _ci(rows, stat):
    return _cached_ci(json.dumps(rows, sort_keys=True), stat)


def _difference(sample):
    first, final = _auc(sample, "confidence_first"), _auc(sample)
    return final - first if first is not None and final is not None else None


def summary(rows: list[dict], episodes: dict[str, dict] | None = None, versions: tuple = ("v2", "v3")) -> dict:
    """Current-judge confidence of `versions` (v2/v3 by default: the question the scores use now; v1 asked "probability
    of success" and is reported apart); v4 clause confidence stays apart, claims cover known v1-v4 questions."""
    current = [r for r in rows if metrics.by_current_judge(r)]
    scored = [r for r in current if metrics.VERSION_OF.get(r.get("confidence_of")) in versions
              and r.get("confidence") is not None]
    claims = [r for r in current if r.get("claimed") in metrics.SCORE
              and metrics.VERSION_OF.get(r.get("confidence_of")) in ("v1", "v2", "v3", "v4")]
    n = len(scored)
    met = sum(r["criterion_met"] == "yes" for r in scored)
    mean = sum(r["confidence"] for r in scored) / n if n else None
    ci = _ci(scored, _auc)
    both = [r for r in current if metrics.second_counted(r)]
    pairs = [(r["confidence"] / 100, float(r["criterion_met"] == "yes")) for r in scored]
    result = {"n": n, "met": met, "hit_rate": met / n if n else None, "mean_confidence": mean,
              "sd": math.sqrt(sum((r["confidence"] - mean) ** 2 for r in scored) / n) if n else None,
              "bias": mean / 100 - met / n if n else None, "auroc": _auc(scored), "ci": ci,
              "h1": "above chance" if ci and ci[0] > 0.5 else "below chance" if ci and ci[1] < 0.5 else "not shown",
              "brier": brier_decomposition(pairs), "meta_i": meta_i(pairs),
              "claim_auroc": _claim_auc(claims), "claim_ci": _ci(claims, _claim_auc),
              "second_agreement": (sum(r["criterion_met"] == r["second"]["criterion_met"] for r in both), len(both))}
    if episodes is not None:
        groups = {}
        for r in scored:
            family = domain(episodes.get(r["episode_ts"], {}))
            groups.setdefault(family, []).append(r)
        result["by_domain"] = {family: {"n": len(group), "hit_rate": sum(r["criterion_met"] == "yes" for r in group)
                                                       / len(group), "auroc": _auc(group)}
                               for family, group in sorted(groups.items())}
    return result


def weekly(rows: list[dict], now: datetime, weeks: int = 4) -> list[dict]:
    """#245: current-judge v2/v3 confidence in rolling seven-day windows, newest first."""
    scored = [(metrics._ts(r.get("episode_ts")), r) for r in rows
              if metrics._ts(r.get("episode_ts")) and metrics.by_current_judge(r)
              and metrics.VERSION_OF.get(r.get("confidence_of")) in ("v2", "v3")
              and r.get("confidence") is not None]
    result = []
    for k in range(weeks):
        end = now - timedelta(days=7 * k)
        start = end - timedelta(days=7)
        group = [r for ts, r in scored if start < ts <= end]
        n = len(group)
        met = sum(r["criterion_met"] == "yes" for r in group)
        result.append({"end": end, "n": n,
                       "auroc": _auc(group) if min(met, n - met) >= MIN_CLASS else None})
    return result


def _weekly_fragment(rows, now):
    return "; ".join(f"{w['end']:%m-%d} {_number(w['auroc'])} (n {w['n']})"
                     for w in weekly(rows, now) if w["n"]) or "unavailable"


def paired_v3(rows: list[dict]) -> dict:
    """Paired ranking change after seeing the base rate, with a day-cluster interval.

    Failing to show non-inferiority is not evidence that v3 hurts. The sample floor
    is not a claim of adequate power; only an interval wholly below the margin says worse.
    """
    paired = [r for r in rows if metrics.by_current_judge(r)
              and metrics.VERSION_OF.get(r.get("confidence_of")) == "v3"
              and r.get("confidence_first") is not None and r.get("confidence") is not None]

    n = len(paired)
    failures = sum(r["criterion_met"] != "yes" for r in paired)
    ci = _ci(paired, _difference)
    verdict = "not enough data"
    if n >= MIN_V3 and failures >= MIN_FAILURES:
        verdict = "non-inferior" if ci and ci[0] > MARGIN else "worse" if ci and ci[1] < MARGIN else "inconclusive"
    return {"n": n, "failures": failures, "auroc_first": _auc(paired, "confidence_first"),
            "auroc_final": _auc(paired), "diff": _difference(paired), "ci": ci, "verdict": verdict}


def _number(value, signed=False):
    return "unavailable" if value is None else format(value, "+.2f" if signed else ".2f")


def _interval(ci):
    return f" [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else " [CI unavailable]"


def _paired_line(p):
    return (f"Paired v3: {p['verdict']}; n {p['n']}, failures {p['failures']}, AUROC "
            f"{_number(p['auroc_first'])} before, {_number(p['auroc_final'])} final, "
            f"change {_number(p['diff'], True)}{_interval(p['ci'])}; margin {MARGIN:+.2f}")


def section(name: str) -> list[str]:
    """At most eleven plain stats.md lines; no section below ten scored confidences."""
    rows = metrics.judgments_for_stats(name)
    scored = [r for r in rows if metrics.by_current_judge(r)
              and metrics.VERSION_OF.get(r.get("confidence_of")) in ("v2", "v3")
              and r.get("confidence") is not None]
    met = sum(r["criterion_met"] == "yes" for r in scored)
    if len(scored) < 10 or min(met, len(scored) - met) < MIN_CLASS:
        return []
    s = summary(rows, {ep["ts"]: ep for ep in metrics.live_episodes(name)})
    b, mi = s["brier"], s["meta_i"]
    lines = ["## How well my confidence tells my met cycles from my missed ones",
             f"- Confidence AUROC {_number(s['auroc'])}{_interval(s['ci'])}; 0.5 means chance; n {s['n']}.",
             f"- By week (ending): {_weekly_fragment(rows, datetime.now())}; 0.55 or below is chance.",
             f"- Above chance: {'yes' if s['h1'] == 'above chance' else 'below' if s['h1'] == 'below chance' else 'not shown yet'}"
             f"; met {s['met']}/{s['n']}, hit rate {s['hit_rate']:.1%}.",
             f"- Mean confidence {s['mean_confidence']:.1f}%; bias {s['bias']:+.2f}; SD {s['sd']:.1f} points.",
             f"- Brier {b['brier']:.3f}; reliability {b['reliability']:.3f} is calibration error; "
             f"resolution {b['resolution']:.3f} separates outcomes; uncertainty {b['uncertainty']:.3f}.",
             f"- Meta-I {mi['bits']:.3f} bits; outcome entropy share {_number(mi['share'])}.",
             f"- After-action claim AUROC {_number(s['claim_auroc'])}{_interval(s['claim_ci'])}; "
             "the judge sees my claim, so this may be judge-anchored.",
             f"- Second-judge agreement {s['second_agreement'][0]}/{s['second_agreement'][1]}."]
    lines.append("- Tool-log domains: " + ", ".join(f"{d} {v['n']}" for d, v in s["by_domain"].items()) + ".")
    p = paired_v3(rows)
    if p["n"]:
        lines.append("- " + _paired_line(p) + "; failure to show non-inferiority does not show harm.")
    return lines


def review_line(rows: list[dict], now: datetime | None = None) -> str:
    """One confidence/claim comparison for the operator's review."""
    s = summary(rows)
    sd = "unavailable" if s["sd"] is None else f"{s['sd']:.1f}"
    return (f"confidence AUROC {_number(s['auroc'])}{_interval(s['ci'])} (n {s['n']}), "
            f"claim AUROC {_number(s['claim_auroc'])}, bias {_number(s['bias'], True)}, SD {sd}"
            f"; by week (ending): {_weekly_fragment(rows, now if now is not None else datetime.now())}")


def report(names: list[str]) -> str:
    """Full read-only report, including tool-log domains and the paired v3 test."""
    lines = []
    for name in names:
        rows = metrics.judgments_for_stats(name)
        episodes = {ep["ts"]: ep for ep in metrics.live_episodes(name)}
        s = summary(rows, episodes)
        old = summary(rows, versions=("v1",))
        clauses = summary(rows, versions=("v4",))
        pooled = [r for r in rows if r.get("confidence") is not None
                  and metrics.VERSION_OF.get(r.get("confidence_of")) != "v4"]
        lines += [f"{name}:", f"  n {s['n']}, met {s['met']}, hit rate {_number(s['hit_rate'])}, "
                  f"mean confidence {_number(s['mean_confidence'])}", "  " + review_line(rows),
                  f"  H1: {s['h1']}",
                  f"  v1 (older question, apart): AUROC {_number(old['auroc'])}{_interval(old['ci'])} (n {old['n']}), "
                  f"SD {_number(old['sd'])}",
                  f"  v4 (clauses, apart): AUROC {_number(clauses['auroc'])}{_interval(clauses['ci'])} (n {clauses['n']}), "
                  f"SD {_number(clauses['sd'])}",
                  f"  non-clause confidence, every judge (the proposal's table): AUROC {_number(_auc(pooled))} "
                  f"(n {len(pooled)})"]
        matrix = claim_matrix(rows)
        lines.append(f"  Claim confusion matrix ({matrix['source']}; n {matrix['n']}, excluded {matrix['excluded']}): "
                     "rows claim, columns judge yes/partial/no; second judge sees the claim, A0 is a separate regrade")
        for claim, counts in matrix["counts"].items():
            lines.append(f"    {claim}: " + "/".join(str(counts[v]) for v in metrics.SCORE))
        alignment = doubt_alignment(rows, episodes)
        lines.append(f"  Doubted step equals or precedes first error: {alignment['matched']}/{alignment['n']} "
                     f"(rate {_number(alignment['rate'])}; cycles with both)")
        b, mi = s["brier"], s["meta_i"]
        lines.append("  Brier: " + (", ".join(f"{k} {b[k]:.4f}" for k in
                     ("brier", "reliability", "resolution", "uncertainty")) if b else "unavailable"))
        lines.append(f"  Meta-I: {_number(mi['bits'] if mi else None)} bits, "
                     f"entropy share {_number(mi['share'] if mi else None)}")
        lines.append(f"  Claim CI:{_interval(s['claim_ci'])}; second-judge agreement "
                     f"{s['second_agreement'][0]}/{s['second_agreement'][1]}")
        for family, d in s["by_domain"].items():
            lines.append(f"  {family}: n {d['n']}, hit rate {d['hit_rate']:.3f}, AUROC {_number(d['auroc'])}")
        lines += ["  " + _paired_line(paired_v3(rows)),
                  "  Failing to show non-inferiority does not show harm; 40 cycles and 12 failures are only a floor.",
                  "  AUROC 0.5 = chance; the claim's AUROC may be judge-anchored until `shaft anchoring` says otherwise."]
    return "\n".join(lines)
