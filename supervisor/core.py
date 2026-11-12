"""Shared supervisor plumbing: paths, knobs, logging, git, instance lookup, commits."""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

from .jsonl import jsonl_lines  # noqa: F401  (re-exported)

PROJECT = Path(__file__).resolve().parent.parent
# What a network call can raise when the far end is unreachable, resets, or stops mid-answer: http.client's
# IncompleteRead and RemoteDisconnected are HTTPExceptions, not OSErrors, so a retry on (URLError, OSError) let a
# reply cut short by OpenRouter escape — it crashed the broker's handler and wisp's sleep of 2026-10-15 04:34.
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
# versioned in its private mind store (supervisor/mind.py).
MIND_PATHS = ("self", "memory", "notes", "inbox")
# The non-code state the instance's branch still holds: skills/ is a gene (evolution recombines it, the template
# ships one), not memory.
SKILL_PATHS = ("skills",)
# The supervisor's copy of SHAFT's own docs in each instance folder (runner.stage_docs), read-only to the instance:
# refreshed at every start from the project, so never staged on its branch (an exam's copy of the instance holds it
# as it is).
DOCS_COPY = "shaft-docs"
# The agent's memory-search vectors, a cache keyed by chunk hash (template shaft/memory.py INDEX and BINARY_INDEX):
# the JSON of an instance whose code predates the vector log, the log of one whose code has it. A copy of an
# instance (trial, exam, workspace experiment) carries whichever exist, so it never embeds its whole mind again.
MEMORY_CACHES = (".tmp/memory-index.json", ".tmp/memory-index.bin")
DEFAULT_INSTANCE = "shaft"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


# -- knobs: the supervisor's settings ---------------------------------------------------------
# Each is read once, when a process starts: from its environment, else the operator's settings file, else its default
# (env > file > default). The launchd services run with PATH alone in their environment, so for them the file is what
# sets a knob; `shaft settings` writes it (settings.py), the web UI's Settings page runs that verb.
SETTINGS_FILE = SUP_DIR / "settings.env"     # the operator's, gitignored (.supervisor/): KEY=value, value verbatim
SETTING_KEY = re.compile(r"^SHAFT_[A-Z0-9_]{1,60}$")
# Where a knob is read, for the operator: a long-running service holds the value it read at its start until it
# restarts; "runs" — cycles, sleeps, trials, batteries, write-ups, CLI commands — are fresh processes that read it
# when they start.
SERVICE_NAMES = ("api", "scheduler", "telegram", "web", "model", "embed")
KNOB_GROUPS = ("Judging", "Models and spend", "Codex", "Grok", "Claude", "Antigravity", "Trials", "Runs and conversations", "Inbox and benchmarks",
               "Local model", "Ports and paths")


@dataclasses.dataclass(frozen=True)
class Knob:
    name: str
    default: str
    parse: object                   # str -> value; ValueError (with the reason) for a value it cannot take
    help: str                       # one line: what it does
    group: str                      # a KNOB_GROUPS entry: where the Settings page shows it
    services: tuple                 # the long-running services that hold it (SERVICE_NAMES), plus "runs"
    web: bool                       # settable from the web UI; False: CLI only (ports, interpreters, endpoints)


KNOBS: dict[str, Knob] = {}         # every knob() call, in the order they were made
HELD: dict[str, dict] = {}          # what this process read for each knob: {"raw", "source"} (a service records it)
_on_knob = None                     # set by version.record_service: a knob read after start-up is recorded too
_file_cache: dict = {"stamp": None, "values": {}}


