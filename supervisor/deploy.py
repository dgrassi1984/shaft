"""`shaft deploy TAG --json`: docs/deploy.md as one verb, for the Mac app's "Update server" (no terminal).

One JSON line per step on stdout, `{"step", "ok", "detail"}` (`"error"` too when it fails), in this order:

- preflight: the primary checkout is on main (or, as the Mac app installs it, detached at a release tag) and its
  template worktree on template, neither with tracked changes (a refusal names the paths); `git fetch origin --tags`;
  TAG and template-TAG (the release's template commit) both exist.
- fetch: `git fetch origin`, so origin/main and origin/template are current too.
- main, template: `git merge --ff-only` to TAG and template-TAG (a detached app install moves its HEAD alike); a
  non-fast-forward is refused with git's message, and each HEAD must then equal its tag's commit (a checkout already
  past the tag merges "Already up to date" and is still refused).
- update: `shaft update` for every instance behind the template (gate-tested, a busy one queued); a refused update is
  a stop, never forced.
- scheduler: a drain-restart (`autonomy.restart(wait=False)`): it restarts once its runs end, cutting none.
- services: api, web and telegram are restarted (`launchctl kickstart -k`, the line's `"restarted"` names them) only
  when no instance has an open conversation; otherwise `{"ok": true, "detail": "waiting: <who>", "waiting": true}`
  ends the verb with exit 0, and the app retries later with `shaft deploy TAG --json --services-only` (services, then
  verify).
- verify: HEAD and template equal the tags' commits; each service restarted here has a new PID that recorded itself.

The first failure ends the verb (exit 1, its line holds the real message); all ok, or waiting, is exit 0; a TAG that is
not vX.Y.Z is exit 2. Only a loaded service whose code changed since it started is restarted, so a rerun after
success is all ok and restarts nothing. `--check --json` answers one line: the release the app may offer.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import core

STEPS = ("preflight", "fetch", "main", "template", "update", "scheduler", "services", "verify")
SERVICES_ONLY = ("services", "verify")
RESTARTED = ("api", "web", "telegram")   # docs/deploy.md: restarted only with no conversation open
TAG_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
VERIFY_S = 90          # how long verify waits for a restarted service's new process to record itself
POLL_S = 1.0


class StepFailed(Exception):
    """A step that did not succeed; the message is what the app shows."""


class Waiting(Exception):
    """The services step holds back: who still has a conversation open."""


def _git(repo, *args: str) -> subprocess.CompletedProcess:
    """git in the operator's own checkout (never an instance's: core.git_wt is for those). It never prompts."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _said(r: subprocess.CompletedProcess) -> str:
    return (r.stderr.strip() or r.stdout.strip() or f"git exited {r.returncode}")


def _template() -> Path:
    return core.PROJECT / "template"


def _commit(repo, ref: str) -> str | None:
    r = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return r.stdout.strip() or None


def _instances() -> list[str]:
    return core.instance_names()


# -- the steps ---------------------------------------------------------------------------------------------------------
def _at_release(repo) -> bool:
    """HEAD is detached exactly at a release tag: how the Mac app installs (`git clone --branch vX.Y.Z`)."""
    r = _git(repo, "tag", "--points-at", "HEAD")
    return any(TAG_RE.fullmatch(t) for t in r.stdout.split())


def _clean(repo, branch: str, what: str, release_ok: bool = False) -> None:
    """On `branch` (or, with `release_ok`, detached at a release tag: an app install), with no tracked changes."""
    r = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    on = r.stdout.strip() if r.returncode == 0 else ""
    if on != branch and not (release_ok and not on and _at_release(repo)):
        raise StepFailed(f"{what} ({repo}) is on {on or 'a detached HEAD'}, not {branch}: that is someone's work, "
                         f"so nothing was deployed")
    r = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if r.returncode:
        raise StepFailed(f"git status in {what} ({repo}) failed: {_said(r)}")
    paths = [line[3:] for line in r.stdout.splitlines() if line.strip()]
    if paths:
        raise StepFailed(f"{what} ({repo}) has tracked changes, so nothing was deployed — commit or discard them "
                         f"first: " + ", ".join(paths))


