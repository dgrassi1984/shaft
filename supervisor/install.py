"""`shaft install --operator NAME [--email ADDR] --json`: the steps of a fresh-Mac install after the server is cloned.

One JSON line per step on stdout, `{"step", "ok", "detail"}`, in this order: identity, setup, api, web, scheduler. The
first failure stops the run (exit 1, its line holds the real error); all ok is exit 0. Every step can be repeated: a
rerun after success changes nothing. The steps run in process; `runner` is the seam tests use instead of them."""
from __future__ import annotations

import contextlib
import getpass
import io
import json
import socket
import subprocess

from . import core

STEPS = ("identity", "setup", "api", "web", "scheduler")


class StepFailed(Exception):
    """A step that did not succeed; the message is what the app shows."""


def _git_config(*args: str) -> str:
    return core.git("config", *args, check=False).stdout.strip()


def _identity(operator: str, email: str | None) -> str:
    """Commits need an author. Keep any identity git already resolves (repo-local or global); else set the repo-local one.
    The global git config is never written."""
    if _git_config("user.name") and _git_config("user.email"):
        return "kept"
    email = email or f"{getpass.getuser()}@{socket.gethostname()}.local"
    for key, value in (("user.name", operator), ("user.email", email)):
        r = core.git("config", "--local", key, value, check=False)
        if r.returncode != 0:
            raise StepFailed(f"git config --local {key} failed: {(r.stderr or r.stdout).strip()}")
    return f"set {operator} <{email}> for this repository"


def _verb(fn, *args, **kwargs) -> str:
    """Run a CLI verb in process: its prints are captured (stdout carries only the JSON lines), a nonzero return is a
    failure whose detail is what the verb said."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args, **kwargs)
    said = "\n".join(t for t in (err.getvalue().strip(), out.getvalue().strip()) if t)
    if code:
        raise StepFailed(said or f"exit {code}")
    return said.splitlines()[-1] if said else "done"


RUNNING = "running"


def _api_right() -> bool:
    """`api on --local` has nothing left to do: both keys, the local login, the service current."""
    from . import api, autonomy
    env = core.secrets_env()
    logins = {x.strip().lower() for x in env.get("SHAFT_API_LOGINS", "").split(",")}
    return bool(env.get("SHAFT_API_KEY") and env.get("SHAFT_INBOX_KEY") and api.LOCAL_LOGIN in logins
                and autonomy.service_current("api"))


def _web_right() -> bool:
    from . import api, autonomy, settings
    logins = {x.strip().lower() for x in core.secrets_env().get("SHAFT_API_LOGINS", "").split(",")}
    return (api.LOCAL_LOGIN in logins and settings.file_values().get("SHAFT_WEB_LOCAL") == "on"
            and autonomy.service_current("web"))


def _default_runner(step: str, operator: str, email: str | None) -> str:
    """A step whose result already holds is skipped ("running"): `on` would boot the service out and in again, and a
    restarted scheduler cuts the cycles it runs."""
    from . import __main__ as cli, api, autonomy, web
    if step == "identity":
        return _identity(operator, email)
    if step == "setup":
        return _verb(cli.cmd_setup)   # idempotent: existing worktrees are left, the guard is rewritten identically
    if step == "api":
        return RUNNING if _api_right() else _verb(api.command, "on", local=True)
    if step == "web":   # needs the api step's operator@local login
        return RUNNING if _web_right() else _verb(web.command, "on", local=True)
    if autonomy.service_current("scheduler"):
        return RUNNING
    return _verb(autonomy.autonomy, "on")   # no instances yet: the scheduler schedules nothing, harmlessly


def run(operator: str, email: str | None = None, runner=None) -> int:
    runner = runner or _default_runner
    for step in STEPS:
        try:
            detail, ok = runner(step, operator, email), True
        except subprocess.CalledProcessError as e:
            said = e.stderr or e.stdout
            detail, ok = (said.strip() if isinstance(said, str) and said.strip() else str(e)), False
        except Exception as e:   # the app shows the real message, never a traceback
            detail, ok = str(e) or type(e).__name__, False
        record = {"step": step, "ok": ok, "detail": detail}
        if not ok:
            record["error"] = detail   # the contract of every --json verb: "error" holds the real message
        print(json.dumps(record), flush=True)
        if not ok:
            return 1
    return 0
