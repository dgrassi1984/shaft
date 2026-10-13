"""Data for `shaft top`'s control-panel views (PLAN-control-panel.md): one instance in full, the judge's
verdicts, trials, benchmarks, self-changes and probation, spending against the caps, services, alerts.

Stdlib only, like dashboard.py, so every view is testable without a terminal. Each function reuses the
supervisor's own readers (metrics, trial, battery, surprise, messages, version), so the panel and the CLI can
never disagree. Files are re-read only when they change (mtime and size), so a view open for an hour does not
re-parse a 200 KB log every second.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .core import (SUP_DIR, disabled_tools, git, instance_config, instance_names, instance_root, journal_entries,
                   knob, paused, shared_folder, write_hosts)

JUDGE_ALERT_USD, _ = knob("SHAFT_JUDGE_ALERT_USD", "10", float)   # the judge has no cap (Daniele): only an alert
CAP_ALERT = 0.8                                                    # a cap this full raises an alert
ALERT_HOURS = 12                                                   # log alerts this recent are shown
SERVICE_LOGS = {"model": "com.shaft.model.log", "embed": "com.shaft.embed.log", "api": "com.shaft.api.log",
                "scheduler": "com.shaft.scheduler.log"}

# -- caching ------------------------------------------------------------------------------------------------
_memo: dict[str, tuple[tuple, object]] = {}


def _stamp(paths) -> tuple:
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(p), None))
    return tuple(out)


def cached(key: str, paths, fn, ttl: float | None = None):
    """fn() again only when one of `paths` changed (or, with ttl, at most every ttl seconds)."""
    stamp = _stamp(paths) + ((int(time.time() // ttl),) if ttl else ())
    hit = _memo.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    value = fn()
    _memo[key] = (stamp, value)
    return value


def jsonl(path: Path) -> list[dict]:
    def read():
        rows = []
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return rows
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows
    return cached(f"jsonl:{path}", [path], read)


# -- costs --------------------------------------------------------------------------------------------------
def _prices() -> dict:
    from . import cloudllm, coder
    return {**coder.PRICES, **cloudllm.PRICES}


def judge_usd(r: dict, prices: dict | None = None) -> float | None:
    """A judge call's dollars from its tokens (the judge log has no dollar field); None for an unknown model."""
    p = (prices or _prices()).get(r.get("model"))
    if not p:
        return None
    return ((r.get("input_tokens") or 0) * p[0] + (r.get("output_tokens") or 0) * p[1]) / 1e6


def _purpose(r: dict) -> str:
    return (r.get("purpose") or "?").split()[0]


