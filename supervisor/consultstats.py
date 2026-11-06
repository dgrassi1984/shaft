"""Measuring an instance's consultations of other models, read-only over the consult ledger.

The ledger (`.supervisor/consult_usage.jsonl`, written by the broker's consult transport — M0) holds one row per
call: ``{"ts", "instance", "model", "caller", "run", "run_started", "input_tokens", "output_tokens", "usd",
"reserved_usd", "seconds", "error"}``. This module reads that ledger, the instance's live episodes and the
supervisor's judgments to report what the consultations bought (M2). It never imports the broker or the consult
transport: it codes only against the row contract above, so it works before M0 lands.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import metrics
from .core import SUP_DIR

LEDGER = "consult_usage.jsonl"
WINDOW_DAYS = 7


def _ts(value) -> datetime | None:
    """An ISO timestamp (as the ledger and episodes store them) as a naive datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value[:19])
    except ValueError:
        return None


def _usd(value: float) -> str:
    """Dollars as the instance can read them: a consult often costs a fraction of a cent ($0.000024 for a real
    glm-5.3 call), which two decimals showed as $0.00."""
    if value == 0 or value >= 0.01:
        return f"${value:.2f}"
    return f"${value:.4f}" if value >= 0.0001 else f"${value:.6f}"




def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def rows(name: str, since=None, until=None) -> list[dict]:
    """The instance's own consult rows, oldest first, bad lines skipped. `since`/`until` bound `ts` (inclusive)."""
    path = SUP_DIR / LEDGER
    since = _ts(since)
    until = _ts(until)
    out = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("instance") != name:
            continue
        t = _ts(rec.get("ts"))
        if since is not None and (t is None or t < since):
            continue
        if until is not None and (t is None or t > until):
            continue
        out.append(rec)
    return out


def vendor(model: str | None) -> str | None:
    """The provider behind a model name: the ``openrouter/<vendor>/<id>`` middle part, or "anthropic" for a plain
    ``claude-…`` name. None when it cannot be told."""
    if not isinstance(model, str) or not model.strip():
        return None
    name = model.strip()
    from . import planchannel
    if planchannel.prefix(name):
        return planchannel.vendor(name)
    if name.startswith("openrouter/"):
        parts = name.split("/")
        return parts[1] if len(parts) > 1 and parts[1] else None
    if name.startswith("claude-"):
        return "anthropic"
    return None


def cycle_of(name: str, row: dict, episodes: list[dict]) -> str | None:
    """The live episode `row` was made in: the one whose run started at the row's `run_started` (the supervisor puts
    that time in the agent's environment and the episode records it). A run killed before it wrote an episode has
    none, so its consults are booked to no cycle — a time window once charged them to the next one (Grok's review,
    2026-10-27). None for a run that is not live."""
    if row.get("run") != "live" or not row.get("run_started"):
        return None
    return next((ep["ts"] for ep in episodes if isinstance(ep, dict) and ep.get("ts")
                 and ep.get("run_started") == row["run_started"]), None)


def submitted(row: dict) -> bool:
    """A consult whose prompt went out: it answered, or it failed after sending (charged its worst case). An outright
    refusal (charged $0) sent nothing anywhere."""
    return row.get("error") is None or _num(row.get("usd")) > 0

def models_in(name: str, ep: dict) -> set[str]:
    """The models this live episode sent a prompt to (`submitted`): the vendors that saw its work."""
    eps = metrics.live_episodes(name)
    found: set[str] = set()
    for row in rows(name):
        if cycle_of(name, row, eps) != ep.get("ts"):
            continue
        if submitted(row) and isinstance(row.get("model"), str) and row["model"]:
            found.add(row["model"])
    return found


def _display(model: str) -> str:
    return model.removeprefix("openrouter/")


def _success_text(ts_set: set[str], js_by_ts: dict[str, dict]) -> str:
    judged = [j for j in js_by_ts.values() if j["episode_ts"] in ts_set and j.get("criterion_met") in metrics.SCORE]
    if not judged:
        return "not judged yet"
    rate = sum(metrics.SCORE[j["criterion_met"]] for j in judged) / len(judged)
    return f"{rate:.0%} ({len(judged)} judged)"


def _value_text(ts_set: set[str], js_by_ts: dict[str, dict]) -> str:
    value = metrics.mean_value([j for j in js_by_ts.values() if j["episode_ts"] in ts_set])
    return "not judged yet" if value is None else f"{value:.2f}"


