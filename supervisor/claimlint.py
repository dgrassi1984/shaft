"""Deterministic checks for claims the recorded tool log cannot support."""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, timedelta

from . import broker, commitments, core, metrics

BUILTIN = frozenset({
    "read_file", "write_file", "append_file", "list_files", "rewrite_facts", "write_digest", "search_memory",
    "remember", "update_self_model", "update_personality", "view_image", "commit_to", "settle_commitment",
    "declare_intent", "confirm_confidence", "finish_cycle", "close_surprise", "retire_fact",
    "record_correction", "close_correction", "propose_drive_amendment", "open_project", "close_project",
    "submit_answer", "queue_for_next_cycle", "plan_reply",
})


def _arg(call: dict, key: str) -> str:
    args = call.get("args", "")
    return metrics._arg(json.dumps(args) if isinstance(args, dict) else args, key)


def _candidate(call: dict, channel: str) -> bool:
    tool = call.get("tool")
    if channel == "message":
        return tool == "send_message" and _arg(call, "to") == "operator"
    if channel == "web_document" and tool in ("write_file", "append_file"):
        return _arg(call, "path").startswith("notes/docs/")
    return (tool in ("http_request", "run") or channel == "mail" and tool in ("mail_draft", "mail_draft_update")
            or isinstance(tool, str) and bool(tool) and tool not in broker.SPECS and tool not in BUILTIN)


def lint(name: str, ep: dict) -> list[dict]:
    """Findings for one live episode. Read records without changing them."""
    findings = []

    def add(check, what, ref):
        findings.append({"episode_ts": ep["ts"], "check": check, "what": what[:200], "ref": ref})

    calls = [c for c in ep.get("tools") or [] if isinstance(c, dict)]   # the episode is the instance's file
    channel = (ep.get("finish") or {}).get("delivered_via")
    if channel in ("message", "mail", "notion", "web_document"):
        if not any(isinstance(c.get("result"), str) and not c["result"].startswith(commitments.FAILED)
                   and _candidate(c, channel) for c in calls):
            detail = ("send_message to the operator" if channel == "message" else f"candidate call for {channel}")
            add("delivered_via",
                f"said delivered via {channel}; no successful {detail} in this cycle's tool log", channel)

    rows = commitments.recorded(name)[0]   # the supervisor's copy too: a settle removed from the file still counts
    start, end = commitments._at(ep.get("run_started")), commitments._at(ep["ts"])
    settled = set()
    for row in rows:
        if row.get("event") != "settled" or row.get("status") != "kept" or not isinstance(row.get("id"), str):
            continue
        if "run_started" not in ep:
            in_cycle = any(c.get("tool") == "settle_commitment" and _arg(c, "id") == row.get("id") for c in calls)
        else:
            at = commitments._at(row.get("at"))
            in_cycle = start is not None and end is not None and at is not None and start < at <= end
        if in_cycle:
            settled.add(row["id"])
    if settled:
        from . import served
        items, episodes, record = commitments.ledger(name, rows=rows), commitments._episodes(name), served.Record(name)
        for cid in sorted(settled):
            item = items.get(cid)
            if item is None or item["status"] != "kept":
                continue
            reason, _ = commitments._evidence(episodes, item, item["evidence"], record)
            if reason:
                add("kept_commitment", f"said commitment {cid} was kept; {reason}", cid)
    return findings


def _path(name: str):
    return core.SUP_DIR / "claimlint" / f"{name}.jsonl"


def records(name: str, as_of: datetime | None = None) -> list[dict]:
    """Read ledger rows, skipping torn lines and rows after the requested time."""
    path = _path(name)
    rows = []
    for line in core.jsonl_lines(path.read_text()) if path.exists() else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        at = commitments._at(row.get("at"))
        if at is None or as_of is not None and at > commitments._local(as_of):
            continue
        rows.append(row)
    return rows


def run(name: str) -> int:
    """Lint the newest unprocessed live episodes once, under a ledger lock."""
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = records(name)
        last = max((at for r in rows if (at := metrics._ts(r.get("episode_ts"))) is not None), default=None)
        episodes = sorted((ep for ep in metrics.live_episodes(name) if metrics._ts(ep.get("ts")) is not None),
                          key=lambda ep: metrics._ts(ep["ts"]))
        episodes = [ep for ep in episodes if metrics._ts(ep["ts"]) > last] if last is not None else episodes[-20:]
        count = 0
        with path.open("a") as out:
            for ep in episodes:
                findings = lint(name, ep)
                row = {"at": datetime.now().isoformat(), "episode_ts": ep["ts"], "findings": findings}
                # A previous interrupted append must not swallow the next complete row.
                out.write("\n" + json.dumps(row) + "\n")
                out.flush()
                count += len(findings)
        return count


def head_lines(name: str, since: str | None = None) -> list[str]:
    """Show up to five recent findings, newest episode first."""
    now = datetime.now()
    findings = []
    for row in records(name):
        ts = row["episode_ts"]
        at = metrics._ts(ts)
        after = metrics._ts(since)
        if (after is not None and at is not None and at > after or since is None and at is not None
                and now - timedelta(hours=24) <= at <= now):
            findings.extend(row["findings"])
    findings.sort(key=lambda finding: metrics._ts(finding["episode_ts"]) or datetime.min, reverse=True)
    return [f"- {f['episode_ts'][:16]}: {f['what']}" for f in findings[:5]]
