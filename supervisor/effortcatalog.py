"""Provider models and reasoning levels, with a shared atomic disk cache."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from . import core

ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")
HOURS, _ = core.knob("SHAFT_EFFORT_CATALOG_HOURS", "24", core.positive_int,
    "hours before refreshing live reasoning levels and plan channels' model lists", group="Models and spend",
    services=("runs", "api", "web"))
VERSION = 1
_locks = defaultdict(threading.Lock)
_failures = {}


def entry(levels: list[str], default: str | None, hidden=False) -> dict:
    if not isinstance(levels, list) or not all(isinstance(level, str) for level in levels):
        raise ValueError("invalid reasoning levels")
    ordered = [level for level in ORDER if level in levels]
    return {"levels": ordered, "default": default if default in ordered else None, "hidden": bool(hidden)}


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


def _cached(path, provider):
    catalog = _read(path).get(provider, {})
    try:
        models = {key: entry(value["levels"], value.get("default"), value.get("hidden", False))
                  for key, value in catalog["models"].items()}
    except (KeyError, ValueError, TypeError, AttributeError):
        return None, False
    try:
        stamp = datetime.fromisoformat(catalog["fetched_at"])
        fresh = (catalog.get("version") == VERSION
                 and all("hidden" in value for value in catalog["models"].values())
                 and (datetime.now(timezone.utc) - stamp).total_seconds() < HOURS * 3600)
    except (KeyError, ValueError, TypeError):
        fresh = False
    return models, fresh


def _refresh(path, provider, fetch, models):
    failure_key = (str(path), provider)
    try:
        models_new = {key: entry(value["levels"], value.get("default"), value.get("hidden", False))
                      for key, value in fetch().items()}
        if not models_new:               # an empty list is a failed listing: cached, it would empty the menu for hours
            raise ValueError("the provider listed no models")
        _write(path, provider, {"version": VERSION, "fetched_at": datetime.now(timezone.utc).isoformat(),
                                "models": models_new})
        _failures.pop(failure_key, None)
        return models_new
    except Exception as exc:
        if failure_key not in _failures:
            core.log(f"{provider} effort catalog refresh failed: {type(exc).__name__}: {exc}")
        _failures[failure_key] = time.monotonic()
        return models


def _background(path, provider, fetch, models, lock):
    try:
        _refresh(path, provider, fetch, models)
    finally:
        lock.release()


def _load(provider, fetch, *, refresh=True, background=False):
    path = core.SUP_DIR / "effort_catalog.json"
    models, fresh = _cached(path, provider)
    if fresh or not refresh:
        return models
    lock = _locks[provider]
    # Menu reads must not wait behind either a background refresh or an effort lookup.
    if not lock.acquire(blocking=not (background and models is not None)):
        return models
    try:
        models, fresh = _cached(path, provider)
        failed_at = _failures.get((str(path), provider))
        if fresh or failed_at is not None and time.monotonic() - failed_at < 60:
            return models
        if background and models is not None:
            threading.Thread(target=_background, args=(path, provider, fetch, models, lock), daemon=True).start()
            lock = None  # The worker releases it after writing the catalog.
            return models
        return _refresh(path, provider, fetch, models)
    finally:
        if lock is not None:
            lock.release()


def listed(provider: str, fetch: Callable[[], dict], *, refresh=True) -> list[str] | None:
    models = _load(provider, fetch, refresh=refresh, background=True)
    return [key for key, value in models.items() if not value["hidden"]] if models is not None else None


def lookup(provider: str, model: str, fetch: Callable[[], dict]) -> tuple[tuple[str, ...], str | None] | None:
    models = _load(provider, fetch)
    found = models.get(model) if models is not None else None
    return (tuple(found["levels"]), found["default"]) if found is not None else None
