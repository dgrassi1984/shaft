"""The cloud judge: models that grade instances, never live their lives.

A rotation (Opus 5.5 was too costly; "stochastic rotation I prefer"): every judgment picks one
model at random from SHAFT_JUDGE_MODELS, never the model the judged instance runs on (no self-preference — wisp runs
on MiMo). Haiku 5.5 left on 2026-10-16: on the same cycles it called 54% of wisp's claims unsupported where the blind
GLM judge called 4%, Sonnet 21% and Opus 17%. Sonnet 5.5 left on 2026-10-20 for cost
($12-25 a day); MiMo and GLM 5.3 Flash since. DeepSeek V4.1 Flash is reserved for the independent second judge.
The main judge grades one live run in SHAFT_JUDGE_EVERY. Every verdict records the model that gave it. Every judge
call is modelcall.ask, whatever the provider: Claude models go straight to the Claude Messages API with the
supervisor's key (structured JSON output via output_config.format; refusals re-run server-side with
`fallbacks: "default"`); "openrouter/…" models go through OpenRouter under the instances' data policy
(cloudllm.OPENROUTER_PROVIDER: no data collection, zero retention), "custom/…" models to the operator's endpoint at
their declared prices and "codex/…" models to a ChatGPT plan, each with the schema in the instructions and the
answer checked here (_valid). Muse Spark was left out: its only provider keeps prompts. An operator whose one provider
offers one model has one judge: the instance's own model then grades it, and the verdict says so
(independence: same-model) — never as a stand-in for a judge whose key is missing.
"""
from __future__ import annotations

import json
import random
import time

from . import modelcall
from .core import SUP_DIR, any_model, any_models, knob, positive_int, whole

JUDGE_MODELS, JUDGE_SRC = knob(
    "SHAFT_JUDGE_MODELS",
    "openrouter/xiaomi/mimo-v2.6-pro,openrouter/z-ai/glm-5.3-flash",
    any_models, "the judge pool: picks at random, excluding the instance's model and the second judge",
    group="Judging", services=("runs", "api", "web"))   # web: its stats count verdicts by the judges in the pool
# Every live run was judged: 250+ a day at ~$0.07 a Sonnet judgment cost $12-25 a day (2026-10-17/19, #45). Which
# runs are judged is drawn at random when a run is first up for judging, and a run left out is recorded apart
# (metrics.judge_sampled), so nothing the instance can see tells it beforehand which of its runs will be graded. A run
# left out has no verdict: not judged, never a failure.
JUDGE_EVERY, _ = knob(
    "SHAFT_JUDGE_EVERY", "2", positive_int, "the main judge grades one live run in this many (1: every run)",
    group="Judging", services=("runs", "api"))          # api: record(kind='judgments') in a conversation says so


class JudgeError(Exception):
    pass


# A conversation turn the operator spoke (its talk episode says `voice`): the text is what speech
# recognition heard on the device. Every judge prompt that shows such a turn marks it, so a misheard word ("music" for
# "mutex") is not taken for what the operator meant.
SPOKEN = "spoken: transcribed speech, may contain recognition errors"


def spoken(turn: dict) -> str:
    """The mark a judge sees after the speaker of a talk turn: SPOKEN for a spoken one, nothing for a typed one."""
    return f" ({SPOKEN})" if turn.get("voice") is True else ""


def _norm(model: str) -> str:
    """'openrouter/xiaomi/mimo-v2.6-pro', 'xiaomi/mimo-v2.6-pro' and 'anthropic/claude-haiku-5.5' compare by model."""
    from . import planchannel
    m = planchannel.unprefix((model or "").strip().lower().removeprefix("openrouter/"))
    m = m.removeprefix("openai/").removeprefix("anthropic/").removeprefix("x-ai/").removeprefix("xai/")
    return m.replace(".", "-")


def same_model(a: str | None, b: str | None) -> bool:
    return bool(a and b) and _norm(a) == _norm(b)


class NoJudgeLeft(JudgeError):
    """Every judge of a pool of several models is one of the judged instance's own models: this judgment cannot be
    made, others can."""


