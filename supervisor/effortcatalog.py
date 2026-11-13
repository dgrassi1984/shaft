"""Measured provider reasoning levels, with a shared atomic disk cache."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from . import core

ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")
HOURS, _ = core.knob("SHAFT_EFFORT_CATALOG_HOURS", "24", core.positive_int,
    "hours before refreshing live model reasoning levels", group="Models and spend",
    services=("runs", "api", "web"))
_locks = {"codex": threading.Lock(), "openrouter": threading.Lock()}
_failures = {}


def entry(levels: list[str], default: str | None) -> dict:
    if not isinstance(levels, list) or not all(isinstance(level, str) for level in levels):
        raise ValueError("invalid reasoning levels")
    ordered = [level for level in ORDER if level in levels]
    return {"levels": ordered, "default": default if default in ordered else None}


def _read(path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, provider, catalog):
    with core.config_lock(path):
        data = _read(path)
        data[provider] = catalog
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="effort-catalog-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def lookup(provider: str, model: str, fetch: Callable[[], dict]) -> tuple[tuple[str, ...], str | None] | None:
    path = core.SUP_DIR / "effort_catalog.json"
    failure_key = (str(path), provider)
    with _locks[provider]:
        catalog = _read(path).get(provider, {})
        try:
            stamp = datetime.fromisoformat(catalog["fetched_at"])
            fresh = (datetime.now(timezone.utc) - stamp).total_seconds() < HOURS * 3600
            models = {key: entry(value["levels"], value.get("default"))
                      for key, value in catalog["models"].items()}
        except (KeyError, ValueError, TypeError, AttributeError):
            fresh, models = False, {}
        failed_at = _failures.get(failure_key)
        if not fresh and (failed_at is None or time.monotonic() - failed_at >= 60):
            try:
                models_new = fetch()
                catalog_new = {"fetched_at": datetime.now(timezone.utc).isoformat(), "models": models_new}
                _write(path, provider, catalog_new)
                models = models_new
                _failures.pop(failure_key, None)
            except Exception as exc:
                if failed_at is None:
                    core.log(f"{provider} effort catalog refresh failed: {type(exc).__name__}: {exc}")
                _failures[failure_key] = time.monotonic()
        found = models.get(model)
        return (tuple(found["levels"]), found["default"]) if found is not None else None
