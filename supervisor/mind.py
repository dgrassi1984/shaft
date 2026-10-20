"""The instance's mind store: a private git repository per instance, never the project's.

An instance's mind — self/, memory/, notes/, inbox/ (core.MIND_PATHS) — is what it remembers and writes for
itself, and may hold what it read (mail, docs/plans/PLAN-mail.md). It is not committed to the project repository, which can
be pushed to GitHub. The supervisor still keeps the mind as it was at every cycle (trial replays, drift, the
instance's own `history` tool) in `.supervisor/minds/<name>.git`: a bare repository whose work tree is the
instance's folder and whose index holds only the mind paths. It has no remote, ever (every commit checks), and a
pre-push hook that always fails. skills/ stays in the project's git (it is a gene: docs/plans/PLAN-mind-out-of-git.md D2).

Every git call on a mind store goes through `git` here: hooks, fsmonitor, the user's and the system's git
configuration and the instance's own .gitattributes are all off, and paths are added with `-f` so the instance's
own .gitignore files cannot hide part of its mind. Nothing in the instance's folder is read as configuration.

The past is backfilled from the instance's branch (backfill): every branch commit that changed the mind gets a
twin here with the same mind tree, author, committer, dates and message, so a replay of any past cycle reads its
mind from one place. Plan, decisions and the cutover: docs/plans/PLAN-mind-out-of-git.md.
"""
from __future__ import annotations

import io
import os
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

from . import core
from .core import MIND_PATHS

BRANCH = "refs/heads/mind"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
BACKFILL_MARK = "shaft-backfill"          # inside the store: "<last branch commit imported> <its twin here>"
PRE_PUSH = ("#!/bin/sh\n# SHAFT: an instance's mind store is never pushed anywhere (docs/plans/PLAN-mind-out-of-git.md).\n"
            "echo \"refused: this is an instance's private mind store; it is never pushed\" >&2\nexit 1\n")
PROJECT_PRE_PUSH = """#!/bin/sh
# SHAFT: instance/* branches are never pushed — their history still holds the minds of the past
# (docs/plans/PLAN-mind-out-of-git.md D4). Installed by `shaft mind guard`. Deleting one on a remote sends nothing
# (its local sha is all zeros) and is the cleanup D4 asks for, so it is let through.
while read -r local_ref local_sha remote_ref remote_sha; do
  case "$local_sha" in *[!0]*) ;; *) continue ;; esac
  case "$local_ref $remote_ref" in
    *refs/heads/instance/*)
      echo "refused: $local_ref -> $remote_ref (instance branches hold the instances' minds; never pushed)" >&2
      exit 1 ;;
  esac
done
exit 0
"""
PROJECT_HOOK_MARK = "Installed by `shaft mind guard`"


class MindError(RuntimeError):
    pass


# -- where, and the one way git is run on it ------------------------------------------------------------------
def minds_dir(project: Path | None = None) -> Path:
    """The supervisor's folder of `project`: SUP_DIR for the real project (read at call time — tests move it),
    `<project>/.supervisor` for another one (a test's repository, an M0 copy)."""
    if project is None or Path(project).resolve() == core.PROJECT.resolve():
        return core.SUP_DIR / "minds"
    return Path(project) / ".supervisor" / "minds"


def store(name: str, project: Path | None = None) -> Path:
    return minds_dir(project) / f"{name}.git"


def exists(name: str, project: Path | None = None) -> bool:
    return (store(name, project) / "HEAD").is_file()


