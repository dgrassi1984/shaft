"""Metered, non-streaming model consultations on an instance's separate allowance."""
from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import date, datetime

from . import cloudllm, ledger
from .core import NETWORK_ERRORS, SUP_DIR, dollars, json_object, knob, model_names, positive_int

MODELS, _ = knob(
    "SHAFT_CONSULT_MODELS", "openrouter/anthropic/claude-opus-5.5,openrouter/anthropic/claude-sonnet-5.5,"
    "openrouter/moonshotai/kimi-k3.1-pro,openrouter/google/gemini-4-argon,openrouter/openai/gpt-6.1-sol,"
    "openrouter/openai/gpt-6.1-terra,openrouter/meta/muse-spark-1.3,openrouter/z-ai/glm-5.3,"
    "openrouter/minimax/minimax-m3.1-flash", model_names, "models on the instance's consult menu",
    group="Models and spend", services=("runs", "api", "web"))
DAILY_USD, _ = knob(
    "SHAFT_CONSULT_DAILY_USD", "3", dollars, "dollars per day per instance for model consultations",
    group="Models and spend", services=("runs", "api", "web"))
PROVIDER, _ = knob(
    "SHAFT_CONSULT_PROVIDER", '{"data_collection": "deny", "zdr": true, "require_parameters": true}', json_object,
    "OpenRouter consult provider routing and data policy (JSON)", group="Models and spend", services=("runs", "api", "web"))
MAX_TOKENS, _ = knob(
    "SHAFT_CONSULT_MAX_TOKENS", "8000", positive_int, "maximum output tokens per model consultation",
    group="Models and spend", services=("runs", "api"))
USAGE = SUP_DIR / "consult_usage.jsonl"


class ConsultError(cloudllm.CloudError):
    pass


def daily_cap(instance: str) -> float:
    from .core import _tool_settings
    own = _tool_settings(instance).get("consult_daily_usd") if instance.replace("-", "").isalnum() else None
    return float(own) if isinstance(own, (int, float)) and not isinstance(own, bool) and 0 <= own < math.inf else DAILY_USD


def spent_today(instance: str) -> float:
    today = date.today().isoformat()
    return sum(usd for ts, usd in ledger.spend(USAGE, instance) if ts.startswith(today))


@contextmanager
def editing_lock():
    """One lock for checking, reserving, settling and deleting consult spend across processes."""
    SUP_DIR.mkdir(parents=True, exist_ok=True)
    with (SUP_DIR / "consult.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _reservations() -> list[dict]:
    try:
        rows = json.loads((SUP_DIR / "consult_reservations.json").read_text())
    except FileNotFoundError:
        return []
    return [r for r in rows if time.time() - r["created"] <= 1800]


def _save_reservations(rows: list[dict]) -> None:
    path = SUP_DIR / "consult_reservations.json"
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rows))
    os.replace(tmp, path)


def _reserved(rows: list[dict], instance: str) -> float:
    return sum(r["usd"] for r in rows if r["instance"] == instance)


_refreshing = threading.Lock()


def _catalogue(fetch: bool = True) -> dict | None:
    """Prices and zero-retention providers from OpenRouter, cached a day. fetch=False (the envelope, which the
    template asks with a 2 s timeout) never waits on the network: it answers from the cache, stale or not, and
    refreshes a stale or missing one in the background; None until there is any."""
    path = SUP_DIR / "consult_catalogue.json"
    cached = None
    try:
        cached = json.loads(path.read_text())
        if not isinstance(cached["models"], list) or not isinstance(cached["endpoints"], list):
            raise ValueError("invalid catalogue")
        if time.time() - cached["fetched"] < 86400:
            return cached
    except (OSError, ValueError, KeyError, TypeError):
        cached = None
    if not fetch:
        if _refreshing.acquire(blocking=False):
            def refresh():
                try:
                    _catalogue()
                except ConsultError:
                    pass
                finally:
                    _refreshing.release()
            threading.Thread(target=refresh, daemon=True).start()
        return cached
    try:
        def fetch(suffix):
            with urllib.request.urlopen("https://openrouter.ai/api/v1/" + suffix, timeout=30) as resp:
                rows = json.load(resp)["data"]
            if not isinstance(rows, list):
                raise ValueError("catalogue data must be a list")
            return rows
        fresh = {"fetched": time.time(), "models": fetch("models"), "endpoints": fetch("endpoints/zdr")}
        SUP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(fresh))
        os.replace(tmp, path)
        return fresh
    except (*NETWORK_ERRORS, ValueError, KeyError, TypeError) as e:
        if cached is not None:
            return cached
        raise ConsultError(503, f"consult catalogue unavailable; no cached prices or providers: {e}") from e


def _price(value):
    try:
        n = float(value)
        return n * 1e6 if 0 <= n < math.inf else None
    except (ValueError, TypeError):
        return None


