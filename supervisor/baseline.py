"""The code the supervisor has approved for each instance, kept where the instance cannot write it.

An instance's approved code is its last commit (only the supervisor commits on an instance's branch: the git dir is
outside the instance's folder, which is all its sandbox may write) plus what the gate passed that git does not hold —
an ignored or untracked file in a code path, a kept change whose commit failed — and minus a committed file the gate
saw deleted. Those differences are recorded here after every settlement (runner._settle): .supervisor/baselines/
<name>.json, {path: sha256 of the approved bytes, or null for "approved absent"}, with the approved bytes in
<name>.files/<sha256>, since git cannot give them back.

At the start of each run (runner._reconcile) whatever differs from the approved code — including ignored and untracked
files, which `git status` does not show — is gated before it can become the run's baseline. A path whose bytes are
the committed ones is approved whatever the record says: every supervisor path that changes code and commits it
(`shaft update`'s merge, probation's revert, a new or bred instance, a coder hand-off settled with its run) needs no
record of its own. Until an instance's first settlement under this scheme there is no record, and git's view
(committed or not) is the baseline.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from . import core, gate


class BaselineError(Exception):
    """The approved code cannot be told: nothing may start on a guess (runner._reconcile refuses the run)."""


def record_path(name: str) -> Path:
    return core.SUP_DIR / "baselines" / f"{name}.json"


def _files(name: str) -> Path:
    return core.SUP_DIR / "baselines" / f"{name}.files"


def _skipped(path: str) -> bool:
    """What gate.snapshot never takes."""
    return "__pycache__" in Path(path).parts or Path(path).name == ".DS_Store"


def committed(root: Path) -> dict[str, tuple[str, str]]:
    """{path: (mode, object id)} of the code paths in the instance's last commit, read through git_wt (the worktree's
    own .git is the instance's to forge), in bytes: a file name that is not UTF-8 must not hide a change."""
    out = core.git_wt(root, "--no-optional-locks", "ls-tree", "-r", "-z", "HEAD", "--", *gate.CODE_PATHS,
                      text=False).stdout
    tree = {}
    for entry in (os.fsdecode(e) for e in out.split(b"\0") if e):
        meta, _, path = entry.partition("\t")
        mode, _, oid = meta.split()
        tree[path] = (mode, oid)
    return tree


def _oid(data: bytes, like: str) -> str:
    """The git object id of a blob of `data`, in the hash its repository uses (told by an id's length)."""
    h = hashlib.sha256() if len(like) == 64 else hashlib.sha1()
    h.update(b"blob %d\0" % len(data) + data)
    return h.hexdigest()


def _is_committed(data, entry: tuple[str, str] | None) -> bool:
    return (isinstance(data, bytes) and entry is not None and entry[0] in ("100644", "100755")
            and _oid(data, entry[1]) == entry[1])


def _own_folder(name: str, root: Path) -> None:
    """Only an instance's own folder has approved code: never a copy of it (a battery exam's, a trial's, the coder's
    scratch), whose record would replace the instance's. BaselineError otherwise."""
    if Path(root).resolve() != (core.INSTANCES / name).resolve():
        raise BaselineError(f"{root} is not {name}'s folder ({core.INSTANCES / name}): no approved code is kept for "
                            "a copy")


def read(name: str, root: Path) -> dict | None:
    """The record, or None when the instance has none yet (or it was made for another folder of that name).
    BaselineError when it is there but cannot be read."""
    path = record_path(name)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise BaselineError(f"{path} cannot be read ({type(e).__name__}: {e})") from None
    if not isinstance(data, dict) or not isinstance(data.get("approved"), dict):
        raise BaselineError(f"{path} is not an approved-code record")
    if data.get("root") != str(Path(root).resolve()):
        return None
    return data


def write(name: str, root: Path, code: dict[str, bytes]) -> None:
    """Record `code` (a gate.snapshot the supervisor has just approved) as the instance's approved code."""
    _own_folder(name, root)
    if (linked := gate.links(code)):
        raise BaselineError(f"links are never approved code: {', '.join(linked)}")
    head = committed(root)
    approved: dict[str, dict] = {}
    for path in sorted(set(code) | {p for p, e in head.items() if e[0] in ("100644", "100755")}):
        if _skipped(path) or _is_committed(code.get(path), head.get(path)):
            continue
        data = code.get(path)
        approved[path] = {"sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
                          "committed": head[path][1] if path in head else None}   # stale once the commit changes
    store = _files(name)
    store.mkdir(parents=True, exist_ok=True)
    for path, entry in approved.items():
        if entry["sha256"] and not (store / entry["sha256"]).exists():
            tmp = store / f".{entry['sha256']}.{os.getpid()}"
            tmp.write_bytes(code[path])
            os.replace(tmp, store / entry["sha256"])
    record = record_path(name)
    tmp = record.with_name(f".{record.name}.{os.getpid()}")
    tmp.write_text(json.dumps({"root": str(Path(root).resolve()), "ts": datetime.now().isoformat(timespec="seconds"),
                               "approved": approved}, indent=1))
    os.replace(tmp, record)
    wanted = {e["sha256"] for e in approved.values() if e["sha256"]}
    for f in store.iterdir():
        if f.name not in wanted and not f.name.startswith("."):
            f.unlink(missing_ok=True)


def unapproved(name: str, root: Path, now: dict[str, bytes]) -> dict[str, bytes | None] | None:
    """{path: the approved bytes to put back, or None for "absent"} for every code path whose content `now` is not
    approved; None when there is no record yet (the caller falls back to git). BaselineError if it cannot tell."""
    _own_folder(name, root)
    record = read(name, root)
    if record is None:
        return None
    try:
        head = committed(root)
    except Exception as e:                                      # anything: refused, never guessed
        raise BaselineError(f"cannot read {name}'s last commit: {type(e).__name__}: {e}") from None
    approved = record["approved"]
    out: dict[str, bytes | None] = {}
    for path in sorted(set(now) | set(head) | set(approved)):
        entry, data = head.get(path), now.get(path)
        if _skipped(path) or (entry is not None and entry[0] not in ("100644", "100755")):
            continue                                            # a link or submodule: never snapshotted
        if _is_committed(data, entry):
            continue
        mine = approved.get(path)
        if mine is not None and mine.get("committed") != (entry[1] if entry else None):
            mine = None                                         # approved against a commit since replaced
        if mine is not None:
            want = mine.get("sha256")
            if (want is None and data is None) or (want and isinstance(data, bytes)
                                                    and hashlib.sha256(data).hexdigest() == want):
                continue
            if want is None:
                out[path] = None
                continue
            try:
                out[path] = (_files(name) / want).read_bytes()
            except OSError as e:
                raise BaselineError(f"the approved {path} of {name} is missing from {_files(name)}: {e}") from None
            continue
        if data is None and entry is None:
            continue
        if entry is None:
            out[path] = None                                    # never approved: it goes
        else:
            try:
                out[path] = core.git_wt(root, "cat-file", "blob", entry[1], text=False).stdout
            except Exception as e:                              # anything: refused, never guessed
                raise BaselineError(f"cannot read the committed {path} of {name}: {e}") from None
    return out
