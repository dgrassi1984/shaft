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
        # Network: only the LLM endpoint(s) on localhost.
        "(deny network*)",
    ]
    if any_localhost:
        lines.append('(allow network* (local ip "localhost:*") (remote ip "localhost:*"))')
    for port in llm_ports:
        lines.append(f'(allow network-outbound (remote ip "localhost:{int(port)}"))')
    return "\n".join(lines) + "\n"


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
