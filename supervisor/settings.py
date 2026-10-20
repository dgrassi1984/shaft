"""The supervisor's settings (docs/plans/PLAN-webui-chat.md §4): `shaft settings`, which the web UI's Settings page runs.

Every setting is a knob (core.knob): read once when a process starts, from its environment, else the operator's
settings file (.supervisor/settings.env), else its default. The launchd services run with PATH alone in their
environment, so for them the file is what sets a knob. This module:
  - lists every knob (its value, where the value comes from, its default, what it does) — the registry is filled by
    importing the modules that call knob(), MODULES; test_settings checks no knob() call is left out;
  - writes the file: one write for several keys, under a lock, compare-and-set on what the editor saw (`expect`),
    each value checked by its own knob's parser first — a value it refuses is never written;
  - says where a saved value is not live yet: each service records the knobs it read at its start (version.py), so
    a service holding another value than the one saved must restart to pick it up. Runs (cycles, sleeps, trials, CLI
    commands) are fresh processes: they read the file when they start.
Ports, interpreter paths and endpoints (Knob.web False) are set from a terminal only, never from the web UI: a wrong
value there cuts the web UI off or breaks a service (`--from-web` refuses them).
"""
from __future__ import annotations

import importlib
import json
import os
import sys

from . import core
from .core import KNOB_GROUPS, KNOBS, SERVICE_NAMES, config_lock, log, resolve

# Every module that calls knob() at import (test_settings greps the code for knob(" calls and checks each is here).
MODULES = ("core", "api", "battery", "broker", "cloudllm", "coder", "finetune", "jobs", "judge", "localmodel",
           "metrics", "panel", "recap", "selfhosted", "telegram", "trial", "vision", "web")
VALUE_MAX = 4000
HEADER = ("# SHAFT's supervisor settings — written by `shaft settings set|unset` (and the web UI's Settings page).\n"
          "# KEY=value, the value verbatim to the end of its line (no quotes, no comments after it).\n"
          "# A process's environment wins over this file; a service reads it when it starts.\n")
RESTART = {"api": "shaft api on", "scheduler": "shaft autonomy on", "telegram": "shaft telegram on",
           "web": "shaft web on", "model": "shaft model on", "embed": "shaft embed on"}


class Refused(Exception):
    pass


def load_all() -> dict:
    """The registry, complete: every module that declares knobs, imported."""
    for m in MODULES:
        importlib.import_module(f"{__package__}.{m}")
    return KNOBS


# -- the file -------------------------------------------------------------------------------------------------------
def _lines() -> list[str]:
    try:
        return core.SETTINGS_FILE.read_text().splitlines()
    except FileNotFoundError:
        return []


def _key(line: str) -> str | None:
    k, eq, _ = line.partition("=")
    return k.strip() if eq and not line.lstrip().startswith("#") and core.SETTING_KEY.match(k.strip()) else None


def file_values() -> dict[str, str]:
    out = {}
    for line in _lines():
        if k := _key(line):
            out[k] = line.partition("=")[2]
    return out


def check(name: str, value: str, from_web: bool = False) -> None:
    """Refuse (with the reason) a key that is no setting, one the web may not set, or a value its knob cannot take."""
    k = KNOBS.get(name)
    if not k:
        raise Refused(f"{name} is not a setting (shaft settings lists them)")
    if from_web and not k.web:
        raise Refused(f"{name} is set from a terminal only (shaft settings set {name}=…): a wrong value could cut "
                      "the web UI off or break a service")
    if value is None:
        return
    if len(value) > VALUE_MAX or any(c in value for c in "\n\r\0"):
        raise Refused(f"{name}: a value is one line of at most {VALUE_MAX} characters")
    try:
        k.parse(value)
    except (ValueError, TypeError) as e:
        raise Refused(f"{name}={value!r} is refused: {e}") from None


