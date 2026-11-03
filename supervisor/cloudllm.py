"""Cloud models for instances, through the broker — the instance never holds a key.

An instance whose config.env says LLM_BASE_URL="broker" sends OpenAI-style chat requests
to its run's broker at /llm/v1/chat/completions. Claude models are translated to the Claude
Messages API (raw HTTP, like the judge), and the reply back. Models named "openrouter/<id>"
(e.g. openrouter/xiaomi/mimo-v2.6-pro) go to OpenRouter, which speaks the OpenAI format. Models named
"<server>/<id>" for a server in SHAFT_SELF_HOSTED (gpu/bonsai2) go to that server: supervisor/selfhosted.py.
Rules:

- Only models in SHAFT_CLOUD_MODELS (operator's choice) are allowed.
- OpenRouter requests go only to the providers the operator chose — NovitaAI first, DeepInfra if Novita is down
  (2026-10-20), never any other — and only if they neither collect nor retain data (SHAFT_OPENROUTER_PROVIDER,
  default {"only": ["novita", "deepinfra"], "order": ["novita", "deepinfra"], "allow_fallbacks": false,
  "data_collection": "deny", "zdr": true, "require_parameters": true}): an
  instance's prompt carries its whole memory, including what it knows about the operator. If the provider does
  not qualify, the call fails and says so — no silent fallback.
- Spending per instance per day is capped (SHAFT_CLOUD_DAILY_USD, default 8: kant on Opus 5.5, 4 wakes a day) and logged in
  .supervisor/cloud_usage.jsonl.
- Assistant turns are resent exactly as the model produced them (thinking blocks included):
  each reply's original content blocks are remembered and substituted when the instance
  sends that turn back, because altered history invalidates replayed thinking.
- Claude models think adaptively; LLM_REASONING_EFFORT is their output_config.effort (claude_thinking).
- Claude requests are prompt-cached for an hour (CACHE): each turn of a loop re-reads the turns before it at the
  cache price. The usage log keeps cache reads and writes apart from input_tokens, and the cap prices them (usd()).
- Claude models: upstream is non-streaming; a streaming request gets the whole reply as one SSE burst.
  OpenRouter models stream end to end (stream_openrouter): the instance sees its reply as it is written.
- Decision models (TypeSafe's Jev, through OpenRouter): not chat models — typed questions
  (choice / score / noul) about a state, answered with probabilities, at OpenRouter's /api/alpha/decisions.
  The broker's `decide` tool calls them (decide()). Only SHAFT_DECISION_MODELS; same daily cap and usage log as
  the chat models; the same data rule (SHAFT_DECISION_PROVIDER: no data collection, zero retention, no fallback
  — Jev's provider is TypeSafe, so no provider pin): a provider that does not qualify fails the call.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime

from . import ledger
from .core import NETWORK_ERRORS, SUP_DIR, knob, dollars, model_names, cloud_models, json_object

API = "https://api.anthropic.com/v1/messages"
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"
CLOUD_MODELS, _ = knob(
    "SHAFT_CLOUD_MODELS", "claude-haiku-5-5,claude-opus-5-5,openrouter/xiaomi/mimo-v2.6-pro", cloud_models, "the cloud "
    "models an instance may run on (through the broker)", group="Models and spend", services=("runs", "api", "web"))
# The operator's choice of provider (2026-10-13): NovitaAI; DeepInfra only if Novita is down (2026-10-20); no other
# provider ("only" + allow_fallbacks false); plus the data policy.
OPENROUTER_PROVIDER, _ = knob(
    "SHAFT_OPENROUTER_PROVIDER",
    '{"only": ["novita", "deepinfra"], "order": ["novita", "deepinfra"], "allow_fallbacks": false, '
    '"data_collection": "deny", "zdr": true, "require_parameters": true}',
    json_object, "OpenRouter's provider routing for every openrouter/ call (JSON): which providers, and the data policy",
    group="Models and spend", services=("runs", "api"))
DAILY_USD, _ = knob(
    "SHAFT_CLOUD_DAILY_USD", "8", dollars, "dollars per day an instance may spend on its cloud model (shaft limits can "
    "set one instance's own cap)", group="Models and spend", services=("runs", "api", "web"))


def daily_cap(instance: str) -> float:
    """The instance's daily cloud cap: the operator's own for it (`shaft limits <name> cloud-cap <usd>`, kept in the
    supervisor's folder — the instance cannot raise it), else SHAFT_CLOUD_DAILY_USD. Trial and battery runs, billed
    under other names, keep the default."""
    from .core import _tool_settings
    own = _tool_settings(instance).get("cloud_daily_usd") if instance.replace("-", "").isalnum() else None
    return float(own) if isinstance(own, (int, float)) and not isinstance(own, bool) and own > 0 else DAILY_USD
DECISIONS_API = "https://openrouter.ai/api/alpha/decisions"
DECISION_MODELS, _ = knob(
    "SHAFT_DECISION_MODELS", "typesafe/jev-1.13,~typesafe/jev-latest", model_names, "the models the broker's decide "
    "tool may call (OpenRouter's decisions API); the first is its default", group="Models and spend",
    services=("runs", "api"))
DECISION_PROVIDER, _ = knob(
    "SHAFT_DECISION_PROVIDER", '{"data_collection": "deny", "zdr": true, "allow_fallbacks": false}', json_object,
    "the decide tool's provider routing and data policy (JSON)", group="Models and spend", services=("runs", "api"))
# $ per million tokens (input, output, cache read) — prompts ≤ 100K tokens; from the Claude API price table.
# Cache writes cost 1.25× input (5-minute entries) or 2× (1-hour). One table for every Claude call the supervisor
# pays for: instances, the coder, the judges (usd() below).
PRICES = {"claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
          "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-fable-5-1": (10.0, 50.0, 0.25)}
# Haiku 5.5 has a second rate card: a prompt over 100K tokens (cache reads and writes included) is billed at 5× for
# the whole call. Priced at the short card, a thelos cycle's longest calls were undercounted 5× by the cap.
LONG_PROMPT = 100_000
PRICES_LONG = {"claude-haiku-5-5": (0.50, 2.50, 0.05)}
# Prompt caching (1-hour entries). Each request marks its end; the next turn of the same loop
# reads everything before its new tail at the cache price. An hour also carries a loop over a pause — a conversation
# that takes the model, a long tool run — which a 5-minute entry would not survive.
CACHE = {"type": "ephemeral", "ttl": "1h"}
USAGE = SUP_DIR / "cloud_usage.jsonl"


class CloudError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _turn_key(content: str, tool_ids: list[str]) -> str:
    return hashlib.sha256(json.dumps([content, tool_ids]).encode()).hexdigest()


def billed(usage: dict) -> dict:
    """A Claude reply's usage as the counts it is billed by. Its input_tokens is only the uncached remainder:
    cache reads and writes are counted (and priced) apart."""
    writes = usage.get("cache_creation") or {}
    w5m, w1h = writes.get("ephemeral_5m_input_tokens"), writes.get("ephemeral_1h_input_tokens")
    if w5m is None and w1h is None:   # no per-TTL split in the reply: priced as the TTL we ask for (the dearer one)
        w5m, w1h = 0, usage.get("cache_creation_input_tokens") or 0
    return {"input_tokens": usage.get("input_tokens") or 0, "output_tokens": usage.get("output_tokens") or 0,
            "cache_read_tokens": usage.get("cache_read_input_tokens") or 0,
            "cache_write_5m_tokens": w5m or 0, "cache_write_1h_tokens": w1h or 0}


def usd(model: str, r: dict) -> float | None:
    """What a Claude call cost, from a record with billed()'s counts (older records have no cache counts: none
    were cached); None for a model without a price."""
    p = PRICES.get(model)
    if not p:
        return None
    pin, pout, pread = PRICES_LONG.get(model, p) if prompt_size(r) > LONG_PROMPT else p
    n = lambda k: r.get(k) or 0
    return (n("input_tokens") * pin + n("output_tokens") * pout + n("cache_read_tokens") * pread
            + n("cache_write_5m_tokens") * pin * 1.25 + n("cache_write_1h_tokens") * pin * 2) / 1e6


def prompt_size(r: dict) -> int:
    """The whole prompt a call sent: the uncached remainder plus what was read from or written to the cache."""
    return sum(r.get(k) or 0 for k in ("input_tokens", "cache_read_tokens", "cache_write_5m_tokens",
                                       "cache_write_1h_tokens"))


def spent_today(instance: str, until: datetime | None = None) -> float:
    if not USAGE.exists():
        return 0.0
    today = (until.date() if until else date.today()).isoformat()
    total = 0.0
    for ts, usd in ledger.spend(USAGE, instance):     # what was appended since the last call, not the whole file
        if ts.startswith(today) and (until is None or datetime.fromisoformat(ts) < until):
            total += usd or 0.0
    return total


def _user_blocks(content) -> list[dict]:
    """A user message's content as Claude blocks: text, or OpenAI-style parts — text and images (data: URLs, already
    checked and prepared by vision.prepare in the broker's proxy)."""
    if not isinstance(content, list):
        return [{"type": "text", "text": content}]
    from .vision import split_data_url
    blocks = []
    for part in content:
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            media, data = split_data_url((part.get("image_url") or {}).get("url", ""))
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                       "data": base64.b64encode(data).decode()}})
    return blocks or [{"type": "text", "text": "(empty)"}]


