"""An instance's documents (PLAN-webui-chat.md §3): Markdown files in its notes/docs/, which the web chat shows beside the
conversation, live, and the operator can edit. Stdlib only.

No supervisor tool writes them for the instance: it writes them with the write_file it has, as any note. The operator's
edits go through `shaft docs <name> set` (the web UI runs it), a compare-and-set on the sha he saw, so an edit never
silently overwrites a write the instance made meanwhile. notes/ is the instance's mind: its next run commits the edit
with the rest of it (Daniele, 2026-10-19), and the instance hears of it as a diff in his next message.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from .core import UnsafePath, config_lock, contained, instance_root, log, safe_write_bytes

DIR = "notes/docs"
MAX_BYTES = 256 * 1024
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")        # one path component: no hidden files, no separators


class Refused(Exception):
    pass


def _rel(path: str) -> str:
    """A document's path inside notes/docs/, checked: Markdown, plain names, no way out of the folder."""
    p = str(path).strip().removeprefix(DIR + "/")
    parts = p.split("/")
    if not p.endswith(".md") or not all(NAME.match(x) and x not in (".", "..") for x in parts) or len(parts) > 4:
        raise Refused(f"{path!r}: a document is a .md file under {DIR}/, named with letters, digits, space, . _ -")
    return p


def _path(name: str, rel: str, create: bool = False) -> Path:
    root = instance_root(name)
    try:
        if create:
            contained(root, *Path(DIR, rel).parent.parts).mkdir(parents=True, exist_ok=True)
        return contained(root, *Path(DIR, rel).parts)      # refuses a link anywhere on the way, and leaving the folder
    except UnsafePath as e:
        raise Refused(f"{DIR}/{rel}: {e}") from None


def sha(data: bytes | None) -> str:
    return hashlib.sha256(data).hexdigest() if data is not None else "none"


def read(name: str, path: str) -> tuple[str | None, str]:
    """(its text, or None when there is no such document; its sha256, "none" then)."""
    rel = _rel(path)
    p = _path(name, rel)
    if p.is_symlink() or not p.is_file():
        return None, "none"
    if (size := p.stat().st_size) > MAX_BYTES:
        raise Refused(f"{DIR}/{rel} is {size} bytes: too big to show or edit here (at most {MAX_BYTES})")
    data = p.read_bytes()
    return data.decode(errors="replace"), sha(data)


def listing(name: str) -> list[dict]:
    """Every document, the last changed first: path (inside notes/docs/), bytes, mtime, sha."""
    try:
        base = _path(name, "x.md").parent
    except Refused:
        return []
    out = []
    for p in base.rglob("*.md") if base.is_dir() else []:
        rel = p.relative_to(base).as_posix()
        try:
            _rel(rel)
            if p.is_symlink() or not p.is_file() or any((base / x).is_symlink() for x in Path(rel).parents
                                                         if str(x) != "."):
                continue
            st = p.stat()
            digest = sha(p.read_bytes()) if st.st_size <= MAX_BYTES else "too-big"
        except (Refused, OSError):
            continue
        out.append({"path": rel, "bytes": st.st_size, "mtime": st.st_mtime, "sha": digest})
    return sorted(out, key=lambda d: d["mtime"], reverse=True)


def snapshot(name: str) -> dict[str, str]:
    """{path: sha}: what a reply changed is the difference of two of these."""
    return {d["path"]: d["sha"] for d in listing(name)}


def changed(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    """Documents created or changed between two snapshots (a removed one is not shown: there is nothing to open)."""
    return [{"path": p, "created": p not in before} for p in sorted(after) if before.get(p) != after[p]]


def write(name: str, path: str, text: str, expect_sha: str) -> dict:
    """The operator's edit: written only if the document is still what he saw ("none": it must not exist yet)."""
    rel = _rel(path)
    data = text.replace("\r\n", "\n").encode()
    if len(data) > MAX_BYTES:
        raise Refused(f"{DIR}/{rel} would be {len(data)} bytes: at most {MAX_BYTES}")
    p = _path(name, rel, create=True)
    with config_lock(p):
        old, now = read(name, rel)
        if now != expect_sha:
            raise Refused(f"{DIR}/{rel} changed since you looked" if expect_sha != "none" else f"{DIR}/{rel} exists already")
        try:
            safe_write_bytes(instance_root(name), f"{DIR}/{rel}", data)
        except (UnsafePath, OSError) as e:
            raise Refused(f"{DIR}/{rel}: {e}") from None
    log(f"{name}: {DIR}/{rel} {'created' if old is None else 'edited'} by the operator")
    return {"path": rel, "old": old, "sha": sha(data)}


# -- `shaft docs` -----------------------------------------------------------------------------------------------------
def command(name: str, action: str | None, path: str | None, file: str | None, expect_sha: str | None) -> int:
    instance_root(name)
    try:
        if not action:
            docs = listing(name)
            for d in docs:
                print(f"{d['sha'][:12]}  {d['bytes']:>7}  {d['path']}")
            if not docs:
                print(f"(no documents in {DIR}/)")
            return 0
        if not path:
            print(f"usage: shaft docs <name> {action} PATH")
            return 2
        if action == "show":
            text, s = read(name, path)
            if text is None:
                print(f"no such document: {DIR}/{_rel(path)}", file=sys.stderr)
                return 1
            print(f"sha256 {s}")
            print(text, end="" if text.endswith("\n") else "\n")
            return 0
        if not file or not expect_sha:
            print("usage: shaft docs <name> set PATH --file FILE --expect-sha SHA  (none: a new document)")
            return 2
        r = write(name, path, Path(file).read_text(), expect_sha)
    except (Refused, OSError) as e:
        print(f"refused: {e}")
        return 1
    print(f"{name}: {DIR}/{r['path']} {'created' if r['old'] is None else 'written'} · sha256 {r['sha']} · its next run "
          "commits it with its notes")
    return 0
