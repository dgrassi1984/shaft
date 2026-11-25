"""JSONL helpers: splitting on LF only, and the supervisor's add-only record files."""
import fcntl as _fcntl
import json as _json
import os as _os
import threading as _threading


def jsonl_lines(text: str, *, keepends: bool = False) -> list[str]:
    """Split JSONL on LF only: str.splitlines breaks JSON strings containing U+2028, U+2029 or U+0085.

    Preserve blank lines and omit the empty item after a final LF, as str.splitlines does.
    With keepends, retain the exact text of each line for JSONL rewrites.
    """
    lines = text.split("\n")
    if keepends:
        return [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


# -- add-only record files the supervisor keeps (served.py, ledger_mirror.py) -------------------------------------
_lock = _threading.Lock()
_cache: dict[str, dict] = {}


def append(f, lines: list[str]) -> None:
    """Append whole lines to an open, exclusively locked file. A last line an interrupted write left without its LF is
    closed first, so it cannot swallow the next row (claimlint's rows do the same)."""
    f.flush()
    size = _os.fstat(f.fileno()).st_size
    if size and _os.pread(f.fileno(), 1, size - 1) != b"\n":
        f.seek(0, 2)
        f.write("\n")
    f.seek(0, 2)
    f.write("".join(lines))
    f.flush()


def append_locked(path, lines: list[str]) -> None:
    """append() under an exclusive lock on `path` (created, with its folder, if missing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        _fcntl.flock(f, _fcntl.LOCK_EX)
        append(f, lines)


def rows(path) -> list[dict]:
    """The JSON-object rows of an add-only file, oldest first, parsed once: a file that only grew is read from where
    the last read stopped (keyed by device, inode, size and mtime, as ledger.spend is). A torn or malformed line costs
    only itself; a last line still being written is left for the next read. [] without a file."""
    key = str(path)
    with _lock:
        try:
            f = open(path, "rb")
        except OSError:
            _cache.pop(key, None)
            return []
        with f:
            st = _os.fstat(f.fileno())
            c = _cache.get(key)
            if (not c or c["id"] != (st.st_dev, st.st_ino) or st.st_size < c["size"]
                    or (st.st_size == c["size"] and st.st_mtime_ns != c["mtime"])):
                c = {"id": (st.st_dev, st.st_ino), "offset": 0, "rows": [], "size": 0, "mtime": 0}
            f.seek(c["offset"])
            data = f.read(max(st.st_size - c["offset"], 0))
            done = data.rfind(b"\n") + 1
            for line in jsonl_lines(data[:done].decode("utf-8", "replace")):
                try:
                    row = _json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    c["rows"].append(row)
            c.update(offset=c["offset"] + done, size=st.st_size, mtime=st.st_mtime_ns)
            _cache[key] = c
            return list(c["rows"])
