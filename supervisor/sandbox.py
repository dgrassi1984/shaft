"""macOS sandbox profile for one agent instance.

An instance may write only inside its own directory (instances/<name>/), may
not read anything else in the operator's home — including other instances
and the supervisor's state — and may only connect to allowed localhost ports.
Its way to the internet is one of those ports: the run's egress proxy
(supervisor/egress.py), which reaches only public addresses. Rules are
evaluated last-match-wins.
"""
from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def _q(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _no_secrets(root: Path) -> str:
    """The instance's keys (secrets/) are handled only by the supervisor's broker: not by the agent, its
    plugins or `run` — a value must never come back into its context. Later rules win, so this follows the
    allows."""
    return f"(deny file-read* file-write* (subpath {_q(root / 'secrets')}))"


@functools.lru_cache(maxsize=1)
def user_folder() -> Path | None:
    """macOS's per-user folder (/private/var/folders/<xx>/<id>/: T temp files, C caches): outside the home, but the
    operator's — every app they run keeps its temp files and caches there. None if macOS does not say where it is."""
    r = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True)
    temp = r.stdout.strip()
    return Path(temp).resolve().parent if r.returncode == 0 and temp else None


# The operator's places outside their home. Until 2026-10-16 only the home was denied, and any sandboxed process could
# list and read these: /private/tmp held Claude Code's working files for every project of theirs; /Users/Shared and
# /Volumes (other disks) hold whatever they put there.
OUTSIDE_HOME = ("/private/tmp", "/Users/Shared", "/Volumes")


def _operator_places_closed() -> list[str]:
    """The per-user folder (every app's temp files and caches) and OUTSIDE_HOME, closed. First in each profile, so
    the rules after it still open what an instance may use there: its own folder, shared folder and environment (a
    test's instance lives in a temp folder), and the image tools' temp names (_image_temp_names)."""
    folder = user_folder()
    if folder is None:
        from .core import log
        log("sandbox: macOS did not say where the per-user temp folder is (getconf DARWIN_USER_TEMP_DIR); "
            "it is left readable")
    places = [*([folder] if folder else []), *OUTSIDE_HOME]
    return ["(deny file-read* file-write* " + " ".join(f"(subpath {_q(p)})" for p in places) + ")"]


def _image_temp_names() -> list[str]:
    """The names Apple's image tools give their own temp files in the per-user folder: `sips` writes
    .<UUID>-<6 chars>, renames it to <UUID>, then copies the result where it was asked (it ignores TMPDIR; without
    these it fails with "Cannot write to file …/T/<UUID>"). They are random and the folder cannot be listed, so no
    other app's file is reachable. After the write rules (writes are denied there). SBPL regexes have no {n}
    counts — a pattern with them never matches — so `+` it is."""
    folder = user_folder()
    if folder is None:
        return []
    hexes = "[0-9A-F]+"
    name = rf"^{re.escape(str(folder / 'T'))}/\.?{hexes}-{hexes}-{hexes}-{hexes}-{hexes}(-[A-Za-z0-9]+)?$"
    return [f'(allow file-read* file-write* (regex #"{name}"))']


# Files in an instance's folder that only the operator writes directly (its sandbox cannot).
OPERATOR_FILES = ("config.env", "drive.md")


def _no_operator_file_writes(root: Path) -> str:
    """config.env has two writers: the operator, and the instance through the broker's set_config, which records
    each change. Writing it directly would make its changes indistinguishable from the operator's (the gate
    reverted an operator's mid-run model switch as the instance's, kant 2026-10-13). drive.md, what the instance is
    for (docs/plans/PLAN-thelos.md), has one writer: the operator — the instance changes how it pursues its drive, not the
    drive. This also stops creating, renaming, unlinking or hard-linking either file."""
    return "(deny file-write* " + " ".join(f"(literal {_q(root / f)})" for f in OPERATOR_FILES) + ")"


def _shared(shared: Path | None, home: Path) -> list[str]:
    """Read-only access to the operator's folder (`shaft share`): reads, and the metadata of its parents so the
    path resolves. No write rule: the instance still writes only in its own directory."""
    if not shared:
        return []
    shared = Path(shared).resolve()
    rules = [f"(allow file-read* (subpath {_q(shared)}))"]
    if (meta := _ancestors_metadata(shared, home)):
        rules.append(meta)
    return rules


def _env_reads(root: Path, env_root: Path | None, home: Path) -> list[str]:
    """A copy of an instance (coder hand-off, trial gate, trial replay) reads the instance's real Python environment
    (supervisor/pyenv.py), read-only: it tests with the packages the instance has, and cannot change them."""
    if not env_root or Path(env_root).resolve() == root:
        return []
    from .pyenv import ENV_DIRS
    env_root = Path(env_root).resolve()
    rules = ["(allow file-read* " + " ".join(f"(subpath {_q(env_root / d)})" for d in ENV_DIRS) + ")"]
    if (meta := _ancestors_metadata(env_root / ENV_DIRS[0], home)):
        rules.append(meta)
    return rules


