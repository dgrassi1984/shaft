"""Client for the supervisor's broker — my only door to the outside world.

The supervisor runs the broker while I run and hands me its address and a token
(SHAFT_BROKER_URL / SHAFT_BROKER_TOKEN). It owns the internet, the API keys and
other instances' mailboxes; I only ever see results. Plugins can use it too:

    from shaft import broker
    text = broker.call("web_search", query="...")
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class BrokerError(Exception):
    pass


def available() -> bool:
    return bool(os.environ.get("SHAFT_BROKER_URL"))


def _request(path: str, payload: dict | None = None, timeout: int = 120) -> dict:
    url = os.environ.get("SHAFT_BROKER_URL")
    if not url:
        raise BrokerError("no broker: SHAFT_BROKER_URL is not set (am I running outside the supervisor?)")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, headers={
        "Content-Type": "application/json", "X-Shaft-Token": os.environ.get("SHAFT_BROKER_TOKEN", "")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise BrokerError(f"broker HTTP {e.code} on {path}: {e.read().decode(errors='replace')[:300]}") from e
    except (urllib.error.URLError, OSError) as e:
        raise BrokerError(f"broker unreachable at {url}: {e}") from e


def tools() -> list[dict]:
    """OpenAI-style function specs of the tools the supervisor offers."""
    return _request("/tools")["tools"]


def call(name: str, **args) -> str:
    out = _request(f"/tools/{name}", args)
    if not out.get("ok"):
        raise BrokerError(out.get("error") or f"{name} failed")
    return out["result"]