def set_values(changes: dict[str, str | None], expect: dict[str, str | None] | None = None,
               from_web: bool = False, who: str = "operator") -> dict:
    """Set (a value) or unset (None) keys of the settings file in one write. `expect`: key -> its value in the file
    as the editor saw it (None: not in the file). Returns {"changed", "not_live", "notes"}; raises Refused."""
    load_all()
    for name, value in changes.items():
        check(name, value, from_web)
    for name in expect or {}:
        check(name, None, from_web)
    with config_lock(core.SETTINGS_FILE):
        lines = _lines()
        now = file_values()
        for name, saw in (expect or {}).items():
            if now.get(name) != saw:
                raise Refused(f"{name} changed since you looked: it is now "
                              + (repr(now[name]) if name in now else "not set in the file") + f" (you saw "
                              + (repr(saw) if saw is not None else "it not set") + ")")
        changed = []
        for name, value in changes.items():
            if (value is None and name not in now) or (value is not None and now.get(name) == value):
                continue
            at = [i for i, line in enumerate(lines) if _key(line) == name]
            for i in reversed(at[1:]):                 # a key set twice by hand: the last line counted; keep one
                del lines[i]
            if value is None:
                del lines[at[0]]
            elif at:
                lines[at[0]] = f"{name}={value}"
            else:
                lines.append(f"{name}={value}")
            changed.append(name)
        if not changed:
            return {"changed": [], "not_live": {}, "notes": []}
        text = "\n".join(lines) + "\n"
        if not text.startswith("#"):
            text = HEADER + text
        core.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = core.SETTINGS_FILE.with_name(f".settings.env.{os.getpid()}")
        tmp.write_text(text)
        os.replace(tmp, core.SETTINGS_FILE)
    changed.sort()
    log(f"settings: {', '.join(f'{n}={changes[n]!r}' if changes[n] is not None else f'{n} unset' for n in changed)}"
        f" by the {who} (was: {', '.join(f'{n}={now[n]!r}' if n in now else f'{n} not set' for n in changed)})")
    notes = [f"{n} is set in this shell's environment ({os.environ[n]!r}), which wins over the file for commands run "
             "from it" for n in changed if n in os.environ]
    late = not_live()
    return {"changed": changed, "not_live": {n: late[n] for n in changed if n in late}, "notes": notes}


# -- what is live where ---------------------------------------------------------------------------------------------
def _same(k, a: str, b: str) -> bool:
    try:
        return k.parse(a) == k.parse(b)
    except (ValueError, TypeError):
        return a == b


def not_live(records: dict | None = None) -> dict[str, list[str]]:
    """{knob: [running services that hold another value than the one they would read now]}. A service's record
    (version.record_service) names each knob it read and the value; one recorded before services kept their knobs
    holds the defaults (launchd gives it no environment) of the knobs it is declared to read."""
    from . import version
    load_all()
    if records is None:
        records = {s: version.service(s, stale=False) for s in SERVICE_NAMES}
    out: dict[str, list[str]] = {}
    for svc, rec in records.items():
        if not rec:
            continue                                   # not running: it reads the file when it starts
        held = rec.get("knobs")
        for k in KNOBS.values():
            h = held.get(k.name) if held is not None else (
                {"raw": k.default, "source": "default"} if svc in k.services else None)
            if not h or h.get("source") == "env":     # its own environment sets it: the file does not reach it
                continue
            _, _, raw = resolve(k, env={})
            if not _same(k, h.get("raw", ""), raw):
                out.setdefault(k.name, []).append(svc)
    return out


# -- the listing ----------------------------------------------------------------------------------------------------
def model_choices() -> list[str]:
    """The cloud models the supervisor knows: the instances' cloud models, the priced Claude models, and the judge
    pool as set and by default."""
    from . import cloudllm
    now = lambda name: resolve(KNOBS[name])[0]                     # as set now, not as this process read it
    pool = KNOBS["SHAFT_JUDGE_MODELS"]
    return list(dict.fromkeys([*now("SHAFT_JUDGE_MODELS"), *pool.parse(pool.default), *now("SHAFT_CLOUD_MODELS"),
                               *sorted(cloudllm.PRICES), now("SHAFT_JUDGE2_MODEL")]))


