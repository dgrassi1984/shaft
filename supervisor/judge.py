"""The cloud judge: a stronger model that grades instances, never lives their lives.

Raw HTTP to the Claude Messages API (the supervisor stays dependency-free by design:
it is the trust anchor). Structured JSON output via output_config.format; refusals
are re-run server-side with `fallbacks: "default"`. The key comes from PROJECT/.env.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .core import SUP_DIR, knob, secrets_env

JUDGE_MODEL, JUDGE_SRC = knob("SHAFT_JUDGE_MODEL", "claude-sonnet-5-5", str)   # Daniele, 2026-10-12
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
