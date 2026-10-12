"""macOS sandbox profile for one agent instance.

An instance may write only inside its own directory (instances/<name>/), may
not read anything else in the operator's home — including other instances
and the supervisor's state — and may only connect to allowed localhost ports.
Rules are evaluated last-match-wins.
"""
from __future__ import annotations

from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def _q(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(instance_root: Path, home: Path, llm_ports: list[int], any_localhost: bool = False) -> str:
    root, home = Path(instance_root).resolve(), Path(home).resolve()
    lines = [
        "(version 1)",
        "(allow default)",
        # Writes: only its own directory and terminal/null devices.
        "(deny file-write*)",
        f"(allow file-write* (subpath {_q(root)}) (literal \"/dev/null\") (literal \"/dev/tty\")"
        " (regex #\"^/dev/ttys[0-9]+$\") (subpath \"/dev/fd\"))",
        # Reads: nothing of the operator's home except its own directory.
        f"(deny file-read* (subpath {_q(home)}))",
        f"(allow file-read* (subpath {_q(root)}))",
        _ancestors_metadata(root, home),
        # Network: only the LLM endpoint(s) on localhost.
        "(deny network*)",
    ]
    if any_localhost:
        lines.append('(allow network* (local ip "localhost:*") (remote ip "localhost:*"))')
    for port in llm_ports:
        lines.append(f'(allow network-outbound (remote ip "localhost:{int(port)}"))')
    return "\n".join(lines) + "\n"


RUN_WRITABLE = ("notes", "plugins", "skills", "tests", "shaft", ".tmp")


def _ancestors_metadata(root: Path, home: Path) -> str:
    """Path resolution (realpath) stats every parent directory; allow only their metadata."""
    parents = [p for p in root.parents if p == home or home in p.parents]
    return "(allow file-read-metadata " + " ".join(f"(literal {_q(p)})" for p in parents) + ")" if parents else ""


def run_profile(instance_root: Path, home: Path) -> str:
    """Stricter than the agent's own sandbox, for commands the agent runs via the broker's `run`:
    writes only to its working areas (never self/, memory/, inbox/, config.env, .git), reads only its
    own folder in the home directory, and no network at all."""
    root, home = Path(instance_root).resolve(), Path(home).resolve()
    writable = " ".join(f"(subpath {_q(root / d)})" for d in RUN_WRITABLE)
    return "\n".join([
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* {writable} (literal "/dev/null") (literal "/dev/tty") (subpath "/dev/fd"))',
        f"(deny file-read* (subpath {_q(home)}))",
        f"(allow file-read* (subpath {_q(root)}))",
        _ancestors_metadata(root, home),
        "(deny network*)",
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
