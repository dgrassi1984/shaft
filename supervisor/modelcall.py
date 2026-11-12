"""One model call the supervisor makes for itself — the judges, the overview's recaps, describe_image — on whichever
provider the operator has: an operator who configured only OpenRouter, only an OpenAI-compatible endpoint or only a
ChatGPT plan still gets judged cycles, recaps and image descriptions.

The model's prefix picks the route:
- `claude-…`: Anthropic's Messages API with the supervisor's ANTHROPIC_API_KEY; a schema answer is structured output
  (output_config.format); a refusal is re-run server-side (`fallbacks: "default"`).
- `openrouter/…`: OpenRouter, under the instances' data policy (cloudllm.OPENROUTER_PROVIDER: the operator's
  providers only, no data collection, zero retention), priced by OpenRouter's own usage.cost.
- `custom/…`: the operator's OpenAI-compatible endpoint (SHAFT_CUSTOM_BASE_URL, SHAFT_CUSTOM_API_KEY), priced at the
  dollars SHAFT_CUSTOM_PRICES declares (cloudllm._custom_counts: the endpoint's counts, else the worst case).
- `codex/…`: a ChatGPT plan, through a Codex app-server of its own: started for this one call, a thread without
  tools, closed. It is the in-run channel (codexcli.Channel: the same process, Seatbelt profile, item checks and
  weekly cap) driven for a single thread; plan usage costs no dollars (usd 0.0, usage["plan"] == "codex"). A Codex
  turn takes no token ceiling, so max_tokens does not reach it; effort is sent per turn.
`effort` maps to Claude's output_config.effort, OpenRouter's reasoning.effort and Codex's turn effort; a custom/
endpoint gets none (its reasoning knobs are not known).

A schema answer off Anthropic has the schema in its instructions; the answer is checked (`check`, by default
valid()) and, when it does not match, shown back to the model with what is wrong and asked for whole again (three
answers at most). An answer cut off at max_tokens is never parsed nor shown back: it is asked once more with twice
the room, then fails (NoAnswerYet).

This module keeps no ledger: each attempt is handed to the caller's `log` (a Call), and the caller writes its own
(judge_usage.jsonl, recap_usage.jsonl, the instance's cloud_usage.jsonl for describe_image). Raw HTTP: the supervisor
stays dependency-free. Keys come from the project .env (secrets_env) and never leave the supervisor.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple

from . import cloudllm, planchannel
from .core import NETWORK_ERRORS, log as supervisor_log, secrets_env

API = "https://api.anthropic.com/v1/messages"
SEEN_CHARS = 400   # the end of an unusable answer, handed to the caller's log: where a dropped field shows


class Answer(NamedTuple):
    text: str            # a schema answer: the JSON object, as text
    usage: dict          # the provider's own usage counts (custom/: the counts it was priced by; codex/: plan usage)
    usd: float | None    # None: the provider gave no cost (unpriced); 0.0 for plan usage
    model: str           # the model that answered


class Call(NamedTuple):
    """One attempt, for the caller's ledger: what it used and cost, and what went wrong (None: a usable answer)."""
    model: str
    usage: dict
    usd: float | None
    seconds: float
    error: str | None
    stop: str | None = None
    provider: str | None = None
    reasoning: int | None = None
    seen: str | None = None


class ModelCallError(Exception):
    pass


class NoAnswerYet(ModelCallError):
    """Cut off at max_tokens twice: there is no answer to use."""


def available(model: str) -> str | None:
    """None when `model` can be called now, else the reason in words that name what is missing."""
    model = str(model or "")
    if model.startswith("claude-"):
        return None if secrets_env().get("ANTHROPIC_API_KEY") else "no ANTHROPIC_API_KEY in the supervisor's .env"
    if model.startswith("openrouter/"):
        return None if secrets_env().get("OPENROUTER_API_KEY") else "no OPENROUTER_API_KEY in the supervisor's .env"
    if model.startswith("custom/"):
        if cloudllm.custom_price(model) is None:
            return f"{model} has no declared price in SHAFT_CUSTOM_PRICES"
        if not cloudllm.CUSTOM_BASE_URL:
            return "no SHAFT_CUSTOM_BASE_URL set: custom/ models have no endpoint"
        return None if secrets_env().get("SHAFT_CUSTOM_API_KEY") else "no SHAFT_CUSTOM_API_KEY in the supervisor's .env"
    if model.startswith("codex/"):
        from . import codexcli
        if model not in codexcli.MODELS:
            return f"{model} is not one of SHAFT_CODEX_MODELS ({', '.join(codexcli.MODELS)})"
        if not (Path(codexcli.HOME).expanduser() / "auth.json").is_file():
            return "Codex login missing: run shaft codex login"
        if not os.access(codexcli.BIN, os.X_OK):
            return f"no Codex CLI at {codexcli.BIN} (SHAFT_CODEX_BIN)"
        return None
    return f"{model!r} is not a model the supervisor can call (claude-…, openrouter/…, custom/… or codex/…)"


