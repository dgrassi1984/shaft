"""Self-hosted model servers the supervisor reaches for instances, through the broker — with the local Bonsai as
fallback.

An instance whose config.env says LLM_BASE_URL="broker" and LLM_MODEL="<server>/<model>" (e.g. racer/bonsai2) thinks
on that server: Daniele's RTX 5070 Ti PC ("racer", llama.cpp over Tailscale; Daniele, 2026-10-14). The instance
cannot reach the tailnet itself — nothing of Daniele's is reachable from the sandbox — so the broker relays the
OpenAI-format request as it is (chat_template_kwargs included: llama.cpp applies them to Bonsai's template like
SHAFT's own server does), and the stream back as it arrives.

- Which servers exist is the operator's choice (SHAFT_SELF_HOSTED: {name: {"url", "sampling", "think_budget"}});
  config.env only names one. "sampling" fills what a request leaves unset. "think_budget" bounds reasoning like
  SHAFT's own server does: llama.cpp ends thinking after `thinking_budget_tokens` (measured 2026-10-14: 100 asked,
  114 tokens in all, against 1,448 unbounded), and a request's own smaller thinking_budget (after a tool result) wins.
- If the server cannot be reached — or fails before sending anything (5xx) — the same request goes to the local
  Bonsai (SHAFT_SELF_HOSTED_FALLBACK, default the first SHAFT_LLM_PORTS server), with its own sampling. A started
  stream is never switched over. Every fallback is logged; with no fallback the call fails and says why.
- Usage goes to the broker's usage log at $0, with the server that actually answered.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .cloudllm import CloudError, _record, _relay
from .core import LLM_PORTS, knob

SERVERS, _ = knob("SHAFT_SELF_HOSTED", json.dumps({"racer": {
    "url": "http://100.120.0.1:8080/v1",   # DMDRacer over Tailscale: Bonsai 2 27B on an RTX 5070 Ti (llama.cpp)
    "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.05},   # Daniele's opencode config
    "think_budget": 4096}}), json.loads)   # the same ceiling as SHAFT's own server (SHAFT_THINK_BUDGET)
FALLBACK, _ = knob("SHAFT_SELF_HOSTED_FALLBACK", f"http://127.0.0.1:{LLM_PORTS[0]}/v1", str)   # "" = none
TIMEOUT_S, _ = knob("SHAFT_SELF_HOSTED_TIMEOUT_S", "1800", int)   # a long prompt is read before the first byte
PROBE_S = 3   # an offline tailnet host does not refuse: it lets a connection hang until the timeout


def serves(model: str | None) -> bool:
    prefix, slash, name = str(model or "").partition("/")
    return bool(slash and name) and prefix in SERVERS


def server(model: str) -> dict:
    return SERVERS[model.partition("/")[0]]


def host_port(model: str) -> str:
    """'100.120.0.1:8080': which model server an instance on `model` takes (runner.model_server)."""
    parts = urllib.parse.urlsplit(server(model)["url"])
    return f"{parts.hostname}:{parts.port or (443 if parts.scheme == 'https' else 80)}"


def think_budget(body: dict, ceiling: int) -> int:
    """The reasoning tokens this request may spend (0 = no bound, or thinking is off): the server's ceiling, or less
    if the request asks for less (chat_template_kwargs.thinking_budget, as for SHAFT's own server)."""
    kwargs = body.get("chat_template_kwargs") or {}
    if kwargs.get("enable_thinking") is False or ceiling <= 0:
        return 0
    asked = kwargs.get("thinking_budget")
    return min(ceiling, asked) if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0 else ceiling


def _reachable(url: str) -> str | None:
    """None if something listens at the URL's host and port, else why not."""
    parts = urllib.parse.urlsplit(url)
    try:
        socket.create_connection((parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)),
                                 timeout=PROBE_S).close()
        return None
    except OSError as e:
        return f"unreachable ({e})"


def _request(url: str, body: dict) -> urllib.request.Request:
    return urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"})


def _open(url: str, body: dict):
    """(response, None) or (None, why): an error worth a fallback is one before anything was generated."""
    why = _reachable(url)
    if why:
        return None, why
    try:
        return urllib.request.urlopen(_request(url, body), timeout=TIMEOUT_S), None
    except urllib.error.HTTPError as e:
        detail = f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}"
        if e.code < 500:   # the request itself is wrong: the local server would refuse it too
            raise CloudError(e.code, f"{url} refused the request: {detail}") from e
        return None, detail
    except (urllib.error.URLError, OSError) as e:
        return None, f"network: {e}"


def open_reply(instance: str, body: dict, fallback: bool = True, note: Callable[[str], None] = lambda s: None):
    """Send `body` to its model's server, or to the local fallback: (response, label of who answers, started)."""
    model, started = body["model"], time.monotonic()
    srv = server(model)
    upstream = {**srv.get("sampling", {}), **body, "model": model.partition("/")[2]}
    budget = think_budget(body, srv.get("think_budget", 0))
    if budget:
        upstream["thinking_budget_tokens"] = budget
    resp, why = _open(srv["url"], upstream)
    if resp is not None:
        return resp, model, started
    target = FALLBACK if fallback else ""
    if not target:
        _record(instance, model, None, None, 0.0, started, None, why)
        raise CloudError(502, f"{model} at {srv['url']}: {why}; no local fallback"
                              f"{'' if fallback else ' for a trial (it never takes the local model)'}")
    note(f"{model} at {srv['url']}: {why} -> local fallback {target}")
    resp, why_local = _open(target, {**body, "model": model.partition("/")[2]})
    if resp is None:
        _record(instance, model, None, None, 0.0, started, None, f"{why}; fallback: {why_local}")
        raise CloudError(502, f"{model} at {srv['url']}: {why}; local fallback {target}: {why_local}")
    return resp, f"{model} (local fallback)", started


def complete(instance: str, body: dict, fallback: bool = True, note: Callable[[str], None] = lambda s: None) -> dict:
    resp, label, started = open_reply(instance, {**body, "stream": False}, fallback, note)
    try:
        with resp:
            data = json.load(resp)
    except (OSError, ValueError) as e:
        _record(instance, label, None, None, 0.0, started, None, f"reply broken: {e}")
        raise CloudError(502, f"{label}: reply broken: {e}") from e
    usage = data.get("usage") or {}
    finish = ((data.get("choices") or [{}])[0]).get("finish_reason")
    _record(instance, label, usage.get("prompt_tokens"), usage.get("completion_tokens"), 0.0, started, finish, None)
    if not data.get("choices"):
        raise CloudError(502, f"{label}: no reply: {json.dumps(data)[:400]}")
    data["model"] = label
    data["usage"] = {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")}
    return data


def stream(instance: str, body: dict, fallback: bool = True, note: Callable[[str], None] = lambda s: None):
    """The server's SSE lines (bytes) as they arrive; opening errors raise CloudError (nothing sent yet)."""
    resp, label, started = open_reply(instance, body, fallback, note)
    return _relay(instance, label, resp, started)