def listing() -> list[dict]:
    """Every setting, in its group: value (as text), source (env / file / default), default, what it does, the
    services that hold it, whether the web may set it, its value in the file, and the services where it is not live."""
    load_all()
    file, late = file_values(), not_live()
    out = []
    for k in KNOBS.values():
        _, source, raw = resolve(k)
        f = {"name": k.name, "value": raw, "source": source, "default": k.default, "help": k.help, "group": k.group,
             "services": list(k.services), "web": k.web, "in_file": file.get(k.name), "not_live": late.get(k.name, []),
             "env": os.environ.get(k.name), "kind": "text", "choices": []}
        if k.parse is core.cloud_models:
            f.update(kind="models", choices=model_choices())
        elif k.parse in (core.cloud_model, core.claude_model, core.openrouter_model):
            f.update(kind="model", choices=model_choices())
        elif k.parse in (core.positive_int, core.whole, core.port):
            f["kind"] = "int"
        out.append(f)
    order = {g: i for i, g in enumerate(KNOB_GROUPS)}
    return sorted(out, key=lambda f: order.get(f["group"], len(order)))


# -- `shaft settings` -----------------------------------------------------------------------------------------------
def _restart_text(services: list[str]) -> str:
    return ", ".join(f"{s} ({RESTART[s]})" for s in services)


def command(action: str | None, items: list[str], expect: list[str], expect_unset: list[str], as_json: bool = False,
            from_web: bool = False) -> int:
    if not action:
        fs = listing()
        if as_json:
            print(json.dumps({"settings": fs, "file": str(core.SETTINGS_FILE)}))
            return 0
        print(f"settings file: {core.SETTINGS_FILE} (env > file > default)")
        group = None
        for f in fs:
            if f["group"] != group:
                group = f["group"]
                print(f"\n{group}")
            extra = [] if f["source"] == "default" else [f"default {f['default']}"]
            extra += [] if f["web"] else ["terminal only"]
            extra += [f"NOT LIVE in {', '.join(f['not_live'])}: restart {_restart_text(f['not_live'])}"] \
                if f["not_live"] else []
            print(f"  {f['name']}={f['value']}  [{f['source']}]" + "".join(f" · {e}" for e in extra))
            print(f"      {f['help']} (read by: {', '.join(f['services'])})")
        return 0
    try:
        if action == "set":
            pairs = [i.partition("=") for i in items]
            if not pairs or any(not eq for _, eq, _ in pairs):
                print("usage: shaft settings set KEY=VALUE [KEY=VALUE …] [--expect KEY=OLD]")
                return 2
            changes = {k.strip(): v for k, _, v in pairs}
        else:
            if not items:
                print("usage: shaft settings unset KEY [KEY …]")
                return 2
            changes = {k.strip(): None for k in items}
        saw = {k.strip(): v for k, _, v in (e.partition("=") for e in expect)} | {k: None for k in expect_unset}
        r = set_values(changes, saw, from_web=from_web, who="web UI" if from_web else "operator")
    except Refused as e:
        print(f"refused: {e}")
        return 1
    if not r["changed"]:
        print("nothing to change")
        return 0
    late = sorted({s for svcs in r["not_live"].values() for s in svcs})
    print(("saved " if action == "set" else "removed ") + f"{', '.join(r['changed'])} "
          + ("in " if action == "set" else "from ") + f"{core.SETTINGS_FILE.name} · "
          + (f"not live yet: restart {_restart_text(late)}; runs read it when they start" if late
             else "runs and services read it when they start (no running service holds another value)")
          + "".join(f" · {n}" for n in r["notes"]))
    return 0


if __name__ == "__main__":
    sys.exit(command(None, [], [], []))