def pick(exclude=(), rng=random, avoid=()) -> str:
    """One judge for one judgment: at random from the pool, never one of `exclude` (the judged instance's models).
    The second judge's model is left out of the main rotation, and `avoid` too, while another judge is left: the
    cross-check stays independent. A pool that still lists it (a SHAFT_JUDGE_MODELS saved before 2026-11-03) uses it
    only when no other judge may grade; that run then gets no second judgment (metrics.judge_pending).
    A pool of one model that is the instance's own (the operator's one provider offers one model) is that model:
    the caller records independence() with the verdict. A pool of several models, all the instance's, has no judge."""
    pool = [m for m in JUDGE_MODELS if not any(same_model(m, x) for x in exclude)]
    if not pool and JUDGE_MODELS and all(same_model(m, JUDGE_MODELS[0]) for m in JUDGE_MODELS):
        return JUDGE_MODELS[0]
    main = [m for m in pool if not same_model(m, JUDGE2_MODEL)]
    pool = [m for m in main if not any(same_model(m, x) for x in avoid)] or main or pool
    if not pool:
        raise NoJudgeLeft(f"no judge left in SHAFT_JUDGE_MODELS={JUDGE_MODELS} once the instance's own models "
                          f"{[x for x in exclude if x]} are left out: while the pool holds other models, an instance "
                          f"is never judged by its own model")
    return rng.choice(pool)


def independence(model: str, exclude=()) -> dict:
    """What a verdict records about its judge's independence: {"independence": "same-model"} when the judge is one of
    the judged instance's own models (pick: a one-model pool), else nothing."""
    return {"independence": "same-model"} if any(same_model(model, x) for x in exclude) else {}


def rotation() -> str:
    """The pool as one label: what a battery era is graded under."""
    return "rotation:" + ",".join(m for m in JUDGE_MODELS if not same_model(m, JUDGE2_MODEL))


def unavailable() -> str | None:
    """None when every model of the rotation can be called, else why one cannot (a missing one would fail a share of
    judgments at random). The rotation is pick()'s: the pool less the second judge, or the whole pool when that is
    all it holds (an operator whose one provider offers one model)."""
    pool = [m for m in JUDGE_MODELS if not same_model(m, JUDGE2_MODEL)] or list(JUDGE_MODELS)
    if not pool:
        return "SHAFT_JUDGE_MODELS is empty"
    for m in pool:
        if (reason := modelcall.available(m)):
            return f"judge {m}: {reason}"
    return None


def available() -> bool:
    return unavailable() is None


def ask(system: str, prompt: str, schema: dict, purpose: str, max_tokens: int = 16000,
        effort: str = "medium", model: str | None = None, exclude=()) -> dict:
    """One judgment by `model` (else one picked from the rotation, never one of `exclude`). Returns the parsed JSON
    object matching `schema`. Callers that record who judged pick() first and pass the model. The call is modelcall's,
    for every provider; it owns the length retries: an answer cut off at `max_tokens` is asked once more with twice
    the room, then fails; callers add none of their own (a trial pair's judgment asked 16k+32k+32k+64k when both
    layers retried, review E5)."""
    model = model or pick(exclude)
    return _ask(model, system, prompt, schema, purpose, max_tokens, effort, label=f"judge {model}")


def _ask(model, system, prompt, schema, purpose, max_tokens, effort, label) -> dict:
    try:
        answer = modelcall.ask(model, system, [{"role": "user", "content": prompt}], max_tokens=max_tokens,
                               effort=effort, schema=schema, purpose=purpose, check=_valid,
                               log=lambda c: _log(purpose, c.model, c.usage, c.seconds, c.error, c.usd, c.provider,
                                                  c.reasoning, c.seen))
    except modelcall.ModelCallError as e:
        raise JudgeError(f"{label}: {e}") from e
    out = json.loads(answer.text)
    normalize_anticipated(out)
    return out


