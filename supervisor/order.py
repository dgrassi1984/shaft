"""Persist operator order so activity cannot move navigation rows."""
import json
import os
import threading
from pathlib import Path

from . import core

_lock = threading.RLock()


class Refused(ValueError):
    """Explain why a stale or invalid move cannot apply."""


def _thread_path(name: str) -> Path:
    if not core.NAME_RE.fullmatch(name):
        raise Refused("invalid instance name")
    return core.SUP_DIR / "web" / "thread-order" / f"{name}.json"


def _instance_path() -> Path:
    return core.SUP_DIR / "web" / "instance-order.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        if (isinstance(data, dict) and type(data.get("rev")) is int and data["rev"] >= 0
                and isinstance(data.get("ids"), list) and all(isinstance(i, str) for i in data["ids"])):
            return {"rev": data["rev"], "ids": list(dict.fromkeys(data["ids"]))}
    except (OSError, ValueError):
        pass
    return {"rev": 0, "ids": []}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
            json.dump(data, f)
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _ordered(stored: list[str], ids: list[str], *, missing_first: bool = False) -> list[str]:
    known = set(ids)
    kept = [i for i in stored if i in known]
    recorded = set(stored)
    missing = [i for i in ids if i not in recorded]
    return missing + kept if missing_first else kept + missing


def threads(name: str, open_chats: list[dict]) -> list[dict]:
    """Recover unrecorded threads first so older installations lose no rows."""
    with _lock:
        stored = _read(_thread_path(name))
        chats = sorted(open_chats, key=lambda c: c.get("created", ""), reverse=True)
        by_id = {c["id"]: c for c in chats}
        return [by_id[i] for i in _ordered(stored["ids"], list(by_id), missing_first=True)]


def _open(name: str) -> list[dict]:
    from . import web_chat
    return [c for c in web_chat.chats(name) if c.get("state") != "ended"]


def add_thread(name: str, cid: str):
    """Save creation order even when timestamps tie."""
    from . import web_chat
    path = _thread_path(name)
    with web_chat._inst_lock(name), _lock:
        data = _read(path)
        ids = [c["id"] for c in threads(name, _open(name)) if c["id"] != cid]
        _write(path, {"rev": data["rev"] + 1, "ids": [cid, *ids]})


def _move(path: Path, ids: list[str], cid: str, after: str | None) -> dict:
    data = _read(path)
    if cid not in ids:
        raise Refused("that item is not open or no longer exists")
    if cid == after:
        return {"rev": data["rev"], "ids": ids}
    moved = [i for i in ids if i != cid]
    moved.insert(moved.index(after) + 1 if after in moved else 0, cid)
    out = {"rev": data["rev"] + 1, "ids": moved}
    _write(path, out)
    return out


def move_thread(name: str, cid: str, after: str | None) -> dict:
    """Serialize lifecycle and moves so two devices preserve each other's moves."""
    from . import web_chat
    path = _thread_path(name)
    with web_chat._inst_lock(name), _lock:
        if cid == "from":
            raise Refused("the main conversation cannot be moved")
        ids = [c["id"] for c in threads(name, _open(name))]
        return _move(path, ids, cid, after)


def instances(names: list[str]) -> list[str]:
    """Append new instances without disturbing the operator's existing order."""
    with _lock:
        path = _instance_path()
        data = _read(path)
        recorded = set(data["ids"])
        unseen = list(dict.fromkeys(n for n in names if n not in recorded))
        if unseen:
            # The first inventory is the baseline; later additions advance its revision.
            data = {"rev": data["rev"] + bool(data["ids"]), "ids": data["ids"] + unseen}
            _write(path, data)
        return _ordered(data["ids"], names)


def move_instance(name: str, after: str | None) -> dict:
    """Apply moves to current names so removed instances cannot return."""
    with _lock:
        return _move(_instance_path(), instances(core.instance_names()), name, after)


def thread_rev(name: str) -> int:
    """Expose the saved counter for content revision hashes."""
    with _lock:
        return _read(_thread_path(name))["rev"]


def instance_rev() -> int:
    """Let clients discard stale instance order responses."""
    with _lock:
        return _read(_instance_path())["rev"]