def settings_file() -> dict[str, str]:
    """The settings file's KEY=value lines (the value verbatim to the end of its line), read again when it changes."""
    try:
        st = SETTINGS_FILE.stat()
        stamp = (str(SETTINGS_FILE), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _file_cache["stamp"] != stamp:
        values = {}
        for line in SETTINGS_FILE.read_text().splitlines():
            key, eq, value = line.partition("=")
            if line.strip() and not line.lstrip().startswith("#") and eq and SETTING_KEY.match(key.strip()):
                values[key.strip()] = value
        _file_cache.update(stamp=stamp, values=values)
    return _file_cache["values"]


def resolve(k: Knob, env=None) -> tuple[object, str, str]:
    """(value, source, the raw text it was parsed from): env > settings file > default. A value its parser refuses
    is warned about and the next layer is used."""
    env = os.environ if env is None else env
    for source, raw in (("env", env.get(k.name)), ("file", settings_file().get(k.name))):
        if raw is None:
            continue
        try:
            return k.parse(raw), source, raw
        except (ValueError, TypeError) as e:
            where = "the environment" if source == "env" else SETTINGS_FILE
            print(f"warning: {k.name}={raw!r} in {where} is invalid ({e}); ignored", file=sys.stderr)
    return k.parse(k.default), "default", k.default


def knob(name: str, default: str, parse, help: str = "", *, group: str = "", services=("runs",), web: bool = True):
    """An operator setting: (value, source). Every call in the supervisor's code registers it with a one-line `help`,
    its Settings `group` and the `services` that hold it (test_settings checks each call does)."""
    k = KNOBS.get(name)
    if help or k is None:
        k = Knob(name, default, parse, help, group, tuple(services), web)
        if help:
            KNOBS[name] = k
    value, source, raw = resolve(k)
    HELD[name] = {"raw": raw, "source": source}
    if _on_knob:
        _on_knob()
    return value, source


# Parsers that refuse what the code using a knob could not run on: a bad value is refused when it is set, and an
# unusable one already in the environment or the file is warned about and skipped (resolve).
def positive_int(s: str) -> int:
    n = int(s)
    if n <= 0:
        raise ValueError(f"must be a whole number above 0, not {s!r}")
    return n


def whole(s: str) -> int:
    n = int(s)
    if n < 0:
        raise ValueError(f"must be a whole number, 0 or more, not {s!r}")
    return n


def dollars(s: str) -> float:
    x = float(s)
    if not 0 <= x < float("inf"):
        raise ValueError(f"must be an amount in dollars, 0 or more, not {s!r}")
    return x


def hours(s: str) -> float:
    x = float(s)
    if not 0 <= x < float("inf"):
        raise ValueError(f"must be a number of hours, 0 or more, not {s!r}")
    return x


def port(s: str) -> int:
    n = int(s)
    if not 1 <= n <= 65535:
        raise ValueError(f"must be a port, 1–65535, not {s!r}")
    return n


def temperature(s: str) -> float:
    x = float(s)
    if not 0 <= x <= 2:
        raise ValueError(f"must be a temperature, 0–2, not {s!r}")
    return x


def gigabytes(s: str) -> float:
    x = float(s)
    if not 0 < x < 1024:
        raise ValueError(f"must be a number of gigabytes above 0, not {s!r}")
    return x


def ports(s: str) -> list[int]:
    return [port(p.strip()) for p in s.split(",")]


def model_name(s: str) -> str:
    s = s.strip()
    if not s or any(c.isspace() or c == "," for c in s):
        raise ValueError(f"must be one model name, e.g. claude-haiku-5-5 or openrouter/xiaomi/mimo-v2.6-pro, not {s!r}")
    return s


def model_names(s: str) -> list[str]:
    """A comma-separated list of model names (may be empty)."""
    return [model_name(m) for m in s.split(",") if m.strip()]


def cloud_model(s: str) -> str:
    """A model the supervisor calls itself: Claude (claude-…) on Anthropic's API, else openrouter/…."""
    s = model_name(s)
    if not (s.startswith("claude-") or s.startswith("openrouter/")):
        raise ValueError(f"must be a claude-… or openrouter/… model, not {s!r}")
    return s


def instance_model(s: str) -> str:
    """A cloud or registered plan model an instance or bake-off may run on."""
    from . import planchannel
    s = model_name(s)
    if not (s.startswith("claude-") or s.startswith("openrouter/") or planchannel.serves(s)):
        plans = " or ".join(f"{p}/…" for p in planchannel.PROVIDERS)
        raise ValueError(f"must be a claude-… or openrouter/… or {plans} model, not {s!r}")
    return s


def claude_model(s: str) -> str:
    """A Claude model, called on Anthropic's API."""
    s = model_name(s)
    if not s.startswith("claude-"):
        raise ValueError(f"must be a Claude model (claude-…): it is called on Anthropic's API, not {s!r}")
    return s


def openrouter_model(s: str) -> str:
    s = model_name(s)
    if not s.startswith("openrouter/"):
        raise ValueError(f"must be an openrouter/… model: it is called on OpenRouter, not {s!r}")
    return s


def cloud_models(s: str) -> list[str]:
    """A non-empty comma-separated list of cloud_model names."""
    out = [cloud_model(m) for m in s.split(",") if m.strip()]
    if not out:
        raise ValueError("must name at least one model")
    return out


def json_object(s: str) -> dict:
    v = json.loads(s)
    if not isinstance(v, dict):
        raise ValueError(f"must be a JSON object ({{…}}), not {type(v).__name__}")
    return v


CYCLE_TIMEOUT_S, CYCLE_SRC = knob(
    "SHAFT_CYCLE_TIMEOUT_S", "2400", positive_int, "seconds a cycle (and a trial's replayed cycle) may run before it "
    "is stopped", group="Runs and conversations")
SLEEP_TIMEOUT_S, SLEEP_SRC = knob(
    "SHAFT_SLEEP_TIMEOUT_S", "5400", positive_int, "seconds the nightly sleep (consolidation) may run; nobody is "
    "waiting for it", group="Runs and conversations")
PAUSE_ALERT_HOURS, _ = knob(
    "SHAFT_PAUSE_ALERT_HOURS", "3", whole, "hours an operator pause may last before an alert; 0 = off",
    group="Runs and conversations", services=("runs", "web"))
# 8780: SHAFT's own Bonsai 2 server (supervisor/bonsai_server.py); Atomic Chat's 1337 is no longer used.
LLM_PORTS, PORTS_SRC = knob(
    "SHAFT_LLM_PORTS", "8780", ports, "the local model servers' ports a run's sandbox may reach (the first is SHAFT's "
    "own server)", group="Ports and paths", services=("runs", "api", "web"), web=False)
CORE_MIN_HOURS, CORE_SRC = knob(
    "SHAFT_CORE_MIN_HOURS", "72", whole, "hours an instance's core personality must stay unchanged before it may "
    "change it again", group="Runs and conversations", services=("runs", "api"))
EMBED_PORT, EMBED_SRC = knob(   # SHAFT's embedding service (memory search)
    "SHAFT_EMBED_PORT", "8782", port, "the embedding service's port (memory search), which every run may reach",
    group="Ports and paths", services=("runs", "api", "web"), web=False)
BROKER_MAX_CALLS, BROKER_SRC = knob(
    "SHAFT_BROKER_MAX_CALLS", "60", positive_int, "supervisor tool calls (broker) one run may make",
    group="Runs and conversations", services=("runs", "api"))
# A conversation (terminal or API) quiet this long is closed and written up into the journal.
IDLE_MINUTES, IDLE_SRC = knob(
    "SHAFT_IDLE_MINUTES", "30", positive_int, "minutes a conversation (terminal, web, Telegram, API) may stay quiet "
    "before it is closed and written up into the journal", group="Runs and conversations",
    services=("runs", "api", "telegram"))
API_TALKS = SUP_DIR / "api_talks.json"
# What the unsandboxed supervisor reads out of an instance's folder, which the instance fills as it likes: one file, and
# one walk of a folder tree (the gate's code snapshot, read into memory; a battery exam's copy, written to disk). Past
# a bound the read fails, explicitly — never an empty or partial answer (the gate then fails closed) — rather than
# taking the Mac's memory, disk or file descriptors (Codex review of 151088d, 2026-10-24).
FILE_READ_MAX_MB, _ = knob(
    "SHAFT_FILE_READ_MAX_MB", "512", positive_int, "megabytes the supervisor reads from one file in an instance's "
    "folder (a bigger one is refused)", group="Runs and conversations",
    services=("runs", "api", "web", "scheduler", "telegram"))
SNAPSHOT_MAX_MB, _ = knob(
    "SHAFT_SNAPSHOT_MAX_MB", "512", positive_int, "megabytes of an instance's code the gate reads to check a change "
    "(more, and the change is not kept)", group="Runs and conversations", services=("runs", "api"))
COPY_MAX_GB, _ = knob(
    "SHAFT_COPY_MAX_GB", "8", gigabytes, "gigabytes the supervisor copies of an instance's folder for a battery exam "
    "or a gate's scratch copy (more, and the exam or the check fails)", group="Runs and conversations",
    services=("runs", "api", "scheduler"))
TREE_MAX_FILES, _ = knob(
    "SHAFT_TREE_MAX_FILES", "200000", positive_int, "files and folders the supervisor reads or copies in one walk of "
    "an instance's folder (the gate's code snapshot, a battery exam's copy)", group="Runs and conversations",
    services=("runs", "api"))
TREE_MAX_DEPTH, _ = knob(
    "SHAFT_TREE_MAX_DEPTH", "64", positive_int, "folders deep the supervisor walks in an instance's folder (the gate's "
    "code snapshot, a battery exam's copy)", group="Runs and conversations", services=("runs", "api"))


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
           input: str | bytes | None = None, text: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run git in an instance worktree without trusting anything inside it.

    The worktree's `.git` file is writable by the instance, which could point it at
    a fake repository whose config runs commands (hooks, fsmonitor). So the git dir
    is taken from the main repository's own worktree registry, and hooks are off.
    """
    admin = _worktree_admin_dir(root, project or PROJECT)
    return subprocess.run(
        ["git", f"--git-dir={admin}", f"--work-tree={root}", "-c", "core.hooksPath=/dev/null",
         "-c", "core.fsmonitor=false", *args],
        cwd=root, capture_output=True, text=text, check=check, input=input,
        env={**os.environ, **env} if env else None)


def _git_common_dir(project: Path) -> Path:
    """The project's own git directory: .git, or — when the supervisor's checkout is itself a linked worktree (a branch
    being worked on) — the main repository's, which its .git file names. Read from the supervisor's checkout, never
    from an instance's."""
    dot = project / ".git"
    if not dot.is_file():
        return dot
    gitdir = Path(dot.read_text().strip().removeprefix("gitdir:").strip())
    gitdir = gitdir if gitdir.is_absolute() else (project / gitdir).resolve()
    common = gitdir / "commondir"
    return (gitdir / common.read_text().strip()).resolve() if common.is_file() else gitdir


def _worktree_admin_dir(root: Path, project: Path) -> Path:
    for admin in (_git_common_dir(project) / "worktrees").glob("*"):
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


def long_pauses(now: datetime | None = None) -> list[dict]:
    """Operator flags past the alert threshold (2026-10-28, A1): a forgotten
    operator pause has no expiry. Old or damaged flag contents fall back to their file's modification time."""
    if not PAUSE_ALERT_HOURS:
        return []
    now = now or datetime.now()
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    out = []
    for flag in sorted(SUP_DIR.glob("paused*")):
        if flag.name != "paused" and not flag.name.startswith("paused-"):
            continue
        try:
            try:
                since = datetime.fromisoformat(flag.read_text().strip())
            except (ValueError, UnicodeError):
                since = datetime.fromtimestamp(flag.stat().st_mtime)
            if since.tzinfo is not None:
                since = since.astimezone().replace(tzinfo=None)
        except OSError:                          # resumed while this snapshot was read
            continue
        hours = (now - since).total_seconds() / 3600
        if hours >= PAUSE_ALERT_HOURS:
            out.append({"name": "all" if flag.name == "paused" else flag.name[7:],
                        "since": since, "hours": hours})
    return out


def set_paused(name: str | None, enabled: bool, *, reason: str = "the operator") -> None:
    """Set or lift a pause flag (`.supervisor/paused-<name>`, or `paused` for all), then tell each instance it touches
    in a journal note: the scheduler leaves a paused instance out without a word, and tundra took an operator pause
    (2026-10-25 12:42 to 26 14:36) for the scheduler silently skipping its wakes. Re-pausing writes nothing; notes
    are best effort and never stop the pause, resume, kill or delete that called this."""
    flag = SUP_DIR / (f"paused-{name}" if name else "paused")
    now = datetime.now()
    SUP_DIR.mkdir(exist_ok=True)
    if enabled:
        try:
            with flag.open("x") as f:
                f.write(now.isoformat())
        except FileExistsError:
            return
        title = f"Supervisor: paused by {reason}"
        body = f"{'Global pause' if name is None else 'Pause'} started at {now:%Y-%m-%d %H:%M} by {reason}. "
        body += "No autonomous cycle, nightly sleep, job or battery starts while paused. Conversations still run."
    else:
        try:
            started = datetime.fromisoformat(flag.read_text().strip())
            if started.tzinfo is not None:      # a stamp written with its zone: as local time
                started = started.astimezone().replace(tzinfo=None)
            minutes = max(0, round((now - started).total_seconds() / 60))
            duration = (f"The pause lasted {minutes // 60} h {minutes % 60} min (since {started:%Y-%m-%d %H:%M})."
                        if minutes >= 60 else f"The pause lasted {minutes} min (since {started:%Y-%m-%d %H:%M}).")
        except (OSError, UnicodeError, ValueError):
            duration = "The pause duration is unknown because its stored start time could not be read."
        try:
            flag.unlink()
        except FileNotFoundError:
            return
        title = f"Supervisor: resumed by {reason}"
        body = f"{'Global pause' if name is None else 'Pause'} ended at {now:%Y-%m-%d %H:%M}. {duration}"
    for affected in ([name] if name else instance_names()):
        try:
            root = INSTANCES / affected
            if not root.is_dir():       # delete also clears the flag after removing the instance
                continue
            note = body
            if not enabled:
                if name is None and (SUP_DIR / f"paused-{affected}").exists():
                    note += " The global pause ended, but this instance is still individually paused."
                elif paused(affected):
                    note += " This instance is still paused by the global pause."
                else:
                    note += " Autonomous starts are allowed again."
            journal_note(root, title, note)
        except Exception as e:
            log(f"{affected}: journal note {title!r} NOT written: {e}")


def update_queued(name: str) -> str | None:
    """When a `shaft update` that found the instance busy was queued, else None. The scheduler runs it once the
    instance is idle, before any wake (autonomy.serve)."""
    try:
        return (SUP_DIR / f"update-queued-{name}").read_text().strip() or "?"
    except OSError:
        return None


def set_update_queued(name: str, on: bool) -> None:
    flag = SUP_DIR / f"update-queued-{name}"
    if not on:
        flag.unlink(missing_ok=True)
    elif not flag.exists():   # the first request's time: a retry does not move it
        SUP_DIR.mkdir(exist_ok=True)
        flag.write_text(datetime.now().isoformat(timespec="seconds"))


def instance_config(name: str) -> dict[str, str]:
    """An instance's config.env (KEY="value"  # comment), read as text — never executed, never through a link."""
    try:
        return config_values(safe_read(instance_root(name), "config.env"))
    except (OSError, UnsafePath, ValueError):
        return {}


CONFIG_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def config_set_line(key: str, value, comment: str = "") -> str:
    """The config.env line that sets `key` to `value`, as both writers write it (the instance's set_config and the
    operator's `shaft config`), or ValueError: a line that would not read back as exactly this one value."""
    key, value = str(key).strip(), str(value)
    if not CONFIG_KEY.match(key):
        raise ValueError(f"{key!r}: a key is an UPPER_CASE name, e.g. WAKE_INTERVAL_MINUTES")
    if len(value) > 500 or any(c in value for c in '"\n\r\0'):
        raise ValueError("a value is one line of at most 500 characters, without double quotes")
    if any(c in comment for c in "\n\r\0"):
        raise ValueError("a comment is one line")
    return f'{key}="{value}"' + (f"   # {comment}" if comment else "")


@contextlib.contextmanager
def config_lock(path: Path):
    """One writer of a config.env at a time, across processes: the broker's set_config, the gate undoing it and the
    operator's `shaft config` each read the file, change one line and write it back. Without the lock an operator
    write landing between set_config's read and write was lost."""
    import fcntl
    locks = SUP_DIR / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()[:16]
    with (locks / f"config-{digest}.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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
    removed if `line` is None. An appended line comes after one blank line: it starts a run of its own, so it never
    shares the comment of the keys above it (opconfig._comments). Removing the last line also removes one blank line
    directly above it, so an append followed by its undo gives back the same bytes. Every other line stays as it is."""
    lines = text.splitlines()
    at = max((i for i, l in enumerate(lines) if _config_key(l) == key), default=None)
    if at is None:
        if line is not None:
            if lines:                               # always, so removing it again (an undo) is exact
                lines.append("")
            lines.append(line)
    elif line is None:
        if at == len(lines) - 1 and at > 0 and not lines[at - 1].strip():
            del lines[at - 1:]
        else:
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


class StaleSetting(ValueError):
    """The operator's setting changed since it was read."""


def limit_value(limit: str, value: str):
    """Parse the operator's CLI or app limit using the same validation."""
    import math
    if value == "off":
        return None
    try:
        new = int(value) if limit == "wake-floor" else round(float(value), 2)
        if math.isfinite(new) and new > 0:
            return new
    except (ValueError, OverflowError):
        pass
    raise ValueError(f"{limit} must be a positive {'whole number of minutes' if limit == 'wake-floor' else 'amount'}"
                     f" (or off), not {value!r}")


def _update_tool_settings(name: str, *, expect: dict | None = None, **changes) -> None:
    """Compare and update owned keys without losing another operator writer's changes."""
    import tempfile
    path = SUP_DIR / "tools" / f"{name}.json"
    with config_lock(path):
        data = _tool_settings(name)
        if expect is not None and any(data.get(k) != v for k, v in expect.items()):
            raise StaleSetting(f"{name}'s tool settings changed")
        data.update(changes)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{name}-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({k: v for k, v in data.items() if v is not None}, stream, indent=1)
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)


def disabled_tools(name: str) -> set[str]:
    """Supervisor tools the operator switched off for this instance (`shaft tools <name> off <tool>`): an
    instance must not switch a tool that costs the operator money (delegate_coding) back on for itself."""
    return set(_tool_settings(name).get("off", []))


# -- the operator's folder an instance may read ----------------------------------------------------------------
# shaft asked for "a clearly scoped folder" of their work. Read-only: the instance still writes
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


# -- the operator's mail an instance may read ------------------------------------------------------
# an instance they pick reads the Gmail threads carrying a label they pick. No Gmail credential can
# be limited to a label, so the grant and the credential stay here and supervisor/mail.py enforces the label.
def mail_grants(name: str) -> list[dict]:
    """The labels this instance may read (`shaft mail <name> grant <alias> <label>`): [{account, label, draft?}].
    `draft` (`shaft mail <name> grant-draft <alias> <label>`) also lets it leave drafts — never send — from that
    account, in that label's threads; drafting implies reading, and removing it keeps the read."""
    raw = _tool_settings(name).get("mail")
    return [{"account": g["account"], "label": g["label"], **({"draft": True} if g.get("draft") is True else {})}
            for g in raw if isinstance(g, dict)
            and isinstance(g.get("account"), str) and isinstance(g.get("label"), str)] if isinstance(raw, list) else []


def set_mail_grants(name: str, grants: list[dict]) -> list[dict]:
    """One row per account/label: a later row for the same label replaces the earlier one (its draft flag with it)."""
    keep: dict[tuple[str, str], dict] = {}
    for g in grants:
        keep[(g["account"], g["label"])] = {"account": g["account"], "label": g["label"],
                                            **({"draft": True} if g.get("draft") is True else {})}
    result = list(keep.values())
    _update_tool_settings(name, mail=result or None)
    return result


# -- the operator's read-only Drive grants -----------------------------------------------------------------------
def gdrive_grants(name: str) -> list[dict]:
    from . import google
    raw = _tool_settings(name).get("gdrive")
    return [{"account": g["account"], "reach": g["reach"]} for g in raw if isinstance(g, dict)
            and isinstance(g.get("account"), str) and google.ALIAS_RE.fullmatch(g["account"])
            and g.get("reach") in ("own", "all")] if isinstance(raw, list) else []


def set_gdrive_grants(name: str, grants: list[dict]) -> list[dict]:
    from . import google
    keep: dict[str, dict] = {}
    for g in grants:
        if not google.ALIAS_RE.fullmatch(g["account"]) or g["reach"] not in ("own", "all"):
            raise ValueError("a Drive grant needs a Google account alias and reach own or all")
        keep[g["account"]] = {"account": g["account"], "reach": g["reach"]}
    result = list(keep.values())
    _update_tool_settings(name, gdrive=result or None)
    return result


@contextlib.contextmanager
def _grant_lock(name: str, lock_name: str):
    """Serialize grants and copy removal with broker download publication across processes."""
    import fcntl
    if not NAME_RE.fullmatch(name):
        raise ValueError("invalid instance name")
    locks = SUP_DIR / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / f"{lock_name}-{name}.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def gdrive_lock(name: str):
    return _grant_lock(name, "gdrive")


def gchat_lock(name: str):
    return _grant_lock(name, "gchat")


def _remove_google_copies(name: str, folder: str, alias: str) -> None:
    from . import google
    if alias and not google.ALIAS_RE.fullmatch(alias):
        raise ValueError("invalid Google account alias")
    path = contained(instance_root(name), folder, *([alias] if alias else []))
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        if path.exists():
            raise


def remove_gdrive_copies(name: str, alias: str = "") -> None:
    _remove_google_copies(name, "gdrive", alias)


# -- the operator's read-only Google Chat grants ---------------------------------------------------------------
def gchat_grants(name: str) -> list[dict]:
    from . import google
    raw = _tool_settings(name).get("gchat")
    return [{"account": g["account"]} for g in raw if isinstance(g, dict)
            and isinstance(g.get("account"), str) and google.ALIAS_RE.fullmatch(g["account"])] if isinstance(raw, list) else []


def set_gchat_grants(name: str, grants: list[dict]) -> list[dict]:
    from . import google
    keep: dict[str, dict] = {}
    for g in grants:
        if not google.ALIAS_RE.fullmatch(g["account"]):
            raise ValueError("a Chat grant needs a Google account alias")
        keep[g["account"]] = {"account": g["account"]}
    result = list(keep.values())
    _update_tool_settings(name, gchat=result or None)
    return result


def remove_gchat_copies(name: str, alias: str = "") -> None:
    _remove_google_copies(name, "gchat", alias)


# -- the operator's calendars an instance may read ----------------------------------------------
# several instances, each with its own grants, one calendar per grant, full event details. A
# calendar token opens every calendar the account can see, so the grant stays here and supervisor/calendar.py makes
# every API call name a granted calendar. Stored by calendar ID (it survives a rename), with its title at grant time.
def calendar_grants(name: str) -> list[dict]:
    """The calendars this instance may read (`shaft calendar <name> grant <alias> <calendar>`):
    [{account, calendar (its ID), name (its title when granted), write?}].
    Write implies read because both levels share one record; removing write keeps read."""
    raw = _tool_settings(name).get("calendar")
    return [{"account": g["account"], "calendar": g["calendar"], "name": g.get("name") or g["calendar"],
             **({"write": True} if g.get("write") is True else {})}
            for g in raw if isinstance(g, dict) and isinstance(g.get("account"), str)
            and isinstance(g.get("calendar"), str) and g["calendar"]] if isinstance(raw, list) else []


def set_calendar_grants(name: str, grants: list[dict]) -> list[dict]:
    keep: list[dict] = []
    for g in grants:
        g = {"account": g["account"], "calendar": g["calendar"], "name": g.get("name") or g["calendar"],
             **({"write": True} if g.get("write") is True else {})}
        if not any((k["account"], k["calendar"]) == (g["account"], g["calendar"]) for k in keep):
            keep.append(g)
    _update_tool_settings(name, calendar=keep or None)
    return keep


# -- an instance's own keys ---------------------------------------------------------------------------
# Its own ("they should have their own key and be able to manage them"), kept in its
# folder at secrets/secrets.json — but handled only by the supervisor: the instance stores, lists and
# deletes keys through broker tools, and names a key in http_request for the broker to add. Its sandbox,
# plugins and `run` cannot read the file, so a value never comes back into its context (prompts may go to
# a cloud model; fetched pages may try to talk it into sending a key elsewhere). Never committed, never
# copied into another instance, never shown to the coder.
SECRETS_DIR = "secrets"
SECRET_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


class KeyStoreUnreadable(ValueError):
    """The key store is there but is not a JSON object of keys: nothing may be written over it."""


def read_secrets(root: Path, strict: bool = False) -> dict[str, dict]:
    """The instance's keys ({} when it has none), read without following a link anywhere (read_beneath). A store that
    is there but cannot be read — not JSON, a link, a pipe, too big — is logged loudly and reads as {} for a use of a
    key, or raises KeyStoreUnreadable when `strict` (every change: store, delete). Until 2026-10-24 it read as {} for
    changes too, so the next `shaft secret set` replaced every key with one (B10)."""
    path = Path(root) / SECRETS_DIR / "secrets.json"
    try:
        store = json.loads(read_beneath(root, [SECRETS_DIR, "secrets.json"]))
        if not isinstance(store, dict) or not all(isinstance(e, dict) for e in store.values()):
            raise ValueError("not a JSON object of keys")
        return store
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnsafePath) as e:   # TooBig is a ValueError
        why = f"{path} cannot be read as a key store ({type(e).__name__}: {e})"
        log(f"KEY STORE UNREADABLE: {why}; its keys are not used until it is repaired by hand")
        if strict:
            raise KeyStoreUnreadable(f"{why}; nothing was changed — repair or remove the file by hand") from None
        return {}