def ask(model: str, system: str, messages: list[dict], *, max_tokens: int, effort: str | None = "medium",
        schema: dict | None = None, purpose: str, check: Callable[[object, dict], str] | None = None,
        log: Callable[[Call], None] | None = None) -> Answer:
    """One answer from `model` to `messages` (OpenAI-style: text, or parts with data: URL images) under `system`.
    `schema` asks for one JSON object (Answer.text is it); `check` (default valid()) says what is wrong with one off
    Anthropic, '' when nothing. `effort` None leaves the model's default. Raises ModelCallError with the reason."""
    if (reason := available(model)):
        raise ModelCallError(f"{model} is unavailable ({purpose}): {reason}")
    log = log or (lambda call: None)
    if model.startswith("claude-"):
        try:
            return _claude(model, system, messages, max_tokens, effort, schema, log)
        except NoAnswerYet:
            return _claude(model, system, messages, max_tokens * 2, effort, schema, log)
    with (_Codex(model) if model.startswith("codex/") else _OpenAI(model)) as transport:
        return _instructed(transport, model, system, messages, max_tokens, effort, schema, check or valid, log)


# -- Anthropic ------------------------------------------------------------------------------------------------------
def _claude_messages(messages: list[dict]) -> tuple[list[str], list[dict]]:
    """(system texts, Claude messages) from OpenAI-style messages: text stays text, image parts become image blocks."""
    system, out = [], []
    for m in messages:
        content = m.get("content") or ""
        if m["role"] == "system":
            system.append(planchannel._text(content))
        else:
            out.append({"role": m["role"], "content": content if isinstance(content, str)
                        else cloudllm._user_blocks(content)})
    return system, out


def _claude(model, system, messages, max_tokens, effort, schema, log) -> Answer:
    more, msgs = _claude_messages(messages)
    body = {"model": model, "max_tokens": max_tokens, "messages": msgs, "fallbacks": "default"}
    if (system := "\n\n".join(s for s in [system, *more] if s)):
        body["system"] = system
    config = {**({"effort": effort} if effort else {}),
              **({"format": {"type": "json_schema", "schema": schema}} if schema is not None else {})}
    if config:
        body["output_config"] = config
    req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
        "content-type": "application/json", "x-api-key": secrets_env().get("ANTHROPIC_API_KEY", ""),
        "anthropic-version": "2023-06-01", "anthropic-beta": "server-side-fallback-2026-07-01"})
    started, last_error = time.monotonic(), None
    for wait in (0, 15, 60):
        time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}"
            if e.code not in (429, 500, 502, 503, 504, 529):
                log(Call(model, {}, None, time.monotonic() - started, last_error))
                raise ModelCallError(f"{model}: {last_error}") from e
        except NETWORK_ERRORS as e:
            last_error = f"network: {e}"
    else:
        log(Call(model, {}, None, time.monotonic() - started, last_error))
        raise ModelCallError(f"{model} unreachable after retries: {last_error}")

    usage, stop = data.get("usage") or {}, data.get("stop_reason")
    said = data.get("model") or model
    usd = cloudllm.usd(said, cloudllm.billed(usage))
    seconds = time.monotonic() - started
    if stop == "refusal":
        log(Call(said, usage, usd, seconds, "refusal", stop))
        raise ModelCallError(f"{model} refused ({(data.get('stop_details') or {}).get('category')})")
    if stop == "max_tokens":
        log(Call(said, usage, usd, seconds, "max_tokens", stop))
        raise NoAnswerYet(f"{model} hit max_tokens={max_tokens}: no answer yet")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if schema is not None:
        try:
            if not isinstance(json.loads(text), dict):
                raise ValueError("not an object")
        except ValueError as e:
            log(Call(said, usage, usd, seconds, f"bad json: {text[:200]}", stop))
            raise ModelCallError(f"{model} returned non-JSON: {text[:200]!r}") from e
    log(Call(said, usage, usd, seconds, None, stop))
    return Answer(text, usage, usd, said)


