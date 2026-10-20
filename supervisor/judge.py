"""The cloud judge: models that grade instances, never live their lives.

A rotation (Opus 5.5 was too costly; "stochastic rotation I prefer"): every judgment picks one
model at random from SHAFT_JUDGE_MODELS (MiMo and Sonnet 5.5; Haiku 5.5 left on 2026-10-16: on the same cycles it
called 54% of wren's claims unsupported where the blind GLM judge called 4%, Sonnet 21% and Opus 17% —
docs/plans/PLAN-judge-mix.md), never the model the judged instance runs on (no self-preference — wren runs
on MiMo). Every verdict records the model that gave it. Claude models go straight to the Claude Messages API with the
supervisor's key (structured JSON output via output_config.format; refusals re-run server-side with
`fallbacks: "default"`); "openrouter/…" models go through OpenRouter under the instances' data policy
(cloudllm.OPENROUTER_PROVIDER: no data collection, zero retention), with the schema in the instructions and the
answer checked here. Muse Spark was left out: its only provider keeps prompts. Raw HTTP: the supervisor stays
dependency-free by design (it is the trust anchor); keys come from PROJECT/.env.
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.request

from .core import NETWORK_ERRORS, SUP_DIR, cloud_models, knob, openrouter_model, secrets_env, whole

JUDGE_MODELS, JUDGE_SRC = knob(
    "SHAFT_JUDGE_MODELS", "openrouter/xiaomi/mimo-v2.6-pro,claude-sonnet-5-5", cloud_models, "the judge pool: each "
    "judgment picks one at random, never the judged instance's own model", group="Judging",
    services=("runs", "api", "web"))   # web: its stats count verdicts by the judges in the pool (by_current_judge)
API = "https://api.anthropic.com/v1/messages"


class JudgeError(Exception):
    pass


def _norm(model: str) -> str:
    """'openrouter/xiaomi/mimo-v2.6-pro', 'xiaomi/mimo-v2.6-pro' and 'anthropic/claude-haiku-5.5' compare by model."""
    m = (model or "").strip().lower().removeprefix("openrouter/").removeprefix("anthropic/")
    return m.replace(".", "-")


def same_model(a: str | None, b: str | None) -> bool:
    return bool(a and b) and _norm(a) == _norm(b)


def pick(exclude=(), rng=random) -> str:
    """One judge for one judgment: at random from the pool, never one of `exclude` (the judged instance's models)."""
    pool = [m for m in JUDGE_MODELS if not any(same_model(m, x) for x in exclude)]
    if not pool:
        raise JudgeError(f"no judge left in SHAFT_JUDGE_MODELS={JUDGE_MODELS} once the instance's own model "
                         f"{[x for x in exclude if x]} is left out: an instance is never judged by its own model")
    return rng.choice(pool)


def rotation() -> str:
    """The pool as one label: what a battery era is graded under."""
    return "rotation:" + ",".join(JUDGE_MODELS)


def _key_for(model: str) -> tuple[str, str]:
    name = "OPENROUTER_API_KEY" if model.startswith("openrouter/") else "ANTHROPIC_API_KEY"
    return name, secrets_env().get(name, "")


def available() -> bool:
    """Every model of the rotation has its key (a missing one would fail a share of judgments at random)."""
    return bool(JUDGE_MODELS) and all(_key_for(m)[1] for m in JUDGE_MODELS)


def ask(system: str, prompt: str, schema: dict, purpose: str, max_tokens: int = 16000,
        effort: str = "medium", model: str | None = None, exclude=()) -> dict:
    """One judgment by `model` (else one picked from the rotation, never one of `exclude`). Returns the parsed JSON
    object matching `schema`. Callers that record who judged pick() first and pass the model."""
    model = model or pick(exclude)
    if model.startswith("openrouter/"):
        return _ask_openrouter(model, system, prompt, schema, purpose, max_tokens, effort)
    return _ask_claude(model, system, prompt, schema, purpose, max_tokens, effort)


def _ask_claude(model, system, prompt, schema, purpose, max_tokens, effort) -> dict:
    key = secrets_env().get("ANTHROPIC_API_KEY")
    if not key:
        raise JudgeError(f"no ANTHROPIC_API_KEY in {SUP_DIR.parent / '.env'}; the judge {model} is unavailable")
    body = {
        "model": model,
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
                _log(purpose, model, None, time.monotonic() - started, last_error)
                raise JudgeError(last_error) from e
        except NETWORK_ERRORS as e:
            last_error = f"network: {e}"
    else:
        _log(purpose, model, None, time.monotonic() - started, last_error)
        raise JudgeError(f"judge {model} unreachable after retries: {last_error}")

    usage = data.get("usage") or {}
    stop = data.get("stop_reason")
    if stop == "refusal":
        _log(purpose, model, usage, time.monotonic() - started, "refusal")
        raise JudgeError(f"judge {model} refused ({(data.get('stop_details') or {}).get('category')})")
    if stop == "max_tokens":
        _log(purpose, model, usage, time.monotonic() - started, "max_tokens")
        raise JudgeError(f"judge {model} hit max_tokens={max_tokens}: no answer yet")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    try:
        out = json.loads(text)
    except ValueError as e:
        _log(purpose, model, usage, time.monotonic() - started, f"bad json: {text[:200]}")
        raise JudgeError(f"judge {model} returned non-JSON: {text[:200]!r}") from e
    _log(purpose, data.get("model") or model, usage, time.monotonic() - started, None)
    return out


SEEN_CHARS = 400   # the end of an unusable answer, kept in its usage line: where a dropped field shows


def _log(purpose, model, usage, seconds, error, usd=None, provider=None, reasoning=None, seen=None) -> None:
    """One line per judge call in judge_usage.jsonl: Claude calls are priced from their tokens (cloudllm.PRICES),
    OpenRouter calls carry OpenRouter's own `usd`; an answer that was not usable carries what it ended with."""
    SUP_DIR.mkdir(exist_ok=True)
    usage = usage or {}
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "purpose": purpose, "model": model,
           "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
           "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
           "seconds": round(seconds, 1), "error": error}
    if usd is not None:
        rec.update(usd=usd, provider=provider, reasoning_tokens=reasoning)
    if seen is not None:
        rec["seen"] = seen
    with (SUP_DIR / "judge_usage.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


# -- the second judge ---------------------------------------------------------------------------------------------
# Another model family grades a sample of cycles blind, so an instance can tell the main judge's idiosyncrasies
# from what its tool log shows (wren, 2026-10-13; the operator chose GLM 5.3 Flash). Through OpenRouter, under the same
# data policy as the instances' cloud models (cloudllm.OPENROUTER_PROVIDER: Novita only, no data collection or
# retention). Novita does not offer structured outputs for this model, so the schema is in the instructions and
# the answer is checked here.
JUDGE2_MODEL, _ = knob(
    "SHAFT_JUDGE2_MODEL", "openrouter/z-ai/glm-5.3-flash", openrouter_model, "the second judge, which grades a "
    "sample of cycles again to measure the judges' agreement", group="Judging", services=("runs", "api"))
JUDGE2_EVERY, _ = knob(
    "SHAFT_JUDGE2_EVERY", "4", whole, "the second judge grades one cycle in this many (0: never)", group="Judging",
    services=("runs", "api"))
# Its hidden reasoning shares this ceiling: measured over 84 calls (2026-10-14/15) p50 6.3k, p90 12.2k, max 16.5k
# reasoning tokens, and 22.9k in one replay — 16000 left no room. At ~47 tokens/s (Novita), 32000 is also about
# what fits in the 600 s HTTP timeout (cloudllm._send): a higher ceiling would trade a length re-ask for a timeout.
JUDGE2_MAX_TOKENS = 32000


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
                t == "integer" and (not isinstance(v, int) or isinstance(v, bool)) or \
                t == "number" and (not isinstance(v, (int, float)) or isinstance(v, bool)) or \
                t == "array" and not isinstance(v, list) or t == "object" and not isinstance(v, dict):
            return f"{k} is not a {t}"
        if "enum" in spec and v not in spec["enum"]:
            return f"{k}={v!r} not in {spec['enum']}"
    return ""


def _json_in(text: str) -> object:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]) if start >= 0 and end > start else json.loads(text)


