"""The operator's read marks, shared by native apps and web browsers.

seen.json holds {"operator": {instance: {cid: {last, last_count, at}}}}.
Writes and migration are atomic under a lock. Legacy timestamp-only marks keep
that comparison until the conversation is read again. Stdlib only.
"""
from __future__ import annotations

import threading
from datetime import datetime

STORE = "seen.json"
_lock = threading.Lock()


def _rank(mark: dict) -> tuple:
    return (mark.get("last") or mark.get("at") or "", mark.get("last_count", -1))


def _operator(data: dict) -> dict:
    """Fold device and legacy single-conversation marks into the shared store."""
    from . import web_chat
    shared = {}
    for instances in data.values():
        for name, marks in (instances or {}).items():
            marks = marks or {}
            if "version" in marks or "last" in marks or "at" in marks:
                chats = web_chat.chats(name)
                opened = [c for c in chats if c.get("state") != "ended"]
                def activity(c):
                    turns = c.get("turns") or []
                    return (turns[-1]["ts"] if turns else c.get("created", ""), c.get("created", ""))
                chat = max(opened, key=activity) if opened else next(iter(chats), None)
                marks = {"from": marks, **({web_chat.public_id(name, chat["id"]): marks} if chat else {})}
            target = shared.setdefault(name, {})
            for cid, mark in marks.items():
                if cid not in target or _rank(mark) > _rank(target[cid]):
                    target[cid] = mark
    return {"operator": shared}


def _marks(device: str, name: str) -> dict:
    """Read shared marks; device is retained for caller compatibility only."""
    from . import web
    with _lock:
        old = web._read(STORE)
        data = _operator(old)
        if data != old:
            web._write(STORE, data)
        return data["operator"].get(name, {})


def _advance(name: str, cid: str, mark: dict) -> None:
    from . import web
    with _lock:
        old = web._read(STORE)
        data = _operator(old)
        marks = data["operator"].setdefault(name, {})
        if cid not in marks or _rank(mark) > _rank(marks[cid]):
            marks[cid] = {**mark, "at": datetime.now().isoformat(timespec="seconds")}
        if data != old:
            web._write(STORE, data)


def seen(device: str, name: str, version: str, cid: str | None = None) -> bool:
    """Accept only the drawn feed version. device is unused for shared storage."""
    from . import web_chat
    model = web_chat.from_feed(name) if cid == "from" else web_chat.feed(name, cid)
    if model is None or model["version"] != version:
        return False
    key = cid or (model.get("chat") or {}).get("id") or "from"
    key = web_chat.public_id(name, key) or "from"
    _advance(name, key, {"version": version, "last": model["last"], "last_count": model["last_count"]})
    return True


def seen_until(device: str, name: str, cid: str, last: str) -> bool:
    """Mark actual instance items at or before last. device is unused for storage.

    Bound future browser stamps to the feed, so later arrivals remain unread.
    """
    return _seen_until(device, name, cid, last, None)


def _seen_until(device: str, name: str, cid: str, last: str, count: int | None) -> bool:
    """Bound browser counts to both the rendered items and the current feed."""
    from . import web_chat
    model = web_chat.from_feed(name, whole=True) if cid == "from" else web_chat.feed(name, cid, whole=True)
    if model is None:
        return False
    stamps = [i["ts"] for i in model["items"] if i["type"] in ("assistant", "sent", "retried")
              and i.get("ts") and i["ts"] <= last]
    if stamps:
        at = max(stamps)
        available = model["last_count"] if at == model["last"] else stamps.count(at)
        count = available if count is None else min(count, available)
        _advance(name, web_chat.public_id(name, cid) or "from", {"last": at, "last_count": count})
    return True


def unread_feeds(device: str, name: str, feeds: dict[str, dict]) -> dict[str, bool]:
    """Compare shared marks; device is unused. Unmarked feeds use earliest pairing."""
    from . import web
    marks = _marks(device, name)
    paired = min((d["created"] for d in web.devices().values() if d.get("created")), default="")
    out = {}
    for cid, model in feeds.items():
        mark = marks.get(cid)
        since = (mark.get("last") or mark.get("at") or "") if mark else paired
        out[cid] = bool(model.get("last")) and (model["last"] > since or (
            model["last"] == since and mark is not None and "last_count" in mark
            and model.get("last_count", 0) > mark["last_count"]))
    return out


def unread_conversations(device: str, name: str) -> dict[str, bool]:
    """Unread per conversation, including the shown ended chat for older clients."""
    from . import web_chat
    chats = web_chat.threads(name)
    if not chats and (chat := web_chat.shown(name)) and not web_chat.is_from(chat):
        chats = [chat]
    feeds = {c["id"]: web_chat.feed(name, c["id"]) for c in chats}
    feeds["from"] = web_chat.from_feed(name)
    return unread_feeds(device, name, feeds)


def unread(device: str, summaries: dict[str, dict] | None = None) -> dict[str, bool]:
    """Aggregate shared unread by instance; device is retained for compatibility."""
    from .core import instance_names
    return {name: any(unread_conversations(device, name).values())
            for name in (summaries if summaries is not None else instance_names())}


def badge(device: str) -> int:
    """Count unread instances for the operator; device is unused for storage."""
    return sum(unread(device).values())


def forget(device: str) -> None:
    """Retain shared marks on unpair; device is unused except for compatibility."""
    # Migrate before old device records can be forgotten by any caller.
    _marks(device, "")