def section(name: str, cap=None) -> list[str]:
    """stats.md: what consulting other models has cost and bought, over the last 7 days. [] until there is a consult
    row in that window. `cap` (the instance's daily allowance in dollars) is the one figure this module cannot read
    from the ledger — the caller passes it; without it the allowance line does not compare against a cap."""
    now = datetime.now()
    since = now - timedelta(days=WINDOW_DAYS)
    window = [r for r in rows(name) if (t := _ts(r.get("ts"))) is not None and since <= t <= now]
    if not window:
        return []
    if cap is None:
        from . import consult
        cap = consult.daily_cap(name)
    lines = ["## Consulting other models (last 7 days)"]

    total = sum(_num(r.get("usd")) for r in window)
    mean = total / WINDOW_DAYS
    allowance = f"- I spent {_usd(total)} consulting other models in the last 7 days ({_usd(mean)}/day)."
    if cap is not None:
        cap = _num(cap)
        today = sum(_num(r.get("usd")) for r in window if (t := _ts(r.get("ts"))) and t.date() == now.date())
        allowance = (f"- I spent {_usd(total)} consulting in the last 7 days ({_usd(mean)}/day); today {_usd(today)} "
                     f"of my ${cap:.2f} daily consult allowance.")
    lines.append(allowance)

    by_model: dict[str, dict] = {}
    for r in window:
        m = r.get("model") if isinstance(r.get("model"), str) and r["model"] else "unknown"
        d = by_model.setdefault(m, {"calls": 0, "usd": 0.0, "seconds": [], "errors": 0})
        d["calls"] += 1
        d["usd"] += _num(r.get("usd"))
        if isinstance(r.get("seconds"), (int, float)) and not isinstance(r.get("seconds"), bool):
            d["seconds"].append(float(r["seconds"]))
        if r.get("error") is not None:
            d["errors"] += 1
    for m in sorted(by_model):
        d = by_model[m]
        secs = f"{sum(d['seconds']) / len(d['seconds']):.0f}s" if d["seconds"] else "?"
        lines.append(f"- {_display(m)}: {d['calls']} call(s), {_usd(d['usd'])}, {secs} each, {d['errors']} error(s)")

    eps = metrics.live_episodes(name)
    consulted_ts = {t for r in window if r.get("run") == "live" and submitted(r)
                    if (t := cycle_of(name, r, eps))}
    window_eps = [ep for ep in eps if (t := _ts(ep.get("ts"))) is not None and since <= t <= now]
    consulted = [ep for ep in window_eps if ep["ts"] in consulted_ts]
    not_consulted = [ep for ep in window_eps if ep["ts"] not in consulted_ts]
    js_by_ts = {j["episode_ts"]: j for j in metrics.judgments_for_stats(name)}
    lines.append(f"- Live cycles in the last 7 days: {len(consulted)} consulted, {len(not_consulted)} did not.")
    lines.append(f"- Consulted: success {_success_text({ep['ts'] for ep in consulted}, js_by_ts)}, "
                 f"mean value {_value_text({ep['ts'] for ep in consulted}, js_by_ts)}; "
                 f"without consulting: success {_success_text({ep['ts'] for ep in not_consulted}, js_by_ts)}, "
                 f"mean value {_value_text({ep['ts'] for ep in not_consulted}, js_by_ts)}.")

    live_usd = sum(_num(r.get("usd")) for r in window if r.get("run") == "live" and cycle_of(name, r, eps))
    met = [ep for ep in consulted if (js_by_ts.get(ep["ts"]) or {}).get("criterion_met") == "yes"]
    if met:
        lines.append(f"- Consult dollars per met consulted cycle: {_usd(live_usd / len(met))} ({len(met)} met).")
    elif consulted:
        lines.append("- Consult dollars per met consulted cycle: no consulted cycle judged met yet.")

    top = max(by_model.items(), key=lambda kv: kv[1]["calls"])
    if len(window) >= 20 and top[1]["calls"] / len(window) >= 0.9:
        lines.append(f"- Consultation collapse: {_display(top[0])} took {top[1]['calls']} of {len(window)} consults "
                     f"({top[1]['calls'] / len(window):.0%}).")
    return lines


def review_line(name: str) -> str:
    """One line for `shaft review` (calls, dollars, top model over the last 7 days) or "" when none."""
    now = datetime.now()
    since = now - timedelta(days=WINDOW_DAYS)
    window = [r for r in rows(name) if (t := _ts(r.get("ts"))) is not None and since <= t <= now]
    if not window:
        return ""
    by_model: dict[str, int] = {}
    for r in window:
        m = r.get("model") if isinstance(r.get("model"), str) and r["model"] else "unknown"
        by_model[m] = by_model.get(m, 0) + 1
    top = max(by_model, key=by_model.get)
    total = sum(_num(r.get("usd")) for r in window)
    return f"{len(window)} consult(s) in 7 days, {_usd(total)}, top model {_display(top)}"


def report(names: list[str]) -> str:
    """The operator's full text for `shaft consults`: per model, per run kind, consulted vs not, and the last 10
    calls with their caller."""
    out: list[str] = []
    since = datetime.now() - timedelta(days=WINDOW_DAYS)
    for name in names:
        all_rows = rows(name, since=since.isoformat(timespec="seconds"))
        out.append(f"{name}: consults (last 7 days)")
        if not all_rows:
            out.append("  (none)")
            continue
        by_model: dict[str, list] = {}
        for r in all_rows:
            m = r.get("model") if isinstance(r.get("model"), str) and r["model"] else "unknown"
            d = by_model.setdefault(m, [0, 0.0])
            d[0] += 1
            d[1] += _num(r.get("usd"))
        out.append("  per model:")
        for m in sorted(by_model):
            out.append(f"    {_display(m)}: {by_model[m][0]} call(s), {_usd(by_model[m][1])}")
        kinds: dict[str, int] = {}
        for r in all_rows:
            k = r.get("run") if isinstance(r.get("run"), str) and r["run"] else "?"
            kinds[k] = kinds.get(k, 0) + 1
        out.append("  per run: " + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())))
        eps = [ep for ep in metrics.live_episodes(name) if (t := _ts(ep.get("ts"))) is not None and t >= since]
        consulted_ts = {t for r in all_rows if r.get("run") == "live" and submitted(r)
                        if (t := cycle_of(name, r, eps))}
        not_consulted = sum(1 for ep in eps if ep.get("ts") not in consulted_ts)
        out.append(f"  live cycles: {len(consulted_ts)} consulted, {not_consulted} did not")
        out.append("  last calls:")
        for r in all_rows[-10:]:
            caller = r.get("caller") if isinstance(r.get("caller"), str) and r["caller"] else "?"
            out.append(f"    {str(r.get('ts', ''))[:16].replace('T', ' ')} {caller}: "
                       f"{_display(str(r.get('model', '?')))} {_usd(_num(r.get('usd')))}"
                       + (f" error: {str(r['error'])[:80]}" if r.get("error") else ""))
    return "\n".join(out)