def ask_second(system: str, prompt: str, schema: dict, purpose: str) -> dict:
    """One blind judgment by the second judge, checked against `schema`."""
    return _ask_openrouter(JUDGE2_MODEL, system, prompt, schema, purpose, JUDGE2_MAX_TOKENS, None, label="second judge")


def _ask_openrouter(model, system, prompt, schema, purpose, max_tokens, effort, label=None) -> dict:
    """A judgment through OpenRouter, checked against `schema`. An answer cut off for length is never parsed nor
    shown back: it is asked again once with twice the room. A finished answer that does not match is shown back to
    the model with what is wrong, and the whole object asked for again: GLM 5.3 Flash ended its JSON after
    difficulty_reason in 14 of 84 attempts (2026-10-14/15), having settled novelty in its reasoning; asked blind
    again it re-judged the case from scratch (2-4 min) and dropped it as often, while shown its answer it returned
    the whole object, the other fields unchanged, in about a minute."""
    from . import cloudllm
    label = label or f"judge {model}"
    key = secrets_env().get("OPENROUTER_API_KEY")
    if not key:
        raise JudgeError(f"no OPENROUTER_API_KEY in {SUP_DIR.parent / '.env'}; the {label} is unavailable")
    fields = json.dumps(schema["properties"], ensure_ascii=False)
    instructions = (f"{system}\n\nAnswer with ONE JSON object and nothing else, with exactly these fields "
                    f"(JSON schema of each): {fields}")
    retried_length, problem, unusable = False, "", ""
    for attempt in range(3):
        started = time.monotonic()
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
        if problem:
            if unusable.strip():
                messages.append({"role": "assistant", "content": unusable})
            messages.append({"role": "user", "content": f"That answer is not usable: {problem}. Answer again with "
                                                        f"the whole JSON object, every one of these fields: "
                                                        f"{', '.join(schema['properties'])}."})
        body = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if effort:
            body["reasoning"] = {"effort": effort}
        data, error = cloudllm._send(cloudllm._openrouter_request(body, key, stream=False))
        usage = (data or {}).get("usage") or {}
        cost = float(usage.get("cost") or 0)
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if error or not data:
            _log(purpose, model, usage, time.monotonic() - started, error or "no response", cost, None, reasoning)
            raise JudgeError(f"{label}: {error}{cloudllm._policy_hint(error or '')}")
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":       # no answer yet: never parsed, asked once with more room
            _log(purpose, model, usage, time.monotonic() - started, f"length at {max_tokens}", cost,
                 data.get("provider"), reasoning)
            if retried_length:
                raise JudgeError(f"{label} ran out of room twice (max_tokens {max_tokens})")
            retried_length, max_tokens = True, max_tokens * 2
            continue
        text = (choice.get("message") or {}).get("content") or ""
        try:
            out = _json_in(text)
            problem = _valid(out, schema)
        except ValueError:
            problem = "not valid JSON"
        _log(purpose, model, usage, time.monotonic() - started, problem or None, cost, data.get("provider"), reasoning,
             seen=text[-SEEN_CHARS:] if problem else None)
        if not problem:
            return out
        unusable = text
    raise JudgeError(f"{label} gave no usable answer: {problem}")
