"""Operator ratings and explicit delivery, kept in an append-only supervisor log."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import secrets
from datetime import datetime

from .core import SUP_DIR

FILE = SUP_DIR / "feedback.jsonl"
_cache: dict = {"stamp": None, "items": []}


def reply_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def key(target: dict) -> str:
    if target["kind"] == "chat":
        return f"chat:{target['chat']}:{target['k']}"
    return f"message:{target['file']}"


def _append(event: str, **fields) -> dict:
    row = {"ts": datetime.now().isoformat(timespec="microseconds"), "event": event, **fields}
    line = json.dumps(row, ensure_ascii=False) + "\n"
    FILE.parent.mkdir(parents=True, exist_ok=True)
    with FILE.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
    return row


def rate(instance, login, target, thumb, reason, reply_ts, reply_text) -> dict:
    if thumb not in ("up", "down"):
        raise ValueError("Choose thumbs up or down.")
    row = _append("rated", id="f-" + secrets.token_hex(12), instance=instance, login=login,
                  target=target, thumb=thumb, reason=reason[:2000], reply_ts=reply_ts, reply_sha=reply_sha(reply_text))
    return {k: v for k, v in {**row, "sent": None}.items() if k != "event"}


def clear(fid, login) -> None:
    _append("cleared", id=fid, login=login)


def mark_sent(fid, via, message) -> None:
    _append("sent", id=fid, via=via, message=message)


def _valid_rating(row: dict) -> bool:
    if not all(isinstance(row.get(k), str) for k in
               ("id", "ts", "instance", "login", "reason", "reply_ts", "reply_sha")):
        return False
    target = row.get("target")
    if not isinstance(target, dict) or row.get("thumb") not in ("up", "down"):
        return False
    return ((target.get("kind") == "chat" and isinstance(target.get("chat"), str)
             and type(target.get("k")) is int and target["k"] >= 0)
            or (target.get("kind") == "message" and isinstance(target.get("file"), str)))


def items(instance=None) -> list[dict]:
    """Fold only valid events; a broken log line must not break the feed."""
    try:
        st = FILE.stat()
        stamp = (str(FILE), st.st_ino, st.st_mtime_ns, st.st_size)
        if _cache["stamp"] != stamp:
            current, ids = {}, {}
            with FILE.open("rb") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                        continue
                    fid = row["id"]
                    if row.get("event") == "rated" and _valid_rating(row):
                        target = (row["instance"], key(row["target"]))
                        previous = current.get(target)
                        if previous:
                            ids.pop(previous["id"], None)
                        item = {k: v for k, v in row.items() if k != "event"}
                        current[target] = {**item, "sent": None}
                        ids[fid] = target
                    elif fid in ids:
                        target = ids[fid]
                        if row.get("event") == "cleared":
                            current.pop(target, None)
                            ids.pop(fid)
                        elif row.get("event") == "sent" and all(isinstance(row.get(k), str)
                                                               for k in ("ts", "via", "message")):
                            current[target]["sent"] = {k: row[k] for k in ("ts", "via", "message")}
            folded = sorted(reversed(list(current.values())), key=lambda r: r["ts"], reverse=True)
            _cache.update(stamp=stamp, items=folded)
    except OSError:
        return []
    return copy.deepcopy([r for r in _cache["items"] if instance is None or r["instance"] == instance])


def by_target(instance) -> dict[str, dict]:
    return {key(r["target"]): r for r in items(instance)}


def down_shas(instance) -> set[str]:
    return {r["reply_sha"] for r in items(instance) if r["thumb"] == "down"}


def note(item, reply_text) -> str:
    """The operator's feedback, quoting only the supervisor's recorded reply."""
    target = item["target"]
    # The turn index counts the operator's turns too: the reply's time names it for both of them.
    where = f"web chat {target['chat']}" if target["kind"] == "chat" else f"message {target['file']}"
    up = item["thumb"] == "up"
    ending = ("This is information about what was useful to the operator. It is not a score and changes no grade."
              if up else "If it names something you got wrong, record it with record_correction.")
    return (f"[Operator feedback: {'👍' if up else '👎'} on your reply of "
            f"{item['reply_ts'][:16].replace('T', ' ')} ({where}):\n"
            f" «{reply_text[:300]}{'…' if len(reply_text) > 300 else ''}»\n"
            f" What was {'good' if up else 'wrong'}: {_sentence(item['reason']) or 'no reason given.'}\n {ending}]")


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    return text + "." if text and text[-1] not in ".!?…" else text


def deliver(item, text) -> None:
    """Retry a logged delivery by rating id without creating a second inbox message."""
    from . import messages
    name, fid = item["instance"], item["id"]
    sent = messages.sent_mid(name, fid)
    file = sent["file"] if sent else messages.deliver_feedback(
        name, text, shown=f"{'👍' if item['thumb'] == 'up' else '👎'} feedback on its reply of "
                         f"{item['reply_ts'][11:16]}", mid=fid).name
    mark_sent(fid, "inbox", file)