# -- every other provider: instructions, a check, re-asks ------------------------------------------------------------
class _Reply(NamedTuple):
    text: str
    finish: str | None
    usage: dict
    usd: float | None
    error: str | None
    provider: str | None = None
    reasoning: int | None = None


def _instructed(transport, model, system, messages, max_tokens, effort, schema, check, log) -> Answer:
    """GLM 5.3 Flash ended its JSON after difficulty_reason in 14 of 84 attempts (2026-10-14/15), having settled novelty
    in its reasoning; asked blind again it re-judged the case from scratch (2-4 min) and dropped it as often, while
    shown its answer it returned the whole object, the other fields unchanged, in about a minute. So an answer that
    does not match is shown back with what is wrong. A cut-off one is never parsed nor shown: asked again once with
    twice the room — without thinking when it was cut off still thinking, since more room then only buys more thinking
    (mimo's provider ignores effort and reasoning.max_tokens, probed 2026-10-30)."""
    instructions = system
    if schema is not None:
        fields = json.dumps(schema.get("properties", {}), ensure_ascii=False)
        instructions = (f"{system}\n\nAnswer with ONE JSON object and nothing else, with exactly these fields "
                        f"(JSON schema of each): {fields}")
    retried_length, problem, unusable, think = False, "", "", True
    for attempt in range(3):
        convo = ([{"role": "system", "content": instructions}] if instructions else []) + list(messages)
        if problem:
            if unusable.strip():
                convo.append({"role": "assistant", "content": unusable})
            convo.append({"role": "user", "content": f"That answer is not usable: {problem}. Answer again with the "
                                                     f"whole JSON object, every one of these fields: "
                                                     f"{', '.join(schema.get('properties', {}))}."})
        started = time.monotonic()
        r = transport.send(convo, max_tokens, effort, think)
        seconds = time.monotonic() - started
        if r.error:
            log(Call(model, r.usage, r.usd, seconds, r.error, r.finish, r.provider, r.reasoning))
            raise ModelCallError(f"{model}: {r.error}")
        if r.finish == "length":                          # no answer yet: never parsed, asked once with more room
            log(Call(model, r.usage, r.usd, seconds, f"length at {max_tokens}", r.finish, r.provider, r.reasoning))
            if retried_length:
                raise NoAnswerYet(f"{model} ran out of room twice (max_tokens {max_tokens})")
            retried_length, max_tokens, think = True, max_tokens * 2, bool(r.text.strip())
            continue
        if schema is None:
            log(Call(model, r.usage, r.usd, seconds, None, r.finish, r.provider, r.reasoning))
            return Answer(r.text, r.usage, r.usd, model)
        try:
            out = _json_in(r.text)
            problem = check(out, schema)
        except ValueError:
            problem = "not valid JSON"
        log(Call(model, r.usage, r.usd, seconds, problem or None, r.finish, r.provider, r.reasoning,
                 r.text[-SEEN_CHARS:] if problem else None))
        if not problem:
            return Answer(json.dumps(out, ensure_ascii=False), r.usage, r.usd, model)
        unusable = r.text
    raise ModelCallError(f"{model} gave no usable answer: {problem}")


def valid(out: object, schema: dict) -> str:
    """'' if `out` matches the (flat) schema — every property present, of its type, in its enum — else what is wrong."""
    if not isinstance(out, dict):
        return "not a JSON object"
    for k, spec in schema["properties"].items():
        if k not in out:
            return f"missing {k}"
        v, t = out[k], spec.get("type")
        if isinstance(t, list):
            if all(valid({k: v}, {"properties": {k: {**spec, "type": kind}}}) for kind in t):
                return f"{k} is not a {t}"
            continue
        if t == "string" and not isinstance(v, str) or t == "boolean" and not isinstance(v, bool) or \
                t == "integer" and (not isinstance(v, int) or isinstance(v, bool)) or \
                t == "number" and (not isinstance(v, (int, float)) or isinstance(v, bool)) or \
                t == "array" and not isinstance(v, list) or t == "object" and not isinstance(v, dict) or \
                t == "null" and v is not None:
            return f"{k} is not a {t}"
        if "enum" in spec and v not in spec["enum"]:
            return f"{k}={v!r} not in {spec['enum']}"
    return ""


def _json_in(text: str) -> object:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]) if start >= 0 and end > start else json.loads(text)


