"""Held candidates and each instance's gate record (docs/plans/PLAN-thelos-review-2.md M2, S2).

A code change whose gate failed on the supervisor's side — its baseline red: the shared supervisor tests failing or
out of time, the gate's own error, a test of the supervisor's that fails on the approved code too (gate.Output) — is
reverted as every failed change is, and held here, not lost: `.supervisor/gate-held/<name>/<id>/` keeps the bytes it
wrote, the digest of each file as it stood before it (its base), the instance's commit then and a patch to read. When a
later warm-up finds the baseline green, runner.regate_held puts each held change back on the code as it is then —
only if every file it touched is still as it was when held — and gates it again like any run's change. The instance
cannot write here (its sandbox writes only its own folder): what is re-applied is what it wrote, byte for byte.

`.supervisor/gate-runs/<name>.jsonl` records every gate an instance's change went through, for record(kind="gate"):
its own rows only, never another instance's.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

HELD = "gate-held"     # under SUP_DIR: <name>/<id>/{meta.json, files/<path>, candidate.patch}
RUNS = "gate-runs"     # under SUP_DIR: <name>.jsonl
TRIES = 3              # re-gates that find the baseline red again before a held change is dropped
TAIL_CHARS = 3000      # of a failed gate's output, in its row
PATCH_CHARS = 200000   # of a held change's patch: it is for reading; the files are what is re-applied
ROWS_READ = 100000     # lines of a record read back at most


def _sup() -> Path:
    from .core import SUP_DIR
    return SUP_DIR


def _digest(v) -> str | None:
    """A file's identity as the base of a held change: None for no file, its bytes' sha256, else what it was."""
    if v is None:
        return None
    return hashlib.sha256(v).hexdigest() if isinstance(v, bytes) else f"not a file: {v!r}"


def _patch(paths: list[str], before: dict, after: dict) -> str:
    out = []
    for p in paths:
        old, new = before.get(p), after.get(p)
        if (old is not None and not isinstance(old, bytes)) or b"\0" in (old or b"") + (new or b""):
            out.append(f"Binary file {p} differs\n")
            continue
        a = (old or b"").decode(errors="replace").splitlines(keepends=True)
        b = (new or b"").decode(errors="replace").splitlines(keepends=True)
        for line in difflib.unified_diff(a, b, f"a/{p}" if old is not None else "/dev/null",
                                         f"b/{p}" if new is not None else "/dev/null"):
            out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    text = "".join(out)
    return text if len(text) <= PATCH_CHARS else text[:PATCH_CHARS] + f"\n[… cut at {PATCH_CHARS} characters]\n"


def save(name: str, head: str, before: dict, after: dict, paths: list[str], why: str, failing: list[str]) -> str:
    """Hold the change `before` → `after` on `paths` (runner._settle, after a baseline failure): its bytes, each
    file's base digest, the instance's commit `head` it was made on, and why it was held. Written whole into a staging
    folder, then renamed: a half-written held change never exists. Returns its id."""
    d = _sup() / HELD / name
    d.mkdir(parents=True, exist_ok=True)
    hid = f"{datetime.now():%Y%m%d-%H%M%S-%f}"
    staging = d / f".new-{hid}"
    try:
        files = {}
        for p in paths:
            new = after.get(p)
            if new is not None and not isinstance(new, bytes):
                raise ValueError(f"{p}: {new!r} is never held as code")
            files[p] = {"base": _digest(before.get(p)), "deleted": new is None}
            if new is not None:
                target = staging / "files" / p
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(new)
        (staging / "candidate.patch").write_text(_patch(paths, before, after))
        meta = {"id": hid, "held_at": datetime.now().isoformat(timespec="seconds"), "base_sha": head,
                "files": files, "why": why, "failing": failing, "tries": 0}
        (staging / "meta.json").write_text(json.dumps(meta, indent=1))
        staging.rename(d / hid)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return hid


def pending(name: str) -> list[dict]:
    """The instance's held changes, oldest first (their ids sort by time). One whose record cannot be read is
    skipped here and named by describe."""
    d = _sup() / HELD / name
    out = []
    for f in sorted(d.glob("*/meta.json")) if d.is_dir() else []:
        if f.parent.name.startswith("."):
            continue
        try:
            meta = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("id") == f.parent.name and isinstance(meta.get("files"), dict):
            out.append(meta)
    return out


def waiting() -> list[str]:
    """The instances with a held change."""
    d = _sup() / HELD
    return sorted(p.name for p in d.iterdir() if p.is_dir() and pending(p.name)) if d.is_dir() else []


def applies(meta: dict, code: dict) -> list[str]:
    """The files a held change touched that are no longer as they were when it was held ([]: it applies to `code`,
    a snapshot of the instance's code now)."""
    return [p for p, f in meta["files"].items() if _digest(code.get(p)) != f.get("base")]


def apply(name: str, root: Path, meta: dict) -> None:
    """Write a held change onto the instance's code: each file as a new file renamed into place, each deleted file
    removed, through descriptors, no link followed (gate._write_fresh, core.unlink_beneath)."""
    from . import gate
    from .core import unlink_beneath
    d = _sup() / HELD / name / meta["id"]
    for p, f in sorted(meta["files"].items()):
        if f.get("deleted"):
            unlink_beneath(root, p)
        else:
            gate._write_fresh(root, p, (d / "files" / p).read_bytes())


def retried(name: str, meta: dict, why: str, failing: list[str]) -> bool:
    """A re-gate found the baseline red again: one more try counted. False when that was the last of TRIES — the
    held change is dropped, as it would be held for ever otherwise."""
    meta = {**meta, "tries": int(meta.get("tries", 0)) + 1, "why": why, "failing": failing}
    if meta["tries"] >= TRIES:
        drop(name, meta["id"])
        return False
    path = _sup() / HELD / name / meta["id"] / "meta.json"
    tmp = path.with_name(f".meta.json.{os.getpid()}")
    tmp.write_text(json.dumps(meta, indent=1))
    os.replace(tmp, path)
    return True


def drop(name: str, hid: str) -> None:
    shutil.rmtree(_sup() / HELD / name / hid, ignore_errors=True)


def log_run(name: str, outcome: str, files: list[str], why: str = "", failing: list[str] | tuple = (),
            output: str = "", held: str = "", result: str = "") -> None:
    """One row of the instance's gate record. `outcome`: kept, candidate-failed, baseline-failed (nothing to hold),
    held, or re-gated (`result` says how it ended). Best effort: a record that cannot be written never stops a
    settle (it is logged)."""
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "outcome": outcome, "files": files}
    row.update({k: v for k, v in (("result", result), ("held", held), ("why", why), ("failing", list(failing)),
                                  ("output", output[-TAIL_CHARS:] if outcome != "kept" else "")) if v})
    try:
        d = _sup() / RUNS
        d.mkdir(parents=True, exist_ok=True)
        with (d / f"{name}.jsonl").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        from .core import log
        log(f"{name}: gate record NOT written: {e}")


