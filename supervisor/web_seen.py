"""What each native app has seen of each instance's threads (docs/app-api.md §12): the server's own unread marks, for the
badge on the app's icon and on every push. The PWA keeps its marks in the browser; an app's badge is set by a push
while the app is closed, so the server must know. Stdlib only.

A feed is unread on a device when its `last`, the newest answer or message sent to the operator,
exceeds its seen stamp, or matches it with more instance items at that stamp. Old marks without a count
keep the timestamp-only comparison. The operator's own messages never make a chat unread, and an
instance the device never marked counts from the device's pairing: what came before it is not unread.

Kept in .supervisor/web/seen.json ({device: {instance: {cid: {version, last, last_count, at}}}}), beside devices.json, written as web.py
writes its files (atomic, mode 600) under a lock.
"""
from __future__ import annotations

import threading
from datetime import datetime

STORE = "seen.json"
_lock = threading.Lock()


def _marks(device: str, name: str) -> dict:
    """Migrate a legacy mark before reading or writing so other devices retain their history."""
    from . import web, web_chat
    with _lock:
        data = web._read(STORE)
        marks = (data.get(device) or {}).get(name) or {}
        if "version" in marks or "last" in marks or "at" in marks:
            chat = web_chat.shown(name)
            marks = {"from": marks, **({chat["id"]: marks} if chat else {})}
            data[device][name] = marks
            web._write(STORE, data)
        return marks


def seen(device: str, name: str, version: str, cid: str | None = None) -> bool:
    """Record only the feed version the device drew; a changed feed must be drawn again."""
    from . import web, web_chat
    model = web_chat.from_feed(name) if cid == "from" else web_chat.feed(name, cid)
    if model is None or model["version"] != version:
        return False
    key = cid or (model.get("chat") or {}).get("id") or "from"
    _marks(device, name)
    with _lock:
        data = web._read(STORE)
        data.setdefault(device, {}).setdefault(name, {})[key] = {
            "version": version, "last": model["last"], "last_count": model["last_count"],
            "at": datetime.now().isoformat(timespec="seconds")}
        web._write(STORE, data)
    return True


def unread_feeds(device: str, name: str, feeds: dict[str, dict]) -> dict[str, bool]:
    """Compare each feed with its own mark, including same-second instance items."""
    from . import web
    marks = _marks(device, name)
    paired = (web.devices().get(device) or {}).get("created", "")
    out = {}
    for cid, model in feeds.items():
        mark = marks.get(cid)
        since = (mark.get("last") or mark.get("at") or "") if mark else paired
        out[cid] = bool(model.get("last")) and (model["last"] > since or (
            model["last"] == since and mark is not None and "last_count" in mark
            and model.get("last_count", 0) > mark["last_count"]))
    return out


def unread(device: str, summaries: dict[str, dict] | None = None) -> dict[str, bool]:
    """Count an instance as unread when any open thread or its From feed is unread."""
    from . import web_chat
    from .core import instance_names
    out = {}
    for name in summaries if summaries is not None else instance_names():
        chats = web_chat.threads(name)
        # Keep the last ended chat visible to old builds until it is cleared.
        if not chats and (chat := web_chat.shown(name)):
            chats = [chat]
        feeds = {c["id"]: web_chat.feed(name, c["id"]) for c in chats}
        feeds["from"] = web_chat.from_feed(name)
        out[name] = any(unread_feeds(device, name, feeds).values())
    return out


def badge(device: str) -> int:
    """How many instances have something `device` has not seen: the app's badge (every push sets it)."""
    return sum(unread(device).values())


def forget(device: str) -> None:
    """Drop a device's marks: it was unpaired (web.unpair)."""
    from . import web
    with _lock:
        data = web._read(STORE)
        if data.pop(device, None) is not None:
            web._write(STORE, data)
