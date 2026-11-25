"""OpenAI-compatible chat client (llama.cpp, Ollama, LM Studio, ...).

Rules this module enforces:
- `finish_reason == "length"` is *no answer yet*: the call is retried once
  with twice the room — without thinking when the cut came while it was still
  thinking; if it is still cut off, `Truncated` is raised and the caller must
  not save anything.
- An empty answer (no text, no tool call) is asked once more, then an error — never a silent "".
- Transient failures (backend restarting: 502/503/504, connection refused)
  are retried with backoff, but never after any text has already streamed.
- Every call — including failed ones — is recorded in logs/llm_usage.jsonl.
- Assembled replies get back the space a model drops between a word and a number (spacing.py), in the
  answer and in every tool's text except code, queries and non-prose files.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import images, pausing
from .spacing import respace


class LLMError(Exception):
    pass


class Truncated(LLMError):
    pass


class Transient(LLMError):
    """The backend is temporarily unavailable; nothing was generated."""


TRANSIENT_HTTP = {502, 503, 504}
BACKOFF_S = (10, 30)  # waits before the 2nd and 3rd attempt
# Tools whose arguments are code, queries or secrets: never respaced. Every other tool, my own plugins included,
# gets its text respaced (spacing.py touches only boundaries that are never a name), so a tool I build is covered.
CODE_TOOLS = {"run", "read_file", "list_files", "search_memory", "view_image", "http_request", "fetch_page",
              "web_search", "secret_set", "secret_list", "secret_delete", "history", "list_instances"}
PROSE_FILES = (".md", ".markdown", ".txt")   # write_file/append_file elsewhere is code or data
_NAMING_KEYS = {"path", "id", "name", "to", "project", "slug", "commitment_id",
                "made_to", "conversation", "supersedes", "reopens"}


@dataclass
class Reply:
    content: str
    reasoning: str = ""
    tool_calls: list = field(default_factory=list)   # [{"id", "name", "arguments": str}]
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    attempts: int = 1
    max_tokens: int = 0
    same_turn: bool = False       # the plan channel has not finished this model turn


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
        self.timeout_s = timeout_s
        self.usage_log = usage_log

    @classmethod
    def from_config(cls, cfg) -> "Client":
        """LLM_BASE_URL="broker" means: a model the supervisor's broker reaches for me (no key here) — a cloud model,
        or a self-hosted Bonsai server such as racer/bonsai2, which reads the same template settings as my local one."""
        base, token = cfg.LLM_BASE_URL, ""
        if base == "broker":
            broker_url = os.environ.get("SHAFT_BROKER_URL")
            if not broker_url:
                raise LLMError("LLM_BASE_URL is 'broker' but no broker is running (SHAFT_BROKER_URL unset)")
            base, token = broker_url.rstrip("/") + "/llm/v1", os.environ.get("SHAFT_BROKER_TOKEN", "")
        return cls(base, cfg.LLM_MODEL, cfg.LLM_API_KEY, cfg.LLM_TIMEOUT_S,
                   cfg.root / "logs" / "llm_usage.jsonl", broker_token=token,
                   presence_penalty=cfg.LLM_PRESENCE_PENALTY / 10,
                   think_followup=cfg.LLM_THINK_FOLLOWUP, reasoning_effort=cfg.LLM_REASONING_EFFORT)

    # -- public -------------------------------------------------------------
    def complete(self, messages: list[dict], max_tokens: int, purpose: str,
                 tools: list[dict] | None = None, think: bool = True) -> Reply:
        return self._with_retry(messages, max_tokens, purpose, stream=False, tools=tools, think=think)

    def stream(self, messages: list[dict], max_tokens: int, purpose: str,
               on_content: Callable[[str], None] | None = None,
               on_reasoning: Callable[[str], None] | None = None,
               on_retry: Callable[[int, int], None] | None = None,
               tools: list[dict] | None = None,
               on_progress: Callable[[int, int], None] | None = None, think: bool = True,
               on_tool_text: Callable[[str, int], None] | None = None, reasoning_effort: str = "") -> Reply:
        """on_progress(processed, total) reports prompt reading, where the server supports it;
        on_tool_text(name, chars) a tool call the model is still writing (SHAFT's server);
        reasoning_effort: this call's effort in place of the client's own (a spoken reply's VOICE_REASONING_EFFORT);
        "" keeps the client's (LLM_REASONING_EFFORT)."""
        return self._with_retry(messages, max_tokens, purpose, stream=True, tools=tools,
                                on_content=on_content, on_reasoning=on_reasoning,
                                on_retry=on_retry, on_progress=on_progress, think=think,
                                on_tool_text=on_tool_text, reasoning_effort=reasoning_effort)

    # -- internals ----------------------------------------------------------
    def _with_retry(self, messages, max_tokens, purpose, stream, on_retry=None, **callbacks) -> Reply:
        reply = self._answer(messages, max_tokens, purpose, stream, on_retry, **callbacks)
        if not reply.content.strip() and not reply.tool_calls:
            # A model that ends its thinking with nothing after it (Bonsai: 21 shaft cycles lost 2026-10-14..30) has
            # not answered yet, as a cut-off reply has not: asked once more, and only then an error.
            first = reply
            reply = self._answer(messages, max_tokens, purpose, stream, on_retry, **callbacks)
            reply.attempts += first.attempts
            if not reply.content.strip() and not reply.tool_calls:
                raise LLMError(f"{purpose}: model returned an empty answer twice "
                               f"(finish_reason={reply.finish_reason!r}, "
                               f"reasoning_chars={len(first.reasoning)} then {len(reply.reasoning)})")
        return reply

    def _answer(self, messages, max_tokens, purpose, stream, on_retry=None, **callbacks) -> Reply:
        reply = self._resilient(messages, max_tokens, purpose, stream, **callbacks)
        if reply.finish_reason == "length":
            print(f"{purpose}: reply cut off at max_tokens={max_tokens}; retrying once with "
                  f"max_tokens={max_tokens * 2}", file=sys.stderr)
            if on_retry:
                on_retry(max_tokens, max_tokens * 2)
            # Cut off while still thinking (no text, no tool call): more room only bought more thinking. mimo spent all
            # 32,768 tokens of a sleep step on it, twice, every night from 2026-10-29, and its provider ignores
            # reasoning.effort and reasoning.max_tokens (probed 10-30). The retry answers without thinking. Cut while
            # writing text or a tool call, it had finished thinking: the retry thinks again. Either way the cut reply,
            # its partial tool call included, is discarded here and never returned.
            thinking_only = not reply.content.strip() and not reply.tool_calls
            retry = {**callbacks, "think": False} if thinking_only else callbacks
            reply = self._resilient(messages, max_tokens * 2, purpose, stream, **retry)
            reply.attempts = 2
            if reply.finish_reason == "length":
                raise Truncated(
                    f"{purpose}: reply cut off at max_tokens={max_tokens * 2} twice "
                    f"(completion_tokens={reply.usage.get('completion_tokens')}); nothing saved")
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
              on_content=None, on_reasoning=None, on_progress=None, think=True, on_tool_text=None,
              reasoning_effort="") -> Reply:
        body = {"model": self.model, "messages": images.for_model(messages), "max_tokens": max_tokens}
        if self.presence_penalty:   # Qwen's advice for quantized models: curbs endless repetition
            body["presence_penalty"] = self.presence_penalty
        # Bonsai's template settings (my local server, or a self-hosted one through the broker); for an OpenRouter
        # model the broker sends my thinking settings in its own form (reasoning.effort, or none for think=False).
        # Earlier steps' thinking is shown only within the current reply, not for older turns: it keeps
        # the plan across tool rounds without carrying every past reply's reasoning in the context.
        kwargs = {"preserve_thinking": False}
        if not think:   # Qwen-style templates: answer directly, no hidden reasoning
            kwargs["enable_thinking"] = False
        elif reasoning_effort or self.reasoning_effort:
            # Bonsai's template defaults to xhigh, which thinks until the server's budget cuts it off on
            # anything non-trivial (4,096 tokens, ~4 min); medium thinks as much as the request needs.
            kwargs["reasoning_effort"] = reasoning_effort or self.reasoning_effort
        if think and self.think_followup and messages and messages[-1].get("role") == "tool":
            # After a tool result I mostly need to read it, not deliberate from scratch: SHAFT's server
            # forces </think> after this many reasoning tokens (its own budget stays the ceiling; llama.cpp ignores it).
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
            with pausing.interruptible(), urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                if stream:
                    self._read_stream(resp, reply, on_content, on_reasoning, on_progress, on_tool_text)
                else:
                    data = json.load(resp)
                    choice = data["choices"][0]
                    message = choice.get("message") or {}
                    reply.content = message.get("content") or ""
                    # llama.cpp says reasoning_content, mlx_lm says reasoning
                    reply.reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                    _move_leaked_thinking(reply)
                    reply.finish_reason = _finish_reason(choice, data)
                    reply.usage = data.get("usage") or {}
                    reply.same_turn = data.get("shaft_same_turn") is True
                    # A cut-off call is kept, read leniently, only as the sign that the answer had begun (as the
                    # stream keeps it): _answer never returns a cut reply, so it is never run.
                    cut = reply.finish_reason == "length"
                    reply.tool_calls = []
                    for i, c in enumerate(message.get("tool_calls") or []):
                        fn = (c.get("function") or {}) if cut else c["function"]
                        reply.tool_calls.append({"id": c.get("id") or f"call_{i}",
                                                 "name": fn.get("name") or "" if cut else fn["name"],
                                                 "arguments": fn.get("arguments") or "{}"})
            _respace_reply(reply)
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
    def _read_stream(resp, reply: Reply, on_content, on_reasoning, on_progress=None, on_tool_text=None) -> None:
        content, reasoning = [], []
        calls: dict[int, dict] = {}
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if line.startswith(": keepalive") and on_progress:   # mlx_lm: ": keepalive 2048/8850"
                done, _, total = line.split()[-1].partition("/")
                if done.isdigit() and total.isdigit():
                    on_progress(int(done), int(total))
                continue
            if line.startswith(": tool ") and on_tool_text:   # SHAFT's server: ": tool write_file 1834"
                parts = line.split()
                if len(parts) == 4 and parts[3].isdigit():
                    on_tool_text(parts[2], int(parts[3]))
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if "shaft_same_turn" in chunk:
                reply.same_turn = chunk["shaft_same_turn"] is True
            if chunk.get("error") and not chunk.get("choices"):   # an error sent mid-stream (OpenRouter does)
                err = chunk["error"]
                raise LLMError(f"stream error: {err.get('message', err) if isinstance(err, dict) else err}")
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
                reason = _finish_reason(choice, chunk)
                if reason:
                    reply.finish_reason = reason
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


def _respace_reply(reply: Reply) -> None:
    """Repair assembled prose; preserve tool identifiers and unchanged argument JSON exactly."""
    def prose(value):
        if isinstance(value, str):
            return respace(value)
        if isinstance(value, list):
            return [prose(item) for item in value]
        if isinstance(value, dict):
            return {key: item if key in _NAMING_KEYS or key.endswith("_id") else prose(item)
                    for key, item in value.items()}
        return value

    reply.content = respace(reply.content or "")
    for call in reply.tool_calls:
        if call["name"] in CODE_TOOLS:
            continue
        try:
            args = json.loads(call["arguments"])
        except (json.JSONDecodeError, TypeError):
            continue
        if call["name"] in {"append_file", "write_file"} and not (
                isinstance(args, dict) and str(args.get("path", "")).lower().endswith(PROSE_FILES)):
            continue
        changed = prose(args)
        if changed != args:
            call["arguments"] = json.dumps(changed, ensure_ascii=False)


def _move_leaked_thinking(reply: Reply) -> None:
    """When the server cuts my thinking at its budget, I sometimes go on thinking in the answer and close
    the block again myself: everything up to that `</think>` is reasoning, not what I said."""
    if "</think>" in reply.content:
        thought, _, said = reply.content.rpartition("</think>")
        reply.reasoning = (reply.reasoning + "\n" + thought).strip()
        reply.content = said.lstrip()


def _finish_reason(choice: dict, data: dict) -> str:
    reasons = (choice.get("finish_reason"), choice.get("stop_reason"), data.get("stop_reason"))
    return "length" if "length" in reasons else next((r for r in reasons if r), "")
