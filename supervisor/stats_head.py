"""The head of self/stats.md: the few lines an instance reads on every run.

The page is 12-18 KB and the instance's prompt keeps about its first 5,500 characters, so what stands first is all that
reaches a choice in time. The head holds, in order: the gauges the instance chose (gauges.py), what the claim lint
found since its last sleep (claimlint.py), what changed since that sleep, a dated line after a model change, the
verdict of each test the supervisor already runs on one of the instance's mechanisms, and the topics its battery
self-description got wrong (never the items). The tables follow, unchanged, as the body.

Everything here is a reading. Nothing instructs, refuses or gates. The head is held under HEAD_CHARS, each line
bounded, so it fits whole even when the prompt's budget for the page is cut.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import core
from .core import log

HEAD_CHARS = 2500            # under half the prompt's budget for the page, so the head survives a shrunk budget
LINE_CHARS = 240
MODEL_NOTE_DAYS = 7          # how long a model change stays announced at the head
BASE_RATE_ALERT_N = 200      # paired cycles after which a "no gain" verdict is raised to the operator
COIN_ALERT_N = 50            # judged cycles per arm after which "curiosity cycles are worth less" is raised
BODY_MARK = "*Below: the full tables (read_file self/stats.md shows all of them).*"


def _last_sleep(name: str) -> str | None:
    from . import metrics
    return max((ep["ts"] for ep in metrics._episodes(name, "sleep") if isinstance(ep.get("ts"), str)), default=None)


def since_sleep(name: str) -> list[str]:
    """One line: what happened since the last finished sleep, from the supervisor's records."""
    from . import metrics
    last = _last_sleep(name)
    if not last:
        return []
    cycles = [ep for ep in metrics.live_episodes(name) if ep["ts"] > last]
    judged = [j for j in metrics.judgments_for_stats(name) if j["episode_ts"] > last]
    met = {k: sum(j["criterion_met"] == k for j in judged) for k in ("yes", "partial", "no")}
    path = core.SUP_DIR / "code_changes" / f"{name}.jsonl"
    kept = 0
    for line in core.jsonl_lines(path.read_text()) if path.exists() else []:
        try:
            kept += json.loads(line).get("ts", "") > last
        except (ValueError, AttributeError, TypeError):
            continue
    return [f"- Since my last sleep ({last[:16].replace('T', ' ')}): {len(cycles)} cycles, {len(judged)} judged "
            f"(yes {met['yes']}, partial {met['partial']}, no {met['no']}); code changes kept {kept}"]


def model_change(name: str, now: datetime) -> list[str]:
    """A dated line for MODEL_NOTE_DAYS after the model an instance's cycles ran on changed: the figures before that
    date were another model's, and the page says nothing else that marks the day."""
    from . import metrics
    js = metrics.judgments_for_stats(name)
    if not js:
        return []
    latest = js[-1].get("model") or "local"
    first = None
    for j in reversed(js):
        if (j.get("model") or "local") != latest:
            break
        first = j
    if first is js[0] or first is None:
        return []           # one model throughout
    at = metrics._ts(first["episode_ts"])
    if not at or now - at > timedelta(days=MODEL_NOTE_DAYS):
        return []
    before = js[js.index(first) - 1].get("model") or "local"
    return [f"- Model: since {first['episode_ts'][:10]} my cycles run on {latest} (before: {before}). Capability and "
            "calibration figures from before that date were another model's"]


