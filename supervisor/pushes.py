"""Operator pushes (#140), measured separately from value and operator reactions."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from bisect import bisect_left
from datetime import datetime, timedelta
from pathlib import Path

from . import core

PUSH_BUDGET, _ = core.knob(
    "SHAFT_PUSH_BUDGET", "6", core.whole,
    "operator pushes per instance per day before holding for a digest (0: unlimited, count hidden)",
    group="Runs and conversations", services=("runs", "api", "web", "telegram"))


def _quiet_hours(value: str) -> tuple[int, int] | None:
    if not value:
        return None
    start, end = map(int, value.split("-"))
    if not (0 <= start < 24 and 0 <= end < 24) or start == end:
        raise ValueError("expected different hours 00-23, e.g. 22-07, or empty")
    return start, end


def _digest_at(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("expected HH:MM")
    return hour * 60 + minute


PUSH_HOLD, _ = core.knob(
    "SHAFT_PUSH_HOLD", "1", core.whole, "hold non-urgent device notifications (0: off)",
    group="Runs and conversations", services=("runs", "api", "web", "telegram"))
QUIET_HOURS, _ = core.knob(
    "SHAFT_QUIET_HOURS", "22-07", _quiet_hours, "local quiet hours for device notifications (empty: none)",
    group="Runs and conversations", services=("runs", "api", "web", "telegram"))
DIGEST_AT, _ = core.knob(
    "SHAFT_DIGEST_AT", "07:15", _digest_at, "local digest time, or quiet hours' end if later",
    group="Runs and conversations", services=("runs", "api", "web", "telegram"))


def hold_until(row: dict, sup_dir: Path, now: datetime, sent: int = 0, live: bool = True) -> tuple[str, str] | None:
    """(when its device notification is due, why it waits), or None to notify now. Called under the message log's
    write lock: concurrent senders share one budget decision.

    The budget counts the notifications that went out today, not the ones held: a night's held messages go out as one
    digest and leave the day's budget whole. Over the budget only urgent=true or an open conversation (the operator
    is in it) notifies at once; a reply within 2 h of the operator's message waits no quiet hours but keeps to the
    budget (review I14: thelos sent 21 pushes against a budget of 6). `sent`: notifications a trial replay simulated;
    `live=False` (a replay) leaves out today's conversations."""
    if not PUSH_HOLD or row.get("urgent") or row.get("to") != "operator" or not row.get("alert"):
        return None
    from . import runner
    if live and runner.open_conversations(row["from"]):
        return None
    rows = records(sup_dir)
    replying = any(r.get("from") == "operator" and r.get("to") == row["from"]
                   and now - timedelta(hours=2) <= _time(r["ts"]) <= now for r in rows)
    quiet = False
    if QUIET_HOURS and not replying:
        start, end = QUIET_HOURS
        quiet = start <= now.hour < end if start < end else now.hour >= start or now.hour < end
    used = sent + sum(_is_push(r, row["from"]) and not r.get("push_due") and _time(r["ts"]).date() == now.date()
                      and _time(r["ts"]) <= now for r in rows)
    over = bool(PUSH_BUDGET) and used >= PUSH_BUDGET
    if not quiet and not over:
        return None
    minute = max(DIGEST_AT, QUIET_HOURS[1] * 60 if QUIET_HOURS else 0)
    if QUIET_HOURS and QUIET_HOURS[0] > QUIET_HOURS[1] and minute >= QUIET_HOURS[0] * 60:
        minute = QUIET_HOURS[1] * 60  # a digest configured inside overnight quiet hours waits for their end
    due = now.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
    if due <= now:
        due += timedelta(days=1)
    why = "quiet hours" if quiet else f"over today's budget ({used} of {PUSH_BUDGET} sent)"
    return due.isoformat(timespec="seconds"), why


