"""The web UI's half of channel.py (docs/plans/PLAN-operator-channel.md): every message an instance sends the operator
that is routed to the web (`alert` "web" in .supervisor/messages.jsonl) wakes the operator's devices with a push that
opens the instance's chat, where the message shows. Telegram's half is telegram.forward_new; both read the one log,
each with its own cursor. Runs in the web service (a thread of web.serve): it is the process that knows which devices have the
instance's chat on screen (web_chat.watchers), and the one a push is sent from.
"""
from __future__ import annotations

import json
import threading

from . import core
from .core import log

EVERY_S = 5


def _cursor_path():
    return core.SUP_DIR / "web" / "alert-cursor.json"


def _read_cursor() -> int | None:
    """How far into the message log this service has read (a byte offset: messages.appended)."""
    try:
        value = json.loads(_cursor_path().read_text()).get("offset")
        return value if isinstance(value, int) else None
    except (OSError, ValueError, AttributeError):
        return None


def _write_cursor(offset: int) -> None:
    path = _cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offset": offset}))


def event(m: dict) -> dict:
    """What the notification shows: who, whether it asks, its first line; it opens the instance's chat."""
    name = m["from"]
    from .web_md import first_line
    first = first_line(m["text"]) or "(a file)"                       # the text, not its Markdown
    return {"title": f"{name} {'asks' if m.get('kind') == 'question' else 'says'}", "body": first[:160],
            "url": f"/i/{name}/chat", "tag": f"{name}-{m['file']}"}   # its own: a second never replaces it


def forward_new() -> int:
    """Push each message to the operator routed to the web since the last call, in the order they were logged. Starts
    from 'now' the first time (no backlog). Returns how many were pushed."""
    from . import messages, web_chat, webpush
    rows, end = messages.appended(_read_cursor())
    pushed = 0
    for m, after in rows:
        if m.get("to") == "operator" and m.get("alert") == "web":
            looking = web_chat.watchers(m["from"])   # a device with its chat on screen sees the message there now
            if looking:
                log(f"web: {m['from']}'s message {m['file']} not pushed to {', '.join(sorted(looking))}: its chat is "
                    "on screen there")
            if webpush.notify(event(m), skip=looking):
                pushed += 1
        _write_cursor(after)
    _write_cursor(end)
    return pushed


def start() -> threading.Event:
    """The forwarding thread, every EVERY_S seconds; set the returned event to stop it."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                forward_new()
            except Exception as e:                   # one bad row must not stop the alerts; say what went wrong
                log(f"web: alerts: {type(e).__name__}: {e}")
            stop.wait(EVERY_S)
    threading.Thread(target=loop, daemon=True, name="web-alerts").start()
    return stop
