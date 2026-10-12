"""Cloud models for instances, through the broker — the instance never holds the key.

An instance whose config.env says LLM_BASE_URL="broker" sends OpenAI-style chat requests
to its run's broker at /llm/v1/chat/completions. This module translates them to the Claude
Messages API (raw HTTP, like the judge), and the reply back. Rules:

- Only models in SHAFT_CLOUD_MODELS (operator's choice; default claude-haiku-5-5) are allowed.
- Spending per instance per day is capped (SHAFT_CLOUD_DAILY_USD, default 2) and logged in
  .supervisor/cloud_usage.jsonl.
- Assistant turns are resent exactly as the model produced them (thinking blocks included):
  each reply's original content blocks are remembered and substituted when the instance
  sends that turn back, because altered history invalidates replayed thinking.
- Upstream is non-streaming; a streaming request gets the whole reply as one SSE burst.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime

from .core import SUP_DIR, knob

API = "https://api.anthropic.com/v1/messages"
CLOUD_MODELS, _ = knob("SHAFT_CLOUD_MODELS", "claude-haiku-5-5", lambda s: [m.strip() for m in s.split(",") if m.strip()])
DAILY_USD, _ = knob("SHAFT_CLOUD_DAILY_USD", "2", float)
# $ per million tokens (input, output) — prompts ≤ 100K tokens; from the Claude API price table.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}
USAGE = SUP_DIR / "cloud_usage.jsonl"


class CloudError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _turn_key(content: str, tool_ids: list[str]) -> str:
    return hashlib.sha256(json.dumps([content, tool_ids]).encode()).hexdigest()


def spent_today(instance: str) -> float:
    if not USAGE.exists():
        return 0.0
    today = date.today().isoformat()
    total = 0.0
    for line in USAGE.read_text().splitlines():
        r = json.loads(line)
        if r["instance"] == instance and r["ts"].startswith(today):
            total += r.get("usd") or 0.0
    return total


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
            push("user", [{"type": "text", "text": content}])
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
    out = {"model": body["model"], "max_tokens": int(body.get("max_tokens") or 16000), "messages": messages}
    if system:
        out["system"] = "\n\n".join(system)
    tools = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
              "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
             for t in body.get("tools") or [] if t.get("type") == "function"]
    if tools:
        out["tools"] = tools
    return out


def from_anthropic(data: dict) -> dict:
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    calls = [{"id": b["id"], "type": "function",
              "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
             for b in data.get("content", []) if b.get("type") == "tool_use"]
    finish = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls",
              "max_tokens": "length"}.get(data.get("stop_reason"), "stop")
    usage = data.get("usage") or {}
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = calls
    return {"id": data.get("id"), "object": "chat.completion", "model": data.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens")}}


def complete(instance: str, body: dict, key: str, remembered: dict[str, list]) -> dict:
    model = body.get("model")
    if model not in CLOUD_MODELS:
        raise CloudError(403, f"model {model!r} is not allowed for instances (SHAFT_CLOUD_MODELS={CLOUD_MODELS})")
    if not key:
        raise CloudError(503, "no ANTHROPIC_API_KEY in the supervisor's .env")
    spent = spent_today(instance)
    if spent >= DAILY_USD:
        raise CloudError(429, f"{instance} spent ${spent:.2f} on cloud models today (cap ${DAILY_USD:.2f})")
    req_body = to_anthropic(body, remembered)
    req = urllib.request.Request(API, data=json.dumps(req_body).encode(), headers={
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    started, data, error = time.monotonic(), None, None
    for wait in (0, 10, 30):
        time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            error = f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}"
            if e.code not in (429, 500, 502, 503, 504, 529):
                break
        except (urllib.error.URLError, OSError) as e:
            error = f"network: {e}"
    usage = (data or {}).get("usage") or {}
    pin, pout = PRICES.get(model, (0, 0))
    usd = ((usage.get("input_tokens") or 0) * pin + (usage.get("output_tokens") or 0) * pout) / 1e6
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    with USAGE.open("a") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "instance": instance, "model": model,
                            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                            "usd": round(usd, 6), "seconds": round(time.monotonic() - started, 1),
                            "stop_reason": (data or {}).get("stop_reason"), "error": None if data else error}) + "\n")
    if data is None:
        raise CloudError(502, f"cloud model call failed: {error}")
    if data.get("stop_reason") == "refusal":
        raise CloudError(502, f"cloud model refused ({(data.get('stop_details') or {}).get('category')})")
    out = from_anthropic(data)
    msg = out["choices"][0]["message"]
    remembered[_turn_key(msg["content"], [c["id"] for c in msg.get("tool_calls", [])])] = data["content"]
    return out


def as_sse(reply: dict) -> bytes:
    """A complete reply as an OpenAI-style stream, for clients that asked for one."""
    msg, choice = reply["choices"][0]["message"], reply["choices"][0]
    chunks = []
    if msg.get("content"):
        chunks.append({"choices": [{"index": 0, "delta": {"content": msg["content"]}, "finish_reason": None}]})
    for i, c in enumerate(msg.get("tool_calls", [])):
        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, **c}]}, "finish_reason": None}]})
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]})
    chunks.append({"choices": [], "usage": reply["usage"]})
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
