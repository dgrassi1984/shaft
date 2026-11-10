"""Data for `shaft top`'s control-panel views: one instance in full, the judge's
verdicts, trials, benchmarks, self-changes and probation, spending against the caps, services, alerts.

Stdlib only, like dashboard.py, so every view is testable without a terminal. Each function reuses the
supervisor's own readers (metrics, trial, battery, surprise, messages, version), so the panel and the CLI can
never disagree. Files are re-read only when they change (mtime and size), so a view open for an hour does not
re-parse a 200 KB log every second.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import browser, core, corrections, egress, planchannel, pyenv
from .core import (jsonl_lines, SUP_DIR, calendar_grants, gdrive_grants, gchat_grants, disabled_tools, dollars, git, instance_config, instance_names,
                   instance_root, journal_entries, knob, list_beneath, mail_grants, paused, safe_read, shared_folder)
from . import selfhosted

# The judge has no cap (the operator): only an alert. $10 sat under the Sonnet judge's $12-25 a day and fired daily;
# with cheap judges and one run in two judged (#45) a day is about $2 (~225 judgments at $0.002-0.01, ~55 second
# opinions at ~$0.008), so $3 flags a day that is half again above that, e.g. a dear model back in the pool.
JUDGE_ALERT_USD, _ = knob(
    "SHAFT_JUDGE_ALERT_USD", "3", dollars, "judge spend per day that raises an alert (the judge has no cap)",
    group="Judging", services=("web",))
CORRECTION_ALERT_CYCLES, _ = knob(
    "SHAFT_CORRECTION_ALERT_CYCLES", "6", core.whole, "live cycles a correction may remain open before an alert; 0 = off",
    group="Runs and conversations", services=("web",))
CAP_ALERT = 0.8                                                    # a cap this full raises an alert
ALERT_HOURS = 12                                                   # log alerts this recent are shown
SERVICE_LOGS = {"model": "com.shaft.model.log", "embed": "com.shaft.embed.log", "api": "com.shaft.api.log",
                "scheduler": "com.shaft.scheduler.log", "telegram": "com.shaft.telegram.log",
                "web": "com.shaft.web.log"}

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
            lines = jsonl_lines(path.read_text(errors="replace"))
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
def judge_usd(r: dict) -> float | None:
    """A judge call's dollars: its own `usd` when logged (the second judge, from OpenRouter's usage.cost), else
    from its tokens and the model's price (cloudllm.usd); None for an unknown model."""
    from . import cloudllm
    if r.get("usd") is not None:
        return float(r["usd"])
    return cloudllm.usd(r.get("model"), r)


def _purpose(r: dict) -> str:
    return (r.get("purpose") or "?").split()[0]


def codex_rate() -> dict | None:
    """Latest weekly window, including calls observed by another supervisor process."""
    return planchannel.saved_rate("codex")


