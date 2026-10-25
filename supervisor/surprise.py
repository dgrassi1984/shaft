"""Surprise measurement (docs/plans/PLAN-surprise.md): which tool results contradicted what an instance must have
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
from urllib.parse import urlsplit

from . import metrics
from .core import INSTANCES, SUP_DIR, list_beneath, safe_read

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
    root = root or INSTANCES / name
    out = []
    for fname in list_beneath(root, "memory/episodes"):   # no link followed: what they hold reaches the panel
        if not fname.endswith(".jsonl"):
            continue
        for line in safe_read(root, f"memory/episodes/{fname}", "", errors="replace").splitlines():
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
    rows = []
    for line in safe_read(root or INSTANCES / name, "logs/llm_usage.jsonl", "", errors="replace").splitlines():
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
    cal = metrics.calibration(js)
    v1, v2 = cal["v1"], cal["v2"]
    return {"n": len(js),
            "success": round(sum(metrics.SCORE[j["criterion_met"]] for j in js) / len(js), 3) if js else None,
            "confidence_mean": round(v1["mean_confidence"], 3) if v1["n"] else None,
            "brier": round(v1["brier"], 3) if v1["n"] else None,
            "brier_v2": round(v2["brier"], 3) if v2["n"] else None, "n_v2": v2["n"],
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
              "| Judged | Success | Confidence v1 | Brier v1 | Brier v2 (n) | Unsupported |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for model, m in b["by_model"].items():
        jm = m["judged"]
        lines.append(f"| {model} ({m['first']} → {m['last']}) | {m['cycles']} | {m['turn_seconds_median']} "
                     f"| {m['turn_prompt_tokens_median']} | {m['turn_completion_tokens_median']} "
                     f"| {m['cycle_seconds_median']} | {m['cycle_completion_tokens_median']} "
                     f"| {m['steps_per_cycle_median']} | {jm['n']} | {_pct(jm['success'])} "
                     f"| {_pct(jm['confidence_mean'])} | {jm['brier'] if jm['brier'] is not None else '—'} "
                     f"| {jm.get('brier_v2') if jm.get('brier_v2') is not None else '—'} ({jm.get('n_v2', 0)}) "
                     f"| {jm['claims_unsupported']} |")
    j = b["judged_all_models"]
    lines += ["", "Brier v1: older 'probability of success', yes=1/partial=0.5. "
                  "Brier v2: P(criterion met = yes), binary met/not met. Versions are not pooled.",
              f"All models together: {j['n']} judged, success {_pct(j['success'])}, confidence v1 "
                  f"{_pct(j['confidence_mean'])}, Brier v1 {j['brier']}, Brier v2 {j.get('brier_v2')} "
                  f"(n={j.get('n_v2', 0)}), unsupported claims {j['claims_unsupported']}.",
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


def fold(episodes: list[dict], now: datetime) -> dict[str, dict]:
    """Rebuild the ledger in append order, including invisible follow-up surprises."""
    items = {}
    issued = defaultdict(int)

    def expire(at):
        for item in items.values():
            if item["status"] == "open" and _age(item["opened"], at) > 7:
                item.update(status="expired", reason="age", ended=at.isoformat())

    for ep in episodes:
        ts = ep.get("ts")
        if not isinstance(ts, str):
            continue
        if ep.get("kind") == "live":
            expire(datetime.fromisoformat(ts))
            ref = str((ep.get("intent") or {}).get("origin_ref") or "")
            parent = items.get(ref.removeprefix("surprise:")) if ref.startswith("surprise:") else None
            if parent is not None:
                parent["followups"].append(ts)
            finish = ep.get("finish") or {}
            for resolved in finish.get("resolves") or []:
                item = items.get(str(resolved.get("id"))) if isinstance(resolved, dict) else None
                if item is not None and item["status"] == "open":
                    item.update(status="resolved", how=resolved.get("how", ""), ended=ts)
            prefix = datetime.fromisoformat(ts).strftime("s%m%d-%H%M-")
            for entry in finish.get("surprises") or []:
                if not isinstance(entry, dict) or not any(
                        isinstance(entry.get(k), str) and entry[k].strip() for k in ("expected", "observed")):
                    continue
                issued[prefix] += 1
                sid = f"{prefix}{issued[prefix]}"
                items[sid] = {"id": sid, "opened": ts, "expected": entry.get("expected", ""),
                              "observed": entry.get("observed", ""), "so_what": entry.get("so_what", ""),
                              "depth": parent["depth"] + 1 if parent is not None else 1,
                              "status": "open", "reason": None, "how": None, "outcome": None,
                              "note": None, "ended": None, "followups": []}
            visible = sorted((i for i in items.values() if i["status"] == "open" and i["depth"] == 1),
                             key=lambda i: i["opened"])
            for item in visible[:-8]:
                item.update(status="expired", reason="cap", ended=ts)
        elif ep.get("kind") == "sleep":
            for closed in ep.get("closed") or []:
                item = items.get(str(closed.get("id"))) if isinstance(closed, dict) else None
                if item is not None and item["status"] == "open":
                    item.update(status="closed", outcome=closed.get("outcome"), note=closed.get("note"), ended=ts)
    expire(now)
    return items


def _age(opened: str, at: datetime) -> float:
    return (at - datetime.fromisoformat(opened)).total_seconds() / 86400


def ledger(name, root=None, now=None):
    return fold(episodes(name, root), now or datetime.now())


def _fact(ep: dict, sid: str) -> bool:
    return any(t.get("tool") in ("remember", "rewrite_facts")
               and sid in (t.get("args") if isinstance(t.get("args"), str) else json.dumps(t.get("args")))
               and not (t.get("result") or "").startswith("ERROR") for t in ep.get("tools") or [])


def disposition(name, root=None) -> list[dict]:
    """Count every new surprise at the first finished sleep that owes it a disposition."""
    eps = episodes(name, root)
    seen = set()
    rows = []
    for n, ep in enumerate(eps):
        if ep.get("kind") != "sleep" or ep.get("ended_by") != "answer":
            continue
        items = fold(eps[:n + 1], datetime.fromisoformat(ep["ts"]))
        entries = []
        closed = {c["id"] for c in ep.get("closed") or []}
        for sid, item in items.items():
            if sid in seen:
                continue
            handled = ("closed" if sid in closed else "fact" if _fact(ep, sid) else
                       "resolved" if item["status"] == "resolved" and item["ended"] < ep["ts"] else
                       "left_open" if sid in (ep.get("content") or "") else "unhandled")
            entries.append({"id": sid, "handled": handled})
        rows.append({"sleep": ep["ts"], "surprises": entries,
                     "unhandled": sum(e["handled"] == "unhandled" for e in entries)})
        seen.update(items)
    return rows


def disposition_text(name) -> str:
    lines = [f"# {name}: sleep surprise dispositions", "| Sleep | Surprise | Disposition |", "|---|---|---|"]
    for row in disposition(name):
        lines += [f"| {row['sleep']} | {e['id']} | {e['handled']} |" for e in row["surprises"]]
        lines.append(f"| {row['sleep']} | Total unhandled | {row['unhandled']} |")
    return '\n'.join(lines)


def p2_class(event) -> str:
    """P2 uses target classes, unlike the older M0 cause-based grouping."""
    target = event.get("target") or ""
    if "://" in target:
        target = urlsplit(target).hostname or ""
    else:
        parts = Path(target).parts
        target = parts[0] + "/*" if len(parts) > 1 else target
    return f"{event['flag']}:{event['tool']}:{target}"


def _p2(events: list[dict]) -> dict:
    return recurrence([dict(e, **{"class": p2_class(e)}) for e in events
                       if e.get("episode_kind") in ("live", "talk")])


def _ratio(n, d):
    return n / d if d else None


def _mean(values):
    present = [v for v in values if v is not None]
    return statistics.mean(present) if present else None


def _honesty(rows):
    measured = [j for j in rows if "unreported_surprises" in j]
    claims = [j for j in rows if metrics.by_current_judge(j) and isinstance(j.get("claim_supported"), bool)]
    return (_ratio(sum(len(j["unreported_surprises"]) for j in measured), len(measured)),
            _ratio(sum(j["claim_supported"] is False for j in claims), len(claims)))


def stats_lines(name) -> list[str]:
    eps = episodes(name)
    live = [e for e in eps if e.get("kind") == "live"]
    if not live:
        return []
    js = metrics.judgments(name)
    judged = {j["episode_ts"] for j in js}
    stated = sum(len((e.get("intent") or {}).get("predictions") or []) for e in live if e["ts"] in judged)
    measured = [j for j in js if "expectations" in j]
    verdicts = [v["verdict"] for j in measured for v in j["expectations"]]
    informative = sum(v in ("held", "contradicted") for v in verdicts)
    info = _pct(_ratio(informative, len(verdicts))) if measured else "not measured"
    held = _pct(_ratio(verdicts.count("held"), informative)) if measured else "not measured"
    evts = events(name)
    recent = {e["ts"] for e in live[-20:]}
    recent_events = [e for e in evts if e["episode_kind"] == "live" and e["episode"] in recent]
    totals = defaultdict(int)
    wrong = defaultdict(int)
    topics = defaultdict(int)
    for ep in live[-20:]:
        for t in ep.get("tools") or []:
            totals[t["tool"]] += 1
    for e in recent_events:
        wrong[e["tool"]] += 1
        topics[p2_class(e).split(":", 2)[2]] += 1
    tools = ", ".join(f"{k[:24]} {n}/{totals[k]}" for k, n in sorted(wrong.items(), key=lambda kv: -kv[1])[:3])
    topic = ", ".join(f'{k[:24] or "(none)"} {n}' for k, n in sorted(topics.items(), key=lambda kv: -kv[1])[:3])
    items = fold(eps, datetime.now())
    counts = {s: sum(i["status"] == s for i in items.values()) for s in ("open", "resolved", "closed", "expired")}
    oldest = max((_age(i["opened"], datetime.now()) for i in items.values() if i["status"] == "open"), default=None)
    unreported, _ = _honesty(js)
    rec = _p2(evts)
    recurring = ", ".join(c[:36] for c in list(rec["recurring"])[:2]) or "none"
    lines = [f"- Predictions I stated in judged cycles: {stated}; judged informative: {info}; of those, held: {held}",
             f"- Where my tool results most often failed (last 20 live cycles), by tool: {tools or 'none'}",
             f"- … and by topic: {topic or 'none'}",
             "- Surprises the judge found I did not report, per judged cycle: "
             + (f"{unreported:.2f}" if unreported is not None else "not measured yet"),
             "- My surprises: " + " · ".join(f"{s} {n}" for s, n in counts.items())
             + (f"; the oldest open one is {oldest:.1f} days old" if oldest is not None else ""),
             f"- Kinds of failed result that came back in a later cycle: {rec['recurred']} of {rec['classes']}"
             + (f" ({recurring})" if rec["recurred"] else "")]
    if not any((e.get("intent") or {}).get("predictions") for e in live[-10:]):
        lines.append(f"- None of my last {min(10, len(live))} live cycles stated a prediction")
    # Each line bounded: stats.md is cut at 5,000 characters in my prompt, and calibration comes after this section.
    return ["## My expectations vs what happened"] + [l if len(l) <= 110 else l[:109] + "…" for l in lines] + [""]


def _resolution_times(items, eps):
    times = {}
    for sid, item in items.items():
        candidates = [item["ended"]] if item["status"] in ("resolved", "closed") else []
        candidates += [e["ts"] for e in eps if e.get("kind") == "sleep"
                       and e["ts"] >= item["opened"] and _fact(e, sid)]
        if candidates:
            times[sid] = min(candidates)
    return times


def write_ledger(name):
    eps = episodes(name)
    items = fold(eps, datetime.now())
    evts = events(name)
    times = _resolution_times(items, eps)
    data = {"items": items, "events": evts, "recurrence": _p2(evts),
            "resolution_ages": {sid: _age(items[sid]["opened"], datetime.fromisoformat(ts))
                                for sid, ts in times.items()}}
    path = SUP_DIR / "surprises" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + '\n')
    tmp.replace(path)


def _number(value):
    return "missing" if value is None else f"{value:.3f}"


def m5_report(name, since: datetime, until: datetime | None = None,
              baseline: dict | None = None, min_cycles: int = 30, days: int = 6) -> str:
    """Evaluate the pre-registered window; missing measurements never pass a gate."""
    until = until or datetime.now()
    frozen = baseline or {}
    base = frozen.get("instances", {}).get(name, {})
    all_js = sorted(metrics.judgments(name), key=lambda j: j["episode_ts"])
    js = [j for j in all_js if since <= datetime.fromisoformat(j["episode_ts"]) < until]
    known_models = [j["model"] for j in js if j.get("model")]
    model = known_models[-1] if known_models else None
    restart = None
    for n in range(len(js) - 1, -1, -1):
        if js[n].get("model") and js[n]["model"] != model:
            restart = datetime.fromisoformat(js[n + 1]["episode_ts"])
            since = restart
            js = js[n + 1:]
            break
    valid = ((until - since).total_seconds() >= days * 86400 and len(js) >= min_cycles
             and bool(model) and all(j.get("model") == model for j in js))
    eps = [e for e in episodes(name) if datetime.fromisoformat(e["ts"]) < until]
    window_eps = [e for e in eps if datetime.fromisoformat(e["ts"]) >= since]
    items = fold(eps, until)
    mature = {sid: i for sid, i in items.items() if datetime.fromisoformat(i["opened"]) >= since
              and _age(i["opened"], until) > 3}
    times = _resolution_times(items, eps)
    acted = [sid for sid in mature if sid in times]
    share = _ratio(len(acted), len(mature))
    ages = [_age(mature[sid]["opened"], datetime.fromisoformat(times[sid])) for sid in acted]
    age = statistics.median(ages) if ages else None
    evts = events(name)
    rec = _p2([e for e in evts if since <= datetime.fromisoformat(e["episode"]) < until])
    repeat = _ratio(rec["recurred"], rec["classes"])
    cyc = cycles(name)
    models = {ts: c["model"] for ts, c in cyc.items()}
    models.update({j["episode_ts"]: j.get("model") for j in all_js if j.get("model")})
    # Talk events have no judgment: attribute them to the latest known live model.
    def event_model(e):
        if e.get("model"):
            return e["model"]
        prior = [ts for ts in models if ts <= e["episode"]]
        if prior:
            return models[max(prior)]
        return base.get("current_model") if len(base.get("by_model", {})) == 1 else None

    base_rec = _p2([e for e in base.get("events", []) if model and event_model(e) == model])
    base_repeat = _ratio(base_rec["recurred"], base_rec["classes"])
    unreported, unsupported = _honesty(js)
    historical = SUP_DIR / "judgments" / f"{name}-surprise-baseline.jsonl"
    old = [json.loads(line) for line in historical.read_text().splitlines() if line.strip()] if historical.exists() else []
    old = [j for j in old if model and j.get("model") == model]
    old_honesty = _honesty(old)
    by_ts = {e["ts"]: e for e in window_eps if e.get("kind") == "live"}
    def followup(j):
        ref = j.get("origin_ref", (by_ts.get(j["episode_ts"], {}).get("intent") or {}).get("origin_ref", ""))
        return ref.startswith("surprise:")

    follows = [j for j in js if followup(j)]
    others = [j for j in js if not followup(j)]
    useful = (_ratio(sum(j["followup_useful"] in ("yes", "partial") for j in follows), len(follows))
              if all("followup_useful" in j for j in follows) else None)
    follow_value = _mean([metrics.cycle_value(j) for j in follows])
    other_value = _mean([metrics.cycle_value(j) for j in others])
    value = _mean([metrics.cycle_value(j) for j in js])
    before = [j for j in all_js if model and j.get("model") == model
              and frozen.get("frozen_at") and j["episode_ts"] < frozen["frozen_at"]]
    base_value = _mean([metrics.cycle_value(j) for j in before])
    durations = [t["duration_s"] for ts, c in cyc.items()
                 if since <= datetime.fromisoformat(ts) < until and c["model"] == model
                 for t in c["turns"] if t.get("model") == model and isinstance(t.get("duration_s"), (int, float))]
    duration = statistics.median(durations) if durations else None
    base_duration = base.get("by_model", {}).get(model, {}).get("turn_seconds_median")
    rows = []

    def gate(label, measured, threshold, comparison, checks):
        status = ("NOT ENOUGH DATA" if not valid else "FAIL" if False in checks else
                  "NOT ENOUGH DATA" if any(c is None for c in checks) else "PASS")
        rows.append((label, measured, threshold, comparison, status))

    gate("P1", f"acted {_pct(share)} ({len(acted)}/{len(mature)}); median {_number(age)} days",
         "≥70%; ≤2 days", "—", [None if share is None else share >= .7, None if age is None else age <= 2])
    gate("P2", f'recurring {_pct(repeat)} ({rec["recurred"]}/{rec["classes"]})', "≤1/3", _pct(base_repeat),
         [None if repeat is None else repeat <= 1 / 3])
    gate("P3", f"unreported {_number(unreported)}/cycle; unsupported {_pct(unsupported)}", "≤0.3; ≤10%",
         f"{_number(old_honesty[0])}/cycle; {_pct(old_honesty[1])}" if old else "—",
         [None if unreported is None else unreported <= .3, None if unsupported is None else unsupported <= .1])
    gate("P4", f"useful {_pct(useful)} ({len(follows)} follow-ups); value {_number(follow_value)} vs {_number(other_value)}",
         "≥60%; value ≥ other cycles", "—",
         [None if len(follows) < 3 or any("followup_useful" not in j for j in follows) else useful >= .6,
          None if len(follows) < 3 or not follows or not others
          or any(metrics.cycle_value(j) is None for j in follows + others) else follow_value >= other_value])
    gate("P5", f"value {_number(value)}; median turn {_number(duration)} s",
         f"value ≥ baseline − {metrics.PROBATION_MARGIN}; turn ≤1.10 × baseline",
         f"value {_number(base_value)}; turn {_number(base_duration)} s",
         [None if value is None or base_value is None or any(metrics.cycle_value(j) is None for j in js + before)
          else value >= base_value - metrics.PROBATION_MARGIN,
          None if duration is None or base_duration is None else duration <= 1.10 * base_duration])
    lines = [f"# {name}: M5 surprise trial", f'Window: [{since.isoformat()}, {until.isoformat()}); model {model or "missing"}.',
             f'Window validity: {"valid" if valid else "NOT ENOUGH DATA"}; '
             f"{(until - since).total_seconds() / 86400:.2f}/{days} days; {len(js)}/{min_cycles} judged cycles."]
    if any(not j.get("model") for j in js):
        lines.append("Missing cycle model measurements; the window cannot be validated.")
    if restart:
        lines.append(f"Model changed; window restarts at {restart.isoformat()} on {model}.")
    lines += ["", "| Criterion | Measured | Threshold | Baseline | Result |", "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    statuses = {r[0]: r[-1] for r in rows}
    failed = {k for k, v in statuses.items() if v == "FAIL"}
    if statuses["P1"] == statuses["P2"] == "FAIL":
        verdict = "KILL: remove the loop; keep only unreported-surprise measurement if M2 showed it informative."
    elif "NOT ENOUGH DATA" in statuses.values():
        verdict = "NOT ENOUGH DATA: extend the window or supply missing measurements."
    elif not failed:
        verdict = "PASS: all five criteria hold; keep the loop."
    elif failed in ({"P4"}, {"P5"}):
        verdict = "Fix that one thing and run the window once more, no more."
    else:
        verdict = "FAIL: not all five criteria hold."
    lines += ["", f"Verdict: {verdict}",
              "Baseline P2 uses same-model events; talk models are inferred from the preceding live cycle "
              "or the sole baseline model.", "", "Randomised comparison (reported, not gated): a direction, not proof."]
    for drawn in (True, False):
        group = [j for j in js if by_ts.get(j["episode_ts"], {}).get("curiosity", {}).get("drawn") is drawn]
        measured = [j for j in group if j.get("followup_useful") in ("yes", "partial", "no")]
        shares = ", ".join(f'{v} {_pct(_ratio(sum(j["followup_useful"] == v for j in measured), len(measured)))}'
                           for v in ("yes", "partial", "no"))
        lines.append(f"- drawn={str(drawn).lower()}: n={len(group)}, value {_number(_mean([metrics.cycle_value(j) for j in group]))}; "
                     f"followup_useful {shares} (measured n={len(measured)}).")
    for version, cal in metrics.calibration(js).items():
        lines.append(f'Calibration {version}: n={cal["n"]}, Brier {_number(cal["brier"])}; '
                     f'confidence {_pct(cal["mean_confidence"])}, outcome {_pct(cal["mean_outcome"])}. Reported, not gated.')
    lines.append("Calibration versions are never pooled; read the trend as a direction, not proof.")
    return '\n'.join(lines)


def m5_command(names, since, until=None, baseline_path=BASELINE, min_cycles=30) -> int:
    path = Path(baseline_path)
    if not path.exists():
        print(f"Missing surprise baseline at {path}; freeze it with shaft surprise --freeze.")
        return 1
    frozen = json.loads(path.read_text())
    for name in names:
        print(m5_report(name, since, until, frozen, min_cycles) + '\n')
    return 0
