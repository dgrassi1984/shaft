"""`shaft provider use|check`: the Mac app's Model step, one switch for a fresh install.

`use openrouter|codex|custom` writes one consistent preset of every model knob the supervisor itself calls (judges,
coder, consults, describe_image, recap, the fallbacks) through settings.set_values — one write, each value checked by
its knob's own parser — and says which functions the provider covers and which stay off, with the reason.
`check PROVIDER` makes one minimal modelcall.ask per distinct configured model of that provider and says what the
provider really answered. Neither verb touches the project .env: keys are set by `shaft secrets`.

A preset covers a function when every model it runs on passes modelcall.available (key, login, price, endpoint). A
function the provider cannot serve is left off — its knob unset, so it falls back to the default — and named in `off`
with the reason, never papered over with a model the operator has no key for.
"""
from __future__ import annotations

import json
import sys

from . import cloudllm, core, modelcall, settings
from .core import NO_FALLBACK

PROVIDERS = ("openrouter", "codex", "custom")
CODEX_DEFAULT = "codex/gpt-6-astra"
# function -> the knobs whose models serve it
FUNCTIONS = {"instances": ("SHAFT_CLOUD_MODELS",), "judge": ("SHAFT_JUDGE_MODELS",), "judge2": ("SHAFT_JUDGE2_MODEL",),
             "coder": ("SHAFT_CODER_MODEL",), "consult": ("SHAFT_CONSULT_MODELS",), "describe": ("SHAFT_DESCRIBE_MODEL",),
             "recap": ("SHAFT_RECAP_MODEL",), "trial": ("SHAFT_TRIAL_FALLBACK_MODEL",)}
MODEL_KNOBS = sorted({k for ks in FUNCTIONS.values() for k in ks} | {"SHAFT_CODEX_FALLBACK_MODEL"})
CHECK_KNOBS = {"SHAFT_CLOUD_MODELS": ("cloudllm", "CLOUD_MODELS"), "SHAFT_JUDGE_MODELS": ("judge", "JUDGE_MODELS"),
               "SHAFT_JUDGE2_MODEL": ("judge", "JUDGE2_MODEL"), "SHAFT_CODER_MODEL": ("coder", "CODER_MODEL"),
               "SHAFT_CONSULT_MODELS": ("consult", "MODELS"), "SHAFT_DESCRIBE_MODEL": ("broker", "DESCRIBE_MODEL"),
               "SHAFT_RECAP_MODEL": ("recap", "MODEL"), "SHAFT_CODEX_FALLBACK_MODEL": ("codexcli", "FALLBACK_MODEL"),
               "SHAFT_TRIAL_FALLBACK_MODEL": ("trial", "FALLBACK_MODEL")}


class Usage(Exception):
    """A missing or contradictory argument (exit 2)."""


