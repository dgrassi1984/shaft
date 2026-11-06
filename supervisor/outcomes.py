"""Item-linked operator outcomes, computed for statistics only.

No supervisor record links a delivered mail draft to a sent mail: `sent` is missing,
not inferred from mail reads, feedback delivery, or unrelated operator activity.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from . import core, doc_comments, feedback, pushes

OUTCOME_WINDOW_H = 48
KINDS = ("followed_up", "edited", "sent", "ignored", "open")


def _ratings(name: str) -> list[dict]:
    # A cleared/replaced thumb still records that the operator acted on this item.
    try:
        lines = feedback.FILE.read_text().splitlines()
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


def records(name: str, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    rows = pushes.records(core.SUP_DIR)
    delivered = [r for r in rows if r.get("from") == name and r.get("to") == "operator" and pushes._time(r["ts"]) <= now]
    if not delivered:
        return []
    replies = {}
    for reply in rows:
        if reply.get("from") != "operator" or reply.get("to") != name:
            continue
        quoted = [" ".join(line[1:].strip() for line in b.splitlines())
                  for b in re.findall(r"(?m)(?:^>[^\n]*\n?)+", reply.get("text", ""))]
        matched = {item["file"] for item in delivered if pushes._time(item["ts"]) <= pushes._time(reply["ts"])
                   for body in [" ".join(item.get("text", "").split())]
                   if any(q and q in body and len(q) >= min(len(body), 20) for q in quoted)}
        target = reply.get("in_reply_to") or (next(iter(matched)) if len(matched) == 1 else None)
        if target:
            replies.setdefault(target, []).append((reply["ts"], "followed_up", f"message:{reply['file']}"))
    ratings = _ratings(name)
    comments = doc_comments.threads(name)
    out = []
    for row in delivered:
        sent = pushes._time(row["ts"])
        end = sent + timedelta(hours=OUTCOME_WINDOW_H)
        links = list(replies.get(row["file"], []))
        for rating in ratings:
            if rating["target"].get("kind") == "message" and rating["target"].get("file") == row["file"]:
                links.append((rating["ts"], "followed_up", f"feedback:{rating['id']}"))
        docs = set()
        for att in row.get("attachments", []):
            path = att.get("path", "")
            marker = "notes/docs/"
            docs.add(path[path.index(marker):] if marker in path else path)
        for thread in comments:
            doc = "notes/docs/" + thread["doc"]
            if doc in docs or re.search(r"(?<![\w/])" + re.escape(doc) + r"(?![\w./-])", row.get("text", "")):
                for post in thread["posts"]:
                    if post["by"] == "operator":
                        links.append((post["ts"], "edited", f"comment:{thread['id']}:{post['pid']}"))
        links = sorted((at, kind, evidence) for ts, kind, evidence in links
                       if (at := pushes._time(ts)) and sent <= at <= end and at <= now)
        if links:
            at, outcome, evidence = links[0]
        else:
            outcome, evidence = ("ignored" if now >= end else "open"), ""
            at = end if now >= end else now
        out.append({"file": row["file"], "ts": row["ts"], "outcome": outcome,
                    "evidence": evidence, "at": at.isoformat(timespec="seconds")})
    return out


def lines(name: str, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    rows = [r for r in records(name, now) if now - timedelta(days=7) <= pushes._time(r["ts"]) <= now]
    if not rows:
        return []
    return [f"operator outcomes (last 7 days' messages, {OUTCOME_WINDOW_H} h window): "
            + ", ".join(f"{k} {sum(r['outcome'] == k for r in rows)}" for k in KINDS if k != "sent")
            + "; sent: missing draft-to-sent record"]