def costs(days: int = 7, today: date | None = None) -> dict:
    """Spending per day for the last `days` days, by component, and today's spend against each cap.

    Components never overlap: a trial's arms and its judge calls are counted once, under trials (its ledger),
    and left out of the instances' cloud use and of the judge; battery runs (cloud rows `name@battery…`) are
    their own component."""
    from . import cloudllm, coder, trial
    today = today or date.today()
    first = today - timedelta(days=days - 1)
    names = set(instance_names())
    by_day: dict[str, dict] = {}

    def add(day: str, comp: str, usd: float, who: str = "") -> None:
        if not (first.isoformat() <= day <= today.isoformat()) or not usd:
            return
        d = by_day.setdefault(day, {"instances": {}, "judge": 0.0, "trials": 0.0, "coder": 0.0, "recaps": 0.0,
                                    "other": 0.0})
        if comp == "instances":
            d["instances"][who] = d["instances"].get(who, 0.0) + usd
        else:
            d[comp] += usd

    unpriced = 0
    grok_equivalent = 0.0
    for r in jsonl(SUP_DIR / "cloud_usage.jsonl"):
        if r.get("ts", "")[:10] == today.isoformat() and str(r.get("model", "")).startswith("grok/"):
            grok_equivalent += r.get("api_equivalent_usd", 0.0)
        if r.get("usd") is None:
            if planchannel.prefix(str(r.get("model", ""))) and " (fallback " not in r["model"]:
                continue
            unpriced += 1
            continue
        who = r.get("instance", "?")
        if "@trial" in who:
            continue                                    # in the trial ledger
        add(r["ts"][:10], "instances" if who in names else "other", r.get("usd") or 0.0,
            who if who in names else "")
    for r in jsonl(trial.LEDGER):
        add(r["ts"][:10], "trials", r.get("usd") or 0.0)
    judge_today: dict[str, float] = {}
    for r in jsonl(SUP_DIR / "judge_usage.jsonl"):
        usd = judge_usd(r)
        if usd is None:
            unpriced += 1
            continue
        if r["ts"][:10] == today.isoformat():
            judge_today[_purpose(r)] = judge_today.get(_purpose(r), 0.0) + usd
        if _purpose(r) != "trial":                      # trial judging is in the trial ledger
            add(r["ts"][:10], "judge", usd)
    for r in jsonl(SUP_DIR / "coder_usage.jsonl"):
        add(r["ts"][:10], "coder", r.get("usd") or 0.0)
    for r in jsonl(SUP_DIR / "recap_usage.jsonl"):       # the overview's one-line recaps (recap.py)
        add(r["ts"][:10], "recaps", r.get("usd") or 0.0)
    rows = []
    for k in range(days):
        day = (first + timedelta(days=k)).isoformat()
        d = by_day.get(day, {"instances": {}, "judge": 0.0, "trials": 0.0, "coder": 0.0, "recaps": 0.0, "other": 0.0})
        d["total"] = sum(d["instances"].values()) + d["judge"] + d["trials"] + d["coder"] + d["recaps"] + d["other"]
        rows.append({"day": day, **d})
    caps = []
    for name in sorted(names):
        used = trial.usage_today(name)
        caps.append({"name": name,
                     "cloud": (cloudllm.spent_today(name), cloudllm.daily_cap(name)),
                     "coder": (coder.spent_today(name), coder.DAILY_USD),
                     "trials": (used["usd"], trial.DAILY_USD), "trials_started": (used["started"], trial.TRIALS_PER_DAY)})
    return {"days": rows, "today": rows[-1], "caps": caps, "judge_today": judge_today,
            "judge_today_total": sum(judge_today.values()), "judge_alert": JUDGE_ALERT_USD,
            "unpriced": unpriced, "codex_rate": codex_rate(), "grok_api_equivalent": grok_equivalent}


# -- the judge's verdicts --------------------------------------------------------------------------------------
def judgments(name: str) -> list[dict]:
    """Every judged cycle of the instance, newest first, with what it achieved (0-5, None if unrated)."""
    from . import metrics
    path = SUP_DIR / "judgments" / f"{name}.jsonl"
    seconds = SUP_DIR / "judgments" / f"{name}-second.jsonl"

    def read():
        out = []
        for j in reversed(metrics.judgments(name, SUP_DIR)):
            if j.get("cited"):
                continue
            try:
                v = metrics.cycle_value(j)
            except KeyError:
                v = None
            out.append({**j, "achieved": None if v is None else 5 * v})
        return out
    return cached(f"judgments:{name}", [path, seconds], read)


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


def journal_heading(entry: str) -> str:
    """A journal entry's first line, as text: its heading without the #, **, ` of its Markdown."""
    from .web_md import plain
    return plain(entry.splitlines()[0]) if entry.strip() else ""


