"""Item-linked operator outcomes, computed for statistics only.

No supervisor record links a delivered mail draft to a sent mail: `sent` is missing,
not inferred from mail reads, feedback delivery, or unrelated operator activity.
`no_link` means there is no item-linked evidence. Explicit app, web and Telegram
replies use the delivered message's stable `file` identity. A quote in an operator
message or conversation turn can also link to a unique delivery. Plain chat
answers without an explicit link still leave the outcome unknown. Links count only
within OUTCOME_WINDOW_H of delivery. These statistics never grade, reward or impose probation.
"""
from __future__ import annotations

import json
import re
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta

from .core import jsonl_lines
from . import core, doc_comments, feedback, metrics, pushes, reply_links

OUTCOME_WINDOW_H = 48
KINDS = ("followed_up", "edited", "sent", "no_link", "open")


def _plain(text: str) -> str:
    text = re.sub(r"\[([^\]\n]+)\]\([^\s)]*\)", r"\1", text)
    text = re.sub(r"[*`]|(?<!\w)_|_(?!\w)", "", text)
    return " ".join(text.split())


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
    bodies = {r["file"]: _plain(r["text"]) if isinstance(r.get("text"), str) else "" for r in delivered}
    by_file = {r["file"]: r for r in delivered}
    replies = {}

    def link_quote(text: object, stamp: str, at: datetime, evidence: str) -> None:
        if not isinstance(text, str):
            return
        quoted = []
        for block in re.findall(r"(?m)(?:^[^\S\r\n]*>[^\n]*\n?)+", text):
            quote = _plain(re.sub(r"(?m)^[^\S\r\n]*>", "", block))
            quote = quote.removesuffix("…").removesuffix("...").rstrip()
            if len(quote) >= 20:
                quoted.append(quote)
        if not quoted:
            return
        eligible = delivered[bisect_left(stamps, at - window):bisect_right(stamps, at)]
        matched = {item["file"] for item in eligible if any(q in bodies[item["file"]] for q in quoted)}
        if len(matched) == 1:
            target, = matched
            replies.setdefault(target, []).append((stamp, "followed_up", evidence))

    for reply in rows:
        if reply.get("from") != "operator" or reply.get("to") != name:
            continue
        at = times[reply["file"]]
        if target := reply.get("in_reply_to"):
            if isinstance(target, str) and target in by_file and at - window <= times[target] <= at:
                replies.setdefault(target, []).append((reply["ts"], "followed_up", f"message:{reply['file']}"))
        else:
            link_quote(reply.get("text"), reply["ts"], at, f"quote:message:{reply['file']}")
    for turn in metrics._episodes(name, "talk"):
        stamp, talk_id = turn.get("ts"), turn.get("talk_id")
        # The operator's own turns only: a guest's turn carries `speaker` (as commitments._source_rows reads it)
        if (turn.get("role") == "user" and not turn.get("speaker") and isinstance(talk_id, str) and talk_id
                and (at := pushes._time(stamp)) and (since is None or since <= at) and at <= now):
            link_quote(turn.get("content"), stamp, at, f"quote:talk:{talk_id}@{stamp}")
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
        text = row.get("text")
        named = set(re.findall(r"(?<![\w/])notes/docs/[\w ./-]+?\.md(?![\w./-])", text)) if isinstance(text, str) else set()
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
            "includes conversation quotes, plain chat without a link remains unknown): "
            + ", ".join(f"{k} {sum(r['outcome'] == k for r in rows)}" for k in KINDS if k != "sent")
            + "; sent: missing draft-to-sent record"]


def offer_lines(name: str, now: datetime | None = None, weeks: int = 3) -> list[str]:
    """Weekly reply counts for messages delivered during judged self-origin cycles."""
    now = now or datetime.now()
    self_judged = {j["episode_ts"] for j in metrics.judgments(name, core.SUP_DIR) if j.get("origin") == "self"}
    if not self_judged:
        return []
    cycles = []
    for episode in metrics.live_episodes(name):
        stamp = episode.get("ts")
        start = pushes._time(episode.get("run_started"))
        end = pushes._time(stamp)
        if start and end and start <= end <= now and stamp in self_judged:
            cycles.append((start, end))
    if not cycles:
        return []

    week = timedelta(days=7)
    offers = []
    for row in records(name, now, since=now - weeks * week):
        sent = pushes._time(row["ts"])
        if any(start <= sent <= end for start, end in cycles):
            offers.append((sent, row["outcome"], row["evidence"]))
    lines = []
    for i in range(weeks):
        end = now - i * week
        counts = dict.fromkeys(("quote", "otherwise", "no_link", "open"), 0)
        for sent, outcome, evidence in offers:
            if end - week < sent <= end:
                kind = ("quote" if evidence.startswith("quote:") else "otherwise") if evidence else outcome
                counts[kind] += 1
        lines.append(f"unprompted offers, week to {end:%m-%d} (self-origin cycles): {sum(counts.values())} — "
                     f"replied by quote {counts['quote']}, other link (reply, thumb or comment) {counts['otherwise']}, "
                     f"no reply {counts['no_link']}, open {counts['open']}")
    return lines
