"""The cloud judge: a stronger model that grades instances, never lives their lives.

Raw HTTP to the Claude Messages API (the supervisor stays dependency-free by design:
it is the trust anchor). Structured JSON output via output_config.format; refusals
are re-run server-side with `fallbacks: "default"`. The key comes from PROJECT/.env.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from .core import SUP_DIR, knob, secrets_env

JUDGE_MODEL, JUDGE_SRC = knob("SHAFT_JUDGE_MODEL", "claude-opus-5-5", str)
API = "https://api.anthropic.com/v1/messages"


class JudgeError(Exception):
    pass


def available() -> bool:
    return bool(secrets_env().get("ANTHROPIC_API_KEY"))


def ask(system: str, prompt: str, schema: dict, purpose: str, max_tokens: int = 16000,
        effort: str = "medium") -> dict:
    """One judgment. Returns the parsed JSON object matching `schema`."""
    key = secrets_env().get("ANTHROPIC_API_KEY")
    if not key:
        raise JudgeError(f"no ANTHROPIC_API_KEY in {SUP_DIR.parent / '.env'}; the judge is unavailable")
    body = {
        "model": JUDGE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        "fallbacks": "default",
    }
    req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01",
        "anthropic-beta": "server-side-fallback-2026-07-01"})
    started, last_error = time.monotonic(), None
    for attempt, wait in enumerate((0, 15, 60)):
        time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            last_error = f"HTTP {e.code}: {detail}"
            if e.code not in (429, 500, 502, 503, 504, 529):
                _log(purpose, None, time.monotonic() - started, last_error)
                raise JudgeError(last_error) from e
        except (urllib.error.URLError, OSError) as e:
            last_error = f"network: {e}"
    else:
        _log(purpose, None, time.monotonic() - started, last_error)
        raise JudgeError(f"judge unreachable after retries: {last_error}")

    usage = data.get("usage") or {}
    stop = data.get("stop_reason")
    if stop == "refusal":
        _log(purpose, usage, time.monotonic() - started, "refusal")
        raise JudgeError(f"judge refused ({(data.get('stop_details') or {}).get('category')})")
    if stop == "max_tokens":
        _log(purpose, usage, time.monotonic() - started, "max_tokens")
        raise JudgeError(f"judge hit max_tokens={max_tokens}: no answer yet")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    try:
        out = json.loads(text)
    except ValueError as e:
        _log(purpose, usage, time.monotonic() - started, f"bad json: {text[:200]}")
        raise JudgeError(f"judge returned non-JSON: {text[:200]!r}") from e
    _log(purpose, usage, time.monotonic() - started, None, data.get("model"))
    return out


def _log(purpose, usage, seconds, error, model=None) -> None:
    SUP_DIR.mkdir(exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "purpose": purpose, "model": model or JUDGE_MODEL,
           "input_tokens": (usage or {}).get("input_tokens"), "output_tokens": (usage or {}).get("output_tokens"),
           "seconds": round(seconds, 1), "error": error}
    with (SUP_DIR / "judge_usage.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


# -- the second judge ---------------------------------------------------------------------------------------------
# Another model family grades a sample of cycles blind, so an instance can tell the main judge's idiosyncrasies
# from what its tool log shows (wren, 2026-10-13; Daniele chose GLM 5.3 Flash). Through OpenRouter, under the same
# data policy as the instances' cloud models (cloudllm.OPENROUTER_PROVIDER: Novita only, no data collection or
# retention). Novita does not offer structured outputs for this model, so the schema is in the instructions and
# the answer is checked here.
JUDGE2_MODEL, _ = knob("SHAFT_JUDGE2_MODEL", "openrouter/z-ai/glm-5.3-flash", str)
JUDGE2_EVERY, _ = knob("SHAFT_JUDGE2_EVERY", "4", int)        # one cycle in this many; 0 = no second judge
JUDGE2_MAX_TOKENS = 16000                                      # its hidden reasoning shares this ceiling


def second_available() -> bool:
    return JUDGE2_EVERY > 0 and bool(secrets_env().get("OPENROUTER_API_KEY"))


def _valid(out: object, schema: dict) -> str:
    """'' if `out` matches the (flat) schema, else what is wrong."""
    if not isinstance(out, dict):
        return "not a JSON object"
    for k, spec in schema["properties"].items():
        if k not in out:
            return f"missing {k}"
        v, t = out[k], spec.get("type")
        if t == "string" and not isinstance(v, str) or t == "boolean" and not isinstance(v, bool) or \
                t == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
            return f"{k} is not a {t}"
        if "enum" in spec and v not in spec["enum"]:
            return f"{k}={v!r} not in {spec['enum']}"
    return ""


def _json_in(text: str) -> object:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]) if start >= 0 and end > start else json.loads(text)


def ask_second(system: str, prompt: str, schema: dict, purpose: str) -> dict:
    """One blind judgment by the second judge, checked against `schema`. An answer cut off for length is never
    parsed: it is asked again once with twice the room; an answer that does not match is asked again once."""
    from . import cloudllm
    key = secrets_env().get("OPENROUTER_API_KEY")
    if not key:
        raise JudgeError(f"no OPENROUTER_API_KEY in {SUP_DIR.parent / '.env'}; the second judge is unavailable")
    fields = json.dumps(schema["properties"], ensure_ascii=False)
    instructions = (f"{system}\n\nAnswer with ONE JSON object and nothing else, with exactly these fields "
                    f"(JSON schema of each): {fields}")
    max_tokens, retried_length, problem = JUDGE2_MAX_TOKENS, False, ""
    for attempt in range(3):
        started = time.monotonic()
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
        if problem:
            messages.append({"role": "user", "content": f"Your previous answer was not usable ({problem}). "
                                                        "Answer again with only the JSON object."})
        data, error = cloudllm._send(cloudllm._openrouter_request(
            {"model": JUDGE2_MODEL, "max_tokens": max_tokens, "messages": messages}, key, stream=False))
        usage = (data or {}).get("usage") or {}
        cost = float(usage.get("cost") or 0)
        if error or not data:
            _log2(purpose, usage, cost, time.monotonic() - started, error or "no response")
            raise JudgeError(f"second judge: {error}{cloudllm._policy_hint(error or '')}")
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":       # no answer yet: never parsed, asked once with more room
            _log2(purpose, usage, cost, time.monotonic() - started, f"length at {max_tokens}")
            if retried_length:
                raise JudgeError(f"second judge ran out of room twice ({max_tokens} tokens)")
            retried_length, max_tokens = True, max_tokens * 2
            continue
        text = (choice.get("message") or {}).get("content") or ""
        try:
            out = _json_in(text)
            problem = _valid(out, schema)
        except ValueError:
            problem = "not valid JSON"
        _log2(purpose, usage, cost, time.monotonic() - started, problem or None, data.get("provider"))
        if not problem:
            return out
    raise JudgeError(f"second judge gave no usable answer: {problem}")


def _log2(purpose, usage, cost, seconds, error, provider=None) -> None:
    SUP_DIR.mkdir(exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "purpose": purpose, "model": JUDGE2_MODEL, "provider": provider,
           "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
           "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
           "usd": cost, "seconds": round(seconds, 1), "error": error}
    with (SUP_DIR / "judge_usage.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")
