"""Item-linked operator outcomes, computed for statistics only.

No supervisor record links a delivered mail draft to a sent mail: `sent` is missing,
not inferred from mail reads, feedback delivery, or unrelated operator activity.
`no_link` is not "ignored": only item-linked evidence counts. Explicit app, web and
Telegram replies are linked by the delivered message's stable `file` identity;
unlinked chat answers still make `followed_up` a floor. Links count only within
OUTCOME_WINDOW_H of delivery. These statistics never grade, reward or impose probation.
"""
from __future__ import annotations

import json
import re
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta

from .core import jsonl_lines
from . import core, doc_comments, feedback, pushes, reply_links

OUTCOME_WINDOW_H = 48
KINDS = ("followed_up", "edited", "sent", "no_link", "open")


def _ratings(name: str) -> list[dict]:
    # A cleared/replaced thumb still records that the operator acted on this item.
    try:
        lines = jsonl_lines(feedback.FILE.read_text())
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") == "rated" and row.get("instance") == name and feedback._valid_rating(row):
            rows.append(row)
    return rows


def records(name: str, now: datetime | None = None, since: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    window = timedelta(hours=OUTCOME_WINDOW_H)
    rows = [r for r in pushes.records(core.SUP_DIR) if isinstance(r.get("file"), str) and r["file"]
            and (at := pushes._time(r.get("ts"))) and (since is None or at >= since) and at <= now]
    times = {r["file"]: pushes._time(r["ts"]) for r in rows}
    delivered = sorted((r for r in rows if r.get("from") == name and r.get("to") == "operator"),
                       key=lambda r: times[r["file"]])
    if not delivered:
        return []
    stamps = [times[r["file"]] for r in delivered]
    bodies = {r["file"]: " ".join(r.get("text", "").split()) for r in delivered}
    by_file = {r["file"]: r for r in delivered}
    replies = {}
    for reply in rows:
        if reply.get("from") != "operator" or reply.get("to") != name:
            continue
        at = times[reply["file"]]
        quoted = [" ".join(" ".join(line[1:].strip() for line in b.splitlines()).split())
                  for b in re.findall(r"(?m)(?:^>[^\n]*\n?)+", reply.get("text", ""))]
        quoted = [q for q in quoted if len(q) >= 20]
        eligible = delivered[bisect_left(stamps, at - window):bisect_right(stamps, at)]
        matched = {item["file"] for item in eligible if any(q in bodies[item["file"]] for q in quoted)}
        target = reply.get("in_reply_to") or (next(iter(matched)) if len(matched) == 1 else None)
        if target in by_file and at - window <= times[target] <= at:
            replies.setdefault(target, []).append((reply["ts"], "followed_up", f"message:{reply['file']}"))
    for link in reply_links.records():
        target = link["in_reply_to"]
        if link.get("instance") == name and target in by_file:
            replies.setdefault(target, []).append((link.get("ts"), "followed_up", f"reply:{link.get('key', '')}"))
    ratings = {}
    for rating in _ratings(name):
        if rating["target"].get("kind") == "message":
            ratings.setdefault(rating["target"].get("file"), []).append(
                (rating["ts"], "followed_up", f"feedback:{rating['id']}"))
    try:
        comments = doc_comments.threads(name)
    except doc_comments.Refused:
        comments = []
    docs = {}
    for row in delivered:
        attached = row.get("attachments") or []
        paths = {att["path"] for att in attached if isinstance(att, dict) and isinstance(att.get("path"), str)} \
            if isinstance(attached, list) else set()
        named = set(re.findall(r"(?<![\w/])notes/docs/[\w ./-]+?\.md(?![\w./-])", row.get("text", "")))
        for path in paths:
            marker = "notes/docs/"
            named.add(path[path.index(marker):] if marker in path else path)
        for doc in named:
            docs.setdefault(doc, []).append(row)
    for thread in comments:
        candidates = docs.get("notes/docs/" + thread["doc"], [])
        dates = [times[r["file"]] for r in candidates]
        for post in thread["posts"]:
            at = pushes._time(post.get("ts"))
            if post["by"] != "operator" or at is None or at > now:
                continue
            index = bisect_right(dates, at) - 1
            if index >= 0 and at - dates[index] <= window:
                target = candidates[index]["file"]
                replies.setdefault(target, []).append((post["ts"], "edited", f"comment:{thread['id']}:{post['pid']}"))
    out = []
    for row in delivered:
        sent = times[row["file"]]
        end = sent + window
        links = replies.get(row["file"], []) + ratings.get(row["file"], [])
        links = sorted((at, kind, evidence) for ts, kind, evidence in links
                       if (at := pushes._time(ts)) and sent <= at <= end and at <= now)
        if links:
            at, outcome, evidence = links[0]
        else:
            outcome, evidence = ("no_link" if now >= end else "open"), ""
            at = end if now >= end else now
        out.append({"file": row["file"], "ts": row["ts"], "outcome": outcome,
                    "evidence": evidence, "at": at.isoformat(timespec="seconds")})
    return out


def lines(name: str, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    since = now - timedelta(days=7, hours=OUTCOME_WINDOW_H)
    rows = [r for r in records(name, now, since=since) if now - timedelta(days=7) <= pushes._time(r["ts"]) <= now]
    if not rows:
        return []
    return [f"operator outcomes (last 7 days' messages, {OUTCOME_WINDOW_H} h window; a floor, "
            "unlinked chat answers remain unknown): "
            + ", ".join(f"{k} {sum(r['outcome'] == k for r in rows)}" for k in KINDS if k != "sent")
            + "; sent: missing draft-to-sent record"]
