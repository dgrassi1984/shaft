"""OpenAI-compatible chat client (llama.cpp, Ollama, LM Studio, ...).

Rules this module enforces:
- `finish_reason == "length"` is *no answer yet*: the call is retried once
  with twice the room; if it is still cut off, `Truncated` is raised and the
  caller must not save anything.
- An empty answer is an error, never a silent "".
- Transient failures (backend restarting: 502/503/504, connection refused)
  are retried with backoff, but never after any text has already streamed.
- Every call — including failed ones — is recorded in logs/llm_usage.jsonl.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable


class LLMError(Exception):
    pass


class Truncated(LLMError):
    pass


class Transient(LLMError):
    """The backend is temporarily unavailable; nothing was generated."""


TRANSIENT_HTTP = {502, 503, 504}
BACKOFF_S = (10, 30)  # waits before the 2nd and 3rd attempt


@dataclass
class Reply:
    content: str
    reasoning: str = ""
    tool_calls: list = field(default_factory=list)   # [{"id", "name", "arguments": str}]
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    attempts: int = 1
    max_tokens: int = 0


class Client:
    def __init__(self, base_url: str, model: str, api_key: str, timeout_s: int, usage_log: Path,
                 broker_token: str = "", presence_penalty: float = 0.0, think_followup: int = 0,
                 reasoning_effort: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.broker_token = broker_token
        self.presence_penalty = presence_penalty
        self.think_followup = think_followup
        self.reasoning_effort = reasoning_effort
        self.local = not broker_token   # SHAFT's own server (template kwargs apply), not the cloud broker
        self.timeout_s = timeout_s
        self.usage_log = usage_log

    @classmethod
    def from_config(cls, cfg) -> "Client":
        """LLM_BASE_URL="broker" means: a cloud model through the supervisor's broker (no key here)."""
        base, token = cfg.LLM_BASE_URL, ""
        if base == "broker":
            broker_url = os.environ.get("SHAFT_BROKER_URL")
            if not broker_url:
                raise LLMError("LLM_BASE_URL is 'broker' but no broker is running (SHAFT_BROKER_URL unset)")
            base, token = broker_url.rstrip("/") + "/llm/v1", os.environ.get("SHAFT_BROKER_TOKEN", "")
        return cls(base, cfg.LLM_MODEL, cfg.LLM_API_KEY, cfg.LLM_TIMEOUT_S,
                   cfg.root / "logs" / "llm_usage.jsonl", broker_token=token,
                   presence_penalty=cfg.LLM_PRESENCE_PENALTY / 10,
                   think_followup=0 if token else cfg.LLM_THINK_FOLLOWUP,   # SHAFT's own server only
                   reasoning_effort="" if token else cfg.LLM_REASONING_EFFORT)

    # -- public -------------------------------------------------------------
    def complete(self, messages: list[dict], max_tokens: int, purpose: str,
                 tools: list[dict] | None = None, think: bool = True) -> Reply:
        return self._with_retry(messages, max_tokens, purpose, stream=False, tools=tools, think=think)

    def stream(self, messages: list[dict], max_tokens: int, purpose: str,
               on_content: Callable[[str], None] | None = None,
               on_reasoning: Callable[[str], None] | None = None,
               on_retry: Callable[[int, int], None] | None = None,
               tools: list[dict] | None = None,
               on_progress: Callable[[int, int], None] | None = None, think: bool = True) -> Reply:
        """on_progress(processed, total) reports prompt reading, where the server supports it."""
        return self._with_retry(messages, max_tokens, purpose, stream=True, tools=tools,
                                on_content=on_content, on_reasoning=on_reasoning,
                                on_retry=on_retry, on_progress=on_progress, think=think)

    # -- internals ----------------------------------------------------------
    def _with_retry(self, messages, max_tokens, purpose, stream, on_retry=None, **callbacks) -> Reply:
        reply = self._resilient(messages, max_tokens, purpose, stream, **callbacks)
        if reply.finish_reason == "length":
            if on_retry:
                on_retry(max_tokens, max_tokens * 2)
            reply = self._resilient(messages, max_tokens * 2, purpose, stream, **callbacks)
            reply.attempts = 2
            if reply.finish_reason == "length":
                raise Truncated(
                    f"{purpose}: reply cut off at max_tokens={max_tokens * 2} twice "
                    f"(completion_tokens={reply.usage.get('completion_tokens')}); nothing saved")
        if not reply.content.strip() and not reply.tool_calls:
            raise LLMError(f"{purpose}: model returned an empty answer "
                           f"(finish_reason={reply.finish_reason!r}, "
                           f"reasoning_chars={len(reply.reasoning)})")
        return reply

    def _resilient(self, messages, max_tokens, purpose, stream, **callbacks) -> Reply:
        for wait in (*BACKOFF_S, None):
            try:
                return self._call(messages, max_tokens, purpose, stream, **callbacks)
            except Transient:
                if wait is None:
                    raise
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _call(self, messages, max_tokens, purpose, stream, tools=None,
              on_content=None, on_reasoning=None, on_progress=None, think=True) -> Reply:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if self.presence_penalty:   # Qwen's advice for quantized models: curbs endless repetition
            body["presence_penalty"] = self.presence_penalty
        if self.local:
            # Earlier steps' thinking is shown only within the current reply, not for older turns: it keeps
            # the plan across tool rounds without carrying every past reply's reasoning in the context.
            kwargs = {"preserve_thinking": False}
            if not think:   # Qwen-style templates: answer directly, no hidden reasoning
                kwargs["enable_thinking"] = False
            elif self.reasoning_effort:
                # Bonsai's template defaults to xhigh, which thinks until the server's budget cuts it off on
                # anything non-trivial (4,096 tokens, ~4 min); medium thinks as much as the request needs.
                kwargs["reasoning_effort"] = self.reasoning_effort
            if think and self.think_followup and messages and messages[-1].get("role") == "tool":
                # After a tool result I mostly need to read it, not deliberate from scratch: SHAFT's server
                # forces </think> after this many reasoning tokens (its own budget stays the ceiling).
                kwargs["thinking_budget"] = self.think_followup
            body["chat_template_kwargs"] = kwargs
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            if on_progress:
                body["return_progress"] = True   # llama.cpp: prompt-processing progress chunks
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.broker_token:
            headers["X-Shaft-Token"] = self.broker_token
        url = f"{self.base_url}/chat/completions"
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        started = time.monotonic()
        reply = Reply(content="", max_tokens=max_tokens)
        error = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                if stream:
                    self._read_stream(resp, reply, on_content, on_reasoning, on_progress)
                else:
                    data = json.load(resp)
                    choice = data["choices"][0]
                    message = choice.get("message") or {}
                    reply.content = message.get("content") or ""
                    # llama.cpp says reasoning_content, mlx_lm says reasoning
                    reply.reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                    _move_leaked_thinking(reply)
                    reply.finish_reason = choice.get("finish_reason") or ""
                    reply.usage = data.get("usage") or {}
                    reply.tool_calls = [
                        {"id": c.get("id") or f"call_{i}", "name": c["function"]["name"],
                         "arguments": c["function"].get("arguments") or "{}"}
                        for i, c in enumerate(message.get("tool_calls") or [])]
            return reply
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            error = f"HTTP {e.code} from {url}: {detail}"
            transient = e.code in TRANSIENT_HTTP and not reply.content
            raise (Transient if transient else LLMError)(error) from e
        except urllib.error.URLError as e:
            error = f"cannot reach LLM at {url} (model={self.model}): {e.reason}"
            refused = isinstance(e.reason, ConnectionRefusedError)
            raise (Transient if refused else LLMError)(error) from e
        except (TimeoutError, OSError) as e:
            error = f"LLM call to {url} failed after {time.monotonic() - started:.0f}s: {e}"
            raise LLMError(error) from e
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            error = f"malformed response from {url}: {e!r}"
            raise LLMError(error) from e
        finally:
            self._log(purpose, reply, time.monotonic() - started, error)

    @staticmethod
    def _read_stream(resp, reply: Reply, on_content, on_reasoning, on_progress=None) -> None:
        content, reasoning = [], []
        calls: dict[int, dict] = {}
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if line.startswith(": keepalive") and on_progress:   # mlx_lm: ": keepalive 2048/8850"
                done, _, total = line.split()[-1].partition("/")
                if done.isdigit() and total.isdigit():
                    on_progress(int(done), int(total))
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if chunk.get("usage"):
                reply.usage = chunk["usage"]
            progress = chunk.get("prompt_progress")
            if progress and on_progress:
                on_progress(int(progress.get("processed", 0)), int(progress.get("total", 0)))
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                thought = delta.get("reasoning_content") or delta.get("reasoning")   # llama.cpp / mlx_lm
                if thought:
                    reasoning.append(thought)
                    if on_reasoning:
                        on_reasoning(thought)
                if delta.get("content"):
                    content.append(delta["content"])
                    if on_content:
                        on_content(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""
                if choice.get("finish_reason"):
                    reply.finish_reason = choice["finish_reason"]
        reply.content = "".join(content)
        reply.reasoning = "".join(reasoning)
        _move_leaked_thinking(reply)
        reply.tool_calls = [{**c, "id": c["id"] or f"call_{i}", "arguments": c["arguments"] or "{}"}
                            for i, c in sorted(calls.items())]

    def _log(self, purpose: str, reply: Reply, duration_s: float, error: str | None) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "purpose": purpose,
            "model": self.model,
            "max_tokens": reply.max_tokens,
            "finish_reason": reply.finish_reason,
            "prompt_tokens": reply.usage.get("prompt_tokens"),
            "completion_tokens": reply.usage.get("completion_tokens"),
            "content_chars": len(reply.content),
            "tool_calls": [c["name"] for c in reply.tool_calls],
            "reasoning_chars": len(reply.reasoning),
            "duration_s": round(duration_s, 2),
            "error": error,
        }
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_log.open("a") as f:
            f.write(json.dumps(record) + "\n")


def _move_leaked_thinking(reply: Reply) -> None:
    """When the server cuts my thinking at its budget, I sometimes go on thinking in the answer and close
    the block again myself: everything up to that `</think>` is reasoning, not what I said."""
    if "</think>" in reply.content:
        thought, _, said = reply.content.rpartition("</think>")
        reply.reasoning = (reply.reasoning + "\n" + thought).strip()
        reply.content = said.lstrip()
