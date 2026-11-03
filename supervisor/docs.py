"""An instance's documents (docs/plans/PLAN-webui-chat.md §3): Markdown files in its notes/docs/, which the web chat shows beside the
conversation, live, and the operator can edit. Stdlib only.

No supervisor tool writes them for the instance: it writes them with the write_file it has, as any note. The operator's
edits go through `shaft docs <name> set` (the web UI runs it), a compare-and-set on the sha they saw, so an edit never
silently overwrites a write the instance made meanwhile. notes/ is the instance's mind: its next run commits the edit
with the rest of it, and the instance hears of it as a diff in their next message.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

from .core import UnsafePath, config_lock, contained, instance_root, log, open_beneath, read_beneath, safe_write_bytes

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


def folder(path: str) -> str:
    """A folder inside notes/docs/ ("" is notes/docs/ itself), checked as _rel checks a document's: a document in it
    still fits the four levels."""
    p = str(path).strip().strip("/")
    p = "" if p == DIR else p.removeprefix(DIR + "/")
    parts = p.split("/") if p else []
    if not all(NAME.match(x) and x not in (".", "..") for x in parts) or len(parts) > 3:
        raise Refused(f"{path!r}: a folder under {DIR}/ is named with letters, digits, space, . _ -, at most 3 deep")
    return p


def _path(name: str, rel: str, create: bool = False) -> Path:
    root = instance_root(name)
    try:
        if create:
            contained(root, *Path(DIR, rel).parent.parts).mkdir(parents=True, exist_ok=True)
        return contained(root, *Path(DIR, rel).parts)      # refuses a link anywhere on the way, and leaving the folder
    except UnsafePath as e:
        raise Refused(f"{DIR}/{rel}: {e}") from None


def _kb(n: int) -> str:
    """A size in a refusal, rounded up: a document 1 byte over the limit never reads as the limit itself."""
    return f"{-(-n // 1024)} KB"


def sha(data: bytes | None) -> str:
    return hashlib.sha256(data).hexdigest() if data is not None else "none"


def read(name: str, path: str) -> tuple[str | None, str]:
    """(its text, or None when there is no such document; its sha256, "none" then)."""
    rel = _rel(path)
    p = _path(name, rel)
    if p.is_symlink() or not p.is_file():
        return None, "none"
    try:                                          # opened with no link followed: nothing swapped in after the check
        data = read_beneath(instance_root(name), Path(DIR, rel).parts, MAX_BYTES)
    except ValueError:
        raise Refused(f"{DIR}/{rel} is {_kb(p.stat().st_size)}: too big to show or edit here (at most "
                      f"{_kb(MAX_BYTES)})") from None
    except (UnsafePath, FileNotFoundError):
        return None, "none"
    return data.decode(errors="replace"), sha(data)


def raw(name: str, path: str) -> bytes | None:
    """A document's bytes, whatever its size up to SHAFT_FILE_READ_MAX_MB (the apps' download, the way to one too big
    to show: /app/v1/docs/<n>/raw); None when there is no such document. TooBig past that."""
    rel = _rel(path)
    p = _path(name, rel)
    if p.is_symlink() or not p.is_file():
        return None
    try:                                          # opened with no link followed: nothing swapped in after the check
        return read_beneath(instance_root(name), Path(DIR, rel).parts)
    except (UnsafePath, FileNotFoundError):
        return None


_digests: dict[str, tuple[tuple, str]] = {}      # file -> ((inode, mtime_ns, size), sha): read again only when it changes


def listing(name: str) -> list[dict]:
    """Every document, the last changed first: path (inside notes/docs/), bytes, mtime, sha. A document's sha is read
    again only when its inode, mtime or size changed: the documents panel asks for this every 1.5 s, and it hashed
    every document each time (W12, review 2026-10-24). The cache holds only what this listing found below this
    instance's folder: a deleted document's entry goes with it."""
    try:
        base = _path(name, "x.md").parent
    except Refused:
        return []
    out, found = [], set()
    for p in base.rglob("*.md") if base.is_dir() else []:
        rel = p.relative_to(base).as_posix()
        try:
            _rel(rel)
            if p.is_symlink() or not p.is_file() or any((base / x).is_symlink() for x in Path(rel).parents
                                                         if str(x) != "."):
                continue
            st = p.stat()
            stamp, hit = (st.st_ino, st.st_mtime_ns, st.st_size), _digests.get(str(p))
            if hit and hit[0] == stamp:
                digest = hit[1]
            else:
                digest = (sha(read_beneath(instance_root(name), Path(DIR, rel).parts)) if st.st_size <= MAX_BYTES
                          else "too-big")
                _digests[str(p)] = (stamp, digest)
            found.add(str(p))
        except (Refused, OSError, UnsafePath):
            continue
        out.append({"path": rel, "bytes": st.st_size, "mtime": st.st_mtime, "sha": digest})
    below = str(base) + os.sep
    for key in [k for k in list(_digests) if k.startswith(below) and k not in found]:
        _digests.pop(key, None)
    return sorted(out, key=lambda d: d["mtime"], reverse=True)


def snapshot(name: str) -> dict[str, str]:
    """{path: sha}: what a reply changed is the difference of two of these."""
    return {d["path"]: d["sha"] for d in listing(name)}


def changed(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    """Documents created or changed between two snapshots (a removed one is not shown: there is nothing to open)."""
    return [{"path": p, "created": p not in before} for p in sorted(after) if before.get(p) != after[p]]


def write(name: str, path: str, text: str, expect_sha: str) -> dict:
    """The operator's edit: written only if the document is still what they saw ("none": it must not exist yet)."""
    rel = _rel(path)
    data = text.replace("\r\n", "\n").encode()
    if len(data) > MAX_BYTES:
        raise Refused(f"{DIR}/{rel} would be {_kb(len(data))}: at most {_kb(MAX_BYTES)}")
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