def to_anthropic(body: dict, remembered: dict[str, list]) -> dict:
    system, messages = [], []

    def push(role: str, blocks: list) -> None:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": list(blocks)})

    for m in body.get("messages", []):
        role, content = m.get("role"), m.get("content") or ""
        if role == "system":
            system.append(content)
        elif role == "user":
            push("user", _user_blocks(content))
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": content or "(empty)"}])
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            original = remembered.get(_turn_key(content, [c["id"] for c in calls]))
            if original is not None:
                push("assistant", original)
                continue
            blocks = [{"type": "text", "text": content}] if content else []
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
            push("assistant", blocks or [{"type": "text", "text": "(no content)"}])
    # Tool results must lead their user message; text added after them (e.g. a wrap-up notice) follows.
    for m in messages:
        if m["role"] == "user":
            m["content"].sort(key=lambda b: b["type"] != "tool_result")
    out = {"model": body["model"], "max_tokens": int(body.get("max_tokens") or 16000), "messages": messages,
           "cache_control": CACHE}   # automatic: the breakpoint moves to the last block as the loop grows
    if system:
        out["system"] = "\n\n".join(system)
    tools = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
              "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
             for t in body.get("tools") or [] if t.get("type") == "function"]
    if tools:
        out["tools"] = tools
    out.update(claude_thinking(body["model"], body.get("chat_template_kwargs") or {}))
    return out


