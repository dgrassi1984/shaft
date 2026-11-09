"""Device-reported voices, assignments and operator decisions on instance proposals."""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from . import core

LANGUAGES = ("en", "it")


class Refused(ValueError):
    pass


def _now():
    return datetime.now().astimezone().isoformat()


def _read():
    try:
        return json.loads((core.SUP_DIR / "voices.json").read_text())
    except FileNotFoundError:
        return {"platforms": {}, "assignments": {}, "proposals": {}}


def _save(data):
    # Same lock + temporary file + replace pattern as core._update_tool_settings.
    path = core.SUP_DIR / "voices.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".voices-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _language(language):
    if language not in LANGUAGES:
        raise Refused(f"unknown language {language!r}; expected en or it")


def report(platform: str, voices: list[dict]) -> None:
    if platform not in ("ios", "macos"):
        raise Refused(f"unknown platform {platform!r}; expected ios or macos")
    if not isinstance(voices, list):
        raise Refused("voices must be a list")
    for voice in voices:
        if not isinstance(voice, dict):
            raise Refused("each voice must be an object")
        for field in ("identifier", "name", "language"):
            if not isinstance(voice.get(field), str) or not voice[field].strip():
                raise Refused(f"voice {field} must be a non-empty string")
        if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", voice["language"]):
            raise Refused(f"invalid voice language {voice['language']!r}")
        if voice.get("quality") not in ("default", "enhanced", "premium"):
            raise Refused(f"invalid voice quality {voice.get('quality')!r}")
    with core.config_lock(core.SUP_DIR / "voices.json"):
        data = _read()
        data["platforms"][platform] = {"voices": voices, "reported": _now()}
        _save(data)


def _available(data, language):
    if language is not None:
        _language(language)
    return {platform: [v for v in row["voices"]
                       if language is None or v["language"].split("-")[0].lower() == language]
            for platform, row in data["platforms"].items()}


def available(language: str | None = None) -> dict[str, list[dict]]:
    return _available(_read(), language)


def get(name: str) -> dict[str, dict | None]:
    held = _read()["assignments"].get(name, {})
    return {language: held.get(language) for language in LANGUAGES}


def _choice(data, name, language, identifier):
    _language(language)
    voice = next((v for row in data["platforms"].values() for v in row["voices"]
                  if v["identifier"] == identifier), None)
    if voice is None:
        raise Refused(f"identifier {identifier!r} is not in any reported voice list")
    if voice["language"].split("-")[0].lower() != language:
        raise Refused(f"identifier {identifier!r} speaks {voice['language']}, not {language}")
    for owner, held in data["assignments"].items():
        if owner != name and held.get(language, {}).get("identifier") == identifier:
            raise Refused(f"identifier {identifier!r} is taken by instance {owner} for language {language}")
    return voice


def _assign(data, name, language, identifier, by):
    if by not in ("operator", "instance"):
        raise Refused(f"unknown assignment author {by!r}; expected operator or instance")
    voice = _choice(data, name, language, identifier)
    row = {"identifier": identifier, "name": voice["name"], "by": by, "ts": _now()}
    data["assignments"].setdefault(name, {})[language] = row
    return row


def assign(name: str, language: str, identifier: str, by: str) -> dict:
    with core.config_lock(core.SUP_DIR / "voices.json"):
        data = _read()
        row = _assign(data, name, language, identifier, by)
        _save(data)
    return row


def clear(name: str, language: str) -> None:
    _language(language)
    with core.config_lock(core.SUP_DIR / "voices.json"):
        data = _read()
        data["assignments"].get(name, {}).pop(language, None)
        _save(data)


def _proposal(data, name, language, identifier, why):
    voice = _choice(data, name, language, identifier)
    return {"id": uuid.uuid4().hex, "language": language, "identifier": identifier, "name": voice["name"],
            "why": why, "ts": _now(), "state": "open"}


def propose(name: str, language: str, identifier: str, why: str) -> dict:
    with core.config_lock(core.SUP_DIR / "voices.json"):
        data = _read()
        row = _proposal(data, name, language, identifier, why)
        data["proposals"][name] = [p for p in data["proposals"].get(name, [])
                                   if p["language"] != language or p["state"] != "open"] + [row]
        _save(data)
    return row


def proposals(name: str) -> list[dict]:
    return _read()["proposals"].get(name, [])


def decide(name: str, pid: str, accept: bool, reason: str = "") -> dict:
    from . import messages
    with core.config_lock(core.SUP_DIR / "voices.json"):
        data = _read()
        row = next((p for p in data["proposals"].get(name, []) if p["id"] == pid), None)
        if row is None:
            raise Refused(f"unknown voice proposal {pid!r} for instance {name}")
        if row["state"] != "open":
            raise Refused(f"voice proposal {pid!r} is already {row['state']}")
        if accept:
            _assign(data, name, row["language"], row["identifier"], "instance")
        row.update(state="accepted" if accept else "rejected", reason=reason, decided_at=_now())
        _save(data)
    # Match calendar_proposals._outcome: deliver after unlocking; failure cannot undo the decision.
    try:
        messages.deliver_background(name, f"Voice proposal {pid} {row['state']}: {row['name']} "
                                    f"({row['identifier']}) for {row['language']}.\n{reason}", shown="voice approval")
    except Exception as e:
        core.log(f"voice proposal {pid}: outcome not delivered to {name}: {e}")
    return row


def _for_instance(name, language):
    data = _read()
    result = _available(data, language)
    for rows in result.values():
        for voice in rows:
            lang = language or voice["language"].split("-")[0].lower()
            owner = next((owner for owner, held in data["assignments"].items()
                          if held.get(lang, {}).get("identifier") == voice["identifier"]), None)
            voice.update(current=owner == name, taken_by=owner if owner != name else None)
    return result
