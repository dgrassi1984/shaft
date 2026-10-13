"""Shared supervisor plumbing: paths, knobs, logging, git, instance lookup, commits."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
# The system interpreter, even when the supervisor itself runs inside a venv (the terminal client
# runs in .venv-ui): sandboxed agents cannot read a venv inside the home folder, and agents and
# launchd jobs must not depend on one.
PYTHON = str(getattr(sys, "_base_executable", None) or sys.executable)
INSTANCES = PROJECT / "instances"
TEMPLATE = PROJECT / "template"
SUP_DIR = Path(os.environ.get("SHAFT_SUPERVISOR_DIR") or PROJECT / ".supervisor")  # tests point it elsewhere
HOME = Path.home()
STATE_PATHS = ("self", "memory", "notes", "skills", "inbox")
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
    out: dict[str, str] = {}
    try:
        text = (instance_root(name) / "config.env").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        key, eq, value = line.partition("=")
        if eq and key.strip().isidentifier():
            value = value.split("#")[0].strip() if not value.strip().startswith('"') else value.strip()[1:].split('"')[0]
            out[key.strip()] = value
    return out


def disabled_tools(name: str) -> set[str]:
    """Supervisor tools the operator switched off for this instance (`shaft tools <name> off <tool>`).
    Kept in the supervisor's folder, not the instance's config.env: an instance must not switch a tool
    that costs the operator money (delegate_coding) back on for itself."""
    try:
        return set(json.loads((SUP_DIR / "tools" / f"{name}.json").read_text()).get("off", []))
    except (OSError, ValueError):
        return set()


def write_hosts(name: str) -> set[str]:
    """Hosts this instance may send POST/PUT/PATCH/DELETE to (`shaft hosts <name> allow <host>`)."""
    try:
        return set(json.loads((SUP_DIR / "tools" / f"{name}.json").read_text()).get("write_hosts", []))
    except (OSError, ValueError):
        return set()


def _save_tools(name: str, off: set[str], hosts: set[str]) -> None:
    (SUP_DIR / "tools").mkdir(parents=True, exist_ok=True)
    (SUP_DIR / "tools" / f"{name}.json").write_text(json.dumps({"off": sorted(off), "write_hosts": sorted(hosts)},
                                                               indent=1))


def set_write_host(name: str, host: str, allow: bool) -> set[str]:
    hosts = write_hosts(name) | {host.lower()} if allow else write_hosts(name) - {host.lower()}
    _save_tools(name, disabled_tools(name), hosts)
    return hosts


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
    _save_tools(name, off, write_hosts(name))
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


def safe_mkdir(root: Path, rel: str) -> Path:
    path = contained(root, *Path(rel).parts)
    path.mkdir(parents=True, exist_ok=True)
    return contained(root, *Path(rel).parts)


def journal_note(root: Path, title: str, body: str) -> None:
    """Supervisor's own minimal journal append (deliberately not the agent's code)."""
    safe_write(root, "memory/journal.md",
               f"\n---\n## {datetime.now():%Y-%m-%d %H:%M} — {title}\n\n{body.strip()}\n", append=True)


def commit_state(name: str, kind: str, kept_code: list[str], root: Path | None = None,
                 project: Path | None = None, note: str = "") -> bool:
    """Commit the instance's mind (always) and its code changes that passed the gate.

    Only files git actually staged are named in the commit: an empty directory
    (notes/, skills/ on a young instance) is unknown to git and would fail as a pathspec.
    Returns False — after logging git's own error — if the commit failed.
    """
    root = root or INSTANCES / name
    wt = lambda *a, check=True, input=None: git_wt(root, *a, check=check, project=project, input=input)  # noqa: E731
    try:
        if kept_code:   # git refuses to add an ignored path, and one such path fails the whole commit
            ignored = set(wt("check-ignore", "--stdin", check=False, input="\n".join(kept_code)).stdout.split())
            kept_code = [p for p in kept_code if p not in ignored]
        paths = [p for p in STATE_PATHS if (root / p).exists()] + kept_code
        wt("add", "-A", "--", *paths)
        staged = wt("diff", "--cached", "--name-only", "-z", "--", *paths).stdout.split("\0")
        staged = [p for p in staged if p]
        if not staged:
            return True
        msg = f"{name}: {kind} {datetime.now():%Y-%m-%d %H:%M}"
        if kept_code:
            msg += "\n\nCode changes made by the instance (tests passed):\n" + "\n".join(f"- {p}" for p in kept_code)
        if note:
            msg += "\n\n" + note
        wt("-c", f"user.name=SHAFT/{name}", "-c", f"user.email={name}@shaft.localhost",
           "commit", "-q", "-m", msg, "--", *staged)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        log(f"{name}: COMMIT FAILED after {kind}: {detail}")
        print(f"{name}: could not commit its state after {kind} — files are on disk, uncommitted.\n"
              f"git said: {detail}", file=sys.stderr)
        return False
    log(f"{name}: committed {kind} on instance/{name} ({len(staged)} files)")
    return True
