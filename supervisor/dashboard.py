"""Everything `shaft top` shows, in one snapshot: each instance's state and what it is doing right now, the
services, the latest supervisor events and the messages between instances ("a supervisor TUI for having at glance the
status of all instances and what they are doing, in real time"). Stdlib only, so it can be tested without the
terminal UI; top.py renders it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

from . import messages
from .core import SUP_DIR, disabled_tools, instance_config, instance_names, instance_root, paused

SERVICES = ("model", "embed", "api", "scheduler", "telegram", "web")
NOISE = ('api "GET', 'api "POST', "background journal write-up started")   # feed lines that say nothing new


# Instance-written text (goals, messages, events) is shown by the TUI and the web UI: neither may pass control
# characters through (rich passes ESC on to the terminal).
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(s: str) -> str:
    return CONTROL.sub("", s.expandtabs(4))


def event_tone(line: str) -> str:
    """How a supervisor event reads at a glance: "bad", "warn", "good" or ""."""
    return ("bad" if any(w in line for w in ("FAILED", "failed", "ERROR", "stopped", "killed")) else
            "warn" if any(w in line for w in ("paused", "skipped", "reverted")) else
            "good" if any(w in line for w in ("passed", "resumed", "committed live")) else "")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def activity(root: Path) -> dict:
    """The instance's own live status (.tmp/activity.json), if the process that wrote it is still running."""
    try:
        a = json.loads((root / ".tmp" / "activity.json").read_text())
    except (OSError, ValueError):
        return {}
    return a if _alive(int(a.get("pid", 0))) else {}


def _today_usage(root: Path) -> tuple[int, int]:
    """(model calls, completion tokens) today, from the instance's own usage log."""
    calls = tokens = 0
    today = date.today().isoformat()
    try:
        lines = (root / "logs" / "llm_usage.jsonl").read_text().splitlines()
    except OSError:
        return 0, 0
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not r.get("ts", "").startswith(today):
            break
        calls += 1
        tokens += r.get("completion_tokens") or 0
    return calls, tokens


def _usd_today(name: str) -> float:
    from . import cloudllm, coder
    try:
        return cloudllm.spent_today(name) + coder.spent_today(name)
    except Exception:
        return 0.0


def _judged(name: str) -> tuple[int, float | None, float | None]:
    """(cycles judged, raw success over the last 10, achieved difficulty 0-5 over the last 10 rated)."""
    from . import metrics
    try:
        js = metrics.judgments(name)
    except Exception:
        return 0, None, None
    recent = js[-10:]
    rated = [v for v in map(metrics.cycle_value, js) if v is not None][-10:]
    return (len(js), (sum(metrics.SCORE.get(j.get("criterion_met"), 0) for j in recent) / len(recent)) if recent else None,
            5 * sum(rated) / len(rated) if rated else None)


def _value(name: str) -> float | None:
    """Mean value (0-5: what each cycle delivered) over the last 10 graded cycles."""
    from . import metrics
    try:
        return metrics.mean_value(metrics.valued(metrics.judgments(name))[-10:])
    except Exception:
        return None


