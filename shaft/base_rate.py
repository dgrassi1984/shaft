"""My base rates, shown when I declare a confidence (PLAN-calibration-feedback).

The supervisor writes self/calibration.json with each stats update: for each confidence band, how often the
criterion was fully met in my judged cycles. Seeing it before I commit to a number is the test of whether the
record moves my confidence; my stats.md has shown the same figures every cycle and has not.
"""
from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

FILE = "self/calibration.json"
LIMIT = 64 * 1024   # a few bands; anything bigger is not the supervisor's file
COUNTS = ("lo", "hi", "met", "n", "older_met", "older_n")


def _read_no_link(root: Path, rel: str) -> bytes:
    """The plain file root/rel, with no component followed if it is a link: my folder can hold links, and the
    record must be the file the supervisor wrote, not whatever a link points to."""
    *folders, name = Path(rel).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in folders:
            inner = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = inner
        # O_NONBLOCK: a planted FIFO must not hang the cycle at open
        fd_file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    with os.fdopen(fd_file, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError("not a plain file")
        data = f.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError(f"larger than {LIMIT} bytes")
    return data


def load(root: Path) -> tuple[dict | None, str | None]:
    """(rates, None), or (None, None) when there is no file yet, or (None, why) when it cannot be used —
    the why goes into my intent as base_rate_error, so a broken file is seen, not silently skipped."""
    try:
        rates = json.loads(_read_no_link(Path(root), FILE))
    except FileNotFoundError:
        return None, None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):   # what O_NOFOLLOW raises on a link
            return None, f"{FILE}: a link (or self/ is not a folder), not read"
        return None, f"{FILE}: {e}"
    except (ValueError, RecursionError) as e:   # bad JSON or nested too deep, bad UTF-8, not a plain file, too big
        return None, f"{FILE}: {e}"
    bands = rates.get("bands") if isinstance(rates, dict) else None
    if not isinstance(bands, list) or not all(
            isinstance(b, dict) and all(type(b.get(k)) is int for k in COUNTS) for b in bands):
        return None, f"{FILE}: not a list of bands with {', '.join(COUNTS)}"
    return rates, None


def has_data(rates: dict | None) -> bool:
    return bool(rates) and any(b["n"] or b["older_n"] for b in rates["bands"])


def band_for(rates: dict, confidence: int) -> dict | None:
    """The band holding this confidence (lo <= c < hi; the last band also holds c == hi), or None if it has
    no judged cycles under either question."""
    bands = rates["bands"]
    for i, b in enumerate(bands):
        if b["lo"] <= confidence < b["hi"] or (i == len(bands) - 1 and confidence == b["hi"]):
            return b if b["n"] or b["older_n"] else None
    return None


def _pct(met: int, n: int) -> int:
    return round(100 * met / n)


def describe(band: dict, confidence: int) -> str:
    """What declare_intent shows me: the band's record, then the request to confirm or revise."""
    clauses = []
    if band["n"]:
        clauses.append(f", the criterion was fully met in {band['met']} of {band['n']} judged cycles "
                       f"({_pct(band['met'], band['n'])}%)")
    if band["older_n"]:
        older = f"{band['older_met']} of {band['older_n']} ({_pct(band['older_met'], band['older_n'])}%)"
        clauses.append(f"; under the older question, {older}" if band["n"] else
                       f", under the older question, the criterion was fully met in {older}")
    return (f"Before acting, look at your record: when you said {band['lo']}–{band['hi']}%{''.join(clauses)}. "
            f"Call confirm_confidence with your final probability (0-100) that the criterion will be fully met — "
            f"keep {confidence} or change it. Your other tools stay closed until you do.")