# How each Claude model stops thinking when asked to (the client's retry after a reply cut off still thinking):
# Haiku 5.5 turns it off; Sonnet 5.5 refuses `disabled` and thinks only between tool calls instead; Opus 5.5 and
# Fable 5.1 refuse both, so they think as little as they can (effort low).
NO_THINKING = {"claude-haiku-5-5": {"thinking": {"type": "disabled"}},
               "claude-sonnet-5-5": {"thinking": {"type": "between_tools"}}}


def claude_thinking(model: str, kwargs: dict) -> dict:
    """An instance's thinking settings, sent as template kwargs (its own server's form), in the Claude API's form.
    Claude 5.5 models think adaptively without being asked, at their default effort (medium for Haiku and Opus,
    high for Sonnet): until 2026-10-30 LLM_REASONING_EFFORT never reached them, as it never reached mimo before
    openrouter_reasoning. It is output_config.effort; no thinking (think=False) is NO_THINKING."""
    if kwargs.get("enable_thinking") is False:
        return NO_THINKING.get(model, {"output_config": {"effort": "low"}})
    if kwargs.get("reasoning_effort"):
        return {"output_config": {"effort": kwargs["reasoning_effort"]}}
    return {}


def from_anthropic(data: dict) -> dict:
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    calls = [{"id": b["id"], "type": "function",
              "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
             for b in data.get("content", []) if b.get("type") == "tool_use"]
    finish = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls",
              "max_tokens": "length"}.get(data.get("stop_reason"), "stop")
    usage = billed(data.get("usage") or {})
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = calls
    return {"id": data.get("id"), "object": "chat.completion", "model": data.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_size(usage), "completion_tokens": usage["output_tokens"],
                      "prompt_tokens_details": {"cached_tokens": usage["cache_read_tokens"]}}}