def profile(instance_root: Path, home: Path, llm_ports: list[int], shared: Path | None = None,
            env_root: Path | None = None) -> str:
    """The agent's own sandbox. `llm_ports` are the local ports it may connect to: its models, the embedding
    service, its broker and its egress proxy. The gate passes none: an instance's tests and plugin check get no
    network at all, like `run` without its proxy. Until 2026-10-18 the gate's profile allowed any localhost port
    (network* on "localhost:*"), so instance-written tests could reach every service on the operator's Mac — their dev
    servers and databases, the model and embedding servers; no instance's tests used it."""
    root, home = Path(instance_root).resolve(), Path(home).resolve()
    lines = [
        "(version 1)",
        "(allow default)",
        *_operator_places_closed(),
        # Writes: only its own directory and terminal/null devices.
        "(deny file-write*)",
        f"(allow file-write* (subpath {_q(root)}) (literal \"/dev/null\") (literal \"/dev/tty\")"
        " (regex #\"^/dev/ttys[0-9]+$\") (subpath \"/dev/fd\"))",
        # Reads: nothing of the operator's home except its own directory.
        f"(deny file-read* (subpath {_q(home)}))",
        f"(allow file-read* (subpath {_q(root)}))",
        _ancestors_metadata(root, home),
        *_shared(shared, home),
        *_env_reads(root, env_root, home),
        *_image_temp_names(),
        _no_secrets(root),
        _no_operator_file_writes(root),
        # Network: only the given localhost ports (the internet only through the egress proxy among them).
        "(deny network*)",
    ]
    for port in llm_ports:
        lines.append(f'(allow network-outbound (remote ip "localhost:{int(port)}"))')
    return "\n".join(lines) + "\n"


RUN_WRITABLE = ("notes", "plugins", "skills", "tests", "shaft", ".tmp", ".venv", ".uv", ".browser")


def _browser_reads(home: Path) -> list[str]:
    """The shared Chromium (supervisor/browser.py, docs/plans/PLAN-browser.md), read-only — only in the sandboxes that may
    browse: `run` (nothing but the egress proxy) and jobs. The agent's own sandbox, which its plugins inherit, carries
    the broker token and reaches the model ports, and the gate's has no network: neither reads it, so a browser
    started there fails to launch."""
    from .browser import BROWSERS
    browsers = Path(BROWSERS).resolve()
    rules = [f"(allow file-read* (subpath {_q(browsers)}))"]
    if (meta := _ancestors_metadata(browsers, Path(home).resolve())):
        rules.append(meta)
    return rules


def _ancestors_metadata(root: Path, home: Path) -> str:
    """Path resolution (realpath) stats every parent directory; allow only their metadata."""
    parents = [p for p in root.parents if p == home or home in p.parents]
    return "(allow file-read-metadata " + " ".join(f"(literal {_q(p)})" for p in parents) + ")" if parents else ""


def run_profile(instance_root: Path, home: Path, shared: Path | None = None, egress_port: int = 0,
                env_root: Path | None = None) -> str:
    """Stricter than the agent's own sandbox, for commands the agent runs via the broker's `run`:
    writes only to its working areas, its Python environment and its browser profile (.browser/, docs/plans/PLAN-browser.md;
    never self/, memory/, inbox/, config.env, .git), reads only its own folder in the home directory (and the
    operator's shared folder, if any, and the shared Chromium), and no network but the run's egress proxy (the
    public internet, nothing local)."""
    from .pyenv import DECLARED
    root = Path(instance_root).resolve()
    writable = " ".join([*(f"(subpath {_q(root / d)})" for d in RUN_WRITABLE),
                         *(f"(literal {_q(root / f)})" for f in DECLARED)])
    return _command_profile(root, home, writable, shared, [egress_port] if egress_port else [], env_root)


def job_profile(instance_root: Path, home: Path, state_dir: Path, shared: Path | None = None, egress_port: int = 0,
                broker_port: int = 0, env_root: Path | None = None) -> str:
    """For a scheduled job (docs/plans/PLAN-jobs.md): the `run` sandbox, but writes only to the job's own state folder
    (.jobs/<plugin>/), .tmp/ and its browser profile — a cycle running at the same time works on the code, notes and
    mind, and nothing of the job's may be mistaken for its change (the profile is never code and never committed) —
    and the network adds the job's own broker (its supervisor tools)."""
    from .browser import PROFILE
    root = Path(instance_root).resolve()
    writable = (f"(subpath {_q(Path(state_dir).resolve())}) (subpath {_q(root / '.tmp')}) "
                f"(subpath {_q(root / PROFILE)})")
    return _command_profile(root, home, writable, shared, [p for p in (egress_port, broker_port) if p], env_root)


def _command_profile(root: Path, home: Path, writable: str, shared: Path | None, ports: list[int],
                     env_root: Path | None) -> str:
    home = Path(home).resolve()
    return "\n".join([
        "(version 1)",
        "(allow default)",
        *_operator_places_closed(),
        "(deny file-write*)",
        f'(allow file-write* {writable} (literal "/dev/null") (literal "/dev/tty") (subpath "/dev/fd"))',
        f"(deny file-read* (subpath {_q(home)}))",
        f"(allow file-read* (subpath {_q(root)}))",
        _ancestors_metadata(root, home),
        *_shared(shared, home),
        *_env_reads(root, env_root, home),
        *_browser_reads(home),
        *_image_temp_names(),
        _no_secrets(root),
        "(deny network*)",
        *(f'(allow network-outbound (remote ip "localhost:{int(port)}"))' for port in ports),
    ]) + "\n"


# Environment variables an instance may inherit. Everything else — API keys,
# tokens, the operator's shell secrets — is dropped.
ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "TERM", "COLUMNS", "LINES", "TZ")
ENV_ALLOW_PREFIXES = ("LC_", "LLM_")


def agent_env(environ: dict[str, str], **extra: str) -> dict[str, str]:
    env = {k: v for k, v in environ.items()
           if k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)}
    env.update(extra)
    return env


def wrap(profile_path: Path, argv: list[str]) -> list[str]:
    return [SANDBOX_EXEC, "-f", str(profile_path), *argv]