@contextlib.contextmanager
def _secrets_lock(root: Path):
    """One change to an instance's key store at a time, across processes (the broker's secret_set and secret_delete,
    `shaft secret`): each reads the store, changes one key and writes it back, and two at once lost one of them."""
    import fcntl
    locks = SUP_DIR / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(Path(root).resolve()).encode()).hexdigest()[:16]
    with (locks / f"secrets-{digest}.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _change_secrets(root: Path, change) -> dict:
    """Read, change (`change(store)` edits it in place and returns False for "nothing to write") and write the store,
    under its lock; never over a store that cannot be read."""
    with _secrets_lock(root):
        store = read_secrets(root, strict=True)
        if change(store) is not False:
            _write_secrets(root, store)
        return store


def store_secret(root: Path, key: str, value: str, hosts: list[str], header: str = "Authorization",
                 scheme: str = "Bearer") -> None:
    if not SECRET_NAME_RE.match(key):
        raise ValueError("a key name is lowercase letters, digits, - or _ (e.g. github)")
    hosts = sorted({h.strip().lower() for h in hosts if h.strip()})
    if not value.strip() or not hosts:
        raise ValueError("a key needs a value and at least one host it may be sent to")
    entry = {"value": value.strip(), "hosts": hosts, "header": header, "scheme": scheme,
             "created": datetime.now().isoformat(timespec="seconds")}
    _change_secrets(root, lambda store: store.__setitem__(key, entry))


def store_google_key(root: Path, key: str, data: dict) -> None:
    """An instance's own Google login, made by `shaft secret <name> google`: a refresh token the
    broker turns into an access token and adds like any key, only to its hosts. It has no `value`: the token it adds
    is made per run and never stored."""
    if not SECRET_NAME_RE.match(key):
        raise ValueError("a key name is lowercase letters, digits, - or _ (e.g. gmail)")
    hosts = sorted({str(h).strip().lower() for h in data.get("hosts") or [] if str(h).strip()})
    missing = [f for f in ("client", "address", "refresh_token", "scope") if not str(data.get(f) or "").strip()]
    if data.get("kind") != "google" or missing or not hosts:
        raise ValueError(f"a Google key needs kind google, hosts and {', '.join(missing) or 'its login'}")
    entry = {"kind": "google", "client": data["client"], "address": data["address"],
             "refresh_token": data["refresh_token"], "scope": data["scope"], "hosts": hosts,
             "header": "Authorization", "scheme": "Bearer",
             "created": data.get("created") or datetime.now().isoformat(timespec="seconds")}
    _change_secrets(root, lambda store: store.__setitem__(key, entry))


def is_google_key(entry: dict | None) -> bool:
    return bool(entry) and entry.get("kind") == "google"


def describe_key(key: str, entry: dict) -> str:
    """One line about a key, never its value: what `secret_list` and `shaft secret <name> list` show."""
    stored = f"stored {str(entry.get('created', '?'))[:16]}"
    if is_google_key(entry):
        from .google import SCOPE_NAMES
        granted = ", ".join(SCOPE_NAMES.get(s, s) for s in str(entry.get("scope") or "").split())
        return (f"{key}: Google account {entry.get('address', '?')} ({granted or 'no scope'}; refreshed by the "
                f"supervisor) · hosts {', '.join(entry['hosts'])} · {stored}")
    return (f"{key}: hosts {', '.join(entry['hosts'])} · {entry.get('header')} {entry.get('scheme', '')}".rstrip()
            + f" · {stored}")


def delete_secret(root: Path, key: str) -> bool:
    found = False

    def drop(store: dict) -> bool:
        nonlocal found
        found = store.pop(key, None) is not None
        return found                                 # False: not there, nothing to write
    _change_secrets(root, drop)
    return found


def _write_secrets(root: Path, store: dict) -> None:
    """The whole store or none of it: written to a new 0600 file beside it, flushed to disk, then renamed over it —
    never truncated and rewritten in place, where a crash halfway left a store that read as empty (B10). Every
    component is opened without following a link (open_beneath), the rename too (it replaces a link, never its
    target). Called under _secrets_lock."""
    safe_mkdir(root, SECRETS_DIR)
    tmp = f".secrets.json.{os.getpid()}.{os.urandom(4).hex()}"
    fd = open_beneath(root, [SECRETS_DIR, tmp], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    dfd = None
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(json.dumps(store, indent=1).encode())
            f.flush()
            os.fsync(f.fileno())
        dfd = dir_beneath(root, [SECRETS_DIR])
        os.rename(tmp, "secrets.json", src_dir_fd=dfd, dst_dir_fd=dfd)   # atomic; os.replace takes no dir_fd here
        os.fsync(dfd)
    except BaseException:
        try:
            if dfd is None:
                dfd = dir_beneath(root, [SECRETS_DIR])
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    finally:
        if dfd is not None:
            os.close(dfd)


def set_tool(name: str, tool: str, on: bool) -> set[str]:
    off = disabled_tools(name) - {tool} if on else disabled_tools(name) | {tool}
    _update_tool_settings(name, off=sorted(off))
    return off


def journal_entries(root: Path) -> list[str]:
    text = safe_read(root, "memory/journal.md", "")
    return [b.strip() for b in text.split("\n---\n") if b.strip().startswith("## ")]


class UnsafePath(Exception):
    pass


class TooBig(ValueError):
    """A file in an instance's folder bigger than the read allows."""


class TreeTooBig(Exception):
    """A walk of an instance's folder past its Budget: never taken for an empty or a complete answer."""


@dataclasses.dataclass
class Budget:
    """What walks of an instance's folder may take (walk_beneath): files and folders, bytes read in all, folder
    depth. A field left None is its knob's value when the budget is made. Walks given the same budget share it: each
    takes what it uses from `files` and `total`."""
    files: int | None = None
    total: int | None = None
    depth: int | None = None

    def __post_init__(self):
        self.files = TREE_MAX_FILES if self.files is None else self.files
        self.total = SNAPSHOT_MAX_MB * 2**20 if self.total is None else self.total
        self.depth = TREE_MAX_DEPTH if self.depth is None else self.depth


def contained(root: Path, *parts: str) -> Path:
    """A path inside an instance folder that is safe for the (unsandboxed) supervisor to write.

    Instances can create symlinks in their own folder; following one would let an instance aim a
    supervisor write anywhere on the Mac. So no component may be a symlink, and the result must
    resolve inside the folder. A check, not a guarantee: the instance can swap a link in after it, so a
    read or write goes through the descriptor helpers below (open_beneath), which leave no between."""
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


def open_beneath(root: Path, parts, flags: int, mode: int = 0o644, make_dirs: bool = False) -> int:
    """A descriptor for root/parts, opened one component at a time from the folder down, none of them followed if it
    is a link (O_NOFOLLOW on each directory, through its parent's descriptor). Checking a path and then opening it by
    name lets an instance swap a link in between (Codex review, 2026-10-19); this leaves no between. UnsafePath if a
    component is a link, not a directory, or not a plain name."""
    import errno
    parts = [str(p) for p in parts]
    if not parts or any(p in ("", ".", "..") or "/" in p for p in parts):
        raise UnsafePath(f"{'/'.join(parts)!r} is not a plain path below {root}")
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise UnsafePath(f"{root}: {e.strerror}") from None
    try:
        for part in parts[:-1]:
            if make_dirs:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return os.open(parts[-1], flags | os.O_NOFOLLOW, mode, dir_fd=fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise UnsafePath(f"{root}/{'/'.join(parts)}: a link on the way; the supervisor does not follow links "
                             "inside instances") from None
        raise
    finally:
        os.close(fd)


def dir_beneath(root: Path, parts=(), make: bool = False) -> int:
    """A descriptor for the folder root/parts (root itself when parts is empty), opened like open_beneath: no
    component followed if it is a link. `make`: each missing folder is created first, through its parent's
    descriptor."""
    parts = [str(p) for p in parts]
    if make and parts:
        parent = dir_beneath(root, parts[:-1], make=True)
        try:
            os.mkdir(parts[-1], 0o755, dir_fd=parent)
        except FileExistsError:
            pass
        finally:
            os.close(parent)
    if parts:
        return open_beneath(root, parts, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise UnsafePath(f"{root}: {e.strerror}") from None


def _open_file_beneath(root: Path, parts, flags: int, mode: int = 0o644, make_dirs: bool = False) -> int:
    """open_beneath for a plain file. Non-blocking, then checked: a pipe planted in its place would hang the
    supervisor on a read nobody writes or a write nobody reads, so a pipe, a device or a folder is refused."""
    import errno
    import stat
    try:
        fd = open_beneath(root, parts, flags | os.O_NONBLOCK, mode, make_dirs)
    except OSError as e:
        if e.errno == errno.ENXIO:   # a pipe with no reader, opened to write
            raise UnsafePath(f"{root}/{'/'.join(map(str, parts))} is not a plain file") from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafePath(f"{root}/{'/'.join(map(str, parts))} is not a plain file")
    return fd


def read_beneath(root: Path, parts, limit: int | None = None) -> bytes:
    """A regular file's bytes, opened with open_beneath (no link followed anywhere); TooBig (a ValueError) past
    `limit` bytes, SHAFT_FILE_READ_MAX_MB when it is None. At most limit + 1 bytes are ever read: the size the file
    had when it was opened says nothing of what it holds by the time it is read."""
    limit = FILE_READ_MAX_MB * 2**20 if limit is None else limit
    with os.fdopen(_open_file_beneath(root, parts, os.O_RDONLY), "rb") as f:
        size = os.fstat(f.fileno()).st_size
        data = _read_upto(f, limit + 1) if size <= limit else b""
        if size > limit or len(data) > limit:
            raise TooBig(f"{max(size, len(data))} bytes, more than {limit}")
        return data


def _read_upto(f, n: int) -> bytes:
    """At most n bytes of f, in chunks: f.read(n) would set aside all n bytes first, however small the file."""
    chunks, got = [], 0
    while got < n and (chunk := f.read(min(2**20, n - got))):
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def safe_read_bytes(root: Path, rel: str, default: bytes | None = None, limit: int | None = None) -> bytes:
    """The bytes of the plain file root/rel, read with read_beneath: no component followed if it is a link. How the
    unsandboxed supervisor reads anything in an instance folder that it copies, shows or sends on: the instance can
    plant a link anywhere in its folder, and reading through one hands it — or a judge's cloud model, or the
    operator's pages — any file the supervisor can read (Codex review B1, 2026-10-24: a journal swapped for a link was
    copied into the instance's own journal.rejected-*.md). `default`, when given, stands for a file that is missing
    or is not a plain file reached without a link (or is past the limit): to its reader a link is no file at all.
    Without it: OSError (FileNotFoundError), UnsafePath or TooBig."""
    try:
        return read_beneath(root, Path(rel).parts, limit)
    except (FileNotFoundError, UnsafePath, TooBig):
        if default is None:
            raise
        return default


def safe_read(root: Path, rel: str, default: str | None = None, errors: str = "strict") -> str:
    """safe_read_bytes as UTF-8 text."""
    try:
        return read_beneath(root, Path(rel).parts).decode("utf-8", errors)
    except (FileNotFoundError, UnsafePath, TooBig):
        if default is None:
            raise
        return default


def list_beneath(root: Path, rel: str = "") -> list[str]:
    """The names in the folder root/rel, opened like open_beneath: [] if it is missing or reached through a link
    (never the listing of the folder a link points to). An entry may itself be a link: safe_read refuses one."""
    try:
        fd = dir_beneath(root, Path(rel).parts)
    except (FileNotFoundError, UnsafePath):
        return []
    try:
        return sorted(os.listdir(fd))
    finally:
        os.close(fd)


def set_aside(root: Path, rel: str, name: str) -> str:
    """Rename root/rel to `name` in the same folder, through the folder's descriptor: the entry itself is moved — a
    link, never what it points to — and nothing is read. Returns the new path, relative to root."""
    parts = Path(rel).parts
    fd = dir_beneath(root, parts[:-1])
    try:
        os.rename(parts[-1], name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)
    return str(Path(*parts[:-1], name))


def unlink_beneath(root: Path, rel: str) -> bool:
    """Remove root/rel — a link itself, never what it points to — through its folder's descriptor. False if it is
    not there, is a folder, or its folder is reached through a link (nothing in that folder is touched)."""
    import stat
    parts = Path(rel).parts
    try:
        fd = dir_beneath(root, parts[:-1])
    except (FileNotFoundError, UnsafePath):
        return False
    try:
        if stat.S_ISDIR(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode):
            return False
        os.unlink(parts[-1], dir_fd=fd)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(fd)


class _Capped:
    """A file of a walk, readable up to what is left of the walk's byte budget (one byte more is a TreeTooBig)."""

    def __init__(self, f, left: int, what: str):
        self.f, self.left, self.used, self.what = f, left, 0, what

    def read(self, n: int = -1) -> bytes:
        room = self.left - self.used + 1
        data = _read_upto(self.f, room) if n is None or n < 0 else self.f.read(min(n, room))
        self.used += len(data)
        if self.used > self.left:
            raise TreeTooBig(f"{self.what}: past what is left of the walk's byte budget ({self.left} bytes)")
        return data

    def fileno(self) -> int:
        return self.f.fileno()


def walk_beneath(root: Path, rel: str = "", skip=lambda rel: False, links: bool = False, budget: Budget | None = None,
                 check=None, others: bool = False):
    """What is below the folder root/rel, opened through descriptors and nothing followed: (path, None) for a folder
    (before what is in it), (path, file) for a plain file, open to read until the next item, and with `links`
    (path, its target) for a link — the link itself, never what it points to. A pipe or a socket, and a link without
    `links`, are passed over — with `others`, (path, its file type, an int: stat.S_IFMT) for anything else, so a
    reader that must not miss one (the gate) sees it. skip(path) leaves out a path and everything below it; check(), if given, runs before
    each entry (a deadline, an interrupt). Paths are relative to root/rel. One folder is open at a time, listed
    whole and then closed — no recursion holding a descriptor per level — and the walk stays inside `budget`
    (files and folders, bytes read from its files, depth), else TreeTooBig. How the unsandboxed supervisor reads a
    whole folder an instance writes: an exam's copy, a trial's diff, the gate's snapshot. FileNotFoundError, or
    UnsafePath when root/rel is not a folder reached without a link, is raised here, before anything is walked."""
    budget = budget or Budget()
    base = [str(p) for p in Path(rel).parts]
    os.close(dir_beneath(root, base))
    return _walk(root, base, skip, links, budget, check, others)


def _walk(root: Path, base: list[str], skip, links: bool, budget: Budget, check, others: bool = False):
    import errno
    import stat
    stack = [()]
    while stack:
        at = stack.pop()
        try:
            fd = dir_beneath(root, [*base, *at])
        except (FileNotFoundError, UnsafePath):
            if not at:
                raise
            continue   # removed, or swapped for a link, since it was listed: passed over
        try:
            for entry in sorted(os.listdir(fd)):
                path = "/".join([*at, entry])
                if skip(path):
                    continue
                if check:
                    check()
                try:
                    mode = os.stat(entry, dir_fd=fd, follow_symlinks=False).st_mode
                    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode) or (stat.S_ISLNK(mode) and links)):
                        if others and not stat.S_ISLNK(mode):
                            yield path, stat.S_IFMT(mode)
                        continue
                    budget.files -= 1
                    if budget.files < 0:
                        raise TreeTooBig(f"{root}: more files and folders than the walk may take")
                    if stat.S_ISDIR(mode):
                        if len(at) + 1 > budget.depth:
                            raise TreeTooBig(f"{root}/{path}: more than {budget.depth} folders deep")
                        yield path, None
                        stack.append((*at, entry))
                    elif stat.S_ISREG(mode):
                        ffd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                        with os.fdopen(ffd, "rb") as f:
                            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                                continue
                            capped = _Capped(f, budget.total, f"{root}/{path}")
                            yield path, capped
                            budget.total -= capped.used
                    else:
                        yield path, os.readlink(entry, dir_fd=fd)
                except (FileNotFoundError, NotADirectoryError):
                    continue   # removed, or swapped for something else, since it was listed: passed over
                except OSError as e:
                    if e.errno != errno.ELOOP:   # swapped for a link since it was listed: passed over
                        raise
        finally:
            os.close(fd)


def read_tree(root: Path, rel: str = "", skip=lambda rel: False, budget: Budget | None = None) -> dict[str, bytes]:
    """{path: bytes} of every plain file below root/rel (walk_beneath: nothing followed, inside the budget)."""
    return {path: f.read() for path, f in walk_beneath(root, rel, skip, budget=budget) if f is not None}


def reserved(root: Path, rel: str, names) -> bool:
    """Whether root/rel is one of root's top-level `names` (secrets/, .git, logs/ …) or below one — decided by what
    it is, not how it is spelt: the volume is case-insensitive (`Secrets/secrets.json` opens `secrets/secrets.json`) and
    folds Unicode, so the first component is compared case-folded, and by device and inode with each reserved entry
    there (lstat: no link followed)."""
    parts = Path(rel).parts
    if not parts:
        return False
    if parts[0].casefold() in {n.casefold() for n in names}:
        return True
    try:
        st = os.lstat(Path(root) / parts[0])
    except OSError:
        return False
    for n in names:
        try:
            r = os.lstat(Path(root) / n)
        except OSError:
            continue
        if (r.st_dev, r.st_ino) == (st.st_dev, st.st_ino):
            return True
    return False


def link_stays_inside(path: str, target: str) -> bool:
    """Whether a link at `path` (relative to the top of a copy) to `target` resolves inside the copy whatever links lie
    on its way: relative, its ".." all first and no more of them than the link's own folder is deep — the copy's
    folders are real ones, so leading ".." climb exactly that far — and then only names, each a real folder of the
    copy or a link kept by this same rule. A ".." after a name could climb from wherever a link there points."""
    if not target or target.startswith("/"):
        return False
    parts = [p for p in target.split("/") if p not in ("", ".")]
    up = 0
    while up < len(parts) and parts[up] == "..":
        up += 1
    return ".." not in parts[up:] and up <= path.count("/")


def copy_beneath(root: Path, dest: Path, skip=lambda rel: False, rel: str = "", links: bool = False,
                 budget: Budget | None = None, check=None) -> int:
    """The folders and plain files below root/rel copied to the new folder dest (mode and times kept), read through
    walk_beneath (nothing followed, inside the budget: SHAFT_COPY_MAX_GB in all by default) and written through
    descriptors too: each folder of the copy is made and reopened through its parent's descriptor, each file created
    with O_EXCL and O_NOFOLLOW, so a folder of the copy swapped for a link while it is filled (a process the copied
    code left running) is refused, never written through. With `links`, a link that stays inside the copy
    (link_stays_inside) is copied as the link it is; any other link is left out. No link is ever read through.
    Returns the files copied; a failure leaves the partial copy for the caller to remove."""
    import shutil
    import stat
    budget = budget or Budget(total=int(COPY_MAX_GB * 2**30))
    walked = walk_beneath(root, rel, skip, links, budget, check)   # FileNotFoundError or UnsafePath before dest is made
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.mkdir(dest, 0o755)                                          # new: FileExistsError for a folder already there
    n, at, fd = 0, None, None
    try:
        for path, item in walked:
            parts = path.split("/")
            if tuple(parts[:-1]) != at:
                if fd is not None:
                    os.close(fd)
                fd = None
                fd, at = dir_beneath(dest, parts[:-1]), tuple(parts[:-1])
            if item is None:
                os.mkdir(parts[-1], 0o755, dir_fd=fd)
            elif isinstance(item, str):
                if link_stays_inside(path, item):
                    os.symlink(item, parts[-1], dir_fd=fd)
            else:
                st = os.fstat(item.fileno())
                out = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              stat.S_IMODE(st.st_mode) | 0o200, dir_fd=fd)
                with os.fdopen(out, "wb") as o:
                    shutil.copyfileobj(item, o)
                    os.fchmod(o.fileno(), stat.S_IMODE(st.st_mode))
                    os.utime(o.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
                n += 1
    finally:
        if fd is not None:
            os.close(fd)
    return n


def safe_write(root: Path, rel: str, text: str, append: bool = False) -> Path:
    """Text written, or appended, to root/rel through descriptors (open_beneath), as safe_write_bytes does: a
    check of the path followed by an open by name let an instance swap a link in between and aim the write — an
    inbox delivery, a journal note — anywhere (Codex review B2, 2026-10-24)."""
    path = contained(root, *Path(rel).parts)       # the clear refusal for a link already there
    Path(root).mkdir(parents=True, exist_ok=True)  # the folder itself is the supervisor's choice
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    with os.fdopen(_open_file_beneath(root, Path(rel).parts, flags, make_dirs=True), "a" if append else "w") as f:
        f.write(text)
    return path


def safe_write_bytes(root: Path, rel: str, data: bytes, exclusive: bool = False) -> Path:
    """safe_write for a file's bytes (attachments, documents): every component opened without following a link
    (open_beneath), so nothing swapped in after the check can aim the write elsewhere."""
    path = contained(root, *Path(rel).parts)       # the clear refusal for a link already there
    Path(root).mkdir(parents=True, exist_ok=True)  # the folder itself is the supervisor's choice (an inbox, an instance)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)   # exclusive: FileExistsError
    with os.fdopen(_open_file_beneath(root, Path(rel).parts, flags, make_dirs=True), "wb") as f:
        f.write(data)
    return path


def safe_mkdir(root: Path, rel: str) -> Path:
    path = contained(root, *Path(rel).parts)
    Path(root).mkdir(parents=True, exist_ok=True)
    os.close(dir_beneath(root, Path(rel).parts, make=True))   # each folder made through its parent's descriptor
    return path


def journal_note(root: Path, title: str, body: str) -> None:
    """Supervisor's own minimal journal append (deliberately not the agent's code). A journal the instance swapped
    for a link (or whose folder it did) gets no note — never one written through the link — and the log says so."""
    try:
        safe_write(root, "memory/journal.md",
                   f"\n---\n## {datetime.now():%Y-%m-%d %H:%M} — {title}\n\n{body.strip()}\n", append=True)
    except UnsafePath as e:
        log(f"{Path(root).name}: journal note {title!r} NOT written: {e}")


def not_mind() -> list[str]:
    """A pathspec for the whole work tree except the mind paths (for reading the branch: log, show)."""
    return [".", *(f":(exclude,top){p}" for p in MIND_PATHS)]


def add_all_but_mind(root: Path, project: Path | None = None) -> None:
    """`git add -A` of everything git would add, except the mind paths and the supervisor's copy of the docs
    (DOCS_COPY), whatever .gitignore says. The files are listed first (an exclude pathspec makes `add` fail once the
    excluded folder is ignored), so no mind file is ever hashed into the project's repository either."""
    out = git_wt(root, "ls-files", "-z", "--others", "--exclude-standard", "--modified", "--deleted",
                 project=project).stdout
    paths = sorted({p for p in out.split("\0") if p and Path(p).parts[0] not in (*MIND_PATHS, DOCS_COPY)})
    if paths:
        git_wt(root, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", input="\0".join(paths),
               project=project)


def err_text(e: BaseException) -> str:
    """A failed command's stderr as text, whether it ran in text mode (str) or not (bytes): calling .decode() on a
    str raised AttributeError in place of the reason (Codex on 9bd175e)."""
    err = getattr(e, "stderr", None) or ""
    return (err.decode(errors="replace") if isinstance(err, bytes) else str(err)).strip()


def _commit_gated(name: str, root: Path, project: Path | None, msg: str, others: list[str], code: list[str],
                  gated: dict, who: tuple[str, str] | None = None, parents: tuple[str, ...] = (),
                  from_index: bool = False) -> list[str]:
    """Commit `code` paths with exactly the bytes in `gated` (what the gate checked; a path missing there is deleted)
    and `others` as the working tree has them, on top of HEAD, through a separate index: the bytes go into the
    object store (hash-object -w) and the index (update-index --cacheinfo, --force-remove), and nothing is read
    from the working tree for code. `git add` read it at the moment of the commit, so a write after the gate's
    snapshot was what got committed, and committed code is approved code (the coordinator on 8c84f11). `who`: the
    author (name, email), the instance's own by default; `parents`: more parents after HEAD (a merge); `from_index`:
    start from the worktree's index as staged (a merge being built), not from HEAD. Returns the
    paths the commit changed; the real index is brought to it for them."""
    import stat
    import tempfile
    fd, index = tempfile.mkstemp(prefix="shaft-index-")
    os.close(fd)
    os.unlink(index)                                    # git makes it
    env = {"GIT_INDEX_FILE": index}

    def wt(*a, **k):
        return git_wt(root, *a, project=project, **k)
    try:
        head = wt("rev-parse", "--verify", "-q", "HEAD", check=False).stdout.strip()
        if from_index:
            import shutil
            shutil.copyfile(wt("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip(), index)
        elif head:
            wt("read-tree", head, env=env)
        else:
            wt("read-tree", "--empty", env=env)
        if others:
            wt("add", "-A", "--", *others, env=env)
        modes = {}   # each file's mode as staged (a birth: the parent it was taken from), else as committed
        listing = (wt("ls-files", "-s", "-z", "--", *code, env=env) if from_index
                   else wt("ls-tree", "-z", head, "--", *code) if head else None) if code else None
        for entry in (listing.stdout.split("\0") if listing else []):
            meta, _, path = entry.partition("\t")
            if path:
                modes[path] = meta.split()[0]
        for path in code:
            data = gated.get(path)
            if isinstance(data, bytes):
                oid = wt("hash-object", "-w", "--stdin", input=data, text=False).stdout.decode().strip()
                mode = modes.get(path)
                if mode not in ("100644", "100755"):
                    try:
                        mode = "100755" if os.lstat(root / path).st_mode & stat.S_IXUSR else "100644"
                    except OSError:
                        mode = "100644"
                wt("update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}", env=env)
            else:
                wt("update-index", "--force-remove", "--", path, env=env)
        tree = wt("write-tree", env=env).stdout.strip()
    finally:
        try:
            os.unlink(index)
        except FileNotFoundError:
            pass
    if head and not parents and tree == wt("rev-parse", f"{head}^{{tree}}").stdout.strip():
        return []
    author, email = who or (f"SHAFT/{name}", f"{name}@shaft.localhost")
    commit = wt("-c", f"user.name={author}", "-c", f"user.email={email}", "commit-tree", tree,
                *(["-p", head] if head else []), *[a for p in parents for a in ("-p", p)], "-m", msg).stdout.strip()
    wt("update-ref", "-m", msg.splitlines()[0], "HEAD", commit, *([head] if head else []))
    changed = [p for p in wt("diff-tree", "-r", "--name-only", "-z", "--no-commit-id", "--root", *([head] if head
                                                                                                    else []),
                             commit).stdout.split("\0") if p]
    def entries(listing: str, field: int) -> dict[str, str]:   # the object id: "mode oid stage" or "mode type oid"
        out = {}
        for entry in listing.split("\0"):
            meta, _, path = entry.partition("\t")
            if path:
                out[path] = meta.split()[field]
        return out
    if changed:
        staged = entries(wt("ls-files", "-s", "-z", "--", *changed).stdout, 1)
        was = entries(wt("ls-tree", "-r", "-z", head, "--", *changed).stdout, 2) if head else {}
        now = entries(wt("ls-tree", "-r", "-z", commit, "--", *changed).stdout, 2)
        theirs = [p for p in changed if p in staged and staged[p] not in (was.get(p), now.get(p))]
        if theirs:   # staged by someone else (the operator): their index entry stays; the commit holds the gated bytes
            log(f"{name}: {', '.join(theirs)} staged by someone else — left staged as they were; committed as checked")
        if (ours := [p for p in changed if p not in theirs]):
            wt("reset", "-q", "--", *ours, check=False)   # the real index: these paths as just committed
    return changed


def changed_vs_head(root: Path, project: Path | None = None) -> list[str]:
    """Every path whose content differs from the worktree's HEAD — tracked (staged or not) and untracked, not
    ignored — but the mind paths and the supervisor's copy of the docs (DOCS_COPY)."""
    def names(*a):
        return [os.fsdecode(e) for e in git_wt(root, *a, project=project, text=False).stdout.split(b"\0") if e]
    head = git_wt(root, "rev-parse", "--verify", "-q", "HEAD", project=project, check=False).stdout.strip()
    paths = set(names("ls-files", "-z", "--others", "--exclude-standard"))
    if head:
        paths |= set(names("diff", "--name-only", "-z", "--no-renames", head))
    return sorted(p for p in paths if Path(p).parts[0] not in (*MIND_PATHS, DOCS_COPY))


def commit_state(name: str, kind: str, kept_code: list[str], root: Path | None = None,
                 project: Path | None = None, note: str = "", gated: dict | None = None) -> bool:
    """Commit the instance's mind to its mind store, then its skills and the code changes that passed the gate
    to its branch — the same message on both sides. A mind path is never staged on the branch, whatever its
    .gitignore says.

    Only files git actually staged are named in the branch commit: an empty directory (skills/ on a young
    instance) is unknown to git and would fail as a pathspec. Returns False — after logging git's own error —
    if either commit failed; the mind is committed first, so a failure there leaves the branch untouched too.
    `gated` (the gate's snapshot): kept code paths are committed with those bytes, never the working tree's
    (_commit_gated).
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
        from .gate import CODE_PATHS
        code = [p for p in kept_code if Path(p).parts[0] in CODE_PATHS] if gated is not None else []
        paths = [p for p in SKILL_PATHS if (root / p).exists()] + [p for p in kept_code if p not in code]
        staged = []
        if gated is not None:                    # committed by _commit_gated itself: never `commit -- paths`, which
            staged = _commit_gated(name, root, project, msg, paths, code, gated)   # re-reads the working tree
        elif paths:
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