def _say(as_json: bool, ok: bool, text: str = "", **extra) -> int:
    if as_json:
        print(json.dumps({"ok": ok, **({"error": text} if not ok else {}), **extra}))
    elif text:
        print(text, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


# -- the presets: {knob: value}, None = unset (the default) -----------------------------------------------------------
OPENROUTER_INSTANCES = ("openrouter/xiaomi/mimo-v2.6-pro", "openrouter/anthropic/claude-sonnet-5.5",
                        "openrouter/anthropic/claude-opus-5.5")
OPENROUTER_CODER = "openrouter/anthropic/claude-opus-5.5"
OPENROUTER_SEEING = "openrouter/anthropic/claude-sonnet-5.5"       # describe and recap


def _openrouter(model: str | None) -> dict:
    """Every model on OpenRouter alone (an operator with only its key): the knobs' OpenRouter defaults are written
    out, and the ones whose default is a claude-… model (Anthropic's own API) get their OpenRouter equivalent."""
    default = lambda k: core.KNOBS[k].default
    return {"SHAFT_CLOUD_MODELS": ",".join(dict.fromkeys([*OPENROUTER_INSTANCES, *([model] if model else [])])),
            "SHAFT_JUDGE_MODELS": default("SHAFT_JUDGE_MODELS"), "SHAFT_JUDGE2_MODEL": default("SHAFT_JUDGE2_MODEL"),
            "SHAFT_CODER_MODEL": OPENROUTER_CODER, "SHAFT_CONSULT_MODELS": default("SHAFT_CONSULT_MODELS"),
            "SHAFT_DESCRIBE_MODEL": OPENROUTER_SEEING, "SHAFT_RECAP_MODEL": OPENROUTER_SEEING,
            "SHAFT_CODEX_FALLBACK_MODEL": default("SHAFT_CODEX_FALLBACK_MODEL"),
            "SHAFT_TRIAL_FALLBACK_MODEL": default("SHAFT_TRIAL_FALLBACK_MODEL")}


def _codex(model: str | None) -> tuple[dict, dict]:
    from . import codexcli
    model = model or CODEX_DEFAULT
    others = [m for m in codexcli.MODELS if m != model] or [model]
    out = {"SHAFT_JUDGE_MODELS": ",".join(others), "SHAFT_JUDGE2_MODEL": others[0], "SHAFT_CODER_MODEL": model,
           "SHAFT_CONSULT_MODELS": ",".join(codexcli.MODELS), "SHAFT_RECAP_MODEL": model,
           "SHAFT_CODEX_FALLBACK_MODEL": NO_FALLBACK, "SHAFT_TRIAL_FALLBACK_MODEL": model,
           }
    why = _reads_images(model)
    out["SHAFT_DESCRIBE_MODEL"] = NO_FALLBACK if why else model
    return out, ({"describe": why} if why else {})


def _reads_images(model: str) -> str | None:
    try:
        return modelcall.reads_images(model)
    except Exception as e:
        return f"{model}: {e}"


def _custom(model: str, judge: str | None) -> tuple[dict, dict]:
    judge = judge or model
    seeing = next((m for m in (model, judge) if modelcall.reads_images(m) is None), None)
    forced = {} if seeing else {"describe": modelcall.reads_images(model)}
    return {"SHAFT_CLOUD_MODELS": model, "SHAFT_JUDGE_MODELS": judge, "SHAFT_JUDGE2_MODEL": judge,
            "SHAFT_CODER_MODEL": model, "SHAFT_CONSULT_MODELS": ",".join(dict.fromkeys((model, judge))),
            "SHAFT_RECAP_MODEL": model, "SHAFT_DESCRIBE_MODEL": seeing or NO_FALLBACK, "SHAFT_CODEX_FALLBACK_MODEL": NO_FALLBACK,
            "SHAFT_TRIAL_FALLBACK_MODEL": NO_FALLBACK}, forced


def _models(knob: str, value: str | None) -> list[str]:
    raw = core.KNOBS[knob].default if value is None else value
    return [m for m in (s.strip() for s in raw.split(",")) if m and m != NO_FALLBACK]


# Functions that work when at least one of their models does: the instances' menu and the consult menu list many.
ANY_OF = ("instances", "consult")


def _coverage(preset: dict, forced: dict[str, str], skip: tuple = (), trusted_vision: bool = False) -> tuple[list[str], dict[str, str]]:
    """(covered functions, {function: reason}) for the models the preset leaves in force; `forced` are the functions
    the preset itself leaves off (their knob is `none` or unset)."""
    covers, off = [], dict(forced)
    for fn, knobs in FUNCTIONS.items():
        if fn in forced or fn in skip:
            continue
        models = [m for k in knobs for m in _models(k, preset.get(k))]
        if not models:
            continue                                   # `none`: a choice, not a gap
        why = [r for m in models if (r := modelcall.available(m))]
        reason = (why[0] if len(why) == len(models) else None) if fn in ANY_OF else (why[0] if why else None)
        if reason is None and fn == "describe" and not trusted_vision:
            reason = next((r for m in models if (r := _reads_images(m))), None)
        (off.__setitem__(fn, reason) if reason else covers.append(fn))
    return covers, off


class _Declared:
    """The custom/ endpoint, prices and vision the verb is about to write, visible to every validator and to
    modelcall.available while one set_values call checks the whole preset (they are read from the file only when a
    process starts, so writing prices and the models that need them in one call would otherwise refuse)."""

    def __init__(self, base_url: str | None, prices: dict, vision: list[str]):
        self.new = {"CUSTOM_BASE_URL": base_url or cloudllm.CUSTOM_BASE_URL,
                    "CUSTOM_PRICES": {**cloudllm.CUSTOM_PRICES, **prices},
                    "CUSTOM_VISION": list(dict.fromkeys([*cloudllm.CUSTOM_VISION, *vision]))}

    def __enter__(self):
        self.old = {k: getattr(cloudllm, k) for k in self.new}
        for k, v in self.new.items():
            setattr(cloudllm, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(cloudllm, k, v)


def _custom_name(m: str) -> str:
    return m if m.startswith("custom/") else f"custom/{m}"


def use(provider: str, model: str | None = None, judge_model: str | None = None, base_url: str | None = None,
        price_in: float | None = None, price_out: float | None = None, judge_price_in: float | None = None,
        judge_price_out: float | None = None, vision: bool = False) -> dict:
    """Write the preset; returns {"provider", "changed", "written", "covers", "off", "not_live"}. Raises
    settings.Refused (a value its knob refuses) or Usage."""
    settings.load_all()
    declared: dict = {}
    extra: dict = {}
    if provider == "custom":
        if not model:
            raise Usage("custom needs --model (the model name on the endpoint)")
        model = _custom_name(model)
        judge = _custom_name(judge_model) if judge_model else None
        if price_in is None or price_out is None:
            raise Usage("custom needs --price-in and --price-out (dollars per million tokens): an unpriced model "
                        "would spend money the daily cap cannot count")
        if not (base_url or cloudllm.CUSTOM_BASE_URL):
            raise Usage("custom needs --base-url (the OpenAI-compatible endpoint, e.g. https://host/v1)")
        declared[model.removeprefix("custom/")] = [price_in, price_out]
        if judge and judge != model and judge_price_in is not None and judge_price_out is not None:
            declared[judge.removeprefix("custom/")] = [judge_price_in, judge_price_out]
        prices = {**{m: list(p) for m, p in cloudllm.CUSTOM_PRICES.items()}, **declared}
        extra["SHAFT_CUSTOM_PRICES"] = json.dumps(prices, separators=(",", ":"))
        if base_url:
            extra["SHAFT_CUSTOM_BASE_URL"] = base_url
        seeing = [m.removeprefix("custom/") for m in ([model] if vision else [])]
        if seeing:
            extra["SHAFT_CUSTOM_VISION"] = ",".join(dict.fromkeys([*cloudllm.CUSTOM_VISION, *seeing]))
        try:
            parsed = core.custom_prices(extra["SHAFT_CUSTOM_PRICES"])
            url = core.http_url(base_url) if base_url else None
        except ValueError as e:
            raise settings.Refused(str(e)) from None
        with _Declared(url, parsed, seeing):
            preset, forced = _custom(model, judge)
            return _write(provider, {**extra, **preset}, forced)
    if judge_model or vision or any(v is not None for v in (price_in, price_out, base_url, judge_price_in,
                                                          judge_price_out)):
        raise Usage(f"--judge-model, --base-url, --price-* and --vision are for custom, not {provider}")
    if provider == "codex":
        return _write(provider, *_codex(model))
    if model and not model.startswith("openrouter/"):
        raise Usage(f"--model for openrouter is an openrouter/… model, not {model!r}")
    return _write(provider, _openrouter(model), trusted_vision=True)


def _write(provider: str, changes: dict, forced: dict | None = None, trusted_vision: bool = False) -> dict:
    preset = {k: v for k, v in changes.items() if k in MODEL_KNOBS}
    # codex: the instance's own model is chosen per instance, not by SHAFT_CLOUD_MODELS, which stays as it is
    covers, off = _coverage(preset, forced or {}, ("instances",) if provider == "codex" else (), trusted_vision)
    r = settings.set_values(changes)
    return {"provider": provider, "changed": r["changed"], "written": changes, "covers": covers, "off": off,
            "not_live": r["not_live"]}


# -- check ------------------------------------------------------------------------------------------------------------
def configured_models(provider: str) -> list[str]:
    """The distinct models of `provider` this process has configured, in knob order."""
    import importlib
    settings.load_all()
    mine = f"{provider}/"
    out: list[str] = []
    for mod, attr in CHECK_KNOBS.values():
        value = getattr(importlib.import_module(f"{__package__}.{mod}"), attr)
        for m in (value if isinstance(value, list) else [value]):
            if isinstance(m, str) and m.startswith(mine) and m not in out:
                out.append(m)
    return out


def check(provider: str) -> list[dict]:
    """One minimal live call per configured model of the provider: [{"ok", "model", "error"}]."""
    models = configured_models(provider)
    if not models:
        return [{"ok": False, "model": None, "error": f"no {provider}/ model is configured: run shaft provider use "
                                                      f"{provider}" + (" --model M" if provider != "openrouter" else "")}]
    rows = []
    for m in models:
        try:
            modelcall.ask(m, "Reply with the single word: ok.", [{"role": "user", "content": "ok?"}], max_tokens=16,
                          effort=None, purpose="provider check")
            rows.append({"ok": True, "model": m, "error": None})
        except Exception as e:                         # the provider's own words: ModelCallError, CloudError, network
            rows.append({"ok": False, "model": m, "error": str(e) or type(e).__name__})
    return rows


def command(args) -> int:
    as_json = args.json
    try:
        if args.action == "use":
            r = use(args.provider, args.model, args.judge_model, args.base_url, args.price_in, args.price_out,
                    args.judge_price_in, args.judge_price_out, args.vision)
            if as_json:
                print(json.dumps({"ok": True, **r}))
            else:
                print(f"{args.provider}: " + (f"saved {', '.join(r['changed'])}" if r["changed"] else "nothing to change"))
                print("covers: " + (", ".join(r["covers"]) or "nothing"))
                for fn, why in r["off"].items():
                    print(f"off: {fn} — {why}")
            return 0
        rows = check(args.provider)
    except Usage as e:
        _say(as_json, False, str(e))
        return 2
    except (settings.Refused, OSError, ValueError) as e:
        return _say(as_json, False, str(e) or type(e).__name__)
    for row in rows:
        print(json.dumps(row) if as_json else (f"{'ok  ' if row['ok'] else 'FAIL'} {row['model']}"
                                                + (f": {row['error']}" if row["error"] else "")))
    return 0 if all(r["ok"] for r in rows) else 1
