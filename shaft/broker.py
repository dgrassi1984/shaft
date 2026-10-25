"""Client for the supervisor's broker — my door to my keys, my models and the other instances.

The supervisor runs the broker while I run and hands me its address and a token
(SHAFT_BROKER_URL / SHAFT_BROKER_TOKEN). It owns the API keys and other instances'
mailboxes; I only ever see results. (Plain internet access goes through the
supervisor's proxy, HTTPS_PROXY.) Plugins can use it too:

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


def envelope() -> dict | None:
    """What I may still spend: calls left this run, today's allowances, probation, next sleep (GET /envelope; not a
    tool, so asking costs no call). None without a supervisor that serves it."""
    if not available():
        return None
    try:
        env = _request("/envelope", timeout=2)["envelope"]
    except (BrokerError, KeyError, TypeError, ValueError):
        return None
    ok = isinstance(env, dict) and all(isinstance(env.get(k), int) for k in ("calls_used", "calls_max"))
    return env if ok else None   # a malformed one must not break the cycle that shows it


SLOW_TOOLS = {"delegate_coding": 1800}   # a coding handoff runs a model loop and the test gate: minutes


def call(tool: str, /, **args) -> str:
    """Positional-only `tool`: a tool's own arguments may be called anything, `name` included (secret_set's
    first argument was taken as the tool name: "call() got multiple values for argument 'name'")."""
    out = _request(f"/tools/{tool}", args, timeout=SLOW_TOOLS.get(tool, 120))
    if not out.get("ok"):
        raise BrokerError(out.get("error") or f"{tool} failed")
    return out["result"]