def _log(purpose, model, usage, seconds, error, usd=None, provider=None, reasoning=None, seen=None) -> None:
    """One line per judge call in judge_usage.jsonl: Claude calls are priced from their tokens (cloudllm.PRICES),
    OpenRouter calls carry OpenRouter's own `usd`, custom/ calls their declared price, codex/ calls plan usage
    ($0, `plan` true); an answer that was not usable carries what it ended with."""
    SUP_DIR.mkdir(exist_ok=True)
    usage = usage or {}
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "purpose": purpose, "model": model,
           "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
           "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
           "seconds": round(seconds, 1), "error": error}
    if model.startswith("claude-"):
        from . import cloudllm
        rec.update(cloudllm.billed(usage))
        usd = cloudllm.usd(model, rec)
    else:
        rec.update(provider=provider, reasoning_tokens=reasoning)   # kept when OpenRouter's usage had no cost too
        if usage.get("plan"):
            rec["plan"] = True
        if usage.get("estimated"):
            rec["estimated"] = True
    rec.update(usd=usd, unpriced=usd is None)
    if seen is not None:
        rec["seen"] = seen
    with (SUP_DIR / "judge_usage.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


# -- the second judge ---------------------------------------------------------------------------------------------
# Another model family grades a sample of cycles blind, so an instance can tell the main judge's idiosyncrasies
# from what its tool log shows (wisp, 2026-10-13). GLM 5.3 Flash until 2026-10-20, when the operator made it a main
# judge too and it graded four runs twice, agreeing with itself (#45); DeepSeek V4.1 Flash since, which no instance runs
# on. Its model is left out of the main rotation (pick: there only when no other judge may grade, then that run gets
# no second judgment), so the cross-check stays an independent family; the agreement
# in stats.md counts only the current second judge against another model (metrics._second_section). By default
# through OpenRouter, under the same data policy as the instances' cloud models (cloudllm.OPENROUTER_PROVIDER: Novita,
# DeepInfra if Novita is down, no data collection or retention); any model modelcall can call may be it (a custom/ or
# codex/ one for an operator with only that provider). Off Anthropic the schema is in the instructions and the answer
# is checked here (_valid).
JUDGE2_MODEL, _ = knob(
    "SHAFT_JUDGE2_MODEL", "openrouter/deepseek/deepseek-v4.1-flash", any_model, "the second judge, which "
    "grades a sample of cycles again to measure the judges' agreement", group="Judging", services=("runs", "api"))
JUDGE2_EVERY, _ = knob(
    "SHAFT_JUDGE2_EVERY", "4", whole, "the second judge grades one cycle in this many (0: never)", group="Judging",
    services=("runs", "api"))
# Its hidden reasoning shares this ceiling: measured on GLM 5.3 Flash over 84 calls (2026-10-14/15) p50 6.3k, p90
# 12.2k, max 16.5k reasoning tokens, and 22.9k in one replay — 16000 left no room. At ~47 tokens/s (Novita), 32000
# is also about what fits in the 600 s HTTP timeout (cloudllm._send): a higher ceiling would trade a length re-ask for a timeout.
JUDGE2_MAX_TOKENS = 32000


def second_available() -> bool:
    return JUDGE2_EVERY > 0 and modelcall.available(JUDGE2_MODEL) is None


def normalize_anticipated(verdict: object) -> None:
    """Keep this optional observation only when the model supplied a boolean."""
    if isinstance(verdict, dict) and not isinstance(verdict.get("anticipated"), bool):
        verdict.pop("anticipated", None)


def _valid(out: object, schema: dict) -> str:
    """'' if `out` matches the (flat) schema, else what is wrong (modelcall.valid). The optional `anticipated` is kept
    only as a boolean and never asked for again."""
    if not isinstance(out, dict):
        return "not a JSON object"
    normalize_anticipated(out)
    props = {k: v for k, v in schema["properties"].items() if k != "anticipated"}
    return modelcall.valid(out, {**schema, "properties": props})


def ask_second(system: str, prompt: str, schema: dict, purpose: str) -> dict:
    """One blind judgment by the second judge, checked against `schema`."""
    return _ask(JUDGE2_MODEL, system, prompt, schema, purpose, JUDGE2_MAX_TOKENS, None, label="second judge")