def git(name: str, *args: str, project: Path | None = None, root: Path | None = None, input=None,
        binary: bool = False, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    """git on the mind store of `name` (its work tree `root`, when given). Nothing configures it but this call:
    no hooks, no fsmonitor, no user or system config, no .gitattributes from the work tree, no GIT_* variable
    of the caller's environment. A failure raises MindError with git's own message."""
    path = store(name, project)
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    clean.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", **(env or {}))
    cmd = ["git", f"--git-dir={path}", *([f"--work-tree={root}"] if root else []),
           "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", f"attr.tree={EMPTY_TREE}",
           "-c", "core.autocrlf=false", "-c", "commit.gpgSign=false", "-c", "core.quotePath=false", *args]
    r = subprocess.run(cmd, cwd=root or path, capture_output=True, text=not binary, input=input, env=clean)
    if check and r.returncode:
        err = r.stderr if not binary else r.stderr.decode(errors="replace")
        raise MindError(f"git {' '.join(args[:2])} on {path}: {err.strip()[:500] or f'exit {r.returncode}'}")
    return r


def _project_git(project: Path, *args: str, input=None, binary: bool = False) -> subprocess.CompletedProcess:
    """Read-only git on the project repository (the backfill's source)."""
    r = subprocess.run(["git", "-C", str(project), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                        *args], capture_output=True, text=not binary, input=input)
    if r.returncode:
        err = r.stderr if not binary else r.stderr.decode(errors="replace")
        raise MindError(f"git {' '.join(args[:2])} on {project}: {err.strip()[:500]}")
    return r


# -- the store itself -------------------------------------------------------------------------------------------
def init(name: str, project: Path | None = None) -> Path:
    """Create the mind store (idempotent): bare, no template files, branch `mind`, the pre-push hook."""
    path = store(name, project)
    if not exists(name, project):
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        r = subprocess.run(["git", "init", "-q", "--bare", "--template=", "-b", "mind", str(path)],
                           capture_output=True, text=True,
                           env={**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
        if r.returncode:
            raise MindError(f"git init {path}: {r.stderr.strip()}")
        git(name, "config", "core.logAllRefUpdates", "true", project=project)   # a reflog, even though bare
        core.log(f"{name}: mind store created at {path}")
    _guard(name, project)
    return path


def _guard(name: str, project: Path | None) -> None:
    """No remote, ever, and the pre-push hook in place; a remote found is refused loudly, never used or removed."""
    path = store(name, project)
    remotes = git(name, "remote", project=project).stdout.split()
    urls = git(name, "config", "--get-regexp", r"^(remote|url)\.", project=project, check=False).stdout.strip()
    if remotes or urls:
        msg = (f"{name}: its mind store {path} has a remote ({', '.join(remotes) or urls}) — a mind store never "
               f"has one. Nothing committed. Remove it by hand after checking who added it.")
        core.log(f"MIND STORE REFUSED: {msg}")
        raise MindError(msg)
    hook = path / "hooks" / "pre-push"
    if not hook.is_file() or hook.read_text() != PRE_PUSH:
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(PRE_PUSH)
        os.chmod(hook, 0o755)


def head(name: str, project: Path | None = None) -> str | None:
    if not exists(name, project):
        return None
    r = git(name, "rev-parse", "--verify", "-q", BRANCH, project=project, check=False)
    return r.stdout.strip() or None


def require(name: str, project: Path | None) -> None:
    if not exists(name, project):
        raise MindError(f"{name} has no mind store at {store(name, project)} — an instance created before the "
                        f"mind left git is moved over with `shaft mind cutover {name}`")


def commit(name: str, message: str, root: Path, project: Path | None = None) -> bool:
    """Commit the mind as it is on disk in `root`. True if a commit was made, False if nothing changed.
    Raises MindError (git's message) when it cannot — never commits to a store that has a remote."""
    require(name, project)
    _guard(name, project)
    on_disk = [p for p in MIND_PATHS if (root / p).exists() or (root / p).is_symlink()]
    known = git(name, "ls-files", "-z", "--", *MIND_PATHS, project=project, root=root).stdout.split("\0")
    paths = sorted(set(on_disk) | {Path(p).parts[0] for p in known if p})
    if not paths:
        return False
    git(name, "add", "-f", "-A", "--", *paths, project=project, root=root)
    if git(name, "diff", "--cached", "--quiet", project=project, root=root, check=False).returncode == 0:
        return False
    git(name, "-c", f"user.name=SHAFT/{name}", "-c", f"user.email={name}@shaft.localhost",
        "commit", "-q", "--no-verify", "-F", "-", project=project, root=root, input=message)
    return True


def at(name: str, when: datetime | str, project: Path | None = None) -> str | None:
    """The mind commit current at `when` (local time): the last one committed before it, or None."""
    if not head(name, project):
        require(name, project)
        return None
    stamp = when if isinstance(when, str) else f"{when:%Y-%m-%dT%H:%M:%S}"
    return git(name, "log", "-1", "--format=%H", f"--before={stamp}", BRANCH, project=project).stdout.strip() or None


def files(name: str, rev: str | None, project: Path | None = None) -> dict[str, tuple[str, bytes]]:
    """{path: (kind, content)} of the mind at `rev` (None: an empty mind). kind: 'x' executable, 'l' symlink
    (content = its target), 'f' a plain file."""
    if rev is None:
        return {}
    return _tar_files(git(name, "archive", "--format=tar", rev, project=project, binary=True).stdout)


def archive(name: str, rev: str | None, dest: Path, project: Path | None = None) -> Path:
    """Lay the mind at `rev` into `dest` (None: nothing to lay)."""
    dest.mkdir(parents=True, exist_ok=True)
    if rev is not None:
        data = git(name, "archive", "--format=tar", rev, project=project, binary=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(dest, filter="data")
    return dest


def show(name: str, rev: str | None, path: str, project: Path | None = None) -> str | None:
    """A mind file's text at `rev` (None or a file not there: None)."""
    require(name, project)
    if rev is None:
        return None
    r = git(name, "show", f"{rev}:{path}", project=project, check=False)
    return r.stdout if r.returncode == 0 else None


def log(name: str, *args: str, project: Path | None = None) -> str:
    """`git log <args>` on the mind store (its branch unless a revision is given)."""
    require(name, project)
    if not head(name, project):
        return ""
    return git(name, "log", *args, project=project).stdout


def _tar_files(data: bytes) -> dict[str, tuple[str, bytes]]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            if m.issym():
                out[m.name] = ("l", m.linkname.encode())
            elif m.isfile():
                out[m.name] = ("x" if m.mode & 0o111 else "f", tar.extractfile(m).read())
    return out


# -- the past: backfill from the project's branch ----------------------------------------------------------------
def _mind_entries(project: Path, commit: str) -> str:
    """The commit's root entries for the mind paths, as `ls-tree -z` prints them (mktree's input)."""
    return _project_git(project, "ls-tree", "-z", commit, "--", *MIND_PATHS).stdout


def backfill(name: str, project: Path | None = None, branch: str | None = None) -> int:
    """Give every commit on instance/<name> (first-parent line) that changed the mind a twin in the mind store:
    the same mind tree, the same author and committer lines (names, dates) and the same message. The project's
    branch is only read. Incremental: a second run continues from the last commit it imported, and refuses once
    the store has commits of its own (the instance is live on it). Returns the number of commits made."""
    project = Path(project or core.PROJECT)
    branch = branch or f"refs/heads/instance/{name}"
    init(name, project)
    mark = store(name, project) / BACKFILL_MARK
    tip = head(name, project)
    since = None
    if tip:
        if not mark.is_file():
            raise MindError(f"{name}: its mind store already has commits of its own; the past is not imported "
                            "into a live store")
        since, twin = mark.read_text().split()
        if twin != tip:
            raise MindError(f"{name}: its mind store moved on since the backfill ({tip[:12]} after {twin[:12]}); "
                            "the past is not imported into a live store")
    if not _mind_entries(project, branch):
        raise MindError(f"{name}: {branch} no longer holds the mind (already cut over?); nothing to import")
    commits = _project_git(project, "rev-list", "--first-parent", "--reverse",
                           f"{since}..{branch}" if since else branch).stdout.split()
    last_entries = git(name, "ls-tree", "-z", tip, project=project).stdout if tip else None
    old = {e.split("\t", 1)[0].split()[2] for e in (last_entries or "").split("\0") if e}
    states, seen = [], set()
    for c in commits:
        entries = _mind_entries(project, c)
        if entries == last_entries or (last_entries is None and not entries):
            continue
        states.append((c, entries))
        last_entries = entries
        seen.update(e.split("\t", 1)[0].split()[2] for e in entries.split("\0") if e)
    if not states:
        return 0
    # The objects of those trees, packed by the project's git (deltas included) and indexed into the store.
    wanted = "\n".join([*sorted(seen - old), *(f"^{o}" for o in sorted(old))]) + "\n"
    objects = _project_git(project, "rev-list", "--objects", "--stdin", input=wanted).stdout
    pack = _project_git(project, "pack-objects", "--stdout", "-q", input=objects.encode(), binary=True).stdout
    git(name, "index-pack", "--stdin", "--fix-thin", project=project, input=pack, binary=True)
    parent = tip
    for c, entries in states:
        tree = git(name, "mktree", "-z", project=project, input=entries).stdout.strip()
        raw = _project_git(project, "cat-file", "commit", c, binary=True).stdout
        header, _, message = raw.partition(b"\n\n")
        lines = [l for l in header.split(b"\n") if l.startswith((b"author ", b"committer ", b"encoding "))]
        obj = b"tree " + tree.encode() + b"\n" + (b"parent " + parent.encode() + b"\n" if parent else b"") \
            + b"\n".join(lines) + b"\n\n" + message
        parent = git(name, "hash-object", "-t", "commit", "-w", "--stdin", project=project, input=obj,
                     binary=True).stdout.decode().strip()
    git(name, "update-ref", "-m", f"backfill from {branch}", BRANCH, parent, *([tip] if tip else []), project=project)
    mark.write_text(f"{states[-1][0]} {parent}\n")
    git(name, "gc", "-q", project=project)
    core.log(f"{name}: mind store backfilled from {branch}: {len(states)} mind commits (up to {states[-1][0][:12]})")
    return len(states)


# -- the equivalence check: the store against the branch, at past live cycles ---------------------------------
def _branch_mind(project: Path, commit: str) -> dict[str, tuple[str, bytes]]:
    present = [p for p in MIND_PATHS if _project_git(project, "ls-tree", commit, "--", p).stdout.strip()]
    if not present:
        return {}
    return _tar_files(_project_git(project, "archive", "--format=tar", commit, "--", *present, binary=True).stdout)


def check(name: str, n: int = 20, project: Path | None = None, root: Path | None = None,
          starts: list[str] | None = None) -> list[dict]:
    """For `n` past live cycles picked across the whole history (trial.state_of), the mind from the store at
    the cycle's start against the mind subtrees of `git archive start_commit`: byte for byte, every path."""
    from . import trial
    project = Path(project or core.PROJECT)
    root = root or core.INSTANCES / name
    starts = trial._starts(name) if starts is None else starts
    states = [s for ep in trial.live_episodes(root) if (s := trial.state_of(project, name, ep, starts))]
    if not states:
        raise MindError(f"{name}: no past live cycle to check against")
    picks = sorted({round(i * (len(states) - 1) / max(1, n - 1)) for i in range(n)})
    out = []
    for s in (states[i] for i in picks):
        mine = at(name, s["t0"], project)
        a, b = files(name, mine, project), _branch_mind(project, s["start_commit"])
        differ = sorted(p for p in a.keys() | b.keys() if a.get(p) != b.get(p))
        out.append({"t0": s["t0"], "start_commit": s["start_commit"], "mind_commit": mine, "files": len(b),
                    "identical": not differ, "differ": differ[:10]})
    return out


def check_every_commit(name: str, project: Path | None = None, branch: str | None = None) -> list[dict]:
    """At the moment of every commit on the branch's own line: the store's mind then (at) against the branch's
    mind then (its last first-parent commit by that moment), by tree ids — equal ids are byte-identical trees.
    Returns the moments that differ (empty: the backfill is exact)."""
    project = Path(project or core.PROJECT)
    branch = branch or f"refs/heads/instance/{name}"
    rows = _project_git(project, "log", "--first-parent", "--format=%H %ct", branch).stdout.split("\n")
    out = []
    for line in filter(None, rows):
        commit, ct = line.split()
        when = datetime.fromtimestamp(int(ct))
        current = _project_git(project, "log", "-1", "--first-parent", "--format=%H",
                               f"--before={when:%Y-%m-%dT%H:%M:%S}", branch).stdout.strip()
        mine = at(name, when, project)
        a = git(name, "ls-tree", "-z", mine, project=project).stdout if mine else ""
        b = _mind_entries(project, current)
        if a != b:
            out.append({"commit": commit, "at": f"{when:%Y-%m-%dT%H:%M:%S}", "branch_commit": current,
                        "mind_commit": mine})
    return out


# -- the cutover (M3), per instance, while it is idle ------------------------------------------------------------
def cutover(name: str, n: int = 20) -> int:
    """Move a live instance's mind out of the project's git: backfill, the equivalence checks (every pick and
    every commit identical, or nothing changes), the untrack commit on instance/<name> (files stay on disk), and
    the mind as it is now. Holds the instance's run lock throughout; refuses while a conversation is open or
    while the API service still runs code from before the mind store."""
    from . import runner, version
    root = core.instance_root(name)
    api = version.service("api")
    if api and api.get("stale"):   # it runs conversations in its own process: old code would commit the mind
        print(f"the API service runs code from before the mind store ({api['commit']}): it would put {name}'s mind "
              "back on its branch. Restart it first: shaft api off && shaft api on")
        return 3
    try:
        with runner.run_lock(name, wait=False, kind="mind cutover"):
            if (convs := runner.open_conversations(name)):
                print(f"{name}: {len(convs)} conversation(s) still open; cut over when it is idle.")
                return 3
            made = backfill(name)
            print(f"{name}: {made} mind commits imported into {store(name)}")
            results = check(name, n)
            bad = [r for r in results if not r["identical"]]
            moments = check_every_commit(name)
            print(f"{name}: every commit of its branch: {len(moments)} moments where the store differs")
            if moments:
                print(f"{name}: not cut over (a backfill bug: fix it): first at {moments[0]}")
                return 1
            for r in results:
                print(f"  {r['t0']}  {r['start_commit'][:10]} ↔ {(r['mind_commit'] or '(none)')[:10]}  "
                      f"{r['files']:4d} files  {'identical' if r['identical'] else 'DIFFER: ' + ', '.join(r['differ'])}")
            if bad:
                print(f"{name}: {len(bad)}/{len(results)} cycles differ — not cut over (a backfill bug: fix it)")
                return 1
            core.git_wt(root, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *MIND_PATHS)
            core.git_wt(root, "-c", "user.name=SHAFT supervisor", "-c", "user.email=supervisor@shaft.localhost",
                        "commit", "-q", "-m", f"{name}: the mind moves to the supervisor's mind store\n\n"
                        f"self/, memory/, notes/ and inbox/ stay on disk and are versioned in "
                        f".supervisor/minds/{name}.git, never in this repository (docs/plans/PLAN-mind-out-of-git.md).")
            commit(name, f"{name}: cutover {datetime.now():%Y-%m-%d %H:%M}\n\nThe mind as it was on disk when it "
                         "left the project's git.", root)
            core.log(f"{name}: mind cut over to {store(name)} ({len(results)}/{len(results)} cycles identical)")
            print(f"{name}: cut over ({len(results)}/{len(results)} identical).")
            return 0
    except runner.Busy:
        print(f"{name} is busy ({runner.busy_with(name)}); cut it over when it is idle.")
        return 3
    except (MindError, subprocess.CalledProcessError) as e:
        print(f"{name}: cutover stopped: {getattr(e, 'stderr', None) or e}")
        return 1


def guard_project(project: Path | None = None) -> Path:
    """The project's pre-push hook refusing every instance/* ref (D4). Never replaces a hook it did not write."""
    project = Path(project or core.PROJECT)
    r = subprocess.run(["git", "-C", str(project), "rev-parse", "--path-format=absolute", "--git-path", "hooks"],
                       capture_output=True, text=True)    # where git itself looks (not _project_git: hooks off there)
    if r.returncode:
        raise MindError(f"git rev-parse on {project}: {r.stderr.strip()}")
    hooks = Path(r.stdout.strip())
    hook = hooks / "pre-push"
    if hook.exists() and PROJECT_HOOK_MARK not in hook.read_text(errors="replace"):
        raise MindError(f"{hook} exists and is not SHAFT's; merge the instance/* refusal into it by hand")
    hooks.mkdir(parents=True, exist_ok=True)
    hook.write_text(PROJECT_PRE_PUSH)
    os.chmod(hook, 0o755)
    return hook


def describe(name: str, project: Path | None = None) -> str:
    if not exists(name, project):
        return f"no mind store (shaft mind cutover {name})"
    tip = head(name, project)
    count = git(name, "rev-list", "--count", BRANCH, project=project).stdout.strip() if tip else "0"
    last = git(name, "log", "-1", "--format=%cr", BRANCH, project=project).stdout.strip() if tip else "never"
    size = sum(p.stat().st_size for p in store(name, project).rglob("*") if p.is_file())
    return f"{count} commits, last {last}, {size / 1e6:.1f} MB at {store(name, project)}"