def costs(days: int = 7, today: date | None = None) -> dict:
    """Spending per day for the last `days` days, by component, and today's spend against each cap.

    Components never overlap: a trial's arms and its judge calls are counted once, under trials (its ledger),
    and left out of the instances' cloud use and of the judge; battery runs (cloud rows `name@battery…`) are
    their own component."""
    from . import cloudllm, coder, trial
    today = today or date.today()
    first = today - timedelta(days=days - 1)
    names = set(instance_names())
    prices = _prices()
    by_day: dict[str, dict] = {}

    def add(day: str, comp: str, usd: float, who: str = "") -> None:
        if not (first.isoformat() <= day <= today.isoformat()) or not usd:
            return
        d = by_day.setdefault(day, {"instances": {}, "judge": 0.0, "trials": 0.0, "coder": 0.0, "other": 0.0})
        if comp == "instances":
            d["instances"][who] = d["instances"].get(who, 0.0) + usd
        else:
            d[comp] += usd

    for r in jsonl(SUP_DIR / "cloud_usage.jsonl"):
        who = r.get("instance", "?")
        if "@trial" in who:
            continue                                    # in the trial ledger
        add(r["ts"][:10], "instances" if who in names else "other", r.get("usd") or 0.0,
            who if who in names else "")
    for r in jsonl(trial.LEDGER):
        add(r["ts"][:10], "trials", r.get("usd") or 0.0)
    unpriced = 0
    judge_today: dict[str, float] = {}
    for r in jsonl(SUP_DIR / "judge_usage.jsonl"):
        usd = judge_usd(r, prices)
        if usd is None:
            unpriced += 1
            continue
        if r["ts"][:10] == today.isoformat():
            judge_today[_purpose(r)] = judge_today.get(_purpose(r), 0.0) + usd
        if _purpose(r) != "trial":                      # trial judging is in the trial ledger
            add(r["ts"][:10], "judge", usd)
    for r in jsonl(SUP_DIR / "coder_usage.jsonl"):
        add(r["ts"][:10], "coder", r.get("usd") or 0.0)
    rows = []
    for k in range(days):
        day = (first + timedelta(days=k)).isoformat()
        d = by_day.get(day, {"instances": {}, "judge": 0.0, "trials": 0.0, "coder": 0.0, "other": 0.0})
        d["total"] = sum(d["instances"].values()) + d["judge"] + d["trials"] + d["coder"] + d["other"]
        rows.append({"day": day, **d})
    caps = []
    for name in sorted(names):
        used = trial.usage_today(name)
        caps.append({"name": name,
                     "cloud": (cloudllm.spent_today(name), cloudllm.DAILY_USD),
                     "coder": (coder.spent_today(name), coder.DAILY_USD),
                     "trials": (used["usd"], trial.DAILY_USD), "trials_started": (used["started"], trial.TRIALS_PER_DAY)})
    return {"days": rows, "today": rows[-1], "caps": caps, "judge_today": judge_today,
            "judge_today_total": sum(judge_today.values()), "judge_alert": JUDGE_ALERT_USD, "unpriced": unpriced}


# -- the judge's verdicts --------------------------------------------------------------------------------------
def judgments(name: str) -> list[dict]:
    """Every judged cycle of the instance, newest first, with what it achieved (0-5, None if unrated)."""
    from . import metrics
    path = SUP_DIR / "judgments" / f"{name}.jsonl"

    def read():
        out = []
        for j in reversed(jsonl(path)):
            try:
                v = metrics.cycle_value(j)
            except KeyError:
                v = None
            out.append({**j, "achieved": None if v is None else 5 * v})
        return out
    return cached(f"judgments:{name}", [path], read)


def traits(name: str) -> dict | None:
    path = SUP_DIR / "judgments" / f"{name}-traits.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# -- one instance in full -----------------------------------------------------------------------------------------
def _git_lines(*args: str) -> list[str]:
    r = git(*args, check=False)
    return r.stdout.splitlines() if r.returncode == 0 else []


def template_merges(name: str) -> list[str]:
    """`shaft update` merges into the instance, newest first ('YYYY-MM-DD HH:MM'); re-read at most every 30 s."""
    return cached(f"merges:{name}", [], lambda: [
        l for l in _git_lines("log", "--merges", "--format=%ad %s", "--date=format:%Y-%m-%d %H:%M", f"instance/{name}")
        if "merge template" in l], ttl=30)


def instance_detail(name: str) -> dict:
    from . import messages, metrics
    root = instance_root(name)
    cfg = instance_config(name)
    stats = root / "self" / "stats.md"
    heads = [e.splitlines()[0].lstrip("# ").strip() for e in journal_entries(root)[-10:]][::-1]
    probation = []
    try:
        probation = json.loads((SUP_DIR / "probation" / f"{name}.json").read_text())
    except (OSError, ValueError):
        pass
    questions = [m for m in messages.pending() if m["from"] == name]
    return {"name": name, "paused": paused(name),
            "model": cfg.get("LLM_MODEL", "?"), "via": "broker (cloud)" if cfg.get("LLM_BASE_URL") == "broker"
            else cfg.get("LLM_BASE_URL", "?"),
            "wake": cfg.get("WAKE_INTERVAL_MINUTES", "120"), "effort": cfg.get("LLM_REASONING_EFFORT", "medium"),
            "tools_off": sorted(disabled_tools(name)), "write_hosts": sorted(write_hosts(name)),
            "shared": str(shared_folder(name) or ""),
            "stats": stats.read_text(errors="replace") if stats.exists() else "(no stats yet)",
            "journal": heads, "probation": probation, "questions": questions,
            "changes": changes(name), "merges": template_merges(name)[:5], "traits": traits(name),
            "judged": len(metrics.judgments(name))}


