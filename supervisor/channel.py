"""Where an instance's message to the operator reaches them: the web UI (a push to their devices) or Telegram (from the
instance's own bot) — docs/plans/PLAN-operator-channel.md. Stdlib only.

Every message to the operator is written to the operator inbox, which the web UI shows, whatever is chosen here; this
decides only which channel alerts them, so that they pick the conversation up there. One decision, made when the
message is delivered (broker._deliver) and recorded with it in the message log (`alert`, `alert_why`); the web
service and the Telegram service each send the rows that name them.

The order (route):
  1. a conversation still open on the web or Telegram: the most recent one's channel — the operator is there now;
  2. the operator's setting for the instance (`shaft channel <name> web|telegram`), if fixed;
  3. `auto` (the default): the channel the operator last wrote to the instance on;
  4. no conversation yet: Telegram if the instance's bot reaches the operator, else the web.
A channel that cannot alert now (Telegram: no bot, not paired, its service off; the web: its service off or no device
with notifications on) gives way to the other when that one can — and the reason says so, in the log.

The channel of a conversation is the client's own word for it (X-Shaft-Channel, kept on the API's talk record): the
web UI says web, the Telegram service says telegram. Those talk records are the one record of where the operator
talked to the instance, open and past.
"""
from __future__ import annotations

import json

from . import core

CHANNELS = ("web", "telegram")           # where an alert can go
SETTINGS = ("auto", *CHANNELS)


def preference(name: str) -> str:
    """The operator's setting for this instance: auto, web or telegram (in the supervisor's folder, which the
    instance cannot write)."""
    value = core._tool_settings(name).get("channel", "auto")
    return value if value in SETTINGS else "auto"


def set_preference(name: str, value: str) -> None:
    if value not in SETTINGS:
        raise ValueError(f"channel must be one of {', '.join(SETTINGS)}, not {value!r}")
    core._update_tool_settings(name, channel=None if value == "auto" else value)


def talks(name: str) -> list[dict]:
    """The API's conversations with this instance on the web or Telegram, newest first."""
    try:
        data = json.loads(core.API_TALKS.read_text())
    except (OSError, ValueError):
        return []
    rows = [t for t in data.values() if isinstance(t, dict) and t.get("model") == name and t.get("channel") in CHANNELS]
    return sorted(rows, key=lambda t: t.get("last", 0), reverse=True)


def can_alert(name: str, channel: str) -> str:
    """Why `channel` cannot alert the operator about this instance now ('' when it can)."""
    from .version import service
    if channel == "telegram":
        from . import telegram
        try:
            return "" if telegram.reachable(name) else (f"Telegram cannot reach the operator from {name} (no bot, "
                                                         "not paired, or its service is off)")
        except Exception as e:                   # its state unreadable: say so, never guess
            return f"Telegram's state is unreadable ({type(e).__name__})"
    from . import webpush
    if not service("web", stale=False):
        return "the web UI is not running"
    if not webpush.subscriptions():
        return "no device has the web UI's notifications on"
    return ""


def route(name: str) -> tuple[str, str]:
    """(channel, why) for a message from `name` to the operator, now."""
    from .version import service
    ts = talks(name)
    open_ = [t for t in ts if not t.get("written")] if service("api", stale=False) else []
    pref = preference(name)
    if open_:
        channel, why = open_[0]["channel"], f"the operator's conversation with {name} there is open"
    elif pref != "auto":
        channel, why = pref, f"the operator set it for {name}"
    elif ts:
        channel, why = ts[0]["channel"], f"the operator last wrote to {name} there"
    else:
        channel = "web" if can_alert(name, "telegram") else "telegram"
        why = f"the operator has not written to {name} on either yet"
    if (cannot := can_alert(name, channel)):
        other = "web" if channel == "telegram" else "telegram"
        if not can_alert(name, other):
            return other, f"{why}, but {cannot}"
        return channel, f"{why}, but {cannot}, nor can {other}: it waits in the inbox"
    return channel, why


def describe(name: str) -> str:
    """For the operator (CLI, Access page): the setting and where a message would go now."""
    channel, why = route(name)
    return f"{name} reaches you on: {preference(name)} — a message now would go to {channel}: {why}"
