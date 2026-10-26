"""Spend ledgers (cloud_usage.jsonl, coder_usage.jsonl) read once, then only what was appended since.

Every budget check and every web page asked spent_today, and each answer parsed the whole ledger: ~41k lines, ~140 ms
a call, 8 calls for each overview the web service builds — a second of every chat it opened (2026-10-26).

The ledgers are appended a line at a time (one write each, O_APPEND) by several processes. The only other writer,
`shaft delete` (delete._rewrite), replaces the file: a new inode. So the same inode, grown, means only the new bytes
need reading; anything else (another inode, shrunk, rewritten in place) reads it all again. A last line without its
newline yet is parsed on every call and kept nowhere, as reading the whole file did. A malformed line raises, as
before: spend that cannot be read is never counted as none.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_lock = threading.Lock()
_cache: dict[str, dict] = {}


def spend(path: Path, instance: str) -> list[tuple]:
    """(ts, usd) of each of `instance`'s rows in `path`, in the file's order; [] if there is no file."""
    with _lock:
        try:
            f = open(path, "rb")
        except FileNotFoundError:
            _cache.pop(str(path), None)
            return []
        with f:
            st = os.fstat(f.fileno())
            c = _cache.get(str(path))
            if (not c or c["id"] != (st.st_dev, st.st_ino) or st.st_size < c["size"]
                    or (st.st_size == c["size"] and st.st_mtime_ns != c["mtime"])):
                c = {"id": (st.st_dev, st.st_ino), "offset": 0, "rows": {}}
            f.seek(c["offset"])
            data = f.read(st.st_size - c["offset"])
            done = data.rfind(b"\n") + 1                   # whole lines: the rest is a line still being written
            new = {}
            for line in data[:done].decode().splitlines():
                r = json.loads(line)
                new.setdefault(r["instance"], []).append((r.get("ts"), r.get("usd")))
            for who, rows in new.items():                  # all parsed: a malformed line leaves the cache as it was
                c["rows"].setdefault(who, []).extend(rows)
            c.update(offset=c["offset"] + done, size=st.st_size, mtime=st.st_mtime_ns)
            _cache[str(path)] = c
            rows = list(c["rows"].get(instance, ()))
            for line in data[done:].decode().splitlines():
                r = json.loads(line)
                if r["instance"] == instance:
                    rows.append((r.get("ts"), r.get("usd")))
            return rows