# -- self-changes and probation -------------------------------------------------------------------------------------
PROBATION_LINE = re.compile(r"^\[(?P<ts>[\d-]+ [\d:]+)\] (?P<name>[\w-]+): (?:probation (?P<commit>[0-9a-f]{8})"
                            r"(?: (?P<verb>passed|failed))?|change (?P<commit2>[0-9a-f]{8}) (?:on|not on) probation)"
                            r"(?P<rest>.*)$")


def probation_events(name: str, limit: int = 30) -> list[dict]:
    """What probation decided about the instance's changes, newest first, from the supervisor log."""
    path = SUP_DIR / "supervisor.log"

    def read():
        out = []
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return out
        for line in lines:
            m = PROBATION_LINE.match(line)
            if not m:
                continue
            out.append({"ts": m["ts"], "name": m["name"], "commit": m["commit"] or m["commit2"],
                        "text": line.split("] ", 1)[1].split(": ", 1)[1]})
        return out
    return [e for e in reversed(cached("probation-log", [path], read)) if e["name"] == name][:limit]


def changes(name: str) -> list[dict]:
    """The instance's kept code changes, newest first: when, files, how many its coder wrote."""
    rows = jsonl(SUP_DIR / "code_changes" / f"{name}.jsonl")
    return [{"ts": r.get("ts", ""), "files": r.get("files", []), "delegated": r.get("delegated_files", [])}
            for r in reversed(rows)]


# -- trials -------------------------------------------------------------------------------------------------------
def trials(name: str | None = None) -> list[dict]:
    """Every trial (of one instance or all), newest first."""
    from . import trial
    base = SUP_DIR / "trials"
    stamp = sorted(base.glob(f"{name or '*'}/t-*/trial.json")) if base.is_dir() else []
    ts = cached(f"trials:{name}", stamp, lambda: trial.all_trials(name))
    return sorted(ts, key=lambda t: t.get("created", ""), reverse=True)


def trial_progress(t: dict) -> str:
    done = sum(1 for s in t.get("states", []) if s.get("status") != "pending")
    return f"{done}/{len(t.get('states', []))}"


# -- benchmarks --------------------------------------------------------------------------------------------------
def battery_status(name: str) -> dict:
    from . import battery
    installed = battery.items_dir().is_dir()
    rows = battery.results(name) if installed or battery.results_path(name).exists() else []
    now = datetime.now()
    start, end = battery.window(now)
    return {"installed": installed, "results": rows[-30:][::-1], "due": installed and battery.due(name, now),
            "window": f"{start:%H:%M}–{end:%H:%M}", "every_days": battery.EVERY_DAYS,
            "lines": battery.stats_lines(name) if rows else []}


def evals(name: str | None = None) -> list[dict]:
    """distinct / breed records, newest first."""
    base = SUP_DIR / "evals"
    out = []
    for p in sorted(base.glob("*.json")) if base.is_dir() else []:
        if p.name.startswith("surprise"):
            continue
        try:
            d = cached(f"eval:{p}", [p], lambda p=p: json.loads(p.read_text()))
        except ValueError:
            continue
        kind = p.name.split("-")[0]
        who = d.get("name") or f"{d.get('a')}+{d.get('b')}→{d.get('child')}"
        if name and name not in (d.get("name"), d.get("a"), d.get("b"), d.get("child")):
            continue
        out.append({"file": p.name, "kind": kind, "who": who, "at": d.get("at", ""), "data": d})
    return sorted(out, key=lambda e: e["at"], reverse=True)


