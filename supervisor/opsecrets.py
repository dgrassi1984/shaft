"""`shaft secrets set|unset|list`: the Mac app's way to store an API key without a terminal.

The value comes on stdin (one line) and is never printed, logged or put in argv. `list` shows names only. Every write
goes through core.set_secret_env, the one atomic writer of the project .env."""
from __future__ import annotations

import json
import sys

from . import core


def _say(as_json: bool, ok: bool, text: str = "", **extra) -> int:
    if as_json:
        print(json.dumps({"ok": ok, **({"error": text} if not ok else {}), **extra}))
    elif text:
        print(text, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def command(action: str, name: str | None, as_json: bool = False) -> int:
    if action == "list":
        try:
            names = sorted(k for k in core.secrets_env() if core.SECRET_NAMES_RE.fullmatch(k))
        except (ValueError, OSError) as e:
            return _say(as_json, False, f"cannot read the secrets file: {e}")
        if as_json:
            print(json.dumps({"ok": True, "names": names}))
        else:
            print("\n".join(names))
        return 0
    if not name:
        print(f"usage: shaft secrets {action} NAME", file=sys.stderr)
        return 2
    if not core.SECRET_NAMES_RE.fullmatch(name):
        return _say(as_json, False, f"{name} is not a secret name the supervisor stores")
    try:
        if action == "set":
            value = sys.stdin.readline().rstrip("\r\n")
            if not value:
                return _say(as_json, False, f"no value for {name} on stdin")
            core.set_secret_env(name, value)
        else:
            core.set_secret_env(name, None)
    except (ValueError, OSError) as e:
        return _say(as_json, False, str(e))
    return _say(as_json, True, f"{name} {'saved' if action == 'set' else 'removed'}" if not as_json else "", name=name)
