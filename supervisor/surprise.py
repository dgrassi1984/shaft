"""Surprise measurement (PLAN-surprise.md): which tool results contradicted what an instance must have
expected, whether the same kind comes back, and the baseline the surprise loop is judged against.

Read from the instances' episodes and usage logs only: the supervisor never imports agent code, and an
instance cannot change how it is measured. The template's in-the-moment rule (M1) mirrors these flags; the
shared fixtures in tests/test_surprise_rules.py keep the two copies in step.

Known blind spot: the tool log keeps the first 2,000 characters of each result, so a result cut short for
the agent ("[... truncated") is only seen here when it was short to begin with.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import metrics
from .core import INSTANCES, SUP_DIR

BASELINE = SUP_DIR / "evals" / "surprise-baseline.json"

EXIT = re.compile(r"^exit (-?\d+) ·")
HTTP = re.compile(r"^HTTP (\d{3})\b")
ERROR_TYPE = re.compile(r"^ERROR: (?:[A-Za-z]+Error: )?([A-Z][A-Za-z]+(?:Error|Exception))\b|^ERROR: ([A-Za-z]+Error)")
# Most specific cause first: "zsh: can't create temp file …: operation not permitted" is the sandbox, and must land
# in the same class as every other sandbox denial.
TROUBLE = ("not permitted", "permission denied", "no such file", "command not found", "traceback", "can't", "cannot")
EMPTY = ("(empty)", "nothing relevant found", "(no results)", "no results")
WRITE_TOOLS = {"write_file", "append_file"}


def flag(result: str) -> tuple[str, str] | None:
    """(flag, detail) when a result has a known failure shape, else None.

    error             'ERROR: …' — detail: the exception type when named (PermissionError, BrokerError…)
    nonzero_exit      a `run` that exited non-zero — detail: what its output complains about
    stderr_on_success a `run` that exited 0 while stderr shows a failure (a heredoc that never applied)
    http_error        an HTTP tool that got a 4xx/5xx
    empty             nothing came back
    truncated         the result was cut short for the agent
    refused           the repeat guard did not run the call
    """
    text = (result or "").strip()
    if text.startswith("ERROR"):
        m = ERROR_TYPE.match(text)
        return "error", (m.group(1) or m.group(2)) if m else ""
    if text.startswith(("(repeat: not run)", "(not run again)", "(stopped)")):
        return "refused", ""
    if m := EXIT.match(text):
        if int(m.group(1)) != 0:   # failed: say why, wherever the command printed it (2>&1 sends it to stdout)
            return "nonzero_exit", _trouble(text)
        # exit 0: only stderr counts — stdout may be a file that merely contains "Traceback"
        trouble = _trouble(text.split("--- stderr ---", 1)[1]) if "--- stderr ---" in text else ""
        return ("stderr_on_success", trouble) if trouble else None
    if (m := HTTP.match(text)) and int(m.group(1)) >= 400:
        return "http_error", m.group(1)
    if text.lower() in EMPTY:
        return "empty", ""
    if "[... truncated" in text:
        return "truncated", ""
    return None


def _trouble(text: str) -> str:
    low = text.lower()
    return next((t for t in TROUBLE if t in low), "")


def _target(tool: str, raw_args: str) -> str:
    """The path (or URL host) a call was about, for drill-down — not part of the class."""
    m = re.search(r'"(?:path|url)"\s*:\s*"([^"]+)"', raw_args or "")
    return m.group(1)[:80] if m else ""


def kind_of(tool: str, f: str, detail: str) -> str:
    """The class a surprise belongs to, for recurrence: same flag, same cause, same kind of tool. Coarse on
    purpose — 'PermissionError on a write' is one lesson whichever protected file it hit."""
    group = "write" if tool in WRITE_TOOLS else tool
    return ":".join(x for x in (f, detail, group) if x)


def episodes(name: str, root: Path | None = None) -> list[dict]:
    base = (root or INSTANCES / name) / "memory" / "episodes"
    out = []
    for p in sorted(base.glob("*.jsonl")) if base.is_dir() else []:
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def events(name: str, root: Path | None = None) -> list[dict]:
    """Every flagged tool result in every episode (live, sleep, talk), oldest first."""
    out = []
    for ep in episodes(name, root):
        for i, t in enumerate(ep.get("tools") or []):
            hit = flag(t.get("result", ""))
            if hit:
                f, detail = hit
                out.append({"episode": ep.get("ts", ""), "episode_kind": ep.get("kind", ""), "call": i + 1,
                            "tool": t.get("tool", ""), "flag": f, "detail": detail,
                            "class": kind_of(t.get("tool", ""), f, detail),
                            "target": _target(t.get("tool", ""), t.get("args", "")),
                            "excerpt": " ".join((t.get("result") or "").split())[:140]})
    return out


def recurrence(evts: list[dict]) -> dict:
    """Classes seen in more than one episode. Retries inside one episode are one surprise, not a recurrence."""
    by_class: dict[str, list[str]] = defaultdict(list)
    for e in evts:
        if e["episode"] not in by_class[e["class"]]:
            by_class[e["class"]].append(e["episode"])
    recurred = {c: eps for c, eps in by_class.items() if len(eps) > 1}
    return {"classes": len(by_class), "recurred": len(recurred),
            "repeat_episodes": sum(len(eps) - 1 for eps in recurred.values()),
            "recurring": {c: [ts[:16] for ts in eps] for c, eps in sorted(recurred.items())}}


def _usage(name: str, root: Path | None = None) -> list[dict]:
    p = (root or INSTANCES / name) / "logs" / "llm_usage.jsonl"
    rows = []
    for line in p.read_text().splitlines() if p.exists() else []:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _med(xs: list) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 1) if xs else None


def cycles(name: str, root: Path | None = None) -> dict[str, dict]:
    """Each finished live cycle (keyed by its episode ts, as judgments are) with the model turns that made it.
    A turn is logged when it ends and the episode when the cycle ends, so a turn belongs to the first live
    episode ending at or after it; turns of a cycle still running (or one that crashed before writing its
    episode) belong to none and are left out. The model is the one most of its turns used — instances have
    switched models (shaft: Haiku, then Bonsai; wren: Bonsai, Haiku, MiMo), so every cost and success number
    is per model."""
    live = {ep["ts"]: ep for ep in episodes(name, root) if ep.get("kind") == "live"}
    ends = sorted(live)
    out = {ts: {"steps": live[ts].get("steps"), "turns": []} for ts in ends}
    for r in _usage(name, root):
        if r.get("purpose") != "live" or r.get("error"):
            continue
        end = next((e for e in ends if e >= r["ts"]), None)
        if end is not None:
            out[end]["turns"].append(r)
    for c in out.values():
        models = [t.get("model") for t in c["turns"]]
        c["model"] = max(set(models), key=models.count) if models else "unknown"
    return out


def _judged(js: list[dict]) -> dict:
    confs = [(j["confidence"] / 100, metrics.SCORE[j["criterion_met"]]) for j in js if j.get("confidence") is not None]
    return {"n": len(js),
            "success": round(sum(metrics.SCORE[j["criterion_met"]] for j in js) / len(js), 3) if js else None,
            "confidence_mean": round(sum(c for c, _ in confs) / len(confs), 3) if confs else None,
            "brier": round(sum((c - s) ** 2 for c, s in confs) / len(confs), 3) if confs else None,
            "claims_unsupported": sum(1 for j in js if not j.get("claim_supported", True))}


def baseline(name: str, root: Path | None = None) -> dict:
    eps = episodes(name, root)
    evts = events(name, root)
    calls = {k: sum(len(ep.get("tools") or []) for ep in eps if ep.get("kind") == k) for k in ("live", "sleep", "talk")}
    flagged = {k: sum(1 for e in evts if e["episode_kind"] == k) for k in calls}
    cyc = cycles(name, root)
    verdicts = {j["episode_ts"]: j for j in metrics.judgments(name)}
    by_model: dict[str, dict] = {}
    for model in dict.fromkeys(c["model"] for c in cyc.values()):   # in order of first use
        mine = {ts: c for ts, c in cyc.items() if c["model"] == model}
        turns = [t for c in mine.values() for t in c["turns"]]
        by_model[model] = {
            "cycles": len(mine), "first": min(mine)[:16], "last": max(mine)[:16],
            "steps_per_cycle_median": _med([c["steps"] for c in mine.values()]),
            "turn_seconds_median": _med([t.get("duration_s") for t in turns]),
            "turn_prompt_tokens_median": _med([t.get("prompt_tokens") for t in turns]),
            "turn_completion_tokens_median": _med([t.get("completion_tokens") for t in turns]),
            "cycle_seconds_median": _med([sum(t.get("duration_s") or 0 for t in c["turns"]) for c in mine.values()]),
            "cycle_completion_tokens_median": _med([sum(t.get("completion_tokens") or 0 for t in c["turns"])
                                                    for c in mine.values()]),
            "turns": len(turns),
            "judged": _judged([verdicts[ts] for ts in mine if ts in verdicts])}
    return {
        "instance": name,
        "window": [eps[0]["ts"] if eps else None, eps[-1]["ts"] if eps else None],
        "live_cycles": len(cyc),
        "current_model": cyc[max(cyc)]["model"] if cyc else None,
        "tool_calls": calls,
        "flagged": flagged,
        "flagged_by_class": {c: sum(1 for e in evts if e["class"] == c) for c in sorted({e["class"] for e in evts})},
        "recurrence": recurrence(evts),
        "by_model": by_model,
        "judged_all_models": _judged(list(verdicts.values())),
        "events": evts,
    }


def _pct(x) -> str:
    return "—" if x is None else f"{x:.0%}"


def report(b: dict) -> str:
    """The baseline as text an operator reads (tables, not JSON)."""
    rec = b["recurrence"]
    lines = [f"# {b['instance']} — surprise baseline",
             f"Window: {str(b['window'][0])[:16]} → {str(b['window'][1])[:16]} · {b['live_cycles']} finished "
             f"live cycles", "",
             "| Episodes | Tool calls | Unexpected results | Share |", "|---|---:|---:|---:|"]
    for k, n in b["tool_calls"].items():
        lines.append(f"| {k} | {n} | {b['flagged'][k]} | {_pct(b['flagged'][k] / n if n else None)} |")
    lines += ["", f"Kinds of unexpected result: {rec['classes']}; seen in more than one episode: {rec['recurred']} "
                  f"({rec['repeat_episodes']} repeat episode(s))."]
    lines += [f"- recurring `{c}`: {', '.join(ts)}" for c, ts in rec["recurring"].items()]
    lines += ["", "| Class | Count |", "|---|---:|"] + [f"| `{c}` | {n} |" for c, n in b["flagged_by_class"].items()]
    lines += ["", f"Live cycles by model (current: {b['current_model']}); medians; success yes=1, partial=0.5:", "",
              "| Model | Cycles | Turn s | Turn prompt tok | Turn compl. tok | Cycle s | Cycle compl. tok | Steps "
              "| Judged | Success | Confidence | Brier | Unsupported |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for model, m in b["by_model"].items():
        jm = m["judged"]
        lines.append(f"| {model} ({m['first']} → {m['last']}) | {m['cycles']} | {m['turn_seconds_median']} "
                     f"| {m['turn_prompt_tokens_median']} | {m['turn_completion_tokens_median']} "
                     f"| {m['cycle_seconds_median']} | {m['cycle_completion_tokens_median']} "
                     f"| {m['steps_per_cycle_median']} | {jm['n']} | {_pct(jm['success'])} "
                     f"| {_pct(jm['confidence_mean'])} | {jm['brier'] if jm['brier'] is not None else '—'} "
                     f"| {jm['claims_unsupported']} |")
    j = b["judged_all_models"]
    lines += ["", f"All models together: {j['n']} judged, success {_pct(j['success'])}, confidence "
                  f"{_pct(j['confidence_mean'])}, Brier {j['brier']}, unsupported claims {j['claims_unsupported']}.",
"", "| When | Episode | Tool | Flag | Target / excerpt |", "|---|---|---|---|---|"]
    lines += [f"| {e['episode'][:16]} | {e['episode_kind']} #{e['call']} | {e['tool']} | {e['flag']}"
              f"{' (' + e['detail'] + ')' if e['detail'] else ''} | {e['target'] or e['excerpt'][:70]} |"
              for e in b["events"]]
    return "\n".join(lines)


def command(names: list[str], freeze: bool) -> int:
    results = {}
    for name in names:
        b = baseline(name)
        results[name] = b
        print(report(b) + "\n")
    if freeze:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BASELINE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"frozen_at": datetime.now().isoformat(timespec="seconds"), "instances": results},
                                  indent=1, ensure_ascii=False) + "\n")
        tmp.replace(BASELINE)
        print(f"baseline frozen to {BASELINE}")
    return 0