def complete(instance: str, body: dict, secrets: dict, remembered: dict[str, list]) -> dict:
    model = body.get("model")
    from . import selfhosted
    if selfhosted.serves(model):   # a self-hosted server (gpu/bonsai2): free, no cap, local fallback
        return selfhosted.complete(instance, body)
    if model not in CLOUD_MODELS:
        raise CloudError(403, f"model {model!r} is not allowed for instances (SHAFT_CLOUD_MODELS={CLOUD_MODELS})")
    if not model.startswith("openrouter/") and model not in PRICES:   # unpriced = uncapped: refuse, never bill $0
        raise CloudError(403, f"model {model!r} has no price in cloudllm.PRICES ({sorted(PRICES)}), so the daily "
                              f"cap could not count it")
    spent = spent_today(instance)
    if spent >= (cap := daily_cap(instance)):
        raise CloudError(429, f"{instance} spent ${spent:.2f} on cloud models today (cap ${cap:.2f})")
    if model.startswith("openrouter/"):
        return _openrouter(instance, body, secrets.get("OPENROUTER_API_KEY", ""))
    key = secrets.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise CloudError(503, "no ANTHROPIC_API_KEY in the supervisor's .env")
    req_body = to_anthropic(body, remembered)
    req = urllib.request.Request(API, data=json.dumps(req_body).encode(), headers={
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    started = time.monotonic()
    data, error = _send(req)
    counts = billed((data or {}).get("usage") or {})
    _record(instance, model, counts["input_tokens"], counts["output_tokens"], usd(model, counts), started,
            (data or {}).get("stop_reason"), None if data else error,
            cache={k: v for k, v in counts.items() if k.startswith("cache_")})
    if data is None:
        raise CloudError(502, f"cloud model call failed: {error}")
    if data.get("stop_reason") == "refusal":
        raise CloudError(502, f"cloud model refused ({(data.get('stop_details') or {}).get('category')})")
    out = from_anthropic(data)
    msg = out["choices"][0]["message"]
    remembered[_turn_key(msg["content"], [c["id"] for c in msg.get("tool_calls", [])])] = data["content"]
    return out


def _record(instance: str, model: str, tokens_in, tokens_out, usd: float | None, started: float, stop, error,
            cache: dict | None = None) -> None:
    """One cloud_usage row. `usd` None is unpriced: a self-hosted model (no price, never $0) or an OpenRouter reply
    without a cost. A self-hosted call spends no dollars, so it adds nothing to the run's charges (Broker._run_usd):
    the run's priced calls still sum. An OpenRouter call without a cost spent money nobody knows, so the run's
    spend is unknown (None), never counted as none."""
    from . import selfhosted
    cost = round(usd, 6) if usd is not None else None
    if cost is not None or not selfhosted.serves(model.split(" (", 1)[0]):
        ledger.record_charge(cost)
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    with USAGE.open("a") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "instance": instance, "model": model,
                            "input_tokens": tokens_in, "output_tokens": tokens_out, **(cache or {}),
                            "usd": cost, "unpriced": cost is None, "seconds": round(time.monotonic() - started, 1),
                            "stop_reason": stop, "error": error}) + "\n")


def _send(req: urllib.request.Request) -> tuple[dict | None, str | None]:
    """POST with retries on overload and network errors; (data, None) or (None, error)."""
    error, throttled = None, 0
    for wait in (0, 10, 30):
        time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.load(resp), None
        except urllib.error.HTTPError as e:
            throttled += e.code == 429
            error = f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}"
            if e.code not in (429, 500, 502, 503, 504, 529):
                break
        except NETWORK_ERRORS as e:
            error = f"network: {e}"
    if throttled:   # the usage log must count retry refusals too
        error += f" [HTTP 429 responses: {throttled}]"
    return None, error


def _openrouter_request(body: dict, key: str, stream: bool) -> urllib.request.Request:
    req_body = {k: v for k, v in body.items() if k not in ("stream", "stream_options", "chat_template_kwargs")}
    if "reasoning" not in req_body:   # the judge sets its own; an instance's settings come as template kwargs
        req_body.update(openrouter_reasoning(body.get("chat_template_kwargs") or {}))
    req_body["model"] = body["model"].removeprefix("openrouter/")
    req_body["provider"] = OPENROUTER_PROVIDER
    if stream:
        req_body["stream"] = True
    return urllib.request.Request(OPENROUTER_API, data=json.dumps(req_body).encode(), headers={
        "content-type": "application/json", "authorization": f"Bearer {key}",
        "x-title": "SHAFT", "http-referer": "https://github.com/dgrassi1984/shaft"})


