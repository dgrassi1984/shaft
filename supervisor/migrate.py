"""Guided migration of a SHAFT setup to a new Mac (docs/migration.md).

`bin/shaft migrate source --to <new-mac>` walks the operator through stopping the old Mac and pushing the
copy over SSH; `bin/shaft migrate target` confirms the old Mac stays stopped, rebuilds and restarts on the new
Mac, from the copied folder. Instance environments are removed and rebuilt at the first talk. Each
step says what it will do and why, shows the exact command, and waits: Enter runs, `q` stops. Finished steps
are saved in `.supervisor/migration/`, so a rerun resumes at the first unfinished one. `--check` runs only the
read-only checks and writes nothing.

Every SHAFT action runs as `PROJECT/bin/shaft <verb>` through an injected runner, so each verb's own refusals
still apply. Reads may use core/mind/runner directly. There is no `--yes`: a terminal is required.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import core

# Launchd labels are global: a worktree run must not stop the live services, so the live-checkout check (step 1)
# refuses a checkout whose plists point elsewhere. The order matches docs/migration.md §2 step 4.
SERVICE_STOP_ORDER = ("scheduler", "telegram", "web", "api", "embed", "model")
SERVICE_START_ORDER = ("model", "embed", "api", "web", "telegram")
VERB = {"scheduler": "autonomy"}              # the scheduler's launchd service is `autonomy`
FORCE_OK = {"autonomy", "api", "embed", "model"}   # the four verbs whose `off` accepts --force
NETWORK_SERVICES = ("api", "web")
UI_PYTHON_CHECK = "import sys; sys.exit(sys.version_info < (3, 12))"
SUP_RE = re.compile(r"(?:^|\s)-m\s+(?:supervisor(?:\.[\w.]+)?|shaft(?:\.plugin_runner)?)(?:\s|$)")
TAILSCALE_PATHS = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                   "~/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                   "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale")


class Stop(Exception):
    """End the run: print the reason, how to resume, and (source) how to bring the old Mac back."""


@dataclass
class Step:
    id: str
    title: str
    why: str
    commands: object              # list[str] or a callable returning list[str]: the exact commands shown to the operator
    run: object                   # callable that does it; raises Stop on failure


@dataclass
class _Mig:
    runner: "Runner"
    stdin: object
    tty: bool
    check: bool
    host: str | None = None
    state_dir: Path | None = None
    state_path: Path | None = None
    state: dict = field(default_factory=dict)
    manifest: dict | None = None
    real: bool = False          # stdin is the real terminal (not a test's or a pipe)

    def save(self) -> None:
        if self.state_path is not None:
            _save_state(self.state_path, self.state)


class Runner:
    """Runs every external command for a migration; tests substitute a fake with the same surface."""

    def __init__(self, host: str | None = None):
        self.host = host

    def run(self, argv, *, cwd=None, env=None, input=None, live=False) -> subprocess.CompletedProcess:
        try:
            if live:
                return subprocess.run(argv, cwd=cwd, env=env, input=input, text=True)
            return subprocess.run(argv, cwd=cwd, env=env, input=input, text=True, capture_output=True)
        except FileNotFoundError as e:
            return subprocess.CompletedProcess(argv, 127, "", f"{e.filename or argv[0]}: command not found")

    def connect(self, host: str) -> None:
        r = subprocess.run(["ssh", "-fN", "-o", "ControlMaster=auto", "-o", f"ControlPath={_control_path()}",
                            "-o", "ControlPersist=1h", "-o", "ConnectTimeout=15", host])
        if r.returncode:
            raise Stop(f"cannot reach {host} over SSH — turn on Remote Login on the new Mac "
                       "(System Settings → General → Sharing → Remote Login)")

    def shaft(self, *argv, live=False) -> subprocess.CompletedProcess:
        return self.run([str(core.PROJECT / "bin" / "shaft"), *argv], live=live)

    def interactive(self, argv, *, cwd=None) -> int:
        return subprocess.run(argv, cwd=cwd or str(core.PROJECT)).returncode

    def talk(self, name: str) -> int:
        return self.interactive([str(core.PROJECT / "bin" / "shaft"), "talk", name])

    def ssh(self, command: str, *, host=None) -> subprocess.CompletedProcess:
        return self.run(["ssh", *_control_opts(), host or self.host, command])

    def rsync(self, flags, source: str, dest: str) -> subprocess.CompletedProcess:
        return self.run(["rsync", *flags, "-e", _ssh_command(), source, dest], live=True)


def _control_opts() -> list[str]:
    return ["-o", "ControlMaster=no", "-o", f"ControlPath={_control_path()}"]


def _control_path() -> str:
    return f"/tmp/shaft-mig-{os.getpid()}"


def _ssh_command() -> str:
    return " ".join(shlex.quote(x) for x in ["ssh", *_control_opts()])


# -- stdin helpers ---------------------------------------------------------------
class _Lines:
    def __init__(self, lines):
        self._it = iter(lines)

    def readline(self):
        try:
            return next(self._it)
        except StopIteration:
            return None


class _File:
    def __init__(self, f):
        self._f = f

    def readline(self):
        line = self._f.readline()
        return None if line == "" else line


def _wrap_stdin(stdin):
    if stdin is None:
        return _File(sys.stdin)
    if isinstance(stdin, (list, tuple)):
        return _Lines(stdin)
    return stdin


def _readline(mig: _Mig):
    return mig.stdin.readline()


def _out(r) -> str:
    return "\n".join(x for x in [(r.stdout or "").rstrip(), (r.stderr or "").rstrip()] if x)


# -- state (`.supervisor/migration/`) ---------------------------------------------
def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _load_state(path: Path) -> dict:
    return _load_json(path) or {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".migration-state-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _load_manifest(mig: _Mig) -> dict | None:
    return _load_json(mig.state_dir / "manifest.json")


# -- SHAFT process detection ------------------------------------------------------
def _shaft_processes(lines, exclude: set = frozenset()) -> list[tuple[int, str]]:
    """Find SHAFT Python or sandbox-exec commands, plus shaft-codex. Shell mentions do not count."""
    found = []
    for line in lines:
        line = line.strip()
        pid, _, command = line.partition(" ")
        if not pid.isdigit() or int(pid) in exclude:
            continue
        command = command.strip()
        first = command.split(maxsplit=1)[0] if command else ""
        executable = os.path.basename(first).lower()
        is_python = executable.startswith("python") or executable == "sandbox-exec"
        is_supervisor = bool(SUP_RE.search(command))
        if (is_python and is_supervisor) or "shaft-codex" in command:
            found.append((int(pid), command))
    return found


def _local_shaft_processes(runner: Runner) -> list[tuple[int, str]]:
    r = runner.run(["ps", "-axo", "pid=,command="])
    return _shaft_processes((r.stdout or "").splitlines(), exclude={os.getpid(), os.getppid()})


def _remote_shaft_processes(runner: Runner, host: str) -> list[tuple[int, str]]:
    r = runner.ssh("ps -axo pid=,command=", host=host)
    if r.returncode:
        raise Stop(f"cannot list {host}'s processes: {_out(r)}")
    return _shaft_processes((r.stdout or "").splitlines())


def _retry_or_yes(mig: _Mig, procs) -> str:
    print("SHAFT process(es) found:", file=sys.stderr)
    for pid, cmd in procs:
        print(f"  {pid} {cmd}", file=sys.stderr)
    if mig.check:
        raise Stop("SHAFT process(es) found (see above)")
    while True:
        print("[Enter] retry · type yes to continue · q to stop", file=sys.stderr)
        line = _readline(mig)
        if line is None:
            raise Stop("SHAFT processes still running")
        s = line.strip().lower()
        if s == "yes":
            return "yes"
        if s in ("", "retry", "r"):
            return "retry"
        if s in ("q", "no", "stop"):
            raise Stop("SHAFT processes still running")


def _check_no_shaft_local(mig: _Mig) -> None:
    while True:
        procs = _local_shaft_processes(mig.runner)
        if not procs:
            return
        if _retry_or_yes(mig, procs) == "yes":
            return


# -- launchd services -------------------------------------------------------------
def _installed_services() -> list[str]:
    """The installed services, as verbs, in stop order (autonomy, telegram, web, api, embed, model)."""
    agents = core.HOME / "Library" / "LaunchAgents"
    return [VERB.get(name, name) for name in SERVICE_STOP_ORDER
            if (agents / f"com.shaft.{name}.plist").exists()]


def _check_live_checkout() -> None:
    """Step 1: every com.shaft.* plist must point at this checkout, or a worktree run would touch live services."""
    agents = core.HOME / "Library" / "LaunchAgents"
    for p in sorted(agents.glob("com.shaft.*.plist")):
        try:
            data = plistlib.loads(p.read_bytes())
        except Exception as e:
            raise Stop(f"cannot read {p}: {e}")
        if data.get("WorkingDirectory") != str(core.PROJECT):
            raise Stop(f"{p.name} points at {data.get('WorkingDirectory')!r}, not this checkout ({core.PROJECT}); "
                       f"run from the live checkout")


# -- remote helpers ----------------------------------------------------------------
def _remote(runner: Runner, host: str, argv) -> subprocess.CompletedProcess:
    return runner.ssh(f"/bin/zsh -lc {shlex.quote(' '.join(shlex.quote(a) for a in argv))}", host=host)


def _remote_test(runner: Runner, host: str, expr: str) -> bool:
    return runner.ssh(f"test {expr}", host=host).returncode == 0


def _remote_path(path: str) -> str:
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def _remote_which_file(runner: Runner, host: str, candidates) -> str | None:
    for p in candidates:
        if _remote_test(runner, host, f"-x {_remote_path(p)}"):
            return p
    return None


def _remote_home(runner: Runner, host: str) -> str:
    r = runner.ssh('printf %s "$HOME"', host=host)
    home = (r.stdout or "").strip()
    if r.returncode or not home:
        raise Stop(f"cannot read {host}'s home directory: {_out(r)}")
    return home


def _remote_plists(runner: Runner, host: str) -> list[str]:
    r = runner.ssh("if [ -d ~/Library/LaunchAgents ]; then /bin/ls -1 ~/Library/LaunchAgents; fi", host=host)
    if r.returncode:
        raise Stop(f"cannot list {host}'s LaunchAgents: {_out(r)}")
    names = [Path(l).name for l in (r.stdout or "").splitlines() if l.strip()]
    return [n for n in names if n.startswith("com.shaft.") and n.endswith(".plist")]


def _dest(runner: Runner, host: str) -> str:
    """$HOME on the new Mac + this project's path relative to this Mac's home (absolute if outside home)."""
    rhome = _remote_home(runner, host)
    try:
        rel = core.PROJECT.resolve().relative_to(core.HOME.resolve())
    except ValueError:
        return str(core.PROJECT)
    return str(Path(rhome) / rel)


def _map_to_remote(runner: Runner, host: str, path: Path) -> str:
    rhome = Path(_remote_home(runner, host))
    p = Path(path).resolve()
    try:
        rel = p.relative_to(core.HOME.resolve())
    except ValueError:
        return str(p)
    return str(rhome / rel)


def _remote_spec(host: str, path: str) -> str:
    return f"{host}:{shlex.quote(path)}"


def _tailscale_exe_local() -> str | None:
    for p in TAILSCALE_PATHS:
        candidate = Path.home() / p[2:] if p.startswith("~/") else Path(p)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("tailscale")


def _tailscale_json(runner: Runner, *, remote: bool = False, host: str | None = None) -> dict | None:
    if remote:
        exe = _remote_which_file(runner, host, TAILSCALE_PATHS)
        if not exe:
            return None
        r = runner.ssh(f"{_remote_path(exe)} status --json", host=host)
    else:
        exe = _tailscale_exe_local()
        if not exe:
            return None
        r = runner.run([exe, "status", "--json"])
    return _json_output(r)


def _json_output(r: subprocess.CompletedProcess) -> dict | None:
    if r.returncode:
        return None
    try:
        data = json.loads(r.stdout or "{}")
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _source_service_modes(runner: Runner, services: list[str]) -> dict[str, str]:
    network = set(services).intersection(NETWORK_SERVICES)
    if not network:
        return {}
    from . import api, web
    exe = _tailscale_exe_local()
    serve = _json_output(runner.run([exe, "serve", "status", "--json"])) if exe else None
    logins = api.allowed_logins()
    local = {"api": api.LOCAL_LOGIN in logins, "web": web.LOCAL}
    if serve is None:
        # Local settings can survive a later tailnet install, and tailnet settings can survive `off`.
        if (("api" in network and local["api"] and logins - {api.LOCAL_LOGIN})
                or ("web" in network and local["web"] and web._read("hosts.json").get("hosts"))):
            raise Stop("the old Mac has both local and tailnet settings. Make Tailscale Serve status readable "
                       "before migrating so SHAFT can preserve which services are exposed.")
        return {verb: "local" if local[verb] else "tailnet" for verb in network}

    modes = {}
    for service, port, https_port in (("api", api.PORT, api.HTTPS_PORT), ("web", web.PORT, web.HTTPS_PORT)):
        if service not in network:
            continue
        exposed = any(
            host.endswith(f":{https_port}")
            and config.get("Handlers", {}).get("/", {}).get("Proxy") == f"http://127.0.0.1:{port}"
            for host, config in (serve.get("Web") or {}).items()
        )
        modes[service] = "local" if local[service] and not exposed else "tailnet"
    return modes


def _needs_tailnet(manifest: dict) -> bool:
    return any(_tailnet_service(manifest, verb) for verb in NETWORK_SERVICES)


def _tailnet_service(manifest: dict, verb: str) -> bool:
    modes = manifest.get("service_modes", {})
    return verb in manifest.get("services", []) and modes.get(verb) != "local"


def _on_args(verb: str, manifest: dict) -> list[str]:
    argv = [verb, "on"]
    if manifest.get("service_modes", {}).get(verb) == "local":
        argv.append("--local")
    return argv


def _folder_kb(runner: Runner, path: Path) -> int | None:
    r = runner.run(["du", "-sk", str(path)])
    m = re.match(r"(\d+)", r.stdout or "")
    return int(m[1]) if m and not r.returncode else None


def _remote_free_kb(runner: Runner, host: str) -> int | None:
    r = runner.ssh('/bin/df -Pk "$HOME"', host=host)
    if r.returncode:
        return None
    lines = (r.stdout or "").splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 4 and parts[3].isdigit():
            return int(parts[3])
    return None


def _uses_codex(name: str) -> bool:
    from . import codexcli
    return codexcli.serves(core.instance_config(name).get("LLM_MODEL"))


def _target_folder_status(runner: Runner, host: str, dest: str) -> dict:
    d = shlex.quote(dest)
    exists = _remote_test(runner, host, f"-d {d}")
    empty = True
    runtime_only = False
    if _remote_test(runner, host, f"-L {d}") or (not exists and _remote_test(runner, host, f"-e {d}")):
        raise Stop(f"{dest} must be a directory on {host}")
    if exists:
        r = runner.ssh(f"/bin/ls -A {d}", host=host)
        if r.returncode:
            raise Stop(f"cannot list {dest} on {host}: {_out(r)}")
        entries = (r.stdout or "").removesuffix("\n")
        empty = not entries
        runtime_only = (entries == ".runtime" and _remote_test(runner, host, f"-d {d}/.runtime")
                        and not _remote_test(runner, host, f"-L {d}/.runtime"))
    return {"exists": exists, "empty": empty, "runtime_only": runtime_only,
            "marker": _remote_test(runner, host, f"-e {d}/.supervisor/migration/incoming"),
            "manifest": _remote_test(runner, host, f"-e {d}/.supervisor/migration/manifest.json"),
            "target_state": _remote_test(runner, host, f"-e {d}/.supervisor/migration/target-state.json")}


def _check_target_folder(runner: Runner, host: str, dest: str) -> None:
    st = _target_folder_status(runner, host, dest)
    if st["target_state"]:
        raise Stop(f"{dest} has already started there: `migrate target` is running on that Mac, so another copy "
                   f"would overwrite it")
    if st["exists"] and not (st["empty"] or st.get("runtime_only") or st["marker"]):
        raise Stop(f"{dest} must be empty, contain only a .runtime directory, or be left by this migration")


def _require_remote_no_shaft(mig: _Mig, host: str) -> None:
    plists = _remote_plists(mig.runner, host)
    if plists:
        raise Stop(f"SHAFT services are installed on {host}: {', '.join(plists)}. Stop them from their own "
                   "checkout with the service off commands before moving that checkout aside.")
    while True:
        procs = _remote_shaft_processes(mig.runner, host)
        if not procs:
            return
        if _retry_or_yes(mig, procs) == "yes":
            return


def _check_new_mac(mig: _Mig) -> None:
    host, runner = mig.host, mig.runner
    problems: list[str] = []

    def bad(msg: str) -> None:
        problems.append(msg)

    copied_runtime = (core.RUNTIME == core.PROJECT / ".runtime"
                      and Path(core.PYTHON).is_relative_to(core.RUNTIME / "python"))
    if copied_runtime:
        # The project rsync replaces any target runtime with these executables.
        python = core.PYTHON
        r = runner.run([python, "--version"])
        try:
            runtime_bin = core.runtime_dir("bin")
        except core.RuntimeLayoutError as e:
            raise Stop(str(e)) from None
        uv_path = runtime_bin / "uv" if runtime_bin else None
        uv = str(uv_path) if uv_path and uv_path.is_file() and os.access(uv_path, os.X_OK) else None
        here = runner.run(["/usr/bin/uname", "-m"])
        there = runner.ssh("/usr/bin/uname -m", host=host)
        if here.returncode or there.returncode or not _out(here).strip() or _out(here).strip() != _out(there).strip():
            bad(f"the copied .runtime needs the same architecture on both Macs (this Mac: "
                f"{_out(here).strip() or 'unknown'}, {host}: {_out(there).strip() or 'unknown'})")
    else:
        python = "python3.12"
        r = _remote(runner, host, [python, "--version"])
        if r.returncode or not re.search(r"^Python\s+\d+\.\d+", r.stdout or "", re.MULTILINE):
            python = "python3"
            r = _remote(runner, host, [python, "--version"])
        uv = _remote_which_file(runner, host, ["/opt/homebrew/bin/uv", "/usr/local/bin/uv"])
    versions = re.findall(r"^Python\s+(\d+)\.(\d+)", r.stdout or "", re.MULTILINE)
    version = versions[-1] if versions else None
    if r.returncode or not version:
        bad(f"{python} not usable for {host}: {_out(r)}")
    elif tuple(map(int, version)) < (3, 12):
        bad(f"{python} {'.'.join(version)} is older than 3.12")
    else:
        print(f"ok   {python} {'.'.join(version)} for {host}")

    gitp = _remote_which_file(runner, host, ["/opt/homebrew/bin/git", "/usr/local/bin/git", "/usr/bin/git"])
    if not uv:
        bad("uv not found in the copied .runtime/bin" if copied_runtime
            else f"uv not found on {host} (looked in /opt/homebrew/bin and /usr/local/bin)")
    else:
        print(f"ok   uv {uv}")
    if not gitp:
        bad(f"git not found on {host}")
    else:
        print(f"ok   git {gitp}")

    exposure = mig.state
    if "services" not in exposure:
        services = _installed_services()
        exposure = {"services": services, "service_modes": _source_service_modes(runner, services)}
    if _needs_tailnet(exposure):
        local = _tailscale_json(runner)
        remote = _tailscale_json(runner, remote=True, host=host)
        local_suffix = (local or {}).get("MagicDNSSuffix")
        remote_suffix = (remote or {}).get("MagicDNSSuffix")
        if local is None:
            bad(f"Tailscale not usable on this Mac (looked for {', '.join(TAILSCALE_PATHS)})")
        elif remote is None:
            bad(f"Tailscale not usable on {host} (looked for {', '.join(TAILSCALE_PATHS)})")
        elif not local_suffix or not remote_suffix:
            bad(f"cannot read the tailnet name: this Mac {local_suffix or 'unknown'}, {host} {remote_suffix or 'unknown'}")
        elif local_suffix != remote_suffix:
            bad(f"tailnets differ: this Mac {local_suffix!r}, {host} {remote_suffix!r}")
        else:
            print(f"ok   tailnet {local_suffix}")

    dest = _dest(runner, host)
    folders = [core.PROJECT]
    local_as = core.HOME / "Library" / "Application Support" / "SHAFT"
    if local_as.is_dir():
        folders.append(local_as)
    folders.extend(Path(folder) for name in core.instance_names()
                   if (folder := core.shared_folder(name)) and Path(folder).is_dir())
    size = 0
    unreadable = False
    for folder in folders:
        kb = _folder_kb(runner, folder)
        if kb is None:
            bad(f"could not read the size of {folder}")
            unreadable = True
        else:
            size += kb
    free = _remote_free_kb(runner, host)
    if free is None:
        bad(f"could not read the free disk on {host}")
    elif free < size:
        bad(f"only {free} KB free on {host}, the copy needs {size} KB")
    elif not unreadable:
        print("ok   free disk")

    if any(_uses_codex(n) for n in core.instance_names()):
        if (_remote_test(runner, host, "-d /Applications/ChatGPT.app")
                or _remote_test(runner, host, '-d "$HOME/Applications/ChatGPT.app"')
                or runner.ssh("\"$SHELL\" -lc 'command -v codex'", host=host).returncode == 0):
            print("ok   Codex present (a codex/* instance)")
        else:
            bad("no Codex (ChatGPT for Mac or the Codex CLI) on " + host)

    if problems:
        raise Stop("\n".join(problems))

    _require_remote_no_shaft(mig, host)
    print("ok   no SHAFT services on " + host)
    _check_target_folder(runner, host, dest)
    print(f"ok   target folder {dest}")


# -- idle (pause + wait) -------------------------------------------------------------
def _scheduler_children(runner: Runner) -> list[str]:
    """Pids in the scheduler's process group other than the scheduler itself, or a blocker string when the group
    cannot be read. The scheduler is found through _local_shaft_processes (a real SHAFT process whose command has
    `-m supervisor autonomy serve`), never through `pgrep -f`, which also matches a shell that merely mentions the
    string. An unreadable pgid while the scheduler exists returns a blocker, never "idle"."""
    scheduler = [pid for pid, cmd in _local_shaft_processes(runner) if "-m supervisor autonomy serve" in cmd]
    if not scheduler:
        return []
    pid = scheduler[0]
    r = runner.run(["ps", "-o", "pgid=", "-p", str(pid)])
    pgid = (r.stdout or "").strip()
    if not pgid.isdigit():
        return [f"unreadable pgid for scheduler {pid}"]
    r = runner.run(["pgrep", "-g", pgid])
    group = [l.strip() for l in (r.stdout or "").splitlines() if l.strip().isdigit()]
    return [p for p in group if p != str(pid)]


def _waiting_on(runner: Runner) -> list[str]:
    from . import runner as runner_mod
    blockers: list[str] = []
    if children := _scheduler_children(runner):
        blockers.append("scheduler group pids: " + ", ".join(children))
    for name in core.instance_names():
        if runner_mod.is_busy(name):
            blockers.append(f"{name} busy")
        if core.update_queued(name):
            blockers.append(f"update queued for {name}")
        if runner_mod.open_conversations(name):
            blockers.append(f"open conversation {name}")
        if runner_mod._held(core.SUP_DIR / "replying" / name):
            blockers.append(f"{name} replying")
    return blockers


def _idle(runner: Runner) -> bool:
    return not _waiting_on(runner)


def _check_q(mig: _Mig) -> None:
    if not mig.real or not mig.tty:
        return
    import select
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        return
    if ready and (line := sys.stdin.readline()) and line.strip().lower().startswith("q"):
        raise Stop("stopped by the operator")


def _wait_idle(mig: _Mig) -> None:
    last: list[str] | None = None
    while True:
        blockers = _waiting_on(mig.runner)
        if not blockers:
            return
        if blockers != last:
            for b in blockers:
                print(f"waiting: {b}", file=sys.stderr)
            last = blockers
        _check_q(mig)
        time.sleep(5)


# -- source steps ---------------------------------------------------------------------
def _dirty_count(runner: Runner, root: Path) -> int:
    r = runner.run(["git", "-C", str(root), "status", "--porcelain"])
    if r.returncode:
        raise Stop(f"git status in {root} failed: {_out(r)}")
    return len((r.stdout or "").splitlines())


def _dirty_and_unpushed(mig: _Mig) -> None:
    r = core.git("worktree", "list", "--porcelain", check=False)
    if r.returncode:
        raise Stop("git worktree list failed: " + _out(r))
    entries = []
    cur: dict | None = None
    for line in (r.stdout or "").splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):], "branch": ""}
            entries.append(cur)
        elif line.startswith("branch ") and cur is not None:
            cur["branch"] = line[len("branch "):]
    known = {str(core.PROJECT.resolve()), str(core.TEMPLATE.resolve())}
    known |= {str((core.INSTANCES / n).resolve()) for n in core.instance_names()}
    rows = []
    clean = 0
    for e in entries:
        p = Path(e["path"]).resolve()
        if str(p) in known:
            continue
        count = _dirty_count(mig.runner, p)
        if not count:
            clean += 1
            continue
        flag = "" if str(p).startswith(str(core.PROJECT.resolve()) + os.sep) else " (not copied)"
        rows.append(f"{e['path']}: {count} dirty{flag}")
    if rows:
        print("development worktrees (uncommitted work):")
        for row in rows:
            print(f"  {row}")
    if rows or clean:
        print(f"{clean} other development worktrees are clean")
    else:
        print("no development worktrees")

    ahead = []
    r2 = core.git("for-each-ref", "--format=%(refname:short) %(upstream:track)", "refs/heads", check=False)
    for line in (r2.stdout or "").splitlines():
        ref, _, track = line.partition(" ")
        if ref == "template" or ref.startswith("instance/"):
            continue
        if track.startswith("[ahead"):
            ahead.append(f"{ref} {track}")
    if ahead:
        print("branches with unpushed commits:")
        for line in ahead:
            print(f"  {line}")
    print("note: commits in .git travel with the folder; only uncommitted work outside the folder is lost "
          "(docs/migration.md)")


def _pause_and_wait(mig: _Mig) -> None:
    if "was_paused" not in mig.state:
        mig.state["was_paused"] = (core.SUP_DIR / "paused").exists()
        mig.save()
    r = mig.runner.shaft("pause")
    print(_out(r), end="")
    if r.returncode:
        raise Stop("bin/shaft pause failed: " + _out(r))
    _wait_idle(mig)


def _freeze(mig: _Mig) -> None:
    for env, out in ((".venv-bonsai", "venv-bonsai.txt"), (".venv-ft", "venv-ft.txt")):
        py = core.PROJECT / env / "bin" / "python"
        if not py.exists():
            continue
        r = mig.runner.run([str(py), "-m", "pip", "freeze"], cwd=core.PROJECT)
        if r.returncode:
            raise Stop(f"pip freeze for {env} failed: {_out(r)}")
        _write_file(mig.state_dir / out, r.stdout)
        if not (mig.state_dir / out).read_text().strip():
            raise Stop(f"the freeze {out} is empty — {env} produced no packages")
    r = mig.runner.run(["brew", "bundle", "dump", "--force", f"--file={mig.state_dir / 'Brewfile'}"],
                       cwd=core.PROJECT)
    if r.returncode == 127:
        print("note: brew is not installed — skipping the Homebrew freeze")
    elif r.returncode:
        raise Stop(f"brew bundle dump failed: {_out(r)}")
    elif not (mig.state_dir / "Brewfile").read_text().strip():
        raise Stop("Brewfile is empty — the brew bundle dump produced nothing")


def _stop_service(mig: _Mig, verb: str) -> None:
    while True:
        r = mig.runner.shaft(verb, "off")
        print(_out(r), end="")
        if r.returncode == 0 and "not now:" not in (r.stdout or "") + (r.stderr or ""):
            return
        print(f"{verb} did not stop (see above).", file=sys.stderr)
        force_hint = " · force" if verb in FORCE_OK else ""
        print(f"[Enter] retry{force_hint} · q stop", file=sys.stderr)
        line = _readline(mig)
        if line is None:
            raise Stop(f"{verb} is still running")
        s = line.strip().lower()
        if s == "force" and verb in FORCE_OK:
            r = mig.runner.shaft(verb, "off", "--force")
            print(_out(r), end="")
            if r.returncode:
                raise Stop(f"{verb} off --force failed: {_out(r)}")
            return
        if s in ("q", "stop", "no"):
            raise Stop(f"{verb} is still running")
        # anything else (blank or unknown): retry


def _stop_services(mig: _Mig) -> None:
    if "services" not in mig.state:
        services = _installed_services()
        modes = _source_service_modes(mig.runner, services)
        mig.state.update(services=services, service_modes=modes)
        mig.save()
    for verb in mig.state["services"]:
        if verb in _installed_services():
            _stop_service(mig, verb)
    if _installed_services():
        raise Stop("services still installed: " + ", ".join(_installed_services()))
    _check_no_shaft_local(mig)


def _manifest(mig: _Mig, services: list[str]) -> dict:
    from . import mind as mind_mod
    r = core.git("for-each-ref", "--format=%(refname:short) %(objectname)",
                 "refs/heads/template", "refs/heads/instance/", check=False)
    branches = {}
    for line in r.stdout.splitlines():
        ref, _, sha = line.partition(" ")
        if ref and sha:
            branches[ref] = sha
    minds = {n: mind_mod.head(n) for n in core.instance_names()}
    shared = {n: str(core.shared_folder(n)) for n in core.instance_names() if core.shared_folder(n)}
    exposure = {"services": services, "service_modes": mig.state.get("service_modes", {})}
    tail = (_tailscale_json(mig.runner) or {}) if _needs_tailnet(exposure) else {}
    return {
        "source_host": socket.gethostname(),
        "project": str(core.PROJECT),
        "home": str(core.HOME),
        "tailnet": {"suffix": tail.get("MagicDNSSuffix", ""),
                    "dns": (tail.get("Self", {}) or {}).get("DNSName", "")},
        **exposure,
        "was_paused": mig.state.get("was_paused", False),
        "shared": shared,
        "branches": branches,
        "minds": minds,
        "browsers": (core.PROJECT / ".browsers").exists(),
    }


def _write_manifest(mig: _Mig) -> None:
    manifest = _manifest(mig, mig.state.get("services", []))
    mig.manifest = manifest
    _write_file(mig.state_dir / "manifest.json", json.dumps(manifest, indent=1))


def _require_source_quiet(mig: _Mig) -> None:
    if not (core.SUP_DIR / "paused").exists():
        raise Stop("the old Mac is not paused (.supervisor/paused is missing) — it changed since the manifest; "
                   "delete .supervisor/migration/source-state.json to start again")
    installed = _installed_services()
    if installed:
        raise Stop(f"services are installed again on the old Mac ({', '.join(installed)}) — it changed since the "
                   "manifest; delete .supervisor/migration/source-state.json to start again")
    _check_no_shaft_local(mig)


def _require_source_stopped(mig: _Mig) -> None:
    _require_source_quiet(mig)
    if blockers := _waiting_on(mig.runner):
        raise Stop("old Mac still has work: " + ", ".join(blockers))
    manifest = mig.manifest or _load_manifest(mig)
    if manifest is None:
        raise Stop("no manifest at .supervisor/migration/manifest.json — run from the start")
    from . import mind as mind_mod
    for ref, sha in manifest.get("branches", {}).items():
        got = core.git("rev-parse", "--verify", "-q", ref, check=False).stdout.strip()
        if got != sha:
            raise Stop(f"{ref} is {got or 'missing'}, the manifest recorded {sha} — the old Mac changed since the "
                       "manifest; delete .supervisor/migration/source-state.json to start again")
    for name, head in manifest.get("minds", {}).items():
        if not head:
            continue
        if mind_mod.head(name) != head:
            raise Stop(f"mind {name} is {mind_mod.head(name) or 'missing'}, the manifest recorded {head} — the old "
                       "Mac changed since the manifest; delete .supervisor/migration/source-state.json to start again")


def _copy(mig: _Mig) -> None:
    mig.state["copied"] = True
    mig.save()
    _require_source_stopped(mig)
    _check_new_mac(mig)
    host, runner = mig.host, mig.runner
    dest = _dest(runner, host)
    r = runner.ssh(f"mkdir -p {shlex.quote(dest + '/.supervisor/migration/incoming')}", host=host)
    if r.returncode:
        raise Stop(f"cannot create the incoming marker on {host}: {_out(r)}")
    excludes = ["/.venv-ui", "/.venv-bonsai", "/.venv-ft", "/.browsers", "/.browsers.new", "/.browsers.old",
                "/.supervisor/migration/incoming", "/.supervisor/migration/source-state.json"]
    r = runner.rsync(["-a", "--delete", "--stats", *[f"--exclude={e}" for e in excludes]],
                     str(core.PROJECT) + "/", _remote_spec(host, dest + "/"))
    if r.returncode:
        raise Stop(f"copy of the project failed: {_out(r)}")
    local_as = core.HOME / "Library" / "Application Support" / "SHAFT"
    if local_as.is_dir():
        remote_as = str(Path(_remote_home(runner, host)) / "Library" / "Application Support" / "SHAFT")
        parent = str(Path(remote_as).parent)
        r = runner.ssh(f"mkdir -p {shlex.quote(parent)}", host=host)
        if r.returncode:
            raise Stop(f"cannot create the Application Support parent on {host}: {_out(r)}")
        r = runner.rsync(["-a", "--stats"], str(local_as) + "/", _remote_spec(host, remote_as + "/"))
        if r.returncode:
            raise Stop(f"copy of Application Support failed: {_out(r)}")
    for name in core.instance_names():
        folder = core.shared_folder(name)
        if not folder:
            continue
        if not Path(folder).is_dir():
            print(f"shared folder {folder} was not copied: it is not a directory")
            continue
        remote_path = _map_to_remote(runner, host, Path(folder))
        parent = str(Path(remote_path).parent)
        r = runner.ssh(f"mkdir -p {shlex.quote(parent)}", host=host)
        if r.returncode:
            raise Stop(f"cannot create the shared-folder parent on {host}: {_out(r)}")
        r = runner.rsync(["-a", "--stats"], str(folder) + "/", _remote_spec(host, remote_path + "/"))
        if r.returncode:
            raise Stop(f"copy of shared folder {folder} failed: {_out(r)}")


def _compare_refs(mig: _Mig) -> None:
    manifest = mig.manifest or _load_manifest(mig)
    host, runner = mig.host, mig.runner
    dest = _dest(runner, host)
    gitp = _remote_which_file(runner, host, ["/opt/homebrew/bin/git", "/usr/local/bin/git", "/usr/bin/git"])
    if not gitp:
        raise Stop(f"git not found on {host}")
    for ref, sha in manifest.get("branches", {}).items():
        r = runner.ssh(" ".join(shlex.quote(a) for a in [gitp, "-C", dest, "rev-parse", "--verify", "-q", ref]), host=host)
        got = (r.stdout or "").strip()
        if got != sha:
            raise Stop(f"{ref} on the new Mac is {got or 'missing'}, the manifest recorded {sha}")
    from . import mind as mind_mod
    for name, head in manifest.get("minds", {}).items():
        if not head:
            continue
        store = dest + "/.supervisor/minds/" + name + ".git"
        r = runner.ssh(" ".join(shlex.quote(a) for a in [gitp, f"--git-dir={store}", "rev-parse", "--verify", "-q", mind_mod.BRANCH]), host=host)
        got = (r.stdout or "").strip()
        if got != head:
            raise Stop(f"mind {name} on the new Mac is {got or 'missing'}, the manifest recorded {head}")


def _print_target_command(mig: _Mig) -> None:
    dest = _dest(mig.runner, mig.host)
    print("on the new Mac, log in at its screen (not over SSH) and run in Terminal:")
    print(f"  cd {dest} && bin/shaft migrate target")
    print("(`on` loads the services into the logged-in session and records the Python that bin/shaft runs.)")


def _stop_commands(mig: _Mig) -> list[str]:
    return [f"bin/shaft {verb} off" for verb in _installed_services()]


def _copy_commands(mig: _Mig) -> list[str]:
    dest = _dest(mig.runner, mig.host)
    host = mig.host
    excludes = " ".join(f"--exclude={e}" for e in
                        ["/.venv-ui", "/.venv-bonsai", "/.venv-ft", "/.browsers", "/.browsers.new", "/.browsers.old",
                         "/.supervisor/migration/incoming", "/.supervisor/migration/source-state.json"])
    lines = [f"rsync -a --delete --stats {excludes} {core.PROJECT}/ {host}:{dest}/"]
    if (core.HOME / "Library" / "Application Support" / "SHAFT").is_dir():
        remote_as = str(Path(_remote_home(mig.runner, host)) / "Library" / "Application Support" / "SHAFT")
        lines.append(f"rsync -a --stats '~/Library/Application Support/SHAFT/' {host}:'{remote_as}/'")
    for name in core.instance_names():
        folder = core.shared_folder(name)
        if folder and Path(folder).is_dir():
            lines.append(f"rsync -a --stats '{folder}/' {host}:'{_map_to_remote(mig.runner, host, Path(folder))}/'")
    return lines


def _source_steps(mig: _Mig) -> list[Step]:
    return [
        Step("1", "Check this checkout is the live one",
             "each com.shaft.* plist must point at this folder, or this run would stop the live services", [],
             _check_live_checkout),
        Step("2", "Check the new Mac over SSH",
             "Python 3.12+, uv, git, free disk, Codex if a codex/ instance, the same tailnet if exposed there, "
             "no installed SHAFT services, and a safe destination folder", [],
             lambda: _check_new_mac(mig)),
        Step("3", "List dirty worktrees and unpushed work",
             "uncommitted work in worktrees outside the folder (../shaft-*) is not copied", [],
             lambda: _dirty_and_unpushed(mig)),
        Step("4", "Pause and wait for nothing to run",
             "pause starts nothing new; wait until no instance is busy, no update is queued and no conversation is "
             "open, and the scheduler's process group is empty", ["bin/shaft pause"],
             lambda: _pause_and_wait(mig)),
        Step("5", "Freeze the environments",
             ".venv-bonsai, .venv-ft and Homebrew into .supervisor/migration/ so the new Mac can rebuild them",
             [".venv-bonsai/bin/python -m pip freeze > .supervisor/migration/venv-bonsai.txt",
              ".venv-ft/bin/python -m pip freeze > .supervisor/migration/venv-ft.txt",
              "brew bundle dump --force --file=.supervisor/migration/Brewfile"],
             lambda: _freeze(mig)),
        Step("6", "Stop the installed services",
             "in the doc's order: autonomy, telegram, web, api, embed, model; `off` deletes each plist",
             lambda: _stop_commands(mig),
             lambda: _stop_services(mig)),
        Step("7", "Write the manifest",
             "the old Mac's name, path, tailnet, installed services, was_paused, shared folders, branch and mind "
             "heads", [], lambda: _write_manifest(mig)),
        Step("8", "Copy to the new Mac",
             "the project with rsync -a --delete (skipping .venv-* and .browsers), then Application Support and "
             "shared folders with no --delete", lambda: _copy_commands(mig), lambda: _copy(mig)),
        Step("9", "Compare refs on the new Mac",
             "every instance branch and mind HEAD must match the manifest", [], lambda: _compare_refs(mig)),
        Step("10", "What to run next",
             "print the exact command to run on the new Mac", [], lambda: _print_target_command(mig)),
    ]


def _bring_back(mig: _Mig) -> list[str]:
    if "was_paused" not in mig.state:
        return []
    services = mig.state.get("services", [])
    was_paused = mig.state.get("was_paused", False)
    cmds = [shlex.join(["bin/shaft", *_on_args(verb, mig.state)]) for verb in SERVICE_START_ORDER if verb in services]
    if not was_paused:
        cmds.append("bin/shaft resume")
    if "autonomy" in services:
        cmds.append("bin/shaft autonomy on")
    return cmds


def _bring_back_message(mig: _Mig) -> None:
    if mig.state.get("copied"):
        try:
            if _remote_plists(mig.runner, mig.host):
                print("the new Mac is running; leave the old Mac stopped", file=sys.stderr)
                return
            dest = _dest(mig.runner, mig.host)
            # A failed test -e alone cannot distinguish absence from an unreadable parent.
            directory = shlex.quote(dest + "/.supervisor/migration")
            state_file = shlex.quote(dest + "/.supervisor/migration/target-state.json")
            r = mig.runner.ssh(
                f'p={directory}; while [ ! -e "$p" ]; do '
                '[ "$p" = / ] && exit 2; p=$(/usr/bin/dirname "$p"); done; '
                '[ -d "$p" ] && [ -r "$p" ] && [ -x "$p" ] || exit 2; '
                f"test -e {state_file}", host=mig.host)
            if r.returncode not in (0, 1):
                raise Stop(_out(r))
            if r.returncode == 0:
                print("leave the old Mac stopped. To recover a partial copy: quit `migrate target`, "
                      "delete .supervisor/migration/target-state.json on the new Mac, then rerun `migrate source`.",
                      file=sys.stderr)
                return
        except Exception:
            print("could not tell whether the new Mac has started; leave the old Mac stopped", file=sys.stderr)
            return
    if cmds := _bring_back(mig):
        print("\nto bring the old Mac back:", file=sys.stderr)
        for c in cmds:
            print(f"  {c}", file=sys.stderr)


# -- target steps ---------------------------------------------------------------------
def _prereqs(mig: _Mig) -> None:
    manifest = mig.manifest
    if not core.git("config", "--get", "user.name", check=False).stdout.strip() \
            or not core.git("config", "--get", "user.email", check=False).stdout.strip():
        raise Stop("git user.name/user.email are not set (the supervisor commits as itself, but new and your own "
                   "commits need an identity)")
    if _installed_services():
        raise Stop("services still installed here: " + ", ".join(_installed_services()))
    _check_no_shaft_local(mig)
    from . import mind as mind_mod
    for ref, sha in manifest.get("branches", {}).items():
        got = core.git("rev-parse", "--verify", "-q", ref, check=False).stdout.strip()
        if got != sha:
            raise Stop(f"{ref} is {got or 'missing'}, the manifest recorded {sha} — check the copy")
    for name, head in manifest.get("minds", {}).items():
        if not head:
            continue
        if mind_mod.head(name) != head:
            raise Stop(f"mind {name} is {mind_mod.head(name) or 'missing'}, the manifest recorded {head} — "
                       f"check the copy")


def _laptop(mig: _Mig) -> None:
    if "InternalBattery" in (mig.runner.run(["pmset", "-g", "batt"]).stdout or ""):
        r = mig.runner.run(["fdesetup", "status"])
        print(_out(r), end="")
        if "FileVault is On" not in (r.stdout or ""):
            raise Stop("FileVault is not on — turn it on (System Settings → Privacy & Security → FileVault)")
        print("Optional with administrator rights: sudo pmset -c sleep 0 keeps the Mac awake on power.")
    else:
        print("not a laptop; skipping FileVault and sleep settings")
    if (core.PROJECT / ".models" / "bonsai2-27b-mlx").is_dir():
        raw = (mig.runner.run(["sysctl", "-n", "hw.memsize"]).stdout or "").strip()
        try:
            mem = int(raw)
        except ValueError:
            mem = None
        if mem is not None and mem < 64 * 1024**3:
            print(f"note: this Mac has {mem // 1024**3} GB RAM and .models/bonsai2-27b-mlx/ exists — lower "
                  f"SHAFT_PROMPT_CACHE_GB (docs/migration.md)")


def _repair(mig: _Mig) -> None:
    moved = mig.manifest.get("project") != str(core.PROJECT)
    if moved:
        r = core.git("worktree", "repair", "template", *[f"instances/{n}" for n in core.instance_names()], check=False)
        if r.returncode:
            raise Stop("git worktree repair failed: " + _out(r))
        print(_out(r), end="")
    else:
        print("ok   project path unchanged")
    _clean_instance_environments()
    if moved:
        old = mig.manifest["project"]
        for root in [core.TEMPLATE, *[core.INSTANCES / n for n in core.instance_names()]]:
            if (root / ".git").exists() and old in (root / ".git").read_text():
                raise Stop(f"{root / '.git'} still holds the old path {old}")
    for name, mapped in _shared_repairs(mig):
        if not mapped.is_dir():
            print(f"shared folder {mapped} does not exist here; restore it before sharing with {name}")
            continue
        r = mig.runner.shaft("share", name, str(mapped))
        print(_out(r), end="")
        if r.returncode:
            raise Stop(f"bin/shaft share {name} {mapped} failed: {_out(r)}")


def _shared_repairs(mig: _Mig) -> list[tuple[str, Path]]:
    old_home = mig.manifest.get("home")
    if not old_home:
        return []
    repairs = []
    for name in core.instance_names():
        folder = core.shared_folder(name)
        if not folder:
            continue
        folder = Path(folder)
        try:
            mapped = core.HOME / folder.relative_to(old_home)
        except ValueError:
            continue
        if folder != mapped:
            repairs.append((name, mapped))
    return repairs


def _clean_instance_environments() -> None:
    base = core.INSTANCES.resolve()
    for name in core.instance_names():
        for rel in (".venv", ".uv", ".tmp"):
            p = core.INSTANCES / name / rel
            if not p.exists() and not p.is_symlink():
                continue
            if not (base == p.resolve() or base in p.resolve().parents):
                raise Stop(f"refusing to remove {p}: outside {core.INSTANCES}")
            try:
                if p.is_symlink() or not p.is_dir():
                    p.unlink()
                else:
                    shutil.rmtree(p)
            except OSError as e:
                raise Stop(f"cannot remove {p}: {e}")


def _setup(mig: _Mig) -> None:
    r = mig.runner.shaft("setup")
    print(_out(r), end="")
    if r.returncode:
        raise Stop("bin/shaft setup failed: " + _out(r))
    created = [l for l in (r.stdout or "").splitlines() if l.startswith("created ")]
    if created:
        raise Stop("worktrees were missing — check the copy: " + "; ".join(created))
    r = core.git("worktree", "prune", check=False)
    if r.returncode:
        raise Stop("git worktree prune failed: " + _out(r))


def _rebuild(mig: _Mig) -> None:
    def build(env: str, req: Path, ui: bool = False) -> None:
        py = core.PROJECT / env / "bin" / "python"
        r = mig.runner.run([core.PYTHON, "-m", "venv", "--clear", str(core.PROJECT / env)], cwd=core.PROJECT)
        if r.returncode:
            raise Stop(f"{core.PYTHON} -m venv --clear {env} failed: {_out(r)}")
        r = mig.runner.run([str(py), "-c", UI_PYTHON_CHECK if ui else "import sys"], cwd=core.PROJECT)
        if r.returncode:
            requirement = "Python 3.12 or later" if ui else "a working Python"
            raise Stop(f"{env} needs {requirement}: {_out(r)}")
        r = mig.runner.run([str(py), "-m", "pip", "install", "-r", str(req)], cwd=core.PROJECT, live=True)
        if r.returncode:
            raise Stop(f"pip install into {env} failed")
        if ui:
            r = mig.runner.run([str(py), "-c", "import prompt_toolkit, rich"], cwd=core.PROJECT)
            if r.returncode:
                raise Stop(f".venv-ui does not import prompt_toolkit/rich: {_out(r)}")

    build(".venv-ui", core.PROJECT / "requirements-ui.txt", ui=True)
    if (mig.state_dir / "venv-bonsai.txt").exists():
        build(".venv-bonsai", mig.state_dir / "venv-bonsai.txt")
    if (mig.state_dir / "venv-ft.txt").exists():
        build(".venv-ft", mig.state_dir / "venv-ft.txt")
    brewfile = mig.state_dir / "Brewfile"
    if brewfile.exists():
        r = mig.runner.run(["brew", "bundle", f"--file={brewfile}"], cwd=core.PROJECT, live=True)
        if r.returncode == 127:
            print("note: brew is not installed — skipping the Homebrew install")
        elif r.returncode:
            print("brew bundle reported failures")
            print("[Enter] continue · q stop", end=" ", flush=True)
            line = _readline(mig)
            if line is None or line.strip().lower().startswith("q"):
                raise Stop("brew bundle failed")
    if mig.manifest.get("browsers"):
        r = mig.runner.shaft("browser", "install", live=True)
        if r.returncode:
            raise Stop("browser install failed")


def _settings_step(mig: _Mig) -> None:
    r = mig.runner.shaft("settings")
    print(_out(r), end="")
    if r.returncode:
        raise Stop("bin/shaft settings failed: " + _out(r))
    from . import settings as settings_mod
    fields = settings_mod.listing()
    missing = [f"{f['name']}={f['value']}" for f in fields
               if isinstance(f["value"], str) and f["value"].startswith("/") and not Path(f["value"]).exists()]
    if missing:
        print("\nsettings whose path does not exist here (fix with shaft settings set/unset):")
        for m in missing:
            print(f"  {m}")

    for f in fields:
        if f["name"] == "SHAFT_WEB_HOSTS" and f["value"]:
            print(f"SHAFT_WEB_HOSTS={f['value']}")
    if not _needs_tailnet(mig.manifest):
        return
    from . import api
    allowed = api.allowed_logins()
    tail = _tailscale_json(mig.runner) or {}
    user_id = (tail.get("Self") or {}).get("UserID")
    login = ((tail.get("User") or {}).get(str(user_id)) or {}).get("LoginName")
    print(f"SHAFT_API_LOGINS={', '.join(sorted(allowed)) or '(empty)'}; this Mac's login: {login or 'unknown'}")
    if not login or login.lower() not in allowed:
        raise Stop("check .env and SHAFT_API_LOGINS: this Mac's tailnet login is unknown or not allowed")


def _service_running(name: str) -> bool:
    from . import autonomy
    return autonomy.running(name)


def _wait_service(mig: _Mig, verb: str) -> None:
    name = "scheduler" if verb == "autonomy" else verb
    deadline = time.monotonic() + 20
    while not _service_running(name):
        if time.monotonic() >= deadline:
            raise Stop(f"{name} is not running; see .supervisor/com.shaft.{name}.log")
        time.sleep(1)


def _confirm_old_stopped(mig: _Mig) -> None:
    print(f"old Mac: {mig.manifest.get('source_host', 'unknown')}")
    while True:
        print("type yes to confirm: the old Mac is stopped and stays stopped (q stops)", end=" ", flush=True)
        line = _readline(mig)
        if line is None or line.strip().lower() == "q":
            raise Stop("old Mac stop was not confirmed")
        if line.strip().lower() == "yes":
            return


def _start_services(mig: _Mig) -> None:
    _confirm_old_stopped(mig)
    installed = set(mig.manifest.get("services", []))
    for verb in SERVICE_START_ORDER:
        if verb not in installed:
            continue
        if verb == "model" and not (core.PROJECT / ".models" / "bonsai2-27b-mlx").is_dir():
            print("note: model service skipped — .models/bonsai2-27b-mlx/ is not present")
            continue
        argv = _on_args(verb, mig.manifest)
        r = mig.runner.shaft(*argv)
        print(_out(r), end="")
        if r.returncode:
            raise Stop(f"bin/shaft {shlex.join(argv)} failed: {_out(r)}")
        r = mig.runner.shaft(verb, "status")
        print(_out(r), end="")
        if r.returncode:
            raise Stop(f"bin/shaft {verb} status failed: {_out(r)}")

        if verb == "model":
            if not (core.HOME / "Library" / "LaunchAgents" / "com.shaft.model.plist").exists():
                raise Stop("model plist is missing; see .supervisor/com.shaft.model.log")
            from . import localmodel
            if problem := localmodel.down_problem([], names=core.instance_names()):
                print(f"note: {problem}")
        else:
            _wait_service(mig, verb)


def _codex(mig: _Mig) -> None:
    if not any(_uses_codex(n) for n in core.instance_names()):
        return
    r = mig.runner.shaft("codex", "status")
    print(_out(r), end="")
    if r.returncode:
        raise Stop("bin/shaft codex status failed: " + _out(r))
    if re.search(r"Codex login:\s*missing", r.stdout or ""):
        if mig.runner.interactive([str(core.PROJECT / "bin" / "shaft"), "codex", "login"]) != 0:
            raise Stop("codex login failed")


def _talk_each(mig: _Mig) -> None:
    for name in core.instance_names():
        if mig.runner.talk(name) != 0:
            raise Stop(f"talk {name} failed")
        uses_codex = _uses_codex(name)
        while True:
            prompt = f"did {name} answer? (Enter yes" + (" · l codex login" if uses_codex else "") + " · q stop)"
            print(prompt, end=" ", flush=True)
            line = _readline(mig)
            if line is None:
                raise Stop(f"talk {name}: stdin closed")
            s = line.strip().lower()
            if s == "":
                break
            if s == "l" and uses_codex:
                if mig.runner.interactive([str(core.PROJECT / "bin" / "shaft"), "codex", "login"]) != 0:
                    raise Stop("codex login failed")
                if mig.runner.talk(name) != 0:
                    raise Stop(f"talk {name} failed")
                continue
            if s.startswith("q"):
                raise Stop("stopped by the operator")
            print("[Enter] answered · l codex login · q stop", file=sys.stderr)


def _check_each(mig: _Mig) -> None:
    for name in core.instance_names():
        r = mig.runner.shaft("check", name, live=True)
        if r.returncode:
            raise Stop(f"check {name} failed: `talk {name}` rebuilds the environment; see the log; "
                       "rerun `migrate target` to check again")


def _resume_and_autonomy(mig: _Mig) -> None:
    installed = set(mig.manifest.get("services", []))
    if not mig.manifest.get("was_paused"):
        r = mig.runner.shaft("resume")
        print(_out(r), end="")
        if r.returncode:
            raise Stop("bin/shaft resume failed: " + _out(r))
    r = mig.runner.shaft("status")
    first = (r.stdout or "").splitlines()[:1]
    print(first[0] if first else "status unavailable")
    if r.returncode:
        raise Stop("bin/shaft status failed: " + _out(r))
    if not mig.manifest.get("was_paused") and first and "ALL PAUSED" in first[0]:
        raise Stop("bin/shaft status still says ALL PAUSED after resume")
    if "autonomy" in installed:
        r = mig.runner.shaft("autonomy", "on")
        print(_out(r), end="")
        if r.returncode:
            raise Stop("bin/shaft autonomy on failed: " + _out(r))
        r = mig.runner.shaft("autonomy", "status")
        print(_out(r), end="")
        if r.returncode:
            raise Stop("bin/shaft autonomy status failed: " + _out(r))
        _wait_service(mig, "autonomy")


def _tailnet_compare(mig: _Mig) -> None:
    if mig.manifest.get("service_modes", {}).get("web") == "local":
        print("pair the Mac app on this Mac: `bin/shaft web pair --local --name <this mac>`")
    if not _needs_tailnet(mig.manifest):
        return
    old_dns = (mig.manifest.get("tailnet", {}).get("dns") or "").rstrip(".")
    new_dns = (((_tailscale_json(mig.runner) or {}).get("Self", {}) or {}).get("DNSName") or "").rstrip(".")
    if not old_dns or not new_dns:
        print(f"cannot compare the tailnet name (old {old_dns or 'unknown'}, new {new_dns or 'unknown'})")
        return
    if old_dns != new_dns:
        print("the tailnet name changed: " + old_dns + " -> " + new_dns)
        if _tailnet_service(mig.manifest, "web"):
            print("  - see the new address with `bin/shaft web status`")
            print("  - pair the Home Screen app again: `bin/shaft web pair --name phone-app`")
            print("  - pair each native app again: `bin/shaft web pair --name <app>`")
            print("  - if a web OAuth client is set (GMAIL_*_WEB_CLIENT_ID), register the new redirect URI: "
                  "https://<mac>.<tailnet>.ts.net:8444/google/callback")
        if _tailnet_service(mig.manifest, "api"):
            print("  - point API clients at the new endpoint (`bin/shaft api status` prints it)")
            print("  - update inbox doorbell callers")
    else:
        print("nothing to do: the tailnet name is unchanged")


def _repair_commands(mig: _Mig) -> list[str]:
    commands = []
    if (mig.manifest or {}).get("project") != str(core.PROJECT):
        commands.append("git worktree repair template " + " ".join(f"instances/{n}" for n in core.instance_names()))
    for name in core.instance_names():
        commands.append("rm -rf " + " ".join(shlex.quote(f"instances/{name}/{rel}") for rel in (".venv", ".uv", ".tmp")))
    commands.extend("bin/shaft share " + shlex.quote(name) + " " + shlex.quote(str(mapped))
                    for name, mapped in _shared_repairs(mig) if mapped.is_dir())
    return commands


def _rebuild_commands(mig: _Mig) -> list[str]:
    python = shlex.quote(core.PYTHON)
    cmds = [f"{python} -m venv --clear .venv-ui",
            shlex.join([".venv-ui/bin/python", "-c", UI_PYTHON_CHECK]),
            ".venv-ui/bin/python -m pip install -r requirements-ui.txt",
            ".venv-ui/bin/python -c 'import prompt_toolkit, rich'"]
    if (mig.state_dir / "venv-bonsai.txt").exists():
        cmds += [f"{python} -m venv --clear .venv-bonsai",
                 ".venv-bonsai/bin/python -m pip install -r .supervisor/migration/venv-bonsai.txt"]
    if (mig.state_dir / "venv-ft.txt").exists():
        cmds += [f"{python} -m venv --clear .venv-ft",
                 ".venv-ft/bin/python -m pip install -r .supervisor/migration/venv-ft.txt"]
    if (mig.state_dir / "Brewfile").exists():
        cmds.append("brew bundle --file=.supervisor/migration/Brewfile")
    if (mig.manifest or {}).get("browsers"):
        cmds.append("bin/shaft browser install")
    return cmds


def _start_commands(mig: _Mig) -> list[str]:
    manifest = mig.manifest or {}
    return [shlex.join(["bin/shaft", *_on_args(verb, manifest)])
            for verb in SERVICE_START_ORDER if verb in manifest.get("services", [])]


def _check_commands(mig: _Mig) -> list[str]:
    return [f"bin/shaft check {n}" for n in core.instance_names()]


def _resume_commands(mig: _Mig) -> list[str]:
    cmds = []
    if not (mig.manifest or {}).get("was_paused"):
        cmds.append("bin/shaft resume")
    cmds.append("bin/shaft status")
    if "autonomy" in set((mig.manifest or {}).get("services", [])):
        cmds += ["bin/shaft autonomy on", "bin/shaft autonomy status"]
    return cmds


def _target_steps(mig: _Mig) -> list[Step]:
    return [
        Step("1", "Check prerequisites and the copy",
             "git identity, no com.shaft.* running, refs against the manifest", [], lambda: _prereqs(mig)),
        Step("2", "FileVault, power, and memory",
             "the folder holds .env, Google tokens and instance keys unencrypted. Sleep settings are optional.",
             [], lambda: _laptop(mig)),
        Step("3", "Repair paths and remove instance environments",
             "repair moved worktrees and shared folders; always delete instance .venv/.uv/.tmp for this Mac",
             lambda: _repair_commands(mig),
             lambda: _repair(mig)),
        Step("4", "Recreate missing worktrees",
             "a `created` line means the copy is incomplete", ["bin/shaft setup", "git worktree prune"],
             lambda: _setup(mig)),
        Step("5", "Rebuild the environments",
             ".venv-ui always; .venv-bonsai and .venv-ft from their freezes; brew bundle; browser install if "
             ".browsers existed", lambda: _rebuild_commands(mig), lambda: _rebuild(mig)),
        Step("6", "Check saved paths, hostnames and allowed logins",
             "missing absolute paths, SHAFT_WEB_HOSTS, and the allowed login if a service uses Tailscale",
             ["bin/shaft settings"],
             lambda: _settings_step(mig)),
        Step("7", "Start the services",
             "model, embed, api, web, telegram — only those installed; model on skipped if the pack is absent",
             lambda: _start_commands(mig), lambda: _start_services(mig)),
        Step("8", "Check the Codex login",
             "only if an instance uses a codex/* model; `missing` runs codex login", ["bin/shaft codex status"],
             lambda: _codex(mig)),
        Step("9", "Talk to every instance",
             "one at a time, in your terminal; confirm each answered before the scheduler starts", [],
             lambda: _talk_each(mig)),
        Step("10", "Check every instance",
             "the test gate on this machine", lambda: _check_commands(mig), lambda: _check_each(mig)),
        Step("11", "Resume and start the scheduler",
             "resume unless the old Mac was paused, then autonomy on if the scheduler was installed",
             lambda: _resume_commands(mig), lambda: _resume_and_autonomy(mig)),
        Step("12", "Reconnect your devices",
             "compare tailnet names and say what to reconnect", [], lambda: _tailnet_compare(mig)),
    ]


# -- the run loop ----------------------------------------------------------------------
def _print_step(step: Step) -> None:
    print()
    print(f"{step.id}. {step.title}")
    print(f"   {step.why}")
    commands = step.commands() if callable(step.commands) else step.commands
    for c in commands:
        print(f"   $ {c}")
    print()


def _confirm(mig: _Mig) -> None:
    while True:
        print("[Enter] run · q stop", end=" ", flush=True)
        line = _readline(mig)
        if line is None:
            raise Stop("stdin closed")
        s = line.strip()
        if s == "":
            return
        if s[0].lower() == "q":
            raise Stop("stopped by the operator")


def _run(mig: _Mig, steps: list[Step], state_path: Path, resume_cmd: str, before_stop=None) -> int:
    state = _load_state(state_path)
    mig.state = state
    mig.state_path = state_path
    done = set(state.get("done", []))
    try:
        for step in steps:
            if step.id in done:
                continue
            _print_step(step)
            _confirm(mig)
            step.run()
            done.add(step.id)
            state["done"] = sorted(done)
            _save_state(state_path, state)
    except Stop as e:
        print(f"\nstopped: {e}", file=sys.stderr)
        if before_stop:
            before_stop(mig)
        print(f"to resume: {resume_cmd}")
        return 1
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"\nstopped: {e}", file=sys.stderr)
        if before_stop:
            before_stop(mig)
        print(f"to resume: {resume_cmd}")
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        if before_stop:
            before_stop(mig)
        print(f"to resume: {resume_cmd}")
        return 1
    return 0


def _source(to: str | None, mig: _Mig) -> int:
    state_path = mig.state_dir / "source-state.json"
    state = _load_state(state_path)
    host = to or state.get("to")
    if not host:
        print("migrate source needs --to HOST (the new Mac)", file=sys.stderr)
        return 2
    if host.startswith("-"):
        print(f"migrate source: refusing a host starting with '-': {host!r}", file=sys.stderr)
        return 2
    mig.host = host
    if not mig.check and "6" in state.get("done", []):
        _require_source_quiet(mig)
    _check_live_checkout()
    if mig.check:
        mig.state = state
        mig.runner.connect(host)
        _check_new_mac(mig)
        print("checks passed")
        return 0
    state["to"] = host
    _save_state(state_path, state)
    if set(s.id for s in _source_steps(mig)) <= set(state.get("done", [])):
        print("migrate source is finished — the copy is on the new Mac; run `bin/shaft migrate target` there.")
        print("to start over, delete .supervisor/migration/source-state.json and .supervisor/migration/manifest.json.")
        return 0
    mig.runner.connect(host)
    return _run(mig, _source_steps(mig), state_path, f"bin/shaft migrate source --to {host}", _bring_back_message)


def _migration_done_path(src: Path) -> Path:
    from datetime import date
    base = src.parent / f"migration-done-{date.today():%Y-%m-%d}"
    p = base
    i = 2
    while p.exists():
        p = base.with_name(f"{base.name}-{i}")
        i += 1
    return p


def _finish_migration(mig: _Mig) -> None:
    src = mig.state_dir
    dst = _migration_done_path(src)
    src.rename(dst)
    print("\nmigration finished: the setup now runs from the new Mac.")
    print("the old Mac stays paused with its services off and is your backup — delete it after a few days "
          "(docs/migration.md §Afterwards).")


def _target_stop_message(mig: _Mig) -> None:
    if "7" in mig.state.get("done", []) or _installed_services():
        print("services run on this Mac; the old Mac must stay stopped", file=sys.stderr)


def _target(mig: _Mig) -> int:
    if (mig.state_dir / "source-state.json").exists():
        print("this is the old Mac (source-state.json is present) — run `bin/shaft migrate target` on the new Mac, "
              "not here", file=sys.stderr)
        return 1
    manifest = _load_manifest(mig)
    if manifest is None:
        if any(mig.state_dir.parent.glob("migration-done-*")):
            print("the move already finished here, nothing to do")
            return 0
        print("no manifest at .supervisor/migration/manifest.json — the copy did not come from a stopped setup; "
              "run `bin/shaft migrate source` on the old Mac", file=sys.stderr)
        return 1
    mig.manifest = manifest
    if mig.check:
        _prereqs(mig)
        print("checks passed")
        return 0
    code = _run(mig, _target_steps(mig), mig.state_dir / "target-state.json", "bin/shaft migrate target",
                _target_stop_message)
    if code == 0:
        _finish_migration(mig)
    return code


def command(kind: str, to: str | None, check: bool, *, stdin=None, tty=None, runner=None) -> int:
    """The `shaft migrate` entry point: `source --to HOST` or `target`, each optionally `--check`."""
    runner = runner or Runner()
    real = stdin is None
    if tty is None:
        tty = getattr(sys.stdin if stdin is None else stdin, "isatty", lambda: False)()
    if not check and not tty:
        print(f"migrate {kind} needs a terminal (there is no --yes); use --check for the read-only checks",
              file=sys.stderr)
        return 1
    mig = _Mig(runner=runner, stdin=_wrap_stdin(stdin), tty=tty, check=check, real=real)
    mig.state_dir = core.SUP_DIR / "migration"
    try:
        if kind == "source":
            return _source(to, mig)
        if kind == "target":
            return _target(mig)
    except Stop as e:
        print(f"stopped: {e}", file=sys.stderr)
        return 1
    print(f"migrate: unknown role {kind!r}", file=sys.stderr)
    return 2
