"""What each native app has seen of each instance's chat (docs/app-api.md §12): the server's own unread marks, for the
badge on the app's icon and on every push. The PWA keeps its marks in the browser; an app's badge is set by a push
while the app is closed, so the server must know. Stdlib only.

An instance is unread on a device when its `last` — web_chat.summary's, the newest answer or message it sent the
operator — exceeds the seen stamp, or matches it with more instance items at that stamp. Old marks without a count
keep the timestamp-only comparison. The operator's own messages never make a chat unread, and an
instance the device never marked counts from the device's pairing: what came before it is not unread.

Kept in .supervisor/web/seen.json ({device: {instance: {version, last, last_count, at}}}), beside devices.json, written as web.py
writes its files (atomic, mode 600) under a lock.
"""
from __future__ import annotations

import threading
from datetime import datetime

STORE = "seen.json"
_lock = threading.Lock()


def seen(device: str, name: str, version: str) -> bool:
    """`device` has shown `name`'s current chat up to `version`: recorded, with the chat's `last` at that version.
    False, recording nothing, when `version` is not the current feed's any more — the feed moved since the device drew
    it, so it must not mark what it has not shown (/app/v1/chat/<n>/seen answers 409 stale_version)."""
    from . import web, web_chat
    model = web_chat.feed(name)
    if model is None or model["version"] != version:
        return False
    with _lock:
        data = web._read(STORE)
        data.setdefault(device, {})[name] = {"version": version, "last": model["last"],
                                             "last_count": model["last_count"],
                                             "at": datetime.now().isoformat(timespec="seconds")}
        web._write(STORE, data)
    return True


def unread(device: str, summaries: dict[str, dict] | None = None) -> dict[str, bool]:
    """Per instance, whether it has something `device` has not seen. `summaries`: web_chat.summary of each, when the
    caller has them already (/app/v1/instances)."""
    from . import messages, web, web_chat
    from .core import instance_names
    marks = web._read(STORE).get(device) or {}
    paired = (web.devices().get(device) or {}).get("created", "")
    if summaries is None:
        history = messages.history()
        summaries = {n: web_chat.summary(n, history) for n in instance_names()}
    out = {}
    for name, s in summaries.items():
        mark = marks.get(name)
        since = (mark.get("last") or mark.get("at") or "") if mark else paired
        out[name] = bool(s.get("last")) and (s["last"] > since or (
            s["last"] == since and mark is not None and "last_count" in mark
            and s.get("last_count", 0) > mark["last_count"]))
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
