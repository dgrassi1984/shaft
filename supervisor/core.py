"""Shared supervisor plumbing: paths, knobs, logging, git, instance lookup, commits."""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
# What a network call can raise when the far end is unreachable, resets, or stops mid-answer: http.client's
# IncompleteRead and RemoteDisconnected are HTTPExceptions, not OSErrors, so a retry on (URLError, OSError) let a
# reply cut short by OpenRouter escape — it crashed the broker's handler and wren's sleep of 2026-10-15 04:34.
NETWORK_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)
# The system interpreter, even when the supervisor itself runs inside a venv (the terminal client
# runs in .venv-ui): sandboxed agents cannot read a venv inside the home folder, and agents and
# launchd jobs must not depend on one.
PYTHON = str(getattr(sys, "_base_executable", None) or sys.executable)
INSTANCES = PROJECT / "instances"
TEMPLATE = PROJECT / "template"
SUP_DIR = Path(os.environ.get("SHAFT_SUPERVISOR_DIR") or PROJECT / ".supervisor")  # tests point it elsewhere
HOME = Path.home()
# The mind: what an instance remembers and writes for itself. Never in the project's git (which can be pushed):
# versioned in its private mind store (supervisor/mind.py, PLAN-mind-out-of-git.md).
MIND_PATHS = ("self", "memory", "notes", "inbox")
# The non-code state the instance's branch still holds: skills/ is a gene (evolution recombines it, the template
# ships one), not memory (PLAN-mind-out-of-git.md D2).
SKILL_PATHS = ("skills",)
DEFAULT_INSTANCE = "shaft"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def knob(name: str, default: str, parse):
    """CLI-less operator knob: env var > default; a bad value warns and falls back."""
    raw = os.environ.get(name)
    if raw is None:
        return parse(default), "default"
    try:
        return parse(raw), "env"
    except ValueError:
        print(f"warning: {name}={raw!r} is invalid; using {default} [default]", file=sys.stderr)
        return parse(default), "default"


CYCLE_TIMEOUT_S, CYCLE_SRC = knob("SHAFT_CYCLE_TIMEOUT_S", "2400", int)
SLEEP_TIMEOUT_S, SLEEP_SRC = knob("SHAFT_SLEEP_TIMEOUT_S", "5400", int)   # nightly, nobody waiting
# 8780: SHAFT's own Bonsai 2 server (supervisor/bonsai_server.py); Atomic Chat's 1337 is no longer used.
LLM_PORTS, PORTS_SRC = knob("SHAFT_LLM_PORTS", "8780", lambda s: [int(p) for p in s.split(",")])
CORE_MIN_HOURS, CORE_SRC = knob("SHAFT_CORE_MIN_HOURS", "72", int)
EMBED_PORT, EMBED_SRC = knob("SHAFT_EMBED_PORT", "8782", int)   # SHAFT's embedding service (memory search)
BROKER_MAX_CALLS, BROKER_SRC = knob("SHAFT_BROKER_MAX_CALLS", "60", int)
# A conversation (terminal or API) quiet this long is closed and written up into the journal.
IDLE_MINUTES, IDLE_SRC = knob("SHAFT_IDLE_MINUTES", "30", int)
API_TALKS = SUP_DIR / "api_talks.json"


def describe_knobs() -> str:
    return (f"cycle_timeout_s={CYCLE_TIMEOUT_S} [{CYCLE_SRC}] sleep_timeout_s={SLEEP_TIMEOUT_S} [{SLEEP_SRC}] llm_ports={LLM_PORTS} [{PORTS_SRC}] "
            f"core_min_hours={CORE_MIN_HOURS} [{CORE_SRC}] broker_max_calls={BROKER_MAX_CALLS} [{BROKER_SRC}] "
            f"idle_minutes={IDLE_MINUTES} [{IDLE_SRC}] "
            f"python={PYTHON}")


def log(msg: str) -> None:
    SUP_DIR.mkdir(exist_ok=True)
    with (SUP_DIR / "supervisor.log").open("a") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def secrets_env() -> dict[str, str]:
    """Supervisor-only secrets from PROJECT/.env (never passed to instances)."""
    out: dict[str, str] = {}
    path = PROJECT / ".env"
    for line in path.read_text().splitlines() if path.exists() else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"\'')
    return out


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT), *args], capture_output=True, text=True, check=check)


def git_wt(root: Path, *args: str, check: bool = True, project: Path | None = None,
           input: str | None = None, text: bool = True) -> subprocess.CompletedProcess:
    """Run git in an instance worktree without trusting anything inside it.

    The worktree's `.git` file is writable by the instance, which could point it at
    a fake repository whose config runs commands (hooks, fsmonitor). So the git dir
    is taken from the main repository's own worktree registry, and hooks are off.
    """
    admin = _worktree_admin_dir(root, project or PROJECT)
    return subprocess.run(
        ["git", f"--git-dir={admin}", f"--work-tree={root}", "-c", "core.hooksPath=/dev/null",
         "-c", "core.fsmonitor=false", *args],
        cwd=root, capture_output=True, text=text, check=check, input=input)


