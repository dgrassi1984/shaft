"""OpenAI-compatible chat client (llama.cpp, Ollama, LM Studio, ...).

Rules this module enforces:
- `finish_reason == "length"` is *no answer yet*: the call is retried once
  with twice the room; if it is still cut off, `Truncated` is raised and the
  caller must not save anything.
- An empty answer is an error, never a silent "".
- Every call — including failed ones — is recorded in logs/llm_usage.jsonl.
"""
from __future__ import annotations

import json
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


@dataclass
class Reply:
    content: str
    reasoning: str = ""
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    attempts: int = 1
    max_tokens: int = 0


class Client:
    def __init__(self, base_url: str, model: str, api_key: str, timeout_s: int, usage_log: Path):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.usage_log = usage_log

    @classmethod
    def from_config(cls, cfg) -> "Client":
        return cls(cfg.LLM_BASE_URL, cfg.LLM_MODEL, cfg.LLM_API_KEY, cfg.LLM_TIMEOUT_S,
                   cfg.root / "logs" / "llm_usage.jsonl")

    # -- public -------------------------------------------------------------
    def complete(self, messages: list[dict], max_tokens: int, purpose: str) -> Reply:
        return self._with_retry(messages, max_tokens, purpose, stream=False)

    def stream(self, messages: list[dict], max_tokens: int, purpose: str,
               on_content: Callable[[str], None] | None = None,
               on_reasoning: Callable[[str], None] | None = None,
               on_retry: Callable[[int, int], None] | None = None) -> Reply:
        return self._with_retry(messages, max_tokens, purpose, stream=True,
                                on_content=on_content, on_reasoning=on_reasoning,
                                on_retry=on_retry)

    # -- internals ----------------------------------------------------------
    def _with_retry(self, messages, max_tokens, purpose, stream, on_retry=None, **callbacks) -> Reply:
        reply = self._call(messages, max_tokens, purpose, stream, **callbacks)
        if reply.finish_reason == "length":
            if on_retry:
                on_retry(max_tokens, max_tokens * 2)
            reply = self._call(messages, max_tokens * 2, purpose, stream, **callbacks)
            reply.attempts = 2
            if reply.finish_reason == "length":
                raise Truncated(
                    f"{purpose}: reply cut off at max_tokens={max_tokens * 2} twice "
                    f"(completion_tokens={reply.usage.get('completion_tokens')}); nothing saved")
        if not reply.content.strip():
            raise LLMError(f"{purpose}: model returned an empty answer "
                           f"(finish_reason={reply.finish_reason!r}, "
                           f"reasoning_chars={len(reply.reasoning)})")
        return reply

    def _call(self, messages, max_tokens, purpose, stream, on_content=None, on_reasoning=None) -> Reply:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = f"{self.base_url}/chat/completions"
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        started = time.monotonic()
        reply = Reply(content="", max_tokens=max_tokens)
        error = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                if stream:
                    self._read_stream(resp, reply, on_content, on_reasoning)
                else:
                    data = json.load(resp)
                    choice = data["choices"][0]
                    message = choice.get("message") or {}
                    reply.content = message.get("content") or ""
                    reply.reasoning = message.get("reasoning_content") or ""
                    reply.finish_reason = choice.get("finish_reason") or ""
                    reply.usage = data.get("usage") or {}
            return reply
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            error = f"HTTP {e.code} from {url}: {detail}"
            raise LLMError(error) from e
        except urllib.error.URLError as e:
            error = f"cannot reach LLM at {url} (model={self.model}): {e.reason}"
            raise LLMError(error) from e
        except (TimeoutError, OSError) as e:
            error = f"LLM call to {url} failed after {time.monotonic() - started:.0f}s: {e}"
            raise LLMError(error) from e
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            error = f"malformed response from {url}: {e!r}"
            raise LLMError(error) from e
        finally:
            self._log(purpose, reply, time.monotonic() - started, error)

    @staticmethod
    def _read_stream(resp, reply: Reply, on_content, on_reasoning) -> None:
        content, reasoning = [], []
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if chunk.get("usage"):
                reply.usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                    if on_reasoning:
                        on_reasoning(delta["reasoning_content"])
                if delta.get("content"):
                    content.append(delta["content"])
                    if on_content:
                        on_content(delta["content"])
                if choice.get("finish_reason"):
                    reply.finish_reason = choice["finish_reason"]
        reply.content = "".join(content)
        reply.reasoning = "".join(reasoning)

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
            "reasoning_chars": len(reply.reasoning),
            "duration_s": round(duration_s, 2),
            "error": error,
        }
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_log.open("a") as f:
            f.write(json.dumps(record) + "\n")