def instance_detail(name: str) -> dict:
    from . import messages, metrics
    root = instance_root(name)
    cfg = instance_config(name)
    stats = safe_read(root, "self/stats.md", "", errors="replace")   # in the instance's folder: never via a link
    heads = [journal_heading(e) for e in journal_entries(root)[-10:]][::-1]
    probation = []
    try:
        probation = json.loads((SUP_DIR / "probation" / f"{name}.json").read_text())
    except (OSError, ValueError):
        pass
    questions = [m for m in messages.pending() if m["from"] == name]
    return {"name": name, "paused": paused(name),
            "model": cfg.get("LLM_MODEL", "?"), "via": (f"broker ({planchannel.label(cfg['LLM_MODEL'])} plan)" if planchannel.prefix(cfg.get("LLM_MODEL")) else
             "broker (self-hosted)" if selfhosted.serves(cfg.get("LLM_MODEL")) else "broker (cloud)")
            if cfg.get("LLM_BASE_URL") == "broker" else cfg.get("LLM_BASE_URL", "?"),
            "wake": cfg.get("WAKE_INTERVAL_MINUTES", "120"), "effort": cfg.get("LLM_REASONING_EFFORT", "medium"),
            "tools_off": sorted(disabled_tools(name)),
            "shared": str(shared_folder(name) or ""),
            "mail": ", ".join(f"{g['account']}/{g['label']}" + (" [drafts]" if g.get("draft") else "") for g in mail_grants(name)),
            "gchat": ", ".join(g["account"] for g in gchat_grants(name)),
            "gdrive": ", ".join(f"{g['account']} ({g['reach']})" for g in gdrive_grants(name)),
            "calendar": "; ".join(f"{g['account']}/{g['name']}" for g in calendar_grants(name)),
            "jobs": _jobs_summary(name),
            "python": pyenv.describe(root),
            "browser": browser.describe(root),
            "internet": egress.summary(egress.recent(egress.log_path(name), 2000), date.today().isoformat()),
            "stats": stats or "(no stats yet)",
            "journal": heads, "probation": probation, "questions": questions,
            "changes": changes(name), "merges": template_merges(name)[:5], "traits": traits(name),
            "judged": len(metrics.judgments_for_stats(name))}


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
    return {"installed": installed, "off": battery.is_off(name), "results": rows[-30:][::-1],
            "due": installed and battery.due(name, now),
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
    return cached(f"surprise:{name}", _episode_files(name), lambda: surprise.baseline(name))


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
        rec = version.service(s["name"], stale=True)
        out.append({**s, "pid": (rec or {}).get("pid"), "stale": bool((rec or {}).get("stale")),
                    "started": (rec or {}).get("started", ""), "version": version.service_version(rec)})
    return out


def scheduled_runs() -> list[str]:
    """Runs the scheduler started that a restart of it would kill (its children share its process group)."""
    from .runner import busy_with
    out = []
    for name in instance_names():
        b = busy_with(name) or ""
        if b.split()[:1] and b.split()[0] in ("live", "sleep", "battery"):
            out.append(f"{name} ({b})")
    # Only workers in the scheduler's own process group: a battery or trial started by hand from a terminal
    # survives a restart, and flagging it blocked the restart for nothing.
    sched = subprocess.run(["pgrep", "-f", "supervisor autonomy serve"], capture_output=True, text=True).stdout.split()
    pgids = {subprocess.run(["ps", "-o", "pgid=", "-p", pid], capture_output=True, text=True).stdout.strip()
             for pid in sched} - {""}
    for pgid in pgids:
        r = subprocess.run(["pgrep", "-g", pgid, "-f", "supervisor (trial work|battery run)"], capture_output=True,
                           text=True)
        if r.stdout.strip():
            out.append("trial or battery worker")
            break
    return out


# -- alerts ---------------------------------------------------------------------------------------------------------
ALERT_LINE = re.compile(r"FAILED|failed tests|restore failed|run stopped|killed after|ended with an error|exit=[1-9]"
                        r"|exit=-|refused|ERROR")


def extras(snap: dict) -> dict:
    """The control panel's additions to a dashboard snapshot, for `shaft top` and the web UI alike: open alerts
    (minus dismissed ones), today's total spend, and each instance's latest recap (recap.py) with whether a new one
    is being written."""
    from . import recap
    stored = recap.load()
    for i in snap["instances"]:
        i["recap"] = {**stored.get(i["name"], {}), "running": recap.running(i["name"])}
    try:
        cost = costs(days=1)
        gone = dismissed()
        snap["alerts"] = [a for a in alerts(snap, cost=cost) if a["id"] not in gone]
        snap["spend"] = cost["today"]["total"]
    except Exception as e:                           # a broken log must not take the panel down; say so
        snap["alerts"] = [{"id": "panel", "text": f"panel data unavailable: {type(e).__name__}: {e}"}]
        snap["spend"] = None
    return snap


def pause_alert(pause: dict) -> dict:
    """One stable, dismissable operator reminder shared with web pushes (2026-10-28, A1)."""
    name, since = pause["name"], pause["since"]
    who = "all instances have" if name == "all" else f"{name} has"
    resume = "shaft resume" + ("" if name == "all" else f" {name}")
    return {"id": f"pause:{name}:{since:%Y-%m-%dT%H:%M}",
            "text": f"{who} been paused by the operator for {pause['hours']:.0f} h "
                    f"(since {since:%m-%d %H:%M}) — {resume}"}


def provider_alerts() -> list[dict]:
    """Provider faults persist until a successful call; rate limits need five refusals in ten minutes."""
    active, throttled = {}, {}
    since = (datetime.now() - timedelta(hours=24)).isoformat()   # a provider unused for a day raises nothing stale
    for row in jsonl(SUP_DIR / "cloud_usage.jsonl"):
        if str(row.get("ts", "")) < since:
            continue
        model, error = row.get("model", ""), row.get("error")
        if " (fallback " in model:
            model = model.split(" (fallback ", 1)[1].removesuffix(")")
        provider = ("openrouter" if model.startswith("openrouter/") else
                    "anthropic" if model.startswith("claude-") else model.partition("/")[0] if planchannel.prefix(model) else "")
        if not provider:
            continue
        if not error:
            active.pop(provider, None)
            throttled.pop(provider, None)
            continue
        causes = active.setdefault(provider, set())
        if error.startswith("HTTP 402:"):
            causes.add("credit")
        elif error.startswith("HTTP 403:") and any(word in error.lower() for word in ("limit", "credit", "quota")):
            causes.add("limit")
        count = re.search(r"\[HTTP 429 responses: (\d+)\]$", error)
        if count or error.startswith("HTTP 429:"):
            try:
                ts = datetime.fromisoformat(row["ts"])
            except (KeyError, ValueError):
                continue
            recent = throttled.setdefault(provider, [])
            recent.extend([ts] * (int(count[1]) if count else 1))
            recent[:] = [t for t in recent if ts - timedelta(minutes=10) <= t <= ts]
            if len(recent) >= 5:
                causes.add("rate-limit")
    descriptions = {"credit": "HTTP 402: payment or credits required",
                    "limit": "HTTP 403: provider limit, credit or quota exhausted",
                    "rate-limit": "at least five HTTP 429 refusals within ten minutes"}
    return [{"id": f"provider:{provider}:{cause}", "text": f"{provider}: {descriptions[cause]}"}
            for provider, causes in sorted(active.items()) for cause in sorted(causes)]


def alerts(snap: dict | None = None, now: datetime | None = None, cost: dict | None = None) -> list[dict]:
    """What needs the operator: a service down, a cap at 80% or more, judge spend past its alert line, and
    failures in the supervisor log of the last ALERT_HOURS hours. Each has a stable id, so it can be dismissed.
    The model server is down most of the day by design (it exits idle and is started on demand, localmodel.py):
    only a down that is a fault raises an alert (model_down_problem)."""
    now = now or datetime.now()
    out = []
    for s in (snap or {}).get("services", []):
        if s["running"]:
            continue
        if s["name"] == "scheduler":
            out.append({"id": f"svc:scheduler:{now:%Y-%m-%d}", "text": "service scheduler is not running"})
        elif s["name"] == "model" and (why := model_down_problem(now)):
            out.append({"id": f"svc:model:{now:%Y-%m-%d}:{hashlib.sha1(why.encode()).hexdigest()[:8]}",
                        "text": f"service model is not running: {why}"})
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
            out.append({"id": log_alert_id(line), "text": line[12:20] + line[21:200]})
    out.extend(pause_alert(p) for p in core.long_pauses(now))
    out.extend(provider_alerts())
    for provider in planchannel.PROVIDERS:
        if (fault := planchannel.outage(provider)):
            label = planchannel.label(provider)
            title = f"{label} security event" if fault.get("security") else f"{label} unavailable"
            out.append({"id": f"provider:{provider}:outage", "text": f"{title}: {fault['reason']}"})
    if CORRECTION_ALERT_CYCLES > 0:
        for name in instance_names():
            out.extend(corrections.alerts(name, CORRECTION_ALERT_CYCLES, now))
    return out


def model_down_problem(now: datetime | None = None) -> str | None:
    """Why the local model server not running is a fault (localmodel.down_problem), or None when it exited idle
    and its next user starts it. A failed on-demand start counts for ALERT_HOURS, until any later start of it."""
    from . import localmodel
    since = ((now or datetime.now()) - timedelta(hours=ALERT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    try:   # its record outlives the process: when it last started, on demand or by hand
        started = json.loads((SUP_DIR / "services" / "model.json").read_text()).get("started", "").replace("T", " ")
    except (OSError, ValueError):
        started = ""
    return localmodel.down_problem(_model_starts(), max(since, started), names=instance_names(),
                                   config_of=instance_config)


def _model_starts() -> list[str]:
    """supervisor.log's lines on starting the model server on demand (localmodel.ensure), oldest first."""
    from .localmodel import START_FAILED, START_OK
    path = SUP_DIR / "supervisor.log"

    def read():
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return []
        return [l for l in lines if l.startswith("[") and (START_OK in l or START_FAILED in l)]
    return cached("model-starts", [path], read)


def log_alert_id(line: str) -> str:
    """A log alert's id: its time and a digest of the line — the same in every process. (It was hash(), which
    Python seeds afresh per process, so dismissed log alerts came back at every relaunch of shaft top.)"""
    return f"log:{line[1:20]}:{hashlib.sha1(line.encode(errors='replace')).hexdigest()[:12]}"


def _log_failures() -> list[str]:
    path = SUP_DIR / "supervisor.log"

    def read():
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return []
        return [l for l in lines if l.startswith("[") and ALERT_LINE.search(l) and "passed" not in l]
    return cached("log-failures", [path], read)


# -- transcripts: every cycle and every conversation, from the instance's own episode records --------------------
CYCLE_KINDS = {"live": "cycle", "sleep": "sleep", "reflect": "reflection"}
WRITEUP_KINDS = ("talk_recovered", "talk_summary")
TRANSCRIPT_CUT = 8000        # a tool result longer than this is cut in the view, which says where the rest is


def _episode_files(name: str) -> list[Path]:
    """The instance's episode logs, listed without following a link; read them with _episode_text."""
    root = instance_root(name)
    return [root / "memory" / "episodes" / n for n in list_beneath(root, "memory/episodes") if n.endswith(".jsonl")]


def _episode_text(path: Path) -> str:
    """An episode log as text, read without following a link anywhere (core.safe_read): "" for a link."""
    return safe_read(path.parent.parent.parent, f"memory/episodes/{path.name}", "", errors="replace")


def transcripts(name: str) -> list[dict]:
    """Every finished cycle (live, sleep, reflection) and every conversation of the instance, newest first. A
    conversation's turns are grouped by talk_id, with its write-up attached. Read from memory/episodes/*.jsonl, the
    instance's own record of what it did; a cycle appears once it has ended (the record is written then)."""
    files = _episode_files(name)

    def read():
        out, talks = [], {}
        for f in files:
            lines = jsonl_lines(_episode_text(f))
            for n, line in enumerate(lines, 1):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                where = f"memory/episodes/{f.name}:{n}"
                kind = r.get("kind")
                if kind in CYCLE_KINDS:
                    out.append({"id": where, "kind": CYCLE_KINDS[kind], "ts": r.get("ts", ""), "where": where,
                                "record": r})
                elif kind == "talk" or kind in WRITEUP_KINDS:
                    tid = r.get("talk_id") or where
                    t = talks.setdefault(tid, {"id": f"talk:{tid}", "kind": "conversation", "ts": r.get("ts", ""),
                                               "where": where, "turns": [], "writeups": [], "via": ""})
                    if kind in WRITEUP_KINDS:
                        t["writeups"].append(r)
                    else:                       # its last turn, not its write-up (that comes when it closes)
                        t["turns"].append(r)
                        t["last"] = r.get("ts", "")
                        t["via"] = t["via"] or (r.get("via") or "")
        out += list(talks.values())
        return sorted(out, key=lambda e: e["ts"], reverse=True)
    return cached(f"transcripts:{name}", files, read)


def transcript_summary(e: dict) -> str:
    """One line for the list."""
    if e["kind"] == "conversation":
        turns = e["turns"]
        first = next((t.get("content", "") for t in turns if t.get("role") == "user"), "")
        via = e["via"] or "terminal"
        span = f" → {e['last'][11:16]}" if e.get("last") and e["last"][:16] != e["ts"][:16] else ""
        return (f"{len(turns)} turns · via {via}{span}{' · written up' if e['writeups'] else ''} · "
                + " ".join(first.split()))
    r = e["record"]
    steps, tools = r.get("steps"), len(r.get("tools") or [])
    end = r.get("ended_by") or ""
    if e["kind"] == "cycle":
        goal = " ".join(((r.get("intent") or {}).get("goal") or "(no intent declared)").split())
        met = (r.get("finish") or {}).get("criterion_met")
        return f"{met or '·':<7} {tools} tools{f', {steps} steps' if steps else ''} · {end} · {goal}"
    from .web_md import plain           # a reflection's or sleep's answer, as text: not its Markdown
    return f"{tools} tools · {end} · " + " ".join(" ".join(plain(l).split()) for l in (r.get("content") or "").splitlines()
                                                  if l.strip())


def _block(out: list, label: str, value, style: str = "") -> None:
    if value in (None, "", [], {}):
        return
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    out.append((label, "bold") if label else ("", ""))
    out += [(line, style) for line in text.splitlines() or [""]]


def _tool_lines(out: list, tools: list, where: str) -> None:
    for k, call in enumerate(tools or [], 1):
        args = call.get("args")
        try:
            args = json.dumps(json.loads(args), ensure_ascii=False, indent=1) if isinstance(args, str) else \
                json.dumps(args, ensure_ascii=False, indent=1)
        except ValueError:
            pass
        out.append((f"▸ {k}. {call.get('tool')}", "bold cyan"))
        out += [(f"  {l}", "cyan") for l in str(args or "").splitlines()]
        result = str(call.get("result", ""))
        if len(result) > TRANSCRIPT_CUT:
            result = (result[:TRANSCRIPT_CUT] + f"\n[… {len(result) - TRANSCRIPT_CUT} more chars, in full at {where}]")
        out += [(f"  │ {l}", "") for l in result.splitlines() or [""]]


def transcript_lines(name: str, e: dict, thinking: bool = False) -> list[tuple[str, str]]:
    """The whole transcript as (line, style) pairs, unwrapped: the panel wraps them to its width."""
    out: list[tuple[str, str]] = [(f"{e['kind']} · {e['ts'].replace('T', ' ')} · {e['where']}", "bold"), ("", "")]
    if e["kind"] == "conversation":
        operator = instance_config(name).get("OPERATOR_NAME") or "operator"
        for t in e["turns"]:
            who = name if t.get("role") == "assistant" else (t.get("speaker") or operator)
            via = f" via {t['via']}" if t.get("via") else ""
            out.append((f"── {t.get('ts', '')[11:19]} {who}{via}", "bold green" if who == name else "bold yellow"))
            if thinking and t.get("reasoning"):
                out += [(f"  (thinking) {l}", "dim") for l in str(t["reasoning"]).splitlines()]
            out += [(l, "") for l in str(t.get("content") or "").splitlines() or [""]]
            if t.get("tools"):
                _tool_lines(out, t["tools"], e["where"])
            out.append(("", ""))
        for w in e["writeups"]:
            _block(out, f"── write-up ({w.get('kind')}, {w.get('ts', '')[11:16]})", w.get("content"))
        return out
    r = e["record"]
    out.append((f"ended by {r.get('ended_by') or '?'} · {r.get('steps') or '?'} steps · "
                f"{len(r.get('tools') or [])} tool calls", "dim"))
    from . import metrics
    intent = r.get("intent") or {}
    intent = {**intent, "confidence": metrics.confidence_shown(intent)}   # "90 → 60" where the base rate moved it
    for label, key in (("Goal", "goal"), ("Success criterion", "success_criterion"), ("Confidence", "confidence"),
                       ("Origin", "origin"), ("Origin reference", "origin_ref")):
        _block(out, label, intent.get(key))
    _block(out, "Inbox at the start", r.get("inbox"))
    if r.get("tools"):
        out += [("", ""), ("Tool calls", "bold underline")]
        _tool_lines(out, r["tools"], e["where"])
    _block(out, "Text", r.get("content"))
    for key, value in (r.get("finish") or {}).items():
        _block(out, f"Finish · {key.replace('_', ' ')}", value)
    return out


DISMISSED = "top_dismissed.json"
DISMISS_KEEP_DAYS = 3        # every alert id carries its day, and none older than ALERT_HOURS can come back
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def dismissed() -> set[str]:
    try:
        return set(json.loads((SUP_DIR / DISMISSED).read_text()))
    except (OSError, ValueError):
        return set()


def dismiss(ids: list[str]) -> None:
    """Remember dismissed alert ids; forget only those of days no alert can come from any more (never by count:
    keeping the alphabetically last 500 dropped today's cap: and judge: dismissals first)."""
    cut = (date.today() - timedelta(days=DISMISS_KEEP_DAYS)).isoformat()
    keep = {i for i in dismissed() | set(ids) if not (m := _DAY.search(i)) or m.group(0) >= cut}
    SUP_DIR.mkdir(exist_ok=True)
    (SUP_DIR / DISMISSED).write_text(json.dumps(sorted(keep)))
    from . import messages
    for ref in ids:
        parts = ref.split(":")
        name = parts[1] if len(parts) > 2 and parts[0] in ("cap", "pause", "correction") else ""
        messages.dismissal(SUP_DIR, "dismiss", ref, name)


def _jobs_summary(name: str) -> str:
    """'watcher every 15 min (can wake), digest every 60 min' — or why none run."""
    from . import jobs
    if jobs.jobs_off(name):
        return "off (shaft jobs " + name + " on)"
    try:
        declared, problems = jobs.declared(name)
    except OSError:
        return "none"
    parts = [f"{j['plugin']} every {j['every_minutes']} min" + (" (can wake)" if j["can_wake"] else "")
             + (f" · {jobs.entry(name, j['plugin']).get('fails')} failing" if jobs.entry(name, j['plugin']).get("fails")
                else "") for j in declared]
    return ", ".join(parts) + (f" · {len(problems)} not run" if problems else "") if parts or problems else "none"
