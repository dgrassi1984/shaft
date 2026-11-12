"""Blind planted delivery failures, kept apart from grades and instance records."""
from __future__ import annotations

import copy
import fcntl
import json
from datetime import datetime, timedelta

from .core import jsonl_lines
from . import judge, metrics

CATCH_LIMIT = 3
CATCH_WINDOW_DAYS = 7


def _path(name: str):
    return metrics.SUP_DIR / "judge-catch" / f"{name}.jsonl"


def records(name: str) -> list[dict]:
    path = _path(name)
    rows = []
    for line in jsonl_lines(path.read_text()) if path.exists() else []:
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            datetime.fromisoformat(row.get("at"))
        except (TypeError, ValueError):
            continue
        rows.append(row)
    return rows


def plant(ep: dict) -> dict | None:
    """Refuse every matching delivery call in a copy. Only unambiguous operator messages are eligible;
    plugin/mail writes without a known recipient contract are skipped rather than planted speculatively."""
    if (ep.get("finish") or {}).get("delivered_via") != "message":
        return None
    planted, changed = copy.deepcopy(ep), False
    for call in planted.get("tools", []):
        if (call.get("tool") == "send_message" and metrics._arg(call.get("args", ""), "to") == "operator"
                and not str(call.get("result", "")).startswith(("ERROR", "(repeat: not run)", "(held, not run yet)"))):
            call["result"] = "ERROR: delivery refused; no message was sent."
            changed = True
    return planted if changed else None


def run(name: str) -> int:
    if not judge.available():
        return 0
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = datetime.now()
        rows = records(name)
        if sum(now - timedelta(days=CATCH_WINDOW_DAYS) <= metrics._ts(r["at"]) <= now for r in rows) >= CATCH_LIMIT:
            return 0
        used = {r.get("episode_ts") for r in rows}
        successful = {j["episode_ts"] for j in metrics.judgments(name) if j.get("delivery_supported") == "yes"}
        for ep in reversed(metrics.live_episodes(name)):
            at = metrics._ts(ep["ts"])
            if ep["ts"] in used or ep["ts"] not in successful or not at or not now - timedelta(days=CATCH_WINDOW_DAYS) <= at <= now:
                continue
            planted = plant(ep)
            if planted is None:
                continue
            row = {"at": now.isoformat(timespec="seconds"), "episode_ts": ep["ts"],
                   "defect": "delivery refused"}
            try:
                own = metrics.own_models(name, ep)
                by = judge.pick(exclude=own)
                case = metrics._case(planted, metrics._written_files(name, planted),
                                     metrics._provenance_evidence(name, planted),
                                     metrics._prior_goals(metrics.judgments_for_stats(name), ep["ts"]),
                                     followed=metrics._followed_surprise(name, planted))
                verdict = judge.ask(metrics.JUDGE_SYSTEM, case, metrics.JUDGE_SCHEMA,
                                    purpose=f"judge catch {name}", model=by)
                metrics._check_ratings(verdict)
                row.update(judge=by, verdict=verdict, **judge.independence(by, own),
                           caught=verdict.get("delivery_supported") != "yes" or verdict.get("claim_supported") is False)
            except judge.JudgeError as e:
                metrics.log(f"{name}: judge catch skipped: {e}")
                row["error"] = str(e)
            with path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            return int("caught" in row)
    return 0


def line(name: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    rows = [r for r in records(name) if "caught" in r and now - timedelta(days=30) <= metrics._ts(r["at"]) <= now]
    rate = f"{sum(r['caught'] for r in rows)}/{len(rows)} over 30 days" if rows else "unmeasured"
    return f"judge catch rate: {rate}"