def menu(instance: str, fetch: bool = True) -> list[dict]:
    catalogue = _catalogue(fetch)
    if catalogue is None:   # the envelope before the first catalogue has loaded
        return [{"model": name, "in_per_m": None, "out_per_m": None, "available": False,
                 "why": "prices not loaded yet (they load in the background; ask again in a minute)"} for name in MODELS]
    models = {m["id"]: m for m in catalogue["models"]}
    result = []
    for name in MODELS:
        model = models.get(name.removeprefix("openrouter/"), {})
        ids = {model.get("id"), model.get("canonical_slug")} - {None}
        endpoints = [e for e in catalogue["endpoints"] if e.get("model_id") in ids]
        if PROVIDER.get("only"):
            endpoints = [e for e in endpoints if e.get("tag") in PROVIDER["only"]
                         or e.get("provider_name") in PROVIDER["only"]]
        endpoints = [e for e in endpoints if e.get("tag") not in PROVIDER.get("ignore", [])
                     and e.get("provider_name") not in PROVIDER.get("ignore", [])]
        pricing = model.get("pricing") or {}
        pin, pout = _price(pricing.get("prompt")), _price(pricing.get("completion"))
        # Reserve at the most expensive qualifying endpoint, rather than the catalogue's lowest price.
        for endpoint in endpoints:
            prices = endpoint.get("pricing") or {}
            a, b = _price(prices.get("prompt")), _price(prices.get("completion"))
            if a is None or b is None:
                pin = pout = None
                break
            if pin is not None and pout is not None:
                pin, pout = max(pin, a), max(pout, b)
        why = "no qualifying zero-retention provider under SHAFT_CONSULT_PROVIDER" if not endpoints else ""
        if pin is None or pout is None:
            why = "unknown price; the daily cap cannot count this model"
        result.append({"model": name, "in_per_m": pin, "out_per_m": pout, "available": not why, "why": why})
    return result


def envelope(instance: str) -> dict:
    models = menu(instance, fetch=False)
    with editing_lock():
        return {"spent_usd": spent_today(instance), "reserved_usd": _reserved(_reservations(), instance),
                "cap_usd": daily_cap(instance), "models": models}


def complete(instance: str, body: dict, secrets: dict, caller, run, run_started) -> dict:
    model = body.get("model")
    if model not in MODELS:
        raise ConsultError(403, f"model {model!r} is not on the consult menu (SHAFT_CONSULT_MODELS)")
    if body.get("stream"):
        raise ConsultError(400, "consults do not support stream: true")
    if body.get("models") or body.get("n", 1) != 1:
        raise ConsultError(400, "consults require one menu model and one completion")
    if body.get("plugins") or body.get("web_search_options"):   # billed on top of tokens: the reservation cannot see it
        raise ConsultError(400, "consults take no OpenRouter plugins or web search (billed beyond the reserved "
                                "tokens); search with my own tools and send what I found")
    item = next(m for m in menu(instance) if m["model"] == model)
    if not item["available"]:
        raise ConsultError(403, f"{model}: {item['why']}")
    key = secrets.get("OPENROUTER_API_KEY")
    if not key:
        raise ConsultError(503, "no OPENROUTER_API_KEY configured")
    tokens = body.get("max_tokens", MAX_TOKENS)
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
        raise ConsultError(400, "max_tokens must be a positive integer")
    tokens = min(tokens, MAX_TOKENS)
    payload = {k: v for k, v in body.items() if k not in ("provider", "stream_options", "max_completion_tokens")}
    payload.update(model=model.removeprefix("openrouter/"), max_tokens=tokens, provider=PROVIDER,
                   usage={"include": True})
    prompt = json.dumps({k: v for k, v in payload.items() if k not in ("provider", "usage")}, ensure_ascii=False)
    worst = (len(prompt) / 3 * item["in_per_m"] + tokens * item["out_per_m"]) / 1e6
    reservation = uuid.uuid4().hex
    with editing_lock():
        rows = _reservations()
        spent, reserved, cap = spent_today(instance), _reserved(rows, instance), daily_cap(instance)
        if spent + reserved + worst > cap:
            raise ConsultError(429, f"consult allowance: spent ${spent:.6f}, reserved ${reserved:.6f}, this call's "
                               f"worst case ${worst:.6f}, cap ${cap:.2f}; the operator can raise it with "
                               f"shaft limits {instance} consult-cap <dollars>")
        rows.append({"id": reservation, "instance": instance, "usd": worst, "created": time.time()})
        _save_reservations(rows)
    started, data, error, cost, usage = time.monotonic(), None, None, worst, {}
    try:
        req = urllib.request.Request(cloudllm.OPENROUTER_API, data=json.dumps(payload).encode(), headers={
            "content-type": "application/json", "authorization": f"Bearer {key}", "x-title": "SHAFT"})
        data, error = cloudllm._send(req)
        usage = (data or {}).get("usage") or {}
        reported = usage.get("cost")
        if isinstance(reported, (int, float)) and not isinstance(reported, bool) and 0 <= reported < math.inf:
            cost = reported
        if data is None and str(error or "").startswith("HTTP 4") and not str(error).startswith("HTTP 429"):
            cost = 0.0   # refused outright: nothing was generated (a timeout or 5xx may have been, so those keep the worst case)
        if data is None or not data.get("choices"):
            raise ConsultError(502, f"OpenRouter consult failed: {error or json.dumps(data)[:400]}")
    except Exception as e:
        error = str(e)
        raise
    finally:
        with editing_lock():
            try:
                row = {"ts": datetime.now().isoformat(timespec="seconds"), "instance": instance, "model": model,
                       "caller": str(caller or "")[:60], "run": run, "run_started": run_started,
                       "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                       "usd": cost, "reserved_usd": worst, "seconds": round(time.monotonic() - started, 1),
                       "error": error}
                with USAGE.open("a") as f:
                    f.write(json.dumps(row) + "\n")
            finally:
                rows = [r for r in _reservations() if r["id"] != reservation]
                _save_reservations(rows)
            shaft = {"usd": cost, "spent_usd": spent_today(instance), "reserved_usd": _reserved(rows, instance),
                     "cap_usd": daily_cap(instance)}
    return {**data, "shaft": shaft}