def verdicts(name: str) -> list[dict]:
    """One verdict per mechanism the supervisor already tests with a paired or randomised comparison:
    {"key", "line", "alert" (text for the operator, or "")}. A test with too little data says so."""
    from . import metacog, metrics, surprise
    out = []
    js = metrics.judgments_for_stats(name)
    p = metacog.paired_v3(js)
    if p["n"]:
        if p["verdict"] == "not enough data":
            word = "not enough data yet"
        elif p["ci"] and p["ci"][0] > 0:
            word = "gain"
        elif p["verdict"] == "worse":
            word = "worse"
        else:
            word = "no gain shown"
        b = metrics.paired_v3([j for j in js if metrics.by_current_judge(j)])
        brier = f"; Brier {b['brier_first']:.2f} before, {b['brier_final']:.2f} after" if b["n"] else ""
        line = (f"- Base-rate step (I see my base rate, then confirm my confidence): {word} on me. Telling met from "
                f"missed cycles changed by {metacog._number(p['diff'], True)}{metacog._interval(p['ci'])} over "
                f"{p['n']} cycles{brier}. It costs one model turn per cycle; the knob is CONFIDENCE_MODE")
        alert = (f"{name}: the base-rate step shows {word} over {p['n']} paired cycles"
                 if word in ("no gain shown", "worse") and p["n"] >= BASE_RATE_ALERT_N else "")
        out.append({"key": "base_rate", "line": line, "alert": alert})
    c = surprise.coin_comparison(name)
    if c["drawn"]["n"] and c["undrawn"]["n"]:
        d, u = c["drawn"], c["undrawn"]
        line = (f"- Curiosity coin (it is random, so this is a fair comparison): cycles it drew were worth "
                f"{d['value']:.2f} (n {d['n']}) against {u['value']:.2f} (n {u['n']}) on my other self-chosen cycles; "
                "a direction, not proof. The knob is in my config")
        alert = (f"{name}: curiosity cycles are worth {d['value']:.2f} against {u['value']:.2f} on its other "
                 f"self-chosen cycles (n {d['n']} and {u['n']})"
                 if min(d["n"], u["n"]) >= COIN_ALERT_N and d["value"] < u["value"] else "")
        out.append({"key": "coin", "line": line, "alert": alert})
    return out


def _verdicts_path(name: str):
    return core.SUP_DIR / "verdicts" / f"{name}.json"


def alerts(name: str) -> list[dict]:
    """The verdicts past their stated sample, for the panel: read from what the last stats build saved, so the panel
    never recomputes a bootstrap."""
    try:
        saved = json.loads(_verdicts_path(name).read_text())
    except (OSError, ValueError):
        return []
    return [{"id": f"verdict:{name}:{v['key']}", "text": v["alert"]} for v in saved
            if isinstance(v, dict) and v.get("alert") and v.get("key")]


def _part(name: str, label: str, build) -> list[str]:
    try:
        return build()
    except Exception as e:   # one unreadable record must never cost the instance the rest of its page
        log(f"{name}: stats head part {label} failed: {type(e).__name__}: {e}")
        return []


def head(name: str, now: datetime | None = None, save: bool = False) -> list[str]:
    """The head's lines ([] when there is nothing to say). `save`: keep the verdicts for the panel's alerts."""
    from . import battery, claimlint, gauges
    now = now or datetime.now()
    lines = _part(name, "gauges", lambda: gauges.head_lines(name, now))
    lint = _part(name, "claim lint", lambda: claimlint.head_lines(name, since=_last_sleep(name)))
    if lint:
        lines += ["## Claims my tool log did not show (checked on every cycle, since my last sleep)"] + lint + [""]
    found = _part(name, "verdicts", lambda: verdicts(name))
    if save:
        try:
            _verdicts_path(name).parent.mkdir(parents=True, exist_ok=True)
            _verdicts_path(name).write_text(json.dumps([{"key": v["key"], "alert": v["alert"]} for v in found]))
        except OSError as e:
            log(f"{name}: verdicts not saved: {type(e).__name__}: {e}")
    notes = (_part(name, "since sleep", lambda: since_sleep(name)) + _part(name, "model", lambda: model_change(name, now))
             + [v["line"] for v in found] + _part(name, "battery topics", lambda: battery.self_miss_lines(name)))
    if notes:
        lines += ["## Where I stand now"] + notes + [""]
    lines = [l if len(l) <= LINE_CHARS else l[:LINE_CHARS - 1] + "…" for l in lines]
    while lines and len("\n".join(lines)) > HEAD_CHARS:   # the last notes go first: the chosen gauges are kept
        lines.pop()
        while lines and not lines[-1].startswith("- "):
            lines.pop()
        lines.append("")
    return lines + [BODY_MARK, ""] if lines else []