def instance_state(name: str, now: float | None = None) -> dict:
    from . import autonomy
    from .runner import busy_with, open_conversations
    now = now or time.time()
    root = instance_root(name)
    busy = busy_with(name) or ""
    convs = open_conversations(name)
    act = activity(root)
    is_paused = any((root / ".tmp").glob("paused-*")) if (root / ".tmp").is_dir() else False
    if paused(name):
        state = "paused by you"
    elif is_paused:
        state = f"{busy.split()[0] if busy else 'cycle'} paused for a conversation"
    elif busy:
        state = {"live": "in a cycle", "sleep": "sleeping", "reflect": "reflecting", "talk": "talking",
                 "api": "answering (API)", "recover": "writing up a conversation",
                 "update": "being updated"}.get(busy.split()[0], busy.split()[0])
    elif convs:
        state = "in a conversation (waiting for you)"
    else:
        state = "idle"
    doing = ""
    if act and not is_paused:
        since = now - act.get("ts", now)
        if act.get("phase") == "tool":
            doing = f"{act.get('tool')} {act.get('args') or ''}".strip()
        elif act.get("phase") == "thinking":
            doing = f"thinking (step {act.get('step')})"
        elif act.get("phase") == "paused":
            doing = "paused"
        doing = f"{doing} · {since:.0f}s" if doing else ""
    cfg = instance_config(name)
    interval = autonomy.wake_interval(name)
    st = autonomy._state()
    last = (st.get("wakes") or {}).get(name)
    if interval is None:
        next_wake = "never"
    elif not last:
        next_wake = "due"
    else:
        at = datetime.fromisoformat(last) + interval
        next_wake = "due" if at <= datetime.fromtimestamp(now) else f"{at:%H:%M}"
    calls, tokens = _today_usage(root)
    n_judged, rate, achieved = _judged(name)
    return {"name": name, "state": state, "since": busy.partition(" since ")[2], "doing": doing,
            "goal": act.get("goal", "") if act else "",
            "model": ("cloud " if cfg.get("LLM_BASE_URL") == "broker" else "") + cfg.get("LLM_MODEL", "?").split("/")[-1],
            "every": f"{int(interval.total_seconds() // 60)} min" if interval else "no wakes",
            "next_wake": next_wake, "conversations": len(convs), "calls_today": calls, "tokens_today": tokens,
            "usd_today": _usd_today(name), "judged": n_judged, "success": rate, "achieved": achieved,
            "value": _value(name),
            "tools_off": sorted(disabled_tools(name)), "trial": _trial_line(name)}


def _trial_line(name: str) -> str:
    """The instance's latest trial, if one is running or finished today."""
    from . import trial
    ts = trial.all_trials(name)
    if not ts:
        return ""
    t = ts[-1]
    if t["status"] in ("done", "rejected") and not t.get("finished", t["created"]).startswith(
            datetime.now().date().isoformat()):
        return ""
    return trial.describe(t).splitlines()[0][2:]


_launchd_cache: dict[str, tuple[float, bool]] = {}


def _scheduler_running() -> bool:
    hit = _launchd_cache.get("scheduler")
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.shaft.scheduler"], capture_output=True, text=True)
    running = r.returncode == 0 and "state = running" in r.stdout
    _launchd_cache["scheduler"] = (time.time(), running)
    return running


def services() -> list[dict]:
    from . import version
    out = []
    for name in SERVICES:
        if name == "scheduler":
            out.append({"name": name, "running": _scheduler_running(), "detail": ""})
            continue
        rec = version.service(name, stale=False)
        detail, idle = "", False
        if rec:
            detail = f"{rec['commit']} since {rec['started'][11:16]}"
            if name == "model":
                s = rec.get("settings", {})
                detail += f" · temp {s.get('temp')} · think {s.get('think_budget')}"
        elif name == "model":   # runs only while used (localmodel.py): down is a fault only if it is needed
            from .panel import model_down_problem
            why = model_down_problem()
            detail, idle = (f"down · {why}" if why else "idle · starts on demand"), not why
        out.append({"name": name, "running": bool(rec), "idle": idle, "detail": detail})
    return out


def events(n: int = 12) -> list[str]:
    try:
        with (SUP_DIR / "supervisor.log").open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            lines = f.read().decode(errors="replace").splitlines()[1:]
    except OSError:
        return []
    keep = [l for l in lines if not any(x in l for x in NOISE)]
    return [l[12:20] + l[21:] if l.startswith("[") else l for l in keep[-n:]]   # [HH:MM:SS] text


def snapshot() -> dict:
    now = time.time()
    return {"at": datetime.fromtimestamp(now).strftime("%H:%M:%S"), "services": services(),
            "instances": [instance_state(n, now) for n in instance_names()], "events": events(),
            "messages": messages.history()}