def preflight(tag: str) -> str:
    project, template = core.PROJECT, _template()
    _clean(project, "main", "the primary checkout", release_ok=True)
    if not (template / ".git").exists():
        raise StepFailed(f"the template worktree ({template}) is missing: `shaft setup` recreates it")
    _clean(template, "template", "the template worktree")
    r = _git(project, "fetch", "--quiet", "origin", "--tags")
    if r.returncode:
        raise StepFailed(f"git fetch origin --tags failed: {_said(r)}")
    main_at, template_at = _commit(project, f"refs/tags/{tag}"), _commit(project, f"refs/tags/template-{tag}")
    if not main_at:
        raise StepFailed(f"{tag} is not a release tag on origin")
    if not template_at:
        raise StepFailed(f"template-{tag} is missing on origin: the release has no recorded template commit, so it "
                         f"cannot be deployed yet")
    return f"main and template are clean; {tag} is {main_at[:12]}, template-{tag} is {template_at[:12]}"


def fetch(tag: str) -> str:
    r = _git(core.PROJECT, "fetch", "--quiet", "origin")
    if r.returncode:
        raise StepFailed(f"git fetch origin failed: {_said(r)}")
    return "fetched origin"


def _fast_forward(repo, ref: str, what: str) -> str:
    target, before = _commit(repo, f"refs/tags/{ref}"), _commit(repo, "HEAD")
    if not target:
        raise StepFailed(f"{ref} is not a tag here")
    if before == target:
        return f"{what} already at {ref} ({target[:12]})"
    r = _git(repo, "merge", "--ff-only", f"refs/tags/{ref}")
    if r.returncode:
        raise StepFailed(f"{what} cannot fast-forward to {ref}, so it was left as it was: {_said(r)}")
    after = _commit(repo, "HEAD")
    if after != target:
        raise StepFailed(f"{what} is at {(after or '?')[:12]} after the merge, not {ref} ({target[:12]}): it is "
                         f"already past the release, so it was left as it was")
    return f"{what} fast-forwarded {before[:12] if before else '?'}..{target[:12]} ({ref})"


def step_main(tag: str) -> str:
    return _fast_forward(core.PROJECT, tag, "main")


def step_template(tag: str) -> str:
    return _fast_forward(_template(), f"template-{tag}", "template")