def remove(name: str, path: str, expect_sha: str) -> dict:
    """The operator's deletion: only the existing document whose sha they saw may be removed."""
    rel = _rel(path)
    p = _path(name, rel)
    with config_lock(p):
        old, now = read(name, rel)
        if old is None:
            raise Refused(f"{DIR}/{rel} is not there (any more)")
        if now != expect_sha:
            raise Refused(f"{DIR}/{rel} changed since you looked")
        try:
            # Hold the parent directory open: a swapped link must never redirect the unlink outside notes/docs.
            fd = open_beneath(instance_root(name), Path(DIR, rel).parent.parts, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.unlink(Path(rel).name, dir_fd=fd)
            finally:
                os.close(fd)
        except (UnsafePath, OSError) as e:
            raise Refused(f"{DIR}/{rel}: {e}") from None
    log(f"{name}: {DIR}/{rel} deleted by the operator")
    return {"path": rel, "old": old, "sha": "none"}


# -- `shaft docs` -----------------------------------------------------------------------------------------------------
def command(name: str, action: str | None, path: str | None, file: str | None, expect_sha: str | None,
            sync: bool = False) -> int:
    from . import doc_comments
    instance_root(name)
    try:
        if not action:
            docs = listing(name)
            for d in docs:
                print(f"{d['sha'][:12]}  {d['bytes']:>7}  {d['path']}")
            if not docs:
                print(f"(no documents in {DIR}/)")
            return 0
        if action == "comments":
            if sync:
                if not path:
                    raise Refused("comments --sync needs PATH")
                doc_comments.sync_copy(name, path)
                print(f"{name}: notes/comments/{_rel(path)} rewritten")
            else:
                for thread in doc_comments.threads(name, path):
                    first = thread["posts"][0]["text"].splitlines()[0]
                    print(f"{thread['id']}  {thread['state']}  {thread['doc']}  {first}")
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
        if action == "rm":
            if not expect_sha:
                print("usage: shaft docs <name> rm PATH --expect-sha SHA")
                return 2
            r = remove(name, path, expect_sha)
            print(f"{name}: {DIR}/{r['path']} deleted · its next run commits it with its notes")
            return 0
        if not file or not expect_sha:
            print("usage: shaft docs <name> set PATH --file FILE --expect-sha SHA  (none: a new document)")
            return 2
        r = write(name, path, Path(file).read_text(encoding="utf-8"), expect_sha)
    except (Refused, doc_comments.Refused, OSError) as e:
        print(f"refused: {e}")
        return 1
    print(f"{name}: {DIR}/{r['path']} {'created' if r['old'] is None else 'written'} · sha256 {r['sha']} · its next run "
          "commits it with its notes")
    return 0
