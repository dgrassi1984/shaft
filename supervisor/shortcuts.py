"""The operator's Shortcuts on the Mac, through `/usr/bin/shortcuts`. Stdlib only.

An instance never reaches this itself: its sandbox has no Shortcuts. The broker runs a shortcut for it, as the operator,
only when the operator granted that shortcut to that instance (`shaft shortcuts <n> grant <shortcut>`, core.
shortcut_grants). A grant names the shortcut's identifier, so a rename keeps it and a new shortcut that takes an old
name does not inherit it.

  available()            [{id, name}] of every shortcut on this Mac, in the Shortcuts app's order.
  resolve(what, rows)    the row whose identifier or name is `what` (exact, then case-insensitive name).
  run(id, ...)           run it once with an optional input file; (output bytes, seconds) or ShortcutError.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

CLI = "/usr/bin/shortcuts"
LIST_TIMEOUT_S = 20
# The instance's tool runner gives a supervisor call 120 s (template broker._request): a run is stopped well before,
# so the instance hears why instead of a timeout of its own.
RUN_TIMEOUT_S = 90
MAX_OUTPUT_BYTES = 50_000_000
_ROW = re.compile(r"^(?P<name>.*) \((?P<id>[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})\)$")
ID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


class ShortcutError(RuntimeError):
    """A shortcut could not be listed or run: the message says why, in words for the instance and the operator."""


def present() -> bool:
    return os.access(CLI, os.X_OK)


def _env() -> dict[str, str]:
    """Only what the CLI needs: the supervisor's environment is never handed to a shortcut."""
    return {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "PATH", "LANG", "TMPDIR")}


def parse(text: str) -> list[dict]:
    """`shortcuts list --show-identifiers` → [{id, name}]; a line without an identifier is skipped."""
    rows = []
    for line in text.splitlines():
        if (m := _ROW.match(line.rstrip("\n"))):
            rows.append({"id": m["id"], "name": m["name"]})
    return rows


def available() -> list[dict]:
    if not present():
        raise ShortcutError("Shortcuts is not on this computer (macOS only)")
    try:
        r = subprocess.run([CLI, "list", "--show-identifiers"], capture_output=True, text=True, timeout=LIST_TIMEOUT_S,
                           env=_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ShortcutError(f"listing the shortcuts took more than {LIST_TIMEOUT_S} s") from None
    if r.returncode != 0:
        raise ShortcutError(f"shortcuts list failed: {(r.stderr or r.stdout).strip()[:300] or f'exit {r.returncode}'}")
    return parse(r.stdout)


def resolve(what: str, rows: list[dict]) -> dict | None:
    """The row `what` names: its identifier, its exact name, or its name in any case when only one matches."""
    what = str(what or "").strip()
    if not what:
        return None
    for key in ("id", "name"):
        if (hit := next((r for r in rows if r[key] == what), None)):
            return hit
    folded = [r for r in rows if r["name"].casefold() == what.casefold()]
    return folded[0] if len(folded) == 1 else None


def run(identifier: str, input_path: Path | None = None, timeout: float | None = None) -> tuple[bytes, float]:
    """Run the shortcut once. Its output (empty when it has none) and how long it took. Stopped at `timeout`: a shortcut
    that waits for someone (a menu, an Allow prompt) is waiting for a person who is not there."""
    timeout = RUN_TIMEOUT_S if timeout is None else timeout   # read at the call: a test or a knob can change it
    if not ID_RE.fullmatch(identifier):
        raise ShortcutError("not a shortcut identifier")
    if not present():
        raise ShortcutError("Shortcuts is not on this computer (macOS only)")
    with tempfile.TemporaryDirectory(prefix="shaft-shortcut-") as tmp:
        out = Path(tmp) / "output"
        args = [CLI, "run", identifier, "--output-path", str(out)]
        if input_path is not None:
            args[3:3] = ["--input-path", str(input_path)]
        started = time.monotonic()
        proc = subprocess.Popen(args, cwd=tmp, env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise ShortcutError(f"stopped after {timeout:.0f} s: it may be waiting for someone to answer it on the Mac "
                                "(a menu, a question or an Allow prompt)") from None
        took = time.monotonic() - started
        if proc.returncode != 0:
            raise ShortcutError(f"it failed: {err.strip()[:500] or f'exit {proc.returncode}'}")
        try:
            size = out.stat().st_size
        except FileNotFoundError:
            return b"", took
        if size > MAX_OUTPUT_BYTES:
            raise ShortcutError(f"its output is {size} bytes, more than {MAX_OUTPUT_BYTES}")
        return out.read_bytes(), took


def as_text(data: bytes) -> str | None:
    """The output as text when it is text (UTF-8, no NUL), else None: then it is kept as a file."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def extension(data: bytes) -> str:
    """A file extension for binary output, from its first bytes: the CLI says nothing of the type it wrote."""
    for magic, ext in ((b"%PDF", ".pdf"), (b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"),
                       (b"PK\x03\x04", ".zip"), (b"bplist", ".plist"), (b"RIFF", ".wav"), (b"ID3", ".mp3")):
        if data.startswith(magic):
            return ext
    if data[4:8] == b"ftyp":
        return ".heic" if data[8:12] in (b"heic", b"heix", b"mif1") else ".m4a" if data[8:11] == b"M4A" else ".mp4"
    return ".bin"