def _quiet(fn, *args, **kwargs) -> tuple[int, str]:
    """A CLI verb in process with its prints captured: stdout carries only the JSON lines."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args, **kwargs)
    return code or 0, "\n".join(t for t in (out.getvalue().strip(), err.getvalue().strip()) if t)


def _behind(name: str) -> int | None:
    from . import version
    return version.instance(name).get("template_behind")


def _update_all(names: list[str]) -> tuple[int, str]:
    from . import __main__ as cli
    return _quiet(cli.cmd_update, names)


def update(tag: str) -> str:
    names = [n for n in _instances() if _behind(n) != 0]   # None (unknown) is updated: cmd_update decides
    if not names:
        return "every instance is already on the template" if _instances() else "no instances yet"
    code, said = _update_all(names)
    if code:
        raise StepFailed(said or f"shaft update exited {code}")
    return "; ".join(l for l in said.splitlines() if l.strip()) or f"updated {', '.join(names)}"


def _record(service: str, stale: bool = True) -> dict | None:
    from . import version
    return version.service(service, stale=stale)


def _drain_pending(rec: dict | None) -> bool:
    """A drain under way, or a request made of the scheduler running now (a leftover one for a process gone is not)."""
    from . import autonomy
    req = autonomy.restart_request()
    return bool(autonomy.draining_now() or (req and rec and req.get("pid") == rec.get("pid")))


def scheduler(tag: str) -> str:
    from . import autonomy, version
    if not autonomy.running("scheduler"):
        return "not running: nothing to restart"
    rec = _record("scheduler")
    if rec and not rec.get("stale"):
        return f"already running {version.service_version(rec)}"
    if _drain_pending(rec):
        return "a drain-restart is already asked: it restarts once its runs end (shaft autonomy status)"
    code, said = _quiet(autonomy.restart, wait=False)
    if code:
        raise StepFailed(said or f"autonomy restart exited {code}")
    return said.splitlines()[-1] if said else "drain-restart asked"


def _open_conversations() -> dict[str, dict]:
    from . import runner
    found = {n: runner.open_conversations(n) for n in _instances()}
    return {n: c for n, c in found.items() if c}


def _kickstart(service: str) -> None:
    from . import autonomy
    r = autonomy._launchctl("kickstart", "-k", f"gui/{os.getuid()}/{autonomy.SERVICES[service]['label']}")
    if r.returncode:
        raise StepFailed(f"launchctl kickstart -k {autonomy.SERVICES[service]['label']} failed: "
                         f"{(r.stderr or r.stdout).strip() or r.returncode}")


def services(tag: str, kicked: dict[str, int | None]) -> str | tuple[str, dict]:
    """Restart the loaded services whose code changed, when no conversation is open; `kicked` gets each one's old PID.
    Its line then names them (`restarted`): the app knows its connection is about to drop."""
    from . import autonomy, version
    at, target = _commit(core.PROJECT, "HEAD"), _commit(core.PROJECT, f"refs/tags/{tag}")
    if not target or at != target:
        raise StepFailed(f"the primary checkout is at {(at or '?')[:12]}, not {tag}: run `shaft deploy {tag}` first")
    due, current = [], []
    for name in RESTARTED:
        if not autonomy.running(name):
            continue                                   # docs/deploy.md: restart only loaded services
        rec = _record(name)
        if rec and not rec.get("stale"):
            current.append(f"{name} {version.service_version(rec)}")
        else:
            due.append(name)
    if not due:
        return "already current: " + ", ".join(current) if current else "none running"
    if (open_now := _open_conversations()):
        who = ", ".join(f"{n} ({len(c)} open conversation{'s' * (len(c) != 1)})" for n, c in open_now.items())
        raise Waiting(f"waiting: {who}")
    for name in due:
        kicked[name] = autonomy.launchd_pid(name)
        _kickstart(name)
    return ("restarted " + ", ".join(due) + (f"; already current: {', '.join(current)}" if current else ""),
            {"restarted": due})


def _new_process(name: str, old: int | None) -> dict:
    """Wait for the restarted service's new process to record itself."""
    from . import autonomy
    deadline = time.monotonic() + VERIFY_S
    while True:
        pid = autonomy.launchd_pid(name)
        rec = _record(name, stale=False)
        if pid and pid != old and rec and rec.get("pid") == pid:
            return rec
        if time.monotonic() >= deadline:
            raise StepFailed(f"{name}: no new process recorded itself within {VERIFY_S} s (launchd pid "
                             f"{pid or 'none'}, was {old or 'none'}): `shaft status` and its log say why")
        time.sleep(POLL_S)


def verify(tag: str, kicked: dict[str, int | None]) -> str:
    from . import version
    for repo, ref, what in ((core.PROJECT, tag, "main"), (_template(), f"template-{tag}", "template")):
        at, target = _commit(repo, "HEAD"), _commit(repo, f"refs/tags/{ref}")
        if not target or at != target:
            raise StepFailed(f"{what} is at {(at or '?')[:12]}, not {ref} ({(target or '?')[:12]})")
    parts = [f"main = {tag}, template = template-{tag}"]
    for name, old in kicked.items():
        rec = _new_process(name, old)
        parts.append(f"{name} pid {rec['pid']} on {version.service_version(rec)}")
    if _drain_pending(_record("scheduler", stale=False)):
        parts.append("the scheduler restarts once its runs end")
    return "; ".join(parts)