def surprise_summary(name: str) -> dict:
    """Unexpected tool results per episode kind (supervisor/surprise.py), re-computed when episodes change."""
    from . import surprise
    eps = instance_root(name) / "memory" / "episodes"
    files = sorted(eps.glob("*.jsonl")) if eps.is_dir() else []
    return cached(f"surprise:{name}", files, lambda: surprise.baseline(name))


# -- services ------------------------------------------------------------------------------------------------------
def log_tail(service: str, n: int = 20) -> list[str]:
    path = SUP_DIR / SERVICE_LOGS.get(service, "")
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8000))
            return f.read().decode(errors="replace").splitlines()[1:][-n:]
    except OSError:
        return []


def services_detail() -> list[dict]:
    from . import dashboard, version
    out = []
    for s in dashboard.services():
        rec = version.service(s["name"], stale=True) if s["name"] != "scheduler" else None
        out.append({**s, "pid": (rec or {}).get("pid"), "stale": bool((rec or {}).get("stale")),
                    "started": (rec or {}).get("started", "")})
    return out


def scheduled_runs() -> list[str]:
    """Runs the scheduler started that a restart of it would kill (its children share its process group)."""
    from .runner import busy_with
    out = []
    for name in instance_names():
        b = busy_with(name) or ""
        if b.split()[:1] and b.split()[0] in ("live", "sleep", "battery"):
            out.append(f"{name} ({b})")
    r = subprocess.run(["pgrep", "-f", "supervisor (trial work|battery run)"], capture_output=True, text=True)
    if r.stdout.strip():
        out.append("trial or battery worker")
    return out


# -- alerts ---------------------------------------------------------------------------------------------------------
ALERT_LINE = re.compile(r"FAILED|failed tests|restore failed|run stopped|killed after|ended with an error|exit=[1-9]"
                        r"|exit=-|refused|ERROR")


def alerts(snap: dict | None = None, now: datetime | None = None, cost: dict | None = None) -> list[dict]:
    """What needs the operator: a service down, a cap at 80% or more, judge spend past its alert line, and
    failures in the supervisor log of the last ALERT_HOURS hours. Each has a stable id, so it can be dismissed."""
    now = now or datetime.now()
    out = []
    for s in (snap or {}).get("services", []):
        if not s["running"] and s["name"] in ("model", "scheduler"):
            out.append({"id": f"svc:{s['name']}:{now:%Y-%m-%d}", "text": f"service {s['name']} is not running"})
    cost = cost or costs(days=1)
    for c in cost["caps"]:
        for comp in ("cloud", "coder", "trials"):
            spent, cap = c[comp]
            if cap and spent >= CAP_ALERT * cap:
                out.append({"id": f"cap:{c['name']}:{comp}:{now:%Y-%m-%d}",
                            "text": f"{c['name']}: {comp} ${spent:.2f} of ${cap:.2f} today"})
    if cost["judge_today_total"] >= cost["judge_alert"]:
        out.append({"id": f"judge:{now:%Y-%m-%d}",
                    "text": f"judge spend ${cost['judge_today_total']:.2f} today (alert at ${cost['judge_alert']:.2f}; no cap)"})
    since = (now - timedelta(hours=ALERT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    for line in _log_failures():
        if line[1:20] >= since:
            out.append({"id": f"log:{line[1:20]}:{hash(line) & 0xffffff:x}", "text": line[12:20] + line[21:200]})
    return out


def _log_failures() -> list[str]:
    path = SUP_DIR / "supervisor.log"

    def read():
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return []
        return [l for l in lines if l.startswith("[") and ALERT_LINE.search(l) and "passed" not in l]
    return cached("log-failures", [path], read)


DISMISSED = "top_dismissed.json"


def dismissed() -> set[str]:
    try:
        return set(json.loads((SUP_DIR / DISMISSED).read_text()))
    except (OSError, ValueError):
        return set()


def dismiss(ids: list[str]) -> None:
    keep = dismissed() | set(ids)
    SUP_DIR.mkdir(exist_ok=True)
    (SUP_DIR / DISMISSED).write_text(json.dumps(sorted(keep)[-500:]))