def _save_digest(path: Path, claimed: set[str]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".push-digest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(sorted(claimed), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def digest(send, now: datetime | None = None) -> int:
    """Claim due rows durably before sending one notification. Shared by both periodic forwarders.

    Claims give at-most-once dispatch even after a restart. A crash between the claim and external send
    can lose a notification; external push services do not support transactional exactly-once delivery.
    The original messages always remain in the log and inbox.
    """
    now = now or datetime.now()
    if QUIET_HOURS:
        start, end = QUIET_HOURS
        if (start <= now.hour < end if start < end else now.hour >= start or now.hour < end):
            return 0  # overdue work must not wake the operator in a later quiet window
    sup = core.SUP_DIR
    sup.mkdir(parents=True, exist_ok=True)
    with (sup / "push-digest.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = sup / "push-digest.json"
        try:
            sent = set(json.loads(path.read_text()))
        except FileNotFoundError:
            sent = set()
        due = [r for r in records(sup) if r.get("push_due") and _time(r["push_due"]) <= now
               and r["file"] not in sent]
        if not due:
            return 0
        claimed = sent | {r["file"] for r in due}
        _save_digest(path, claimed)
        from .web_md import first_line
        body = "\n".join(f"{r['from']}: {first_line(r['text']) or '(a file)'}" for r in due)
        delivered = send({"title": f"SHAFT digest ({len(due)})", "body": body,
                          "url": "/overview", "tag": "shaft-digest-" + due[-1]["file"]})
        if not delivered:  # no paired destinations: keep these eligible for the next service/loop
            _save_digest(path, sent)
            return 0
        return 1


def _time(text: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(text)
        return at.astimezone().replace(tzinfo=None) if at.tzinfo else at
    except (TypeError, ValueError):
        return None


def records(sup_dir: Path) -> list[dict]:
    """The supervisor's message rows, without history()'s instance-controlled inbox read state."""
    try:
        lines = (sup_dir / "messages.jsonl").read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and _time(row.get("ts")):
            rows.append(row)
    return rows


def _is_push(row: dict, name: str) -> bool:
    return row.get("from") == name and row.get("to") == "operator" and bool(row.get("alert"))


def usage(name: str, sup_dir: Path, now: datetime | None = None) -> dict | None:
    if not PUSH_BUDGET:
        return None
    now = now or datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {"today": sum(_is_push(r, name) and start <= _time(r["ts"]) <= now for r in records(sup_dir)),
            "budget": PUSH_BUDGET}


def reactions(name: str, rows: list[dict], sup_dir: Path, now: datetime) -> list[dict]:
    """One outcome per push, using the next operator message or supervisor-logged conversation start.

    No response means none recorded yet as of now, within 24 h. It does not claim causation. Inbox read flags
    and panel dismissals do not establish a response. Timed dismissals are measured separately (#141).
    """
    replies = [_time(r["ts"]) for r in rows if r.get("from") == "operator" and r.get("to") == name]
    try:
        lines = (sup_dir / "supervisor.log").read_text(errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    pattern = re.compile(r"^\[([^]]+)\] " + re.escape(name) + r": start (?:api|talk)(?: |$)")
    for line in lines:
        if (m := pattern.match(line)) and (at := _time(m[1])):
            replies.append(at)
    replies = sorted(at for at in replies if at and at <= now)
    out = []
    for row in rows:
        if not _is_push(row, name) or (sent := _time(row["ts"])) > now:
            continue
        i = bisect_left(replies, sent)
        delay = replies[i] - sent if i < len(replies) else timedelta.max
        outcome = "within_1h" if delay <= timedelta(hours=1) else (
            "within_24h" if delay <= timedelta(hours=24) else "no_response")
        out.append({"file": row["file"], "ts": row["ts"], "outcome": outcome})
    return out


def recent_judgments(judgments: list[dict], now: datetime) -> list[dict]:
    """The judged cycles of the last 7 days (today and the 6 before), none after `now`."""
    first = (now - timedelta(days=6)).date()
    return [j for j in judgments if (at := _time(j.get("episode_ts"))) and first <= at.date() and at <= now]


def waited_share(recent: list[dict]) -> str:
    """should_have_waited=yes among the judged cycles graded yes or no: "2/5 (40%)", or "not graded"."""
    rated = [j for j in recent if j.get("should_have_waited") in ("yes", "no")]
    yes = sum(j["should_have_waited"] == "yes" for j in rated)
    return f"{yes}/{len(rated)} ({yes / len(rated):.0%})" if rated else "not graded"


def lines(name: str, judgments: list[dict], now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    days = [(now - timedelta(days=d)).date() for d in range(6, -1, -1)]
    rows = records(core.SUP_DIR)
    daily = {day: 0 for day in days}
    for row in rows:
        if _is_push(row, name) and (at := _time(row["ts"])) <= now and at.date() in daily:
            daily[at.date()] += 1
    budget = f" of budget {PUSH_BUDGET}" if PUSH_BUDGET else ""
    recent = recent_judgments(judgments, now)
    share = waited_share(recent)
    counts = {k: 0 for k in ("within_1h", "within_24h", "no_response")}
    for row in reactions(name, rows, core.SUP_DIR, now):
        if _time(row["ts"]).date() in daily:
            counts[row["outcome"]] += 1
    return [f"pushes to the operator today: {daily[now.date()]}{budget}",
            "pushes per day (last 7 days" + (f", budget {PUSH_BUDGET}/day" if PUSH_BUDGET else "") + "): "
            + ", ".join(f"{day:%m-%d}: {daily[day]}" for day in days),
            f"should_have_waited=yes (last 7 days, judged push cycles): {share}; "
            f"na {sum(j.get('should_have_waited') == 'na' for j in recent)}, "
            f"ungraded {sum(j.get('should_have_waited') not in ('yes', 'no', 'na') for j in recent)}",
            f"operator reactions (last 7 days' pushes, as of now): within 1 h {counts['within_1h']}, "
            f"1–24 h {counts['within_24h']}, no response within 24 h recorded yet {counts['no_response']}"]