class _OpenAI:
    """openrouter/ and custom/: one OpenAI-format request per send, through cloudllm's senders (its retries on
    overload and network errors, its data policy for OpenRouter)."""

    def __init__(self, model: str):
        self.model = model

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, convo, max_tokens, effort, think) -> _Reply:
        body = {"model": self.model, "max_tokens": max_tokens, "messages": convo}
        if self.model.startswith("custom/"):
            return self._custom(body)
        name = self.model.removeprefix("openrouter/")
        if not think:
            body.update(cloudllm.openrouter_reasoning(name, {"enable_thinking": False}))
        elif effort:
            body["reasoning"] = {"effort": effort}
        data, error = cloudllm._send(cloudllm._openrouter_request(body, secrets_env().get("OPENROUTER_API_KEY", ""),
                                                                  stream=False))
        if data is not None and not data.get("choices"):   # OpenRouter can answer 200 with an error object
            error = f"no reply: {json.dumps(data.get('error') or data)[:400]}"
        usage = (data or {}).get("usage") or {}
        cost = float(usage["cost"]) if usage.get("cost") is not None else None
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if error:
            return _Reply("", None, usage, cost, error + cloudllm._policy_hint(error), (data or {}).get("provider"),
                          reasoning)
        choice = data["choices"][0]
        return _Reply((choice.get("message") or {}).get("content") or "", choice.get("finish_reason"), usage, cost,
                      None, data.get("provider"), reasoning)

    def _custom(self, body) -> _Reply:
        price = cloudllm.custom_price(self.model)
        data, error = cloudllm._send(cloudllm._custom_request(body, secrets_env().get("SHAFT_CUSTOM_API_KEY", ""),
                                                              stream=False))
        if data is None:                                  # nothing was generated: no charge
            return _Reply("", None, {}, 0.0, f"custom endpoint {cloudllm.CUSTOM_BASE_URL}: {error}")
        choice = ((data.get("choices") or [{}])[0]) or {}
        message = choice.get("message") or {}
        tin, tout, usd, extra = cloudllm._custom_counts(price, body, data.get("usage") or {},
                                                        cloudllm._message_chars(message))
        usage = {"prompt_tokens": tin, "completion_tokens": tout, **extra}
        if not data.get("choices"):
            return _Reply("", None, usage, usd, f"no reply: {json.dumps(data.get('error') or data)[:400]}")
        return _Reply(message.get("content") or "", choice.get("finish_reason"), usage, usd, None)


# -- Codex ----------------------------------------------------------------------------------------------------------
def _codex_channel_class():
    from . import codexcli

    class OneThread(codexcli.Channel):
        """A Codex app-server of the supervisor's own for one call: the in-run channel's start (Seatbelt profile,
        native tools off, the weekly-cap check), item checks and teardown, unchanged; one thread without tools, one
        turn per send. Its usage row is the caller's to keep, so the channel writes none."""

        def _record(self, step):
            step.recorded = True

        def turn(self, model, system, text, images, effort) -> planchannel.Step:
            with self._serial:
                if self._process is None:
                    self._start(model, effort)
                self._check_rate()
                with self._cv:
                    if self._session is None:
                        self._session = self._open_session(model, system, [], effort)
                    step = planchannel.Step(model, "supervisor")
                    self._step = step
                    turn = self._start_turn(text, images, effort)
                    step.turn = step.turn or turn
            step.event.wait()
            with self._cv:
                if step.error:
                    raise step.error
            return step

    return OneThread


class _Codex:
    """codex/: one short-lived app-server per ask(); its first send opens the thread with the system text as the base
    instructions, and a re-ask is that thread's next turn (Codex sees its own answer there)."""

    def __init__(self, model: str):
        self.model, self.sent = model, 0
        self.channel = _codex_channel_class()("supervisor", "supervisor", supervisor_log)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.channel.close()
        return False

    def send(self, convo, max_tokens, effort, think) -> _Reply:
        system = "\n\n".join(planchannel._text(m.get("content")) for m in convo if m["role"] == "system")
        turns = [m for m in convo if m["role"] != "system"][self.sent:]
        if self.sent:                                     # the thread holds the earlier turns: only the new message
            turns = [m for m in turns if m["role"] == "user"]
        texts, images = [], []
        for m in turns:
            text, more = planchannel._split(m.get("content"))
            texts.append(text)
            images += more
        self.sent = len([m for m in convo if m["role"] != "system"])
        try:
            step = self.channel.turn(self.model, system, "\n\n".join(texts), images, effort)
        except planchannel.ChannelFailure as e:
            return _Reply("", None, {"plan": "codex"}, 0.0, e.reason)
        usage = {**step.usage, "cached_input_tokens": step.cached, "plan": "codex"}
        return _Reply(step.text, "stop", usage, 0.0, None)
