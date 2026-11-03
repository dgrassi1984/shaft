"""Commitments measured against the instance's record and the supervisor's judges.

The event fold and evidence checks mirror shaft/commitments.py. Shared fixtures keep them in step;
the supervisor never imports the agent's code.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from . import metrics
from .core import INSTANCES, SUP_DIR, safe_read

FILE = "memory/commitments.jsonl"
STATUSES = ("kept", "broken", "superseded", "void")
FIELD_CHARS = 300
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
ID = re.compile(r"c\d{4}-\d{4}-[1-9]\d*")
FAILED = ("ERROR", "(repeat: not run)", "(held, not run yet)")
FIELDS = ("promise", "due", "criterion", "made_to")
# Calls that record or report rather than do: commit_to's own "recorded" made any committing cycle its own evidence
# (Grok's review). Evidence is a call that did something.
BOOKKEEPING = ("commit_to", "settle_commitment", "declare_intent", "confirm_confidence", "finish_cycle",
               "close_surprise")


def _at(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _local(at)


def _local(at: datetime) -> datetime:
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def _entry(value) -> dict | None:
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) for k in FIELDS):
        return None
    item = {k: " ".join(value[k].split()) for k in FIELDS}
    if any(not item[k] for k in FIELDS) or any(len(item[k]) > FIELD_CHARS for k in ("promise", "criterion")):
        return None
    return item if NAME.fullmatch(item["made_to"]) else None


def _rows(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _fold(rows: list[dict], now: datetime) -> dict[str, dict]:
    # The agent keeps its own copy; shared fixtures pin the event contract.
    items, self_ids = {}, set()
    for rec in rows:
        cid, at = rec.get("id"), _at(rec.get("at"))
        if not isinstance(cid, str) or not ID.fullmatch(cid) or at is None:
            continue
        if rec.get("event") == "made":
            item, due = _entry(rec), _at(rec.get("due"))
            if item is None or due is None or due < at or rec.get("source") not in ("cycle", "talk") or cid in items:
                continue
            items[cid] = {"id": cid, **item, "source": rec["source"], "made_at": rec["at"],
                          "status": "open", "settled_at": None, "evidence": None}
            items[cid].update({k: rec[k] for k in ("talk_id", "message_file", "note") if k in rec})
            if item["made_to"] in ("self", "own") or rec.get("self_commitment"):
                self_ids.add(cid)
        elif rec.get("event") == "settled":
            item, evidence = items.get(cid), rec.get("evidence")
            if (item and item["status"] == "open" and rec.get("status") in STATUSES
                    and not (cid in self_ids and at - _at(item["due"]) >= timedelta(hours=24))
                    and isinstance(evidence, str) and evidence.strip() and at >= _at(item["made_at"])):
                item.update(status=rec["status"], settled_at=rec["at"], evidence=evidence)
    for item in items.values():
        if item["status"] == "open":
            if item["id"] in self_ids and _local(now) - _at(item["due"]) >= timedelta(hours=24):
                expires = _at(item["due"]) + timedelta(hours=24)
                item.update(status="expired", settled_at=expires.isoformat(), evidence="self commitment expired 24h after due")
            elif _at(item["due"]) < _local(now):
                item["status"] = "overdue"
    return items


def _source_rows(rows: list[dict], episodes: list[dict], delivered, own: str) -> list[dict]:
    """An operator promise needs the conversation it was made in, or a message the instance delivered to the
    operator (review I10); a note in its own folder is not one. The agent keeps its own copy."""
    talks = {ep["talk_id"] for ep in episodes if ep.get("kind") == "talk" and ep.get("role") == "user"
             and not ep.get("speaker") and isinstance(ep.get("talk_id"), str) and ep["talk_id"]}
    out = []
    for row in rows:
        row = dict(row)
        if row.get("event") == "made":
            if row.get("made_to") == "operator" and row.get("sourced"):   # older rows predate the rule: kept as made
                talk, message = row.get("talk_id"), row.get("message_file")
                valid = isinstance(talk, str) and talk in talks
                if not valid and isinstance(message, str) and _message_file(own).fullmatch(message):
                    valid = delivered(message)
                if not valid:
                    row.update(made_to="self", note="operator source missing or invalid; recorded as self")
            if row.get("made_to") == own:
                row["self_commitment"] = True
        out.append(row)
    return out


def _citation(evidence: str) -> tuple[str, int | None] | None:
    ts, sep, step = evidence.partition("#")
    if sep and (not re.fullmatch(r"[1-9]\d{0,8}", step)):
        return None
    if _at(ts) is None:
        return None
    return ts, int(step) if sep else None


def _matches(ts, cited: str) -> bool:
    return isinstance(ts, str) and (ts == cited or len(cited) == 16 and ts[:16].replace("T", " ") == cited.replace("T", " "))


def _evidence(episodes: list[dict], item: dict, evidence: str) -> tuple[str, dict | None]:
    citation = _citation(evidence)
    if citation is None:
        return "cite an episode ts, optionally #step", None
    ts, step = citation
    matches = [ep for ep in episodes if _matches(ep.get("ts"), ts)]
    if not matches:
        return "no such episode", None
    if len(matches) != 1:
        return "episode citation is ambiguous; use the exact ts", None
    ep = matches[0]
    ended, made = _at(ep.get("ts")), _at(item.get("made_at"))
    if ended is None or made is None or ended <= made:
        return "cited episode did not end after my commitment", ep
    calls = ep.get("tools")
    calls = calls if isinstance(calls, list) else []
    if step is not None:
        if step > len(calls):
            return "no such tool-log step", ep
        calls = [calls[step - 1]]
    good = any(isinstance(c, dict) and isinstance(c.get("tool"), str) and bool(c["tool"])
               and c["tool"] not in BOOKKEEPING
               and isinstance(c.get("result"), str) and not c["result"].startswith(FAILED) for c in calls)
    return ("" if good else "cited evidence has no successful tool call"), ep


def ledger(name: str, now: datetime | None = None) -> dict[str, dict]:
    if not NAME.fullmatch(name):
        return {}
    text = safe_read(INSTANCES / name, FILE, "", errors="replace")
    rows = _source_rows(_rows(text), _episodes(name), lambda file: bool(operator_message(name, file)), name)
    return _fold(rows, now if now is not None else datetime.now())


def _message_file(name: str) -> re.Pattern:
    return re.compile(r"\d{8}-\d{6}-\d{6}-from-" + re.escape(name) + r"\.md")


def operator_message(name: str, file: str) -> str:
    """A message this instance delivered to the operator, still waiting or already read (messages._archive moves it
    to operator-inbox/read/ under the same name), without following links."""
    if not NAME.fullmatch(name) or not _message_file(name).fullmatch(file):
        return ""
    for folder in ("operator-inbox", "operator-inbox/read"):
        text = safe_read(SUP_DIR, f"{folder}/{file}", "", errors="replace")
        head = text.split("\n\n", 1)[0]
        if re.search(r"(?m)^To: operator$", head) and re.search(r"(?m)^From: " + re.escape(name) + r"$", head):
            return text
    return ""


def _episodes(name: str) -> list[dict]:
    return metrics.live_episodes(name) + metrics._episodes(name, "talk") + metrics._episodes(name, "sleep")


def _verification(item: dict, episodes: list[dict], judgments: list[dict]) -> tuple[str, dict | None]:
    if item.get("status") != "kept":
        return "not kept", None
    evidence = item.get("evidence")
    if not isinstance(evidence, str):
        return "no episode citation", None
    reason, ep = _evidence(episodes, item, evidence)
    if reason:
        return reason, ep
    if ep.get("kind") != "live":
        return "cited a conversation or a sleep: no judge graded it", ep
    verdict = next((j.get("criterion_met") for j in judgments if j.get("episode_ts") == ep["ts"]), None)
    if verdict is None:
        return "cited cycle not judged", ep   # unjudged evidence is a claim the record cannot check yet
    if verdict == "no":
        return "cited cycle judged no", ep
    return "verified", ep


def verify(name: str, item: dict) -> str:
    if not NAME.fullmatch(name):
        return "invalid instance name"
    return _verification(item, _episodes(name), metrics.judgments(name))[0]


def _counts(items: list[dict], episodes: list[dict], judgments: list[dict]) -> str:
    verified = sum(_verification(it, episodes, judgments)[0] == "verified" for it in items if it["status"] == "kept")
    counts = {s: sum(it["status"] == s for it in items) for s in (*STATUSES, "expired", "overdue", "open")}
    return (f"made {len(items)}; kept (verified) {verified}; kept (not verified) {counts['kept'] - verified}; "
            + "; ".join(f"{s} {counts[s]}" for s in ("broken", "superseded", "void", "expired", "overdue", "open")))


def section(name: str) -> list[str]:
    items = list(ledger(name).values())
    if not items:
        return []
    items = [it for it in items if it["made_to"] not in ("self", "own", name)]   # promises to self are no longer kept
    if not items:
        return []
    episodes, judgments = _episodes(name), metrics.judgments(name)
    operator = [it for it in items if it["made_to"] == "operator"]
    others = [it for it in items if it["made_to"] != "operator"]
    cited = set()
    for it in items:
        if it["status"] == "kept":
            _, ep = _verification(it, episodes, judgments)
            if ep and ep.get("kind") == "live":
                cited.add(ep["ts"])
    # Count each cited cycle once even when it kept several promises.
    difficulties = {j["episode_ts"]: j["difficulty"] for j in judgments if j.get("episode_ts") in cited
                    and isinstance(j.get("difficulty"), (int, float)) and not isinstance(j["difficulty"], bool)
                    and 1 <= j["difficulty"] <= 5}
    mean = f"{sum(difficulties.values()) / len(difficulties):.2f}/5 (n={len(difficulties)})" if difficulties else "not judged"
    return ["## Promises I made and kept", "", f"- To the operator: {_counts(operator, episodes, judgments)}.",
            f"- To other instances: {_counts(others, episodes, judgments)}.",
            f"- Mean judged difficulty of cited cycles: {mean}."]


def report(names: list[str]) -> str:
    lines = []
    for name in names:
        items = ledger(name)
        lines.append(f"{name}: commitments")
        if not items:
            lines.append("  (none)")
            continue
        episodes, judgments = _episodes(name), metrics.judgments(name)
        self_count = sum(it["made_to"] in ("self", "own", name) for it in items.values())
        if self_count:
            expired = sum(it["status"] == "expired" and it["made_to"] in ("self", "own", name) for it in items.values())
            lines.append(f"  to self (no longer kept): {self_count}; expired {expired}")
        for item in items.values():
            if item["made_to"] in ("self", "own", name):
                continue
            verification = f"; {_verification(item, episodes, judgments)[0]}" if item["status"] == "kept" else ""
            lines.append(f"  {item['id']}: {item['status']}{verification}; to {item['made_to']}; due {item['due']}; "
                         f"{item['promise']} — check: {item['criterion']}"
                         + (f"; evidence {item['evidence']}" if item["evidence"] else "")
                         + (f"; {item['note']}" if item.get("note") else ""))
    return "\n".join(lines)