def openrouter_reasoning(kwargs: dict) -> dict:
    """An instance's thinking settings, sent as template kwargs (its own server's form), in OpenRouter's form: no
    thinking (think=False) is `reasoning.enabled` false, LLM_REASONING_EFFORT is `reasoning.effort`; dropped with the
    kwargs, thelos's and wren's "medium" never reached mimo. Novita accepts effort for mimo but does not enforce it (nor
    reasoning.max_tokens: 16,000 reasoning tokens of 16,000 at every level, probed 2026-10-30); only enabled=false
    stops its thinking, which is what the client's retry after a cut-off still thinking asks for (llm.py).
    thinking_budget (SHAFT's own server) and preserve_thinking have no equivalent."""
    if kwargs.get("enable_thinking") is False:
        return {"reasoning": {"enabled": False}}
    if kwargs.get("reasoning_effort"):
        return {"reasoning": {"effort": kwargs["reasoning_effort"]}}
    return {}


def _policy_hint(error: str, knob_name: str = "SHAFT_OPENROUTER_PROVIDER", policy: dict | None = None) -> str:
    if "provider" in error.lower() or "endpoint" in error.lower():
        return (f" — the data policy allows only providers that neither collect nor retain data "
                f"({knob_name}={json.dumps(OPENROUTER_PROVIDER if policy is None else policy)})")
    return ""


def decide(instance: str, model: str, state, questions: dict, secrets: dict) -> dict:
    """One call to a decision model: OpenRouter's response ({model, answers, usage, ...}), cost recorded."""
    if model not in DECISION_MODELS:
        raise CloudError(403, f"decision model {model!r} is not allowed for instances "
                              f"(SHAFT_DECISION_MODELS={DECISION_MODELS})")
    spent = spent_today(instance)
    if spent >= (cap := daily_cap(instance)):
        raise CloudError(429, f"{instance} spent ${spent:.2f} on cloud models today (cap ${cap:.2f})")
    key = secrets.get("OPENROUTER_API_KEY", "")
    if not key:
        raise CloudError(503, "no OPENROUTER_API_KEY in the supervisor's .env")
    body = {"model": model, "state": state, "questions": questions, "provider": DECISION_PROVIDER}
    req = urllib.request.Request(DECISIONS_API, data=json.dumps(body).encode(), headers={
        "content-type": "application/json", "authorization": f"Bearer {key}",
        "x-title": "SHAFT", "http-referer": "https://github.com/dgrassi1984/shaft"})
    started = time.monotonic()
    data, error = _send(req)
    usage = (data or {}).get("usage") or {}
    _record(instance, model, usage.get("input_tokens"), usage.get("output_tokens"), float(usage.get("cost") or 0),
            started, "decided" if data else None, None if data else error)
    if data is None:
        raise CloudError(502, f"decision call to {model} failed: {error}"
                              f"{_policy_hint(error, 'SHAFT_DECISION_PROVIDER', DECISION_PROVIDER)}")
    return data


def openrouter_stream(instance: str, body: dict, secrets: dict):
    """A streaming OpenRouter call, passed through to the instance as it arrives: yields SSE lines (bytes).
    Opening errors raise CloudError (nothing sent yet); usage and cost come from the final chunk."""
    model = body.get("model")
    if model not in CLOUD_MODELS:
        raise CloudError(403, f"model {model!r} is not allowed for instances (SHAFT_CLOUD_MODELS={CLOUD_MODELS})")
    spent = spent_today(instance)
    if spent >= (cap := daily_cap(instance)):
        raise CloudError(429, f"{instance} spent ${spent:.2f} on cloud models today (cap ${cap:.2f})")
    key = secrets.get("OPENROUTER_API_KEY", "")
    if not key:
        raise CloudError(503, "no OPENROUTER_API_KEY in the supervisor's .env")
    started, resp, throttled = time.monotonic(), None, 0
    for wait in (0, 10, 30):   # retry only before the first byte: a started stream cannot be replayed
        time.sleep(wait)
        try:
            resp = urllib.request.urlopen(_openrouter_request(body, key, stream=True), timeout=600)
            break
        except urllib.error.HTTPError as e:
            throttled += e.code == 429
            error = f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}"
            if e.code not in (429, 500, 502, 503, 504, 529):
                break
        except NETWORK_ERRORS as e:
            error = f"network: {e}"
    if resp is None:
        if throttled:
            error += f" [HTTP 429 responses: {throttled}]"
        _record(instance, model, None, None, 0.0, started, None, error)
        raise CloudError(502, f"OpenRouter call for {model} failed: {error}{_policy_hint(error)}")
    return _relay(instance, model, resp, started)


