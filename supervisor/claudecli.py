"""Claude Code (`claude -p`) backend for retained plan sessions, on the operator's own Claude subscription.

The unmodified `claude` binary does all of the authentication: `shaft claude login` runs `claude auth login`
with a private config directory, and SHAFT never reads, copies or stores the resulting credential. Nothing
here calls Anthropic's API directly with subscription credentials.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from . import planchannel
from .core import SUP_DIR, cloud_model, knob, positive_int

PREFIX = 'claudecode/'


def serves(model):
    return isinstance(model, str) and model.startswith(PREFIX) and bool(model[len(PREFIX):])


def _models(value):
    names = [s.strip() for s in value.split(',') if s.strip()]
    if not names or not all(serves(s) for s in names):
        raise ValueError('must name claudecode/ models separated by commas')
    return names


def _percent(value):
    n = int(value)
    if not 0 <= n <= 100:
        raise ValueError('must be between 0 and 100')
    return n


HOME, _ = knob("SHAFT_CLAUDE_HOME", str(SUP_DIR / 'claude-home'), Path,
    'private Claude Code configuration and login directory', group='Claude', services=('runs', 'api'))
BIN, _ = knob("SHAFT_CLAUDE_BIN", str(Path.home() / '.local/bin/claude'), str,
    'Claude Code CLI executable', group='Claude', services=('runs', 'api'))
MODELS, _ = knob("SHAFT_CLAUDE_MODELS", 'claudecode/claude-opus-5-5,claudecode/claude-sonnet-5-5', _models,
    'Claude models offered to instances through the Claude plan, comma separated', group='Claude', services=('runs', 'api'))
WEEKLY_PCT, _ = knob("SHAFT_CLAUDE_WEEKLY_PCT", '85', _percent,
    'stop Claude plan calls at this percentage of the weekly allowance', group='Claude', services=('runs', 'api'))
FALLBACK_MODEL, _ = knob("SHAFT_CLAUDE_FALLBACK_MODEL", 'openrouter/deepseek/deepseek-v4.1-flash', cloud_model,
    'cloud model used for the run after a Claude plan failure', group='Claude', services=('runs', 'api'))
TIMEOUT_S, _ = knob("SHAFT_CLAUDE_TIMEOUT_S", '900', positive_int,
    'seconds Claude Code may take per step; held calls are exempt', group='Claude', services=('runs', 'api'))
LABEL, VENDOR, SEES_IMAGES = 'Claude', 'anthropic', True
STATE = planchannel.ProviderState('claudecode', LABEL)


def models():
    return list(MODELS)


def home() -> Path:
    path = Path(HOME).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def child_env(config_dir: Path, folder: Path) -> dict:
    """The environment of every `claude` child: private config dir, no updater, no telemetry.

    HOME stays the operator's: Claude Code finds its login in the login keychain through HOME (probe 2026-11-12:
    HOME=<run folder> reads as "Not logged in"). The sandbox still closes the home folder except that keychain file.
    """
    return dict(HOME=str(Path.home()), USER=os.environ.get('USER', ''), PATH='/usr/bin:/bin:/usr/sbin:/sbin',
                # Claude Code keeps its own temp root (/tmp/claude-<uid>) apart from TMPDIR; the sandbox closes /tmp.
                TMPDIR=str(folder), CLAUDE_CODE_TMPDIR=str(folder), LANG='en_US.UTF-8', CLAUDE_CONFIG_DIR=str(config_dir),
                DISABLE_AUTOUPDATER='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                # Held SHAFT calls can outlast the default MCP tool timeout; the channel's own timeout governs.
                MCP_TOOL_TIMEOUT=str((TIMEOUT_S + 3600) * 1000))


def login_status() -> dict:
    """`claude auth status` for the private config dir. Never contains the credential itself."""
    config_dir = home()
    env = child_env(config_dir, config_dir)
    try:
        out = subprocess.run([str(Path(BIN).expanduser()), 'auth', 'status'], env=env,
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {'loggedIn': False, 'error': type(exc).__name__}