def rows(name: str, as_of: datetime | None = None) -> list[dict]:
    path = _sup() / RUNS / f"{name}.jsonl"
    out = []
    try:
        lines = path.read_text(errors="replace").splitlines()[-ROWS_READ:]
    except OSError:
        return []
    cut = as_of.isoformat(timespec="seconds") if as_of else None
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:   # one torn line never takes the record down
            continue
        if isinstance(r, dict) and (cut is None or str(r.get("ts", "")) < cut):
            out.append(r)
    return out


def describe(name: str, limit: int = 20, as_of: datetime | None = None) -> str:
    """record(kind="gate"): the instance's last gate runs, newest last — time, outcome, files, why, failing tests and
    the last TAIL_CHARS characters of a failed gate's output — then its held changes. `as_of` (a trial replay): the
    runs before then only; what is held now is not what was held then, so it is left out."""
    lines = []
    for r in rows(name, as_of)[-limit:]:
        what = r.get("outcome", "?") + (f" ({r['result']})" if r.get("result") else "")
        lines.append(f"{r.get('ts', '?').replace('T', ' ')} {what}: {', '.join(r.get('files') or []) or '(config only)'}"
                     + (f" — {r['why']}" if r.get("why") else "") + (f" [held {r['held']}]" if r.get("held") else ""))
        lines += [f"  {n}" for n in r.get("failing") or []]
        if r.get("output"):
            lines.append("  last output:\n" + "\n".join(f"    {l}" for l in r["output"].splitlines()))
    if as_of is None:
        held = pending(name)
        lines.append(f"held changes: {len(held)}" + (" (re-gated automatically when a warm-up finds the gate's "
                                                     "baseline green)" if held else ""))
        for m in held:
            lines.append(f"  {m['id']} held {m.get('held_at', '?').replace('T', ' ')}, re-gated {m.get('tries', 0)} of "
                         f"{TRIES} times: {', '.join(m['files'])} — {m.get('why', '')}")
    return "\n".join(lines)