def _relay(instance: str, model: str, resp, started: float):
    usage, finish, error = {}, None, None
    try:
        with resp:
            for raw in resp:
                line = raw.rstrip(b"\r\n")
                if line.startswith(b"data:") and line[5:].strip() not in (b"", b"[DONE]"):
                    try:
                        chunk = json.loads(line[5:])
                    except ValueError:
                        chunk = {}
                    usage = chunk.get("usage") or usage
                    if chunk.get("error"):
                        error = json.dumps(chunk["error"])[:400]
                    for c in chunk.get("choices") or []:
                        finish = c.get("finish_reason") or finish
                yield line + b"\n"
    except NETWORK_ERRORS as e:
        error = f"stream broken: {e!r}"
        raise
    finally:
        from . import selfhosted
        hosted = selfhosted.serves(model.split(" (", 1)[0])
        cost = None if hosted else usage.get("cost")
        details = usage.get("prompt_tokens_details") or {}
        _record(instance, model, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                float(cost) if cost is not None else None, started, finish,
                error or _unsaid({} if hosted else usage, finish),
                cache={"cache_read_tokens": details.get("cached_tokens"),
                       "cache_write_tokens": details.get("cache_write_tokens", details.get("cache_write"))})


def _unsaid(usage: dict, finish) -> str | None:
    """What went wrong with an OpenRouter reply that carried no error object: a provider error as finish_reason, or
    tokens without a cost (unpriced). Both were silent: 12 of 42,000 mimo calls to 2026-10-30, mostly a provider
    error mid-stream, recorded as error None."""
    said = ["the provider ended the reply with finish_reason=error"] if finish == "error" else []
    if usage.get("cost") is None and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
        said.append("no cost in its usage: unpriced")
    return "; ".join(said) or None


def _openrouter(instance: str, body: dict, key: str) -> dict:
    """An OpenAI-format request to OpenRouter, routed by the data policy; cost from its own usage.cost."""
    if not key:
        raise CloudError(503, "no OPENROUTER_API_KEY in the supervisor's .env")
    model = body["model"]
    started = time.monotonic()
    data, error = _send(_openrouter_request(body, key, stream=False))
    if data is not None and not data.get("choices"):   # OpenRouter can answer 200 with an error object
        error, data = f"no reply: {json.dumps(data.get('error') or data)[:400]}", None
    usage = (data or {}).get("usage") or {}
    finish = ((data or {}).get("choices") or [{}])[0].get("finish_reason")
    details = usage.get("prompt_tokens_details") or {}
    cost = float(usage["cost"]) if usage.get("cost") is not None else None
    _record(instance, model, usage.get("prompt_tokens"), usage.get("completion_tokens"), cost,
            started, finish, error or _unsaid(usage, finish),
            cache={"cache_read_tokens": details.get("cached_tokens"),
                   "cache_write_tokens": details.get("cache_write_tokens", details.get("cache_write"))})
    if data is None:
        raise CloudError(502, f"OpenRouter call for {model} failed: {error}{_policy_hint(error or '')}")
    message = data["choices"][0].get("message") or {}
    for c in message.get("tool_calls") or []:
        fn = c.get("function") or {}
        if not isinstance(fn.get("arguments"), str):
            fn["arguments"] = json.dumps(fn.get("arguments") or {}, ensure_ascii=False)
    data["usage"] = {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                     "cost": usage.get("cost")}
    return data


def as_sse(reply: dict) -> bytes:
    """A complete reply as an OpenAI-style stream, for clients that asked for one."""
    msg, choice = reply["choices"][0]["message"], reply["choices"][0]
    chunks = []
    if msg.get("reasoning"):   # OpenRouter's reasoning text, so a thinking-aware client shows it
        chunks.append({"choices": [{"index": 0, "delta": {"reasoning": msg["reasoning"]}, "finish_reason": None}]})
    if msg.get("content"):
        chunks.append({"choices": [{"index": 0, "delta": {"content": msg["content"]}, "finish_reason": None}]})
    for i, c in enumerate(msg.get("tool_calls", [])):
        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, **c}]}, "finish_reason": None}]})
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]})
    chunks.append({"choices": [], "usage": reply["usage"]})
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