def _worktree_admin_dir(root: Path, project: Path) -> Path:
    for admin in (project / ".git" / "worktrees").glob("*"):
        gitdir = admin / "gitdir"
        if gitdir.exists() and Path(gitdir.read_text().strip()).resolve() == (root / ".git").resolve():
            return admin
    raise SystemExit(f"{root} is not a registered worktree of {project} (try: shaft setup)")


def instance_root(name: str) -> Path:
    root = INSTANCES / name
    if not (root / "shaft").is_dir() or not (root / ".git").exists():
        known = ", ".join(instance_names()) or "none"
        raise SystemExit(f"no instance named {name!r} (instances: {known})")
    return root


def instance_names() -> list[str]:
    return sorted(p.name for p in INSTANCES.iterdir() if (p / "shaft").is_dir()) if INSTANCES.exists() else []


def paused(name: str) -> bool:
    return (SUP_DIR / "paused").exists() or (SUP_DIR / f"paused-{name}").exists()


def instance_config(name: str) -> dict[str, str]:
    """An instance's config.env (KEY="value"  # comment), read as text — never executed."""
    try:
        return config_values((instance_root(name) / "config.env").read_text())
    except OSError:
        return {}


CONFIG_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _config_key(line: str) -> str | None:
    key, eq, _ = line.partition("=")
    return key.strip() if eq and key.strip().isidentifier() else None


