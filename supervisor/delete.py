"""Remove an instance and every trace of it (`shaft delete <name>`, docs/plans/PLAN-kill-delete.md).

Refused while anything runs for it (`shaft kill <name>` first). Then, holding its run lock so no reply or wake can
start: an archive in .supervisor/deleted/<name>-<stamp>.tar.gz (unless --no-archive) — its mind store, a git bundle of
its branch, its folder without what can be rebuilt (.venv, .uv, .tmp), every file of its under .supervisor/ and every
row purged from the shared records — then the removal: the files named by it, its entries and rows in the shared
files, its Telegram token, its worktree and branch, its mind store last. Each step can run again: a delete interrupted
half-way is finished by running it again, and it also clears the leftovers of an instance whose folder is gone.

Every place an instance's state lives is listed here (`own_files`, `shared_edits`); what reads them by a name copes
with a name that is no longer an instance (trial.py, metrics.py, jobs.py, api.py, telegram.py) and never writes into
instances/<name>/ for it — that would recreate a half folder that blocks `shaft new <name>`.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .core import INSTANCES, NAME_RE, PROJECT, SUP_DIR, git, log, set_paused

ARCHIVES = "deleted"
SKIP_IN_FOLDER = {".venv", ".uv", ".tmp", "__pycache__"}   # rebuilt on demand (pyenv.py) or scratch


class Refused(Exception):
    pass


# -- what belongs to it ----------------------------------------------------------------------------------------------
def _matching(folder: Path, pattern: str) -> list[Path]:
    """Entries of `folder` whose whole name matches `pattern` (a regex: `wren-*` must not take `wren-2`'s files)."""
    rx = re.compile(pattern)
    return sorted(p for p in folder.iterdir() if rx.fullmatch(p.name)) if folder.is_dir() else []


def own_files(name: str) -> list[Path]:
    """Every file or folder that is the instance's alone, under .supervisor/ (and its battery items' backup)."""
    from . import battery
    n, s = re.escape(name), SUP_DIR
    ts = r"\d{8}-\d{6}"
    exact = [s / f"{name}.sb", s / f"core-changed-{name}", s / f"manual-wake-{name}.log", s / f"talk-history-{name}",
             s / f"update-{name}.log", s / f"update-queued-{name}", s / f"backfill-value-{name}.log",
             s / "locks" / f"recap-{name}.lock", s / "conversations" / name, s / "replying" / name,
             s / "judgments" / f"{name}.jsonl", s / "judgments" / f"{name}-second.jsonl",
             s / "judgments" / f"{name}-unsampled.jsonl", s / "judgments" / f"{name}-surprise-baseline.jsonl",
             s / "judgments" / f"{name}-surprise-baseline-cross-judge.jsonl", s / "judgments" / f"{name}-surprise-control.jsonl",
             s / "judgments" / f"{name}-traits.json", s / "code_changes" / f"{name}.jsonl",
             s / "gate-held" / name, s / "gate-runs" / f"{name}.jsonl",
             s / "probation" / f"{name}.json", s / "egress" / f"{name}.jsonl", s / "egress" / f"{name}-offline.jsonl",
             s / "egress" / f"{name}-control-offline.jsonl", s / "trials" / f"egress-{name}.jsonl", s / "trials" / name,
             s / "jobs" / f"{name}.jsonl", s / "watch" / name, s / "battery" / f"off-{name}", s / "battery" / "results" / f"{name}.jsonl",
             s / "battery" / f"{name}.sb", s / "battery" / f"{name}-control.sb", s / "battery" / "control" / name,
             s / "battery" / "exam" / name, s / "web" / "comments" / f"{name}.json", s / "tools" / f"{name}.json",
             s / "baselines" / f"{name}.json", s / "baselines" / f"{name}.files",
             s / "judgments" / f"{name}-anchoring.jsonl", s / "expectations" / f"{name}.jsonl",
             s / "expectations" / f"{name}-operator-said.jsonl",
             s / "expectations" / f"{name}-scored.jsonl", s / "expectations" / f"{name}-baselines.jsonl",
             s / "experiments" / f"{name}.jsonl",
             s / "experiments" / f"{name}-workspace.jsonl"]
    found = [p for p in exact if p.exists() or p.is_symlink()]
    found += _matching(s / "judgments" / "second-cases", rf"{n}@.+")
    found += _matching(s / "gate-logs", rf"{n}-{ts}-\d+\.log")
    found += _matching(s / "rejected", rf"{n}-{ts}-\d+")
    found += _matching(s / "battery", rf"control-{n}(-.*)?\.log")
    found += _matching(s / "evals", rf"(battery-{n}(-control)?|distinct-{n})-{ts}\.json")
    found += _matching(s / "datasets", rf"{n}-{ts}") + _matching(s / "adapters", rf"{n}-{ts}")
    found += _matching(s / "operator-inbox", rf"\d{{8}}-\d{{6}}-\d+-from-{n}\.md")
    found += _matching(s / "operator-inbox" / "read", rf"\d{{8}}-\d{{6}}-\d+-from-{n}\.md")
    found += _matching(s / "operator-inbox" / "attachments", rf"\d{{8}}-\d{{6}}-\d+-from-{n}")
    for folder in sorted((s / "battery" / "items").glob("*")) + sorted(Path(battery.BACKUP).glob("*")):
        found += [p for p in [folder / f"recall-{name}.json"] if p.exists()]
    found += [p for p in sorted((s / "evals").glob("breed-*.json")) if _bred_with(p, name)]
    found += [p for p in sorted((s / "web" / "chats").glob("*.json")) if _field(p, "instance") == name]
    return found


def _bred_with(path: Path, name: str) -> bool:
    """A breed record names its parents and child in its file name, but names may hold '-': read who it was."""
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return name in {rec.get("a"), rec.get("b"), rec.get("child")} or name in (rec.get("parents") or [])


def _field(path: Path, key: str):
    try:
        return json.loads(path.read_text()).get(key)
    except (OSError, ValueError, AttributeError):
        return None


# -- its rows and entries in the shared files ------------------------------------------------------------------------
@dataclass
class Edit:
    """One shared file and how to take the instance out of it: purge(text) -> (new text, what was taken out)."""
    path: Path
    purge: Callable[[str], tuple[str, list]]
    lock: Callable | None = None   # a context manager the file's own writers hold, if they hold one


def _jsonl(pred) -> Callable[[str], tuple[str, list]]:
    def purge(text: str) -> tuple[str, list]:
        keep, out = [], []
        for line in text.splitlines(keepends=True):
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            (out if isinstance(row, dict) and pred(row) else keep).append(line)
        return "".join(keep), out
    return purge


def _log_lines(name: str) -> Callable[[str], tuple[str, list]]:
    """A text log's lines about the instance: those whose subject — the word after the timestamp — is it
    ('[2026-10-20 14:18:52] wren: start live', '… wren http_request …', '… wren@trial:…'). A line that only mentions
    it in passing is another subject's."""
    rx = re.compile(rf"^\[[^\]]*\] {re.escape(name)}(:|@|\s|$)")

    def purge(text: str) -> tuple[str, list]:
        keep, out = [], []
        for line in text.splitlines(keepends=True):
            (out if rx.match(line) else keep).append(line)
        return "".join(keep), out
    return purge


def _json(fn) -> Callable[[str], tuple[str, list]]:
    """fn(data) takes the instance out of the loaded JSON in place and returns what it took (falsy: nothing)."""
    def purge(text: str) -> tuple[str, list]:
        try:
            data = json.loads(text)
        except ValueError:
            return text, []
        taken = fn(data)
        return (json.dumps(data, indent=1, ensure_ascii=False), [taken]) if taken else (text, [])
    return purge


def _pop(*path: str):
    """Take data[path[0]]…[path[-1]] out (a missing level: nothing)."""
    def fn(data):
        for key in path[:-1]:
            data = data.get(key) if isinstance(data, dict) else None
        return {path[-1]: data.pop(path[-1])} if isinstance(data, dict) and path[-1] in data else None
    return fn


def _all(*fns):
    def fn(data):
        taken = [t for f in fns if (t := f(data))]
        return taken or None
    return fn


def shared_edits(name: str) -> list[Edit]:
    from . import autonomy, battery, consult, jobs
    s = SUP_DIR
    mine = lambda v: v == name or (isinstance(v, str) and v.startswith(name + "@"))   # 'wren@trial:t-…:0A' too

    def jobs_state(data):
        taken = _all(_pop("jobs", name), _pop("wake", name))(data) or []
        if name in (data.get("reported") or []):
            data["reported"] = [n for n in data["reported"] if n != name]
            taken.append({"reported": name})
        return taken or None

    def telegram_state(data):
        taken = _all(_pop("bots", name), _pop("offsets", name), _pop("pending", name), _pop("requests", name), _pop("failures", name),
                     _pop("quarantined", name))(data) or []
        qs = data.get("questions") or {}
        gone = {k: qs.pop(k) for k in [k for k in qs if k.split(":")[0] == name]}
        return taken + ([{"questions": gone}] if gone else []) or None

    def dismissed(data):
        if not isinstance(data, list):
            return None
        gone = [i for i in data if f":{name}:" in i or i.endswith(f":{name}")]
        for i in gone:
            data.remove(i)
        return gone or None

    def answered(data):                                  # the API's kept answers (api.Answered), keyed "<name>:<id>"
        if not isinstance(data, dict):
            return None
        gone = {k: data.pop(k) for k in [k for k in data if k.split(":")[0] == name]}
        return [gone] if gone else None

    def dismissed_consults(data):
        gone = [r for r in data if mine(r.get("instance"))]
        data[:] = [r for r in data if not mine(r.get("instance"))]
        return gone

    edits = [Edit(s / "jobs" / "state.json", _json(jobs_state), jobs._editing_lock),
             Edit(s / "api-answered.json", _json(answered)),
             Edit(s / "schedule.json", _json(_all(_pop("wakes", name), _pop("cycles", name), _pop("skipped", name))),
                  autonomy.editing_lock),
             Edit(s / "battery" / "schedule.json", _json(_pop(name)), battery.schedule_lock),
             Edit(s / "recaps.json", _json(_pop(name))),
             Edit(s / "evals" / "surprise-baseline.json", _json(_pop("instances", name))),
             Edit(s / "telegram.json", _json(telegram_state)),
             Edit(s / "top_dismissed.json", _json(dismissed)),
             Edit(s / "cloud_usage.jsonl", _jsonl(lambda r: mine(r.get("instance")))),
             Edit(s / "consult_usage.jsonl", _jsonl(lambda r: mine(r.get("instance"))), consult.editing_lock),
             Edit(s / "consult_reservations.json", _json(dismissed_consults), consult.editing_lock),
             Edit(s / "coder_usage.jsonl", _jsonl(lambda r: mine(r.get("instance")))),
             Edit(s / "trial_usage.jsonl", _jsonl(lambda r: mine(r.get("instance")))),
             Edit(s / "doorbell.jsonl", _jsonl(lambda r: mine(r.get("instance")))),
             Edit(s / "judge_usage.jsonl", _jsonl(lambda r: str(r.get("purpose", "")).split()[-1:] == [name])),
             Edit(s / "messages.jsonl", _jsonl(lambda r: name in (r.get("from"), r.get("to"))))]
    for log_file in ("supervisor.log", "broker.log", "trials/broker.log", "battery/broker.log", "top-actions.log",
                     "web-actions.log"):
        edits.append(Edit(s / log_file, _log_lines(name)))
    return [e for e in edits if e.path.exists()]


def _rewrite(edit: Edit, apply: bool) -> list:
    """What `edit` takes out of its file now; with `apply`, the file without it. A writer that appends meanwhile
    (the ledgers are appended by other processes, without a lock) makes it read and filter again: nothing appended
    is lost."""
    for _ in range(20):
        try:
            before = edit.path.stat()
            text = edit.path.read_text()
        except FileNotFoundError:
            return []
        new, taken = edit.purge(text)
        if not apply or not taken:
            return taken
        tmp = edit.path.with_name(f".{edit.path.name}.delete-{os.getpid()}")
        tmp.write_text(new)
        os.chmod(tmp, before.st_mode & 0o777)
        now = edit.path.stat()
        if (now.st_size, now.st_mtime_ns) == (before.st_size, before.st_mtime_ns):
            os.replace(tmp, edit.path)
            return taken
        tmp.unlink(missing_ok=True)
        time.sleep(0.05)
    raise RuntimeError(f"{edit.path} kept changing while the deleted instance's rows were taken out; run it again")


def _edit(edit: Edit, apply: bool) -> list:
    if edit.lock is None:
        return _rewrite(edit, apply)
    with edit.lock():
        return _rewrite(edit, apply)


# -- its Telegram token, in the supervisor's .env (never read out, never archived) ----------------------------------------
def _env_key(name: str) -> str:
    from .telegram import token_key
    return token_key(name)


def _env_has(key: str) -> bool:
    path = PROJECT / ".env"
    return path.exists() and any(l.startswith(f"{key}=") for l in path.read_text().splitlines())


def _env_drop(key: str) -> bool:
    path = PROJECT / ".env"
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    keep = [l for l in lines if not l.startswith(f"{key}=")]
    if len(keep) == len(lines):
        return False
    tmp = path.with_name(f".env.delete-{os.getpid()}")
    tmp.write_text("\n".join(keep) + ("\n" if keep else ""))
    tmp.chmod(0o600)
    os.replace(tmp, path)
    return True


# -- the whole of it -------------------------------------------------------------------------------------------------
@dataclass
class Found:
    name: str
    folder: Path | None
    branch: bool
    mind: Path | None
    files: list[Path] = field(default_factory=list)
    edits: list[tuple[Edit, list]] = field(default_factory=list)
    token: bool = False

    def anything(self) -> bool:
        return bool(self.folder or self.branch or self.mind or self.files or self.edits or self.token)

    def describe(self) -> list[str]:
        out = []
        if self.folder:
            out.append(f"its folder {_shown(self.folder)}")
        if self.branch:
            out.append(f"branch instance/{self.name}")
        if self.mind:
            out.append(f"its mind store {_shown(self.mind)}")
        if self.files:
            out.append(f"{len(self.files)} file(s) of its under .supervisor/")
        if self.edits:
            rows = sum(len(t) for _, t in self.edits)
            out.append(f"{rows} row(s)/entr(y/ies) in {len(self.edits)} shared file(s)")
        if self.token:
            out.append("its Telegram bot token")
        return out


def find(name: str) -> Found:
    from . import mind
    folder = INSTANCES / name
    branch = bool(git("rev-parse", "--verify", "-q", f"refs/heads/instance/{name}", check=False).stdout.strip())
    store = mind.store(name)
    edits = [(e, taken) for e in shared_edits(name) if (taken := _edit(e, apply=False))]
    return Found(name, folder if folder.exists() or folder.is_symlink() else None, branch,
                 store if store.exists() else None, own_files(name), edits, _env_has(_env_key(name)))


def archive(f: Found, stamp: str) -> Path:
    """One tar.gz with everything of it that delete removes (but its bot token); read back whole before it counts."""
    out_dir = SUP_DIR / ARCHIVES
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{f.name}-{stamp}.tar.gz"
    part = final.with_name(final.name + ".part")
    try:
        _write_archive(f, stamp, part)
    except BaseException:
        part.unlink(missing_ok=True)   # nothing was removed yet: the delete stops here, leaving no half archive
        raise
    os.replace(part, final)
    return final


def _write_archive(f: Found, stamp: str, part: Path) -> None:
    skip_folder = lambda ti: None if Path(ti.name).name in SKIP_IN_FOLDER or ti.name == "folder/.git" else ti
    with tempfile.TemporaryDirectory(prefix="shaft-delete-") as tmp, tarfile.open(part, "w:gz") as tar:
        if f.mind:
            tar.add(f.mind, arcname="mind.git")
        if f.branch:
            bundle = Path(tmp) / f"instance-{f.name}.bundle"
            git("bundle", "create", "-q", str(bundle), f"refs/heads/instance/{f.name}")
            tar.add(bundle, arcname=bundle.name)
        if f.folder:
            tar.add(f.folder, arcname="folder", filter=skip_folder)
        for p in f.files:
            arc = (f"supervisor/{p.relative_to(SUP_DIR)}" if p.is_relative_to(SUP_DIR)
                   else f"elsewhere/{p.name}" if not p.is_dir() else f"elsewhere/{p.parent.name}/{p.name}")
            tar.add(p, arcname=arc)
        for edit, taken in f.edits:
            body = "".join(t if isinstance(t, str) else json.dumps(t, ensure_ascii=False) + "\n" for t in taken)
            _add_bytes(tar, f"purged/{edit.path.relative_to(SUP_DIR)}", body.encode())
        _add_bytes(tar, "RESTORE.md", RESTORE.format(name=f.name, stamp=stamp).encode())
    with tarfile.open(part, "r:gz") as tar:   # every member decompressed once: a truncated archive fails here
        for member in tar:
            if member.isfile():
                tar.extractfile(member).read()


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(arcname)
    info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
    tar.addfile(info, io.BytesIO(data))


RESTORE = """# {name}, deleted {stamp}

What `shaft delete {name}` removed, kept here. To bring it back by hand (no `shaft new {name}` meanwhile):

    mkdir /tmp/{name} && tar -xzf .supervisor/deleted/{name}-{stamp}.tar.gz -C /tmp/{name}
    git fetch /tmp/{name}/instance-{name}.bundle refs/heads/instance/{name}:refs/heads/instance/{name}
    git worktree add instances/{name} instance/{name}
    rsync -a /tmp/{name}/folder/ instances/{name}/          # its mind, notes, browser profile, own secrets
    mv /tmp/{name}/mind.git .supervisor/minds/{name}.git
    rsync -a /tmp/{name}/supervisor/ .supervisor/          # its records (tools, judgments, trials, …)

purged/ holds its rows taken out of the shared files (ledgers, logs, state): append them back if you need them.
Its Telegram bot token is not here: `shaft telegram token {name}` sets one again.
"""


def delete(name: str, *, keep_archive: bool = True, say=print) -> int:
    """Remove the instance (refusals raise Refused). The caller has asked for the confirmation."""
    from . import browser, kill, mind
    from .runner import Busy, run_lock
    if not NAME_RE.match(name) or name == "operator":
        raise Refused(f"not an instance name: {name!r}")
    found = find(name)
    if not found.anything():
        raise Refused(f"no instance named {name!r}, and nothing of one left")
    if left := kill.running(name):
        raise Refused(f"{name} is busy ({'; '.join(left)}): shaft kill {name} first")
    set_paused(name, True, reason="a delete")
    try:
        with run_lock(name, wait=False, kind="delete"):   # nor a reply or a cycle started by hand
            found = find(name)                              # as it is now, under the lock
            own_accounts = bool(found.folder and (found.folder / "secrets").exists())
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            where = archive(found, stamp) if keep_archive else None
            log(f"delete {name} by operator: " + ", ".join(found.describe())
                + (f"; archived to {_shown(where)}" if where else "; no archive (--no-archive)"))
            if found.folder:
                browser.sweep(found.folder)   # no orphaned Chromium keeps its profile open
            for edit, _ in found.edits:
                _edit(edit, apply=True)
            if found.token:
                _env_drop(_env_key(name))
            for p in found.files:
                _remove(p)
            _remove_git(name, found)
            if found.mind:
                shutil.rmtree(found.mind)
    except Busy:
        raise Refused(f"{name} was just taken by a run: shaft kill {name} first") from None
    (SUP_DIR / "locks" / f"{name}.lock").unlink(missing_ok=True)
    set_paused(name, False, reason="a delete")
    gone = [p for p in own_files(name) if p.exists()] + ([INSTANCES / name] if (INSTANCES / name).exists() else [])
    if gone or mind.exists(name):
        raise RuntimeError(f"{name}: still there after the delete: {', '.join(map(str, gone or [mind.store(name)]))}")
    say(f"deleted {name}: " + ", ".join(found.describe()))
    say(f"archive: {_shown(where)} (RESTORE.md inside)" if where else "no archive kept")
    if own_accounts:
        say(f"note: accounts {name} signed in to on its own (its secrets/) are not revoked: revoke them at the "
            "provider if they should end too")
    log(f"delete {name}: done")
    return 0


def _shown(p: Path) -> str:
    """A path as the operator reads it: relative to the project when it is inside it."""
    return str(p.relative_to(PROJECT)) if p.is_relative_to(PROJECT) else str(p)


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink(missing_ok=True)


def _remove_git(name: str, found: Found) -> None:
    folder = INSTANCES / name
    if folder.exists():
        r = git("worktree", "remove", "--force", "--force", str(folder), check=False)   # twice: even a locked one
        if r.returncode != 0 and folder.exists():
            shutil.rmtree(folder)   # a half folder no worktree owns (or git refused): the archive has it
    git("worktree", "prune")
    if found.branch:
        git("branch", "-D", f"instance/{name}")