# -- the verb ----------------------------------------------------------------------------------------------------------
def _emit(record: dict) -> None:
    print(json.dumps(record), flush=True)


class AlreadyRunning(Exception):
    """Another deploy holds the lock."""


@contextlib.contextmanager
def _one_at_a_time():
    core.SUP_DIR.mkdir(parents=True, exist_ok=True)
    with open(core.SUP_DIR / "deploy.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning("another `shaft deploy` is running") from None
        yield


def run(tag: str, services_only: bool = False) -> int:
    if not TAG_RE.fullmatch(tag):
        _emit({"ok": False, "error": f"{tag!r} is not a release tag (vX.Y.Z)"})
        return 2
    order = SERVICES_ONLY if services_only else STEPS
    try:
        with _one_at_a_time():
            return _run(tag, order)
    except AlreadyRunning as e:
        _emit({"step": order[0], "ok": False, "detail": str(e), "error": str(e)})
        return 1


def _run(tag: str, order: tuple[str, ...]) -> int:
    kicked: dict[str, int | None] = {}
    steps = {"preflight": preflight, "fetch": fetch, "main": step_main, "template": step_template, "update": update,
             "scheduler": scheduler, "services": lambda t: services(t, kicked), "verify": lambda t: verify(t, kicked)}
    for step in order:
        record: dict = {"step": step}
        try:
            said = steps[step](tag)
            detail, extra = said if isinstance(said, tuple) else (said, {})
            record.update(ok=True, detail=detail, **extra)
        except Waiting as e:
            record.update(ok=True, detail=str(e), waiting=True)
        except Exception as e:   # the app shows the real message, never a traceback
            detail = str(e) or type(e).__name__
            record.update(ok=False, detail=detail, error=detail)
        core.log((f"deploy {tag}: {step} " + ("ok" if record["ok"] else "FAILED") + f" — {record['detail']}")[:400])
        _emit(record)
        if not record["ok"]:
            return 1
        if record.get("waiting"):
            return 0
    return 0


# -- --check -----------------------------------------------------------------------------------------------------------
def _semver(tag: str) -> tuple[int, int, int] | None:
    m = TAG_RE.fullmatch(tag)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def check() -> int:
    """One line: the checked-out release (`current`, git describe of HEAD), the newest release on origin newer than it
    that has its template tag (`latest`, null when up to date), and `template_tag`: false when origin has a newer
    vX.Y.Z still without template-vX.Y.Z (not deployable yet), true otherwise."""
    project = core.PROJECT
    d = _git(project, "describe", "--tags", "--match", "v[0-9]*", "--always", "HEAD")
    if d.returncode:
        _emit({"ok": False, "error": f"git describe failed: {_said(d)}"})
        return 1
    current = d.stdout.strip()
    r = _git(project, "ls-remote", "--tags", "--refs", "origin", "v*", "template-v*")
    if r.returncode:
        _emit({"ok": False, "error": f"git ls-remote origin failed: {_said(r)}"})
        return 1
    releases, templates = {}, set()
    for line in r.stdout.splitlines():
        name = line.rpartition("\t")[2].removeprefix("refs/tags/")
        if name.startswith("template-") and (v := _semver(name.removeprefix("template-"))):
            templates.add(v)
        elif (v := _semver(name)):
            releases[v] = name
    have = _semver(current.split("-", 1)[0])
    newer = [v for v in releases if have is None or v > have]
    deployable = [v for v in newer if v in templates]
    latest = releases[max(deployable)] if deployable else None
    _emit({"ok": True, "current": current, "latest": latest,
           "template_tag": not newer or max(newer) in templates})
    return 0