def config_values(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if key := _config_key(line):
            value = line.partition("=")[2].strip()
            out[key] = value[1:].split('"')[0] if value.startswith('"') else value.split("#")[0].strip()
    return out


def config_line(text: str, key: str) -> str | None:
    """The line that sets `key` in config.env text (the last one: it is the one that counts), or None."""
    found = [line for line in text.splitlines() if _config_key(line) == key]
    return found[-1] if found else None


def config_with(text: str, key: str, line: str | None) -> str:
    """config.env text with the line that sets `key` replaced by `line` — appended if no line sets it,
    removed if `line` is None. Every other line is kept as it is."""
    lines = text.splitlines()
    at = max((i for i, l in enumerate(lines) if _config_key(l) == key), default=None)
    if at is None:
        lines += [line] if line is not None else []
    elif line is None:
        del lines[at]
    else:
        lines[at] = line
    return "\n".join(lines) + "\n" if lines else ""


def _tool_settings(name: str) -> dict:
    """What the operator set for this instance (`shaft tools`, `shaft share`), in the supervisor's
    folder — never in the instance's config.env, which the instance can change (set_config): it must not widen
    its own access."""
    try:
        data = json.loads((SUP_DIR / "tools" / f"{name}.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _update_tool_settings(name: str, **changes) -> None:
    """Change some keys and keep the rest: each setter owns its own key (one setter once rewrote the file with
    only its own keys, which dropped the shared-folder grant)."""
    data = {**_tool_settings(name), **changes}
    (SUP_DIR / "tools").mkdir(parents=True, exist_ok=True)
    (SUP_DIR / "tools" / f"{name}.json").write_text(json.dumps({k: v for k, v in data.items() if v is not None},
                                                               indent=1))


def disabled_tools(name: str) -> set[str]:
    """Supervisor tools the operator switched off for this instance (`shaft tools <name> off <tool>`): an
    instance must not switch a tool that costs the operator money (delegate_coding) back on for itself."""
    return set(_tool_settings(name).get("off", []))


# -- the operator's folder an instance may read ----------------------------------------------------------------
# Daniele, 2026-10-13: shaft asked for "a clearly scoped folder" of his work. Read-only: the instance still writes
# only in its own folder. Its sandbox and `run` may read it; its file tools show it as shared/ (SHAFT_SHARED).
def _share_forbidden() -> tuple[Path, ...]:
    """SHAFT's own state: the project (instances, supervisor code), the supervisor's folder wherever it is now, and
    the battery's held-out items backup. Read at call time, not import time: SUP_DIR is moved by tests."""
    return PROJECT, SUP_DIR, HOME / "Library" / "Application Support" / "SHAFT"


def shared_folder(name: str) -> Path | None:
    """The folder the operator lets this instance read (`shaft share <name> <folder>`), or None."""
    path = _tool_settings(name).get("shared")
    return Path(path) if path else None


def check_shared_folder(folder: Path | str) -> Path:
    """The resolved folder, if it may be shared: an existing directory that neither holds nor sits inside SHAFT's
    own state (other instances, the supervisor's folder, the battery's held-out items)."""
    path = Path(folder).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    for forbidden in (f.resolve() for f in _share_forbidden()):
        if path == forbidden or forbidden in path.parents or path in forbidden.parents:
            raise ValueError(f"{path} overlaps {forbidden}: SHAFT's own state is never shared")
    return path


def set_shared_folder(name: str, folder: Path | str | None) -> Path | None:
    path = check_shared_folder(folder) if folder else None
    _update_tool_settings(name, shared=str(path) if path else None)
    return path


# -- the operator's mail an instance may read (PLAN-mail.md) ------------------------------------------------------
# Daniele, 2026-10-15: an instance he picks reads the Gmail threads carrying a label he picks. No Gmail credential can
# be limited to a label, so the grant and the credential stay here and supervisor/mail.py enforces the label.
def mail_grants(name: str) -> list[dict]:
    """The labels this instance may read (`shaft mail <name> grant <alias> <label>`): [{account, label}]."""
    raw = _tool_settings(name).get("mail")
    return [{"account": g["account"], "label": g["label"]} for g in raw if isinstance(g, dict)
            and isinstance(g.get("account"), str) and isinstance(g.get("label"), str)] if isinstance(raw, list) else []


def set_mail_grants(name: str, grants: list[dict]) -> list[dict]:
    keep: list[dict] = []
    for g in grants:
        g = {"account": g["account"], "label": g["label"]}
        if g not in keep:
            keep.append(g)
    _update_tool_settings(name, mail=keep or None)
    return keep


# -- an instance's own keys ---------------------------------------------------------------------------
# Its own (Daniele, 2026-10-13: "they should have their own key and be able to manage them"), kept in its
# folder at secrets/secrets.json — but handled only by the supervisor: the instance stores, lists and
# deletes keys through broker tools, and names a key in http_request for the broker to add. Its sandbox,
# plugins and `run` cannot read the file, so a value never comes back into its context (prompts may go to
# a cloud model; fetched pages may try to talk it into sending a key elsewhere). Never committed, never
# copied into another instance, never shown to the coder.
SECRETS_DIR = "secrets"
SECRET_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


def read_secrets(root: Path) -> dict[str, dict]:
    path = contained(root, SECRETS_DIR, "secrets.json")
    try:
        return json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def store_secret(root: Path, key: str, value: str, hosts: list[str], header: str = "Authorization",
                 scheme: str = "Bearer") -> None:
    if not SECRET_NAME_RE.match(key):
        raise ValueError("a key name is lowercase letters, digits, - or _ (e.g. github)")
    hosts = sorted({h.strip().lower() for h in hosts if h.strip()})
    if not value.strip() or not hosts:
        raise ValueError("a key needs a value and at least one host it may be sent to")
    store = read_secrets(root)
    store[key] = {"value": value.strip(), "hosts": hosts, "header": header, "scheme": scheme,
                  "created": datetime.now().isoformat(timespec="seconds")}
    _write_secrets(root, store)


def delete_secret(root: Path, key: str) -> bool:
    store = read_secrets(root)
    found = store.pop(key, None) is not None
    if found:
        _write_secrets(root, store)
    return found


def _write_secrets(root: Path, store: dict) -> None:
    safe_mkdir(root, SECRETS_DIR)
    path = contained(root, SECRETS_DIR, "secrets.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(store, indent=1))
    os.chmod(path, 0o600)


def set_tool(name: str, tool: str, on: bool) -> set[str]:
    off = disabled_tools(name) - {tool} if on else disabled_tools(name) | {tool}
    _update_tool_settings(name, off=sorted(off))
    return off


def journal_entries(root: Path) -> list[str]:
    journal = root / "memory" / "journal.md"
    if not journal.exists():
        return []
    return [b.strip() for b in journal.read_text().split("\n---\n") if b.strip().startswith("## ")]


class UnsafePath(Exception):
    pass


def contained(root: Path, *parts: str) -> Path:
    """A path inside an instance folder that is safe for the (unsandboxed) supervisor to write.

    Instances can create symlinks in their own folder; following one would let an instance aim a
    supervisor write anywhere on the Mac. So no component may be a symlink, and the result must
    resolve inside the folder."""
    root = Path(root)
    if root.is_symlink():
        raise UnsafePath(f"{root} is a symlink")
    path = root.joinpath(*parts)
    probe = root
    for part in path.relative_to(root).parts:
        probe = probe / part
        if probe.is_symlink():
            raise UnsafePath(f"{probe} is a symlink; the supervisor does not follow links inside instances")
    if path.resolve() != root.resolve() and root.resolve() not in path.resolve().parents:
        raise UnsafePath(f"{path} resolves outside {root}")
    return path


def safe_write(root: Path, rel: str, text: str, append: bool = False) -> Path:
    path = contained(root, *Path(rel).parts)
    contained(root, *Path(rel).parent.parts).mkdir(parents=True, exist_ok=True)
    contained(root, *Path(rel).parts)            # re-check after creating parents
    with path.open("a" if append else "w") as f:
        f.write(text)
    return path


def safe_write_bytes(root: Path, rel: str, data: bytes) -> Path:
    """safe_write for a file's bytes (attachments); the last component is opened without following a link."""
    contained(root, *Path(rel).parent.parts).mkdir(parents=True, exist_ok=True)
    path = contained(root, *Path(rel).parts)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def safe_mkdir(root: Path, rel: str) -> Path:
    path = contained(root, *Path(rel).parts)
    path.mkdir(parents=True, exist_ok=True)
    return contained(root, *Path(rel).parts)


def journal_note(root: Path, title: str, body: str) -> None:
    """Supervisor's own minimal journal append (deliberately not the agent's code)."""
    safe_write(root, "memory/journal.md",
               f"\n---\n## {datetime.now():%Y-%m-%d %H:%M} — {title}\n\n{body.strip()}\n", append=True)


def not_mind() -> list[str]:
    """A pathspec for the whole work tree except the mind paths (for reading the branch: log, show)."""
    return [".", *(f":(exclude,top){p}" for p in MIND_PATHS)]


def add_all_but_mind(root: Path, project: Path | None = None) -> None:
    """`git add -A` of everything git would add, except the mind paths, whatever .gitignore says. The files are
    listed first (an exclude pathspec makes `add` fail once the excluded folder is ignored), so no mind file is
    ever hashed into the project's repository either."""
    out = git_wt(root, "ls-files", "-z", "--others", "--exclude-standard", "--modified", "--deleted",
                 project=project).stdout
    paths = sorted({p for p in out.split("\0") if p and Path(p).parts[0] not in MIND_PATHS})
    if paths:
        git_wt(root, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", input="\0".join(paths),
               project=project)


def commit_state(name: str, kind: str, kept_code: list[str], root: Path | None = None,
                 project: Path | None = None, note: str = "") -> bool:
    """Commit the instance's mind to its mind store, then its skills and the code changes that passed the gate
    to its branch — the same message on both sides. A mind path is never staged on the branch, whatever its
    .gitignore says.

    Only files git actually staged are named in the branch commit: an empty directory (skills/ on a young
    instance) is unknown to git and would fail as a pathspec. Returns False — after logging git's own error —
    if either commit failed; the mind is committed first, so a failure there leaves the branch untouched too.
    """
    from . import mind
    root = root or INSTANCES / name
    wt = lambda *a, check=True, input=None: git_wt(root, *a, check=check, project=project, input=input)  # noqa: E731
    msg = f"{name}: {kind} {datetime.now():%Y-%m-%d %H:%M}"
    try:
        if kept_code:   # git refuses to add an ignored path, and one such path fails the whole commit
            ignored = set(wt("check-ignore", "--stdin", check=False, input="\n".join(kept_code)).stdout.split())
            kept_code = [p for p in kept_code if p not in ignored and Path(p).parts[0] not in MIND_PATHS]
        if kept_code:
            msg += "\n\nCode changes made by the instance (tests passed):\n" + "\n".join(f"- {p}" for p in kept_code)
        if note:
            msg += "\n\n" + note
        minded = mind.commit(name, msg, root, project)
        paths = [p for p in SKILL_PATHS if (root / p).exists()] + kept_code
        staged = []
        if paths:
            wt("add", "-A", "--", *paths)
            staged = wt("diff", "--cached", "--name-only", "-z", "--", *paths).stdout.split("\0")
            staged = [p for p in staged if p and Path(p).parts[0] not in MIND_PATHS]
        if staged:
            wt("-c", f"user.name=SHAFT/{name}", "-c", f"user.email={name}@shaft.localhost",
               "commit", "-q", "-m", msg, "--", *staged)
    except (subprocess.CalledProcessError, mind.MindError) as e:
        detail = (getattr(e, "stderr", None) or getattr(e, "stdout", None) or str(e)).strip()
        log(f"{name}: COMMIT FAILED after {kind}: {detail}")
        print(f"{name}: could not commit its state after {kind} — files are on disk, uncommitted.\n"
              f"git said: {detail}", file=sys.stderr)
        return False
    if minded or staged:
        log(f"{name}: committed {kind}: " + ", ".join(
            [*(["the mind in its mind store"] if minded else []), *([f"{len(staged)} files on instance/{name}"]
                                                                   if staged else [])]))
    return True
