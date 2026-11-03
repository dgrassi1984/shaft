"""Scheduled jobs (docs/plans/PLAN-jobs.md): an instance runs one of its own plugins every N minutes, without anyone installing
anything. wren, 2026-10-16: "I can write the watcher … but the installation lives outside my sandbox".

A job is declared in the plugin's own manifest — `"schedule": {"every_minutes": 15, "args": {…}, "can_wake": true}`
in plugins/<name>/plugin.json — so the instance creates, changes or removes it by editing its plugin in a cycle (the
gate tests it, the commit records it). SHAFT's scheduler service runs due jobs as children (`shaft job run <name>
<plugin>`), each in the `run` sandbox with its own broker and egress proxy:
  - it writes only to .jobs/<plugin>/ (its state) and .tmp/ — a cycle running at the same time works on the code,
    notes and mind, and nothing of the job's is mistaken for its change;
  - no model (a job is a script: it never takes the local model, costs no tokens); no delegate_coding, trial or
    send_message (the instance it wakes does that);
  - at most MAX_CALLS supervisor tool calls, TIMEOUT_S seconds, MAX_OUTPUT characters delivered.
What it returns goes to the instance's inbox, from "job <plugin>"; nothing returned, nothing delivered. A run may
ask to wake the instance — `{"message": …, "wake": true}` — which starts a cycle as soon as it is idle, if the job
declared `can_wake` (no rate limit: the operator, 2026-10-16). A job never overlaps itself: a run still going when
the next comes due means that one is skipped and logged. Failures are visible: every run is logged in
.supervisor/jobs/<instance>.jsonl; after FAIL_REPORT failures in a row the instance gets one message with the last
error, and the job backs off (its interval doubles, up to a day) until it succeeds or its manifest changes.

What a job makes reaches the instance whole (docs/plans/PLAN-thelos-review-2.md S1, S6; 2026-10-31: Thelos's
morning brief could not write notes/ and its 20 KB fallback was cut at 4,000 characters, the rest nowhere it was told):
  - hand-off: `{"message": …, "files": ["brief.md", …]}` — at most MAX_FILES files of MAX_FILE_BYTES from its state
    folder — are copied beside the inbox message (inbox/<stamp>-from-job-<plugin>.files/), which lists them with their
    sizes, and names each refused one with why; a text past MAX_OUTPUT is kept there whole and the cut names the file;
  - publish: `"publishes": {"notes/briefing.md": "briefing.md"}` in the schedule — targets under notes/ only, sources
    in its state folder — copies each source that changed since it was last published onto its target, atomically,
    after a successful run only (the instance's next notes commit records it);
  - the broker's `run` inside a job writes only the job's own set when SHAFT_JOB_RUN_CONFINED is 1 (until then a
    plugin reached notes/ through it, and the job's write limit protected nothing);
  - `"long": true`: LONG_TIMEOUT_S and LONG_CALLS instead of TIMEOUT_S and MAX_CALLS, one long job at a time per
    instance, SHAFT_JOB_CHECKPOINT in its environment; a run that times out having written that checkpoint since it
    started is resumed at the next scheduler tick with SHAFT_JOB_RESUME=1, up to LONG_RESUMES times, then gives up
    (one message to the instance).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import resource
import signal
import stat
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from .core import (HOME, INSTANCES, PYTHON, SUP_DIR, TooBig, UnsafePath, _tool_settings, _update_tool_settings,
                   dir_beneath, instance_root, knob, list_beneath, positive_int, log, safe_mkdir, safe_read,
                   safe_read_bytes, safe_write, whole)


def _switch(s: str) -> bool:
    if s not in ("0", "1"):
        raise ValueError(f"must be 0 or 1, not {s!r}")
    return s == "1"



MIN_MINUTES, _ = knob(
    "SHAFT_JOB_MIN_MINUTES", "5", positive_int, "the shortest interval an instance's own job (a plugin on a timer) may "
    "run at", group="Inbox and benchmarks", services=("runs", "api", "scheduler"))
MAX_MINUTES = 7 * 24 * 60
MAX_JOBS, _ = knob(
    "SHAFT_JOBS_MAX", "12", positive_int, "scheduled plugins (jobs) an instance may run; beyond it, the rest by name "
    "are not run", group="Inbox and benchmarks", services=("runs", "api", "scheduler"))
TIMEOUT_S = 120
MAX_CALLS = 30
MAX_OUTPUT = 4000
MAX_KEPT = 1024 ** 2          # characters of a cut output kept whole beside its message
MAX_FILES = 5                 # files one run hands over, and targets one schedule publishes
MAX_FILE_BYTES = 256 * 1024   # each of them
LONG_TIMEOUT_S, _ = knob(
    "SHAFT_JOB_LONG_TIMEOUT_S", "900", positive_int, "seconds one run of a long scheduled job (\"long\": true in its "
    "schedule) may take before it is stopped (and resumed if it wrote its checkpoint)", group="Inbox and benchmarks",
    services=("runs", "api", "scheduler"))
LONG_CALLS, _ = knob(
    "SHAFT_JOB_LONG_CALLS", "200", positive_int, "supervisor tool calls one run of a long scheduled job may make",
    group="Inbox and benchmarks", services=("runs", "api", "scheduler"))
LONG_RESUMES, _ = knob(
    "SHAFT_JOB_LONG_RESUMES", "4", whole, "times a long scheduled job that timed out after writing its checkpoint is "
    "resumed before it gives up (0: never resumed)", group="Inbox and benchmarks",
    services=("runs", "api", "scheduler"))
RUN_CONFINED, _ = knob(
    "SHAFT_JOB_RUN_CONFINED", "0", _switch, "1: the broker's `run` inside a scheduled job writes only where the job "
    "may (.jobs/<plugin>/, .tmp/, .browser/); 0: where a cycle's `run` may (notes/, code). Turn it on once every "
    "instance's scheduled plugins publish through `publishes`", group="Inbox and benchmarks",
    services=("runs", "api", "scheduler"))
FAIL_REPORT = 3
BACKOFF_CAP = timedelta(days=1)
OFF_IN_JOBS = {"delegate_coding": "in a scheduled job (it is a script; your next cycle can delegate)",
               "trial": "in a scheduled job (your next cycle can propose one)",
               "send_message": "in a scheduled job: what it returns reaches your inbox; the cycle it wakes can "
                               "write to the operator",
               "secret_set": "in a scheduled job (keys are managed in a cycle)",
               "secret_delete": "in a scheduled job (keys are managed in a cycle)",
               "set_config": "in a scheduled job (your config is changed in a cycle)",
               "record_order": "in a scheduled job (an order is recorded by the cycle or conversation that heard it)"}


def _dir() -> Path:
    return SUP_DIR / "jobs"   # read at call time: tests move SUP_DIR


def _state_path() -> Path:
    return _dir() / "state.json"


def _load_state() -> dict:
    try:
        st = json.loads(_state_path().read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    tmp = _state_path().with_name(f"state.{os.getpid()}.tmp")   # one per process: a shared name was renamed away
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(_state_path())


@contextlib.contextmanager
def _editing_lock():
    """The state file's lock, for anyone rewriting it (here, and `shaft delete`: supervisor/delete.py)."""
    _dir().mkdir(parents=True, exist_ok=True)
    with open(_dir() / "state.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def _editing_state():
    """Load, change and save the state under one lock. Every job run is its own process, and the scheduler and the
    jobs write the same file: until 2026-10-19 two jobs ending in the same second shared one temp name (the second
    rename failed: FileNotFoundError, the job counted as failed) and could overwrite each other's update."""
    with _editing_lock():
        st = _load_state()
        yield st
        _save_state(st)


def _update(name: str, plugin: str, **changes) -> dict:
    with _editing_state() as st:
        entry = st.setdefault("jobs", {}).setdefault(name, {}).setdefault(plugin, {})
        entry.update(changes)
    return entry


def entry(name: str, plugin: str) -> dict:
    return dict(_load_state().get("jobs", {}).get(name, {}).get(plugin, {}))


# -- the operator's switch -------------------------------------------------------------------------------------------
def jobs_off(name: str) -> bool:
    """`shaft jobs <name> off`: kept in the supervisor's folder, so the instance cannot switch them back on."""
    return bool(_tool_settings(name).get("jobs_off"))


def set_jobs_off(name: str, off: bool) -> None:
    _update_tool_settings(name, jobs_off=True if off else None)


# -- what the instance declared ----------------------------------------------------------------------------------------
def uncommitted(root: Path) -> set[str] | None:
    """Plugin folders whose files differ from the instance's last commit — being written by a cycle or a
    conversation, or just reverted by the gate. None if git cannot tell (then no job runs: fail closed).
    A job runs only committed, gate-tested code (2026-10-16: wren's watcher ran mid-conversation, half-written —
    "NameError: name 'broker' is not defined" — and a plugin.json written before its main.py was reported as broken)."""
    from .core import git_wt
    try:
        r = git_wt(root, "status", "--porcelain", "--untracked-files=all", "--", "plugins", check=False,
                   project=Path(root).parent.parent)
    except SystemExit as e:   # not a registered worktree
        log(f"jobs: {root.name}: cannot tell which plugins are committed ({e}); no job runs")
        return None
    if r.returncode != 0:
        log(f"jobs: {root.name}: git status failed ({r.stderr.strip()[:200]}); no job runs")
        return None
    out = set()
    for line in r.stdout.splitlines():
        path = line[3:].strip().strip('"').split(" -> ")[-1]
        parts = Path(path).parts
        if len(parts) >= 2 and parts[0] == "plugins":
            out.add(parts[1])
    return out


def declared(name: str, root: Path | None = None, committed: bool = False) -> tuple[list[dict], list[str]]:
    """(jobs, problems): every committed plugin whose manifest has a valid `schedule` (at most MAX_JOBS, by name),
    and why each other committed schedule is not run. A plugin with uncommitted changes is neither: it waits.
    `committed`: `root` is a commit's tree (a trial replay's copy, no git of its own), so every plugin in it is."""
    root = Path(root or instance_root(name))
    jobs, problems = [], []
    changing = set() if committed else uncommitted(root)
    if changing is None:
        return [], []
    for plugin in list_beneath(root, "plugins"):   # read without following a link (core.safe_read)
        if plugin in changing:
            continue
        try:
            text = safe_read(root, f"plugins/{plugin}/plugin.json", errors="replace")
        except (OSError, UnsafePath):
            continue
        try:
            spec = json.loads(text)
        except ValueError as e:
            if '"schedule"' in text:
                problems.append(f"plugins/{plugin}/plugin.json does not parse ({e}), so its schedule is not run")
            continue
        sched = spec.get("schedule") if isinstance(spec, dict) else None
        if sched is None:
            continue
        why = _invalid(sched)
        if why:
            problems.append(f"plugins/{plugin}: schedule not run — {why}")
            continue
        if not (root / "plugins" / plugin / "main.py").is_file():
            problems.append(f"plugins/{plugin}: schedule not run — no main.py")
            continue
        jobs.append({"plugin": plugin, "every_minutes": int(sched["every_minutes"]), "args": sched.get("args") or {},
                     "can_wake": bool(sched.get("can_wake", False)), "long": bool(sched.get("long", False)),
                     "publishes": dict(sched.get("publishes") or {}),
                     "hash": hashlib.sha256(json.dumps(sched, sort_keys=True).encode()).hexdigest()[:16]})
    if len(jobs) > MAX_JOBS:
        problems.append(f"{len(jobs)} scheduled plugins; at most {MAX_JOBS} run — not run: "
                        + ", ".join(j["plugin"] for j in jobs[MAX_JOBS:]))
        jobs = jobs[:MAX_JOBS]
    return jobs, problems


def _manifest_text(name: str, plugin: str) -> str:
    return safe_read(instance_root(name), f"plugins/{plugin}/plugin.json", "", errors="replace")


KEYS = ("every_minutes", "args", "can_wake", "publishes", "long")


def _plain(rel) -> bool:
    """A relative path with plain components: no absolute path, no `..`, `.` or empty part."""
    return isinstance(rel, str) and bool(rel) and not rel.startswith("/") and "\\" not in rel \
        and all(p not in ("", ".", "..") for p in rel.split("/"))


def _invalid(sched) -> str:
    if not isinstance(sched, dict):
        return "`schedule` must be an object: {\"every_minutes\": 15, \"args\": {...}, \"can_wake\": false}"
    extra = set(sched) - set(KEYS)
    if extra:
        return f"unknown key(s) {sorted(extra)} ({', '.join(KEYS)})"
    m = sched.get("every_minutes")
    if not isinstance(m, int) or isinstance(m, bool):
        return "every_minutes must be a whole number of minutes"
    if not MIN_MINUTES <= m <= MAX_MINUTES:
        return f"every_minutes {m} is outside {MIN_MINUTES}–{MAX_MINUTES} (SHAFT_JOB_MIN_MINUTES)"
    if not isinstance(sched.get("args", {}), dict):
        return "args must be an object (the arguments run() gets)"
    if not isinstance(sched.get("can_wake", False), bool):
        return "can_wake must be true or false"
    if not isinstance(sched.get("long", False), bool):
        return "long must be true or false"
    pub = sched.get("publishes", {})
    if not isinstance(pub, dict):
        return ("publishes must be an object: {\"notes/briefing.md\": \"briefing.md\"} (target under notes/: source "
                "in the job's state folder .jobs/<plugin>/)")
    if len(pub) > MAX_FILES:
        return f"publishes names {len(pub)} targets; at most {MAX_FILES}"
    for target, source in pub.items():
        if not _plain(target) or target.split("/")[0] != "notes" or target.count("/") < 1:
            return (f"publishes target {target!r} is not under notes/: a job publishes only to notes/ (never code, "
                    "plugins, skills, tests, shaft, self or memory)")
        if not _plain(source):
            return (f"publishes {target!r}: its source must be a path inside the job's state folder (.jobs/<plugin>/), "
                    f"e.g. \"briefing.md\", not {source!r}")
    return ""


def report_problems(name: str, problems: list[str]) -> None:
    """Each distinct problem reaches the instance's inbox once (the scheduler checks every tick)."""
    if not problems:
        return
    key = hashlib.sha256("\n".join(problems).encode()).hexdigest()[:16]
    with _editing_state() as st:
        if st.setdefault("reported", {}).get(name) == key:
            return
        st["reported"][name] = key
    _deliver(name, "scheduler", "Some of my scheduled jobs are not run:\n\n" + "\n".join(f"- {p}" for p in problems))
    log(f"{name}: jobs not run — " + "; ".join(problems))


# -- when ---------------------------------------------------------------------------------------------------------------
def interval(job: dict, e: dict) -> timedelta:
    """Its own interval, doubled per failure past the FAIL_REPORT-th (up to a day), until a run succeeds or the
    manifest changes."""
    base = timedelta(minutes=job["every_minutes"])
    fails = e.get("fails", 0) if e.get("hash") == job["hash"] else 0
    return min(base * (2 ** max(0, fails - FAIL_REPORT + 1)), max(base, BACKOFF_CAP))


def due(name: str, job: dict, now: datetime) -> bool:
    e = entry(name, job["plugin"])
    if job.get("long") and e.get("resume") and e.get("hash") == job["hash"]:
        return True   # a long job to resume goes at the next tick, not its interval
    last = e.get("last_start")
    return not last or now - datetime.fromisoformat(last) >= interval(job, e)


# -- one run ------------------------------------------------------------------------------------------------------------
@contextlib.contextmanager
def _long_slot(name: str):
    """One long job at a time per instance (the scheduler starts no second one; this holds for `shaft job run` too):
    yields False if another long job of the instance holds it."""
    _dir().mkdir(parents=True, exist_ok=True)
    with open(_dir() / f"long-{name}.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def run(name: str, plugin: str) -> int:
    """One run of a job: the plugin in the job sandbox, its output to the inbox, a wake request kept for the
    scheduler. Returns 0 on success, 1 on a failed run, 2 if there is no such job, 3 if it is a long job and another
    long job of the instance is running."""
    root = instance_root(name)
    job = next((j for j in declared(name, root)[0] if j["plugin"] == plugin), None)
    if job is None:
        changing = plugin in (uncommitted(root) or set())
        log(f"{name}: job {plugin} not run — " + ("it is being changed (not committed yet)" if changing
                                                  else "it is not (or no longer) a valid scheduled plugin"))
        return 2
    if not job["long"]:
        return _run(name, root, job)
    with _long_slot(name) as free:
        if not free:
            _log_row(name, {"ts": datetime.now().isoformat(timespec="seconds"), "plugin": plugin,
                            "skipped": "another long job of mine is running"})
            log(f"{name}: job {plugin} not run — another long job of {name} is running (one at a time)")
            return 3
        return _run(name, root, job)


def _run(name: str, root: Path, job: dict) -> int:
    from . import pyenv, sandbox
    from .broker import Broker
    from .core import secrets_env
    plugin, long = job["plugin"], job["long"]
    timeout, max_calls = (LONG_TIMEOUT_S, LONG_CALLS) if long else (TIMEOUT_S, MAX_CALLS)
    before = entry(name, plugin)
    resumed = int(before.get("resume") or 0) if long and before.get("hash") == job["hash"] else 0
    started, stamp = time.monotonic(), datetime.now()
    # resume cleared while it runs: the tick would otherwise find it due every 30 s and log it as skipped
    _update(name, plugin, last_start=stamp.isoformat(timespec="seconds"), running=stamp.isoformat(timespec="seconds"),
            pid=os.getpid(), resume=None)
    # The project the instance lives in (its broker's own folder, grants and environment are found from it).
    broker = Broker(name, root.parent.parent, secrets_env(), SUP_DIR / "broker.log", max_calls=max_calls)
    broker.unavailable = dict(OFF_IN_JOBS)
    broker.model_allowed = False
    state_dir = safe_mkdir(root, f".jobs/{plugin}")
    broker.run_confined_to = state_dir if RUN_CONFINED else None
    broker.start()
    safe_mkdir(root, ".tmp")
    home = safe_mkdir(root, f".jobs/{plugin}/.home")
    prof = state_dir / ".job.sb"
    prof.write_text(sandbox.job_profile(root, HOME, state_dir, broker.shared, egress_port=broker.egress.port,
                                        broker_port=broker.port, env_root=broker.env_root))
    python = pyenv.python(broker.env_root)
    env = {"PATH": ":".join([*pyenv.path_dirs(root, broker.env_root), str(Path(PYTHON).parent), "/usr/bin:/bin"]),
           "HOME": str(home), "TMPDIR": str(home), "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "SHAFT_ROOT": str(root), "SHAFT_BROKER_URL": f"http://127.0.0.1:{broker.port}",
           "SHAFT_BROKER_TOKEN": broker.token, "SHAFT_JOB": plugin, "SHAFT_JOB_STATE": f".jobs/{plugin}",
           **broker.sandbox_env(), **pyenv.uv_env(root)}
    if long:
        env.update(SHAFT_JOB_CHECKPOINT=f".jobs/{plugin}/{CHECKPOINT}", SHAFT_JOB_RESUME="1" if resumed else "0")
    for key, value in (("SHAFT_SHARED", str(broker.shared) if broker.shared else ""),
                       ("SHAFT_MAIL", broker.mail_env()), ("SHAFT_CALENDAR", broker.calendar_env())):
        if value:
            env[key] = value

    def limits():
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5, timeout + 5))
        resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 ** 2, 100 * 1024 ** 2))

    timed_out, since_ns = False, time.time_ns()
    try:
        proc = subprocess.Popen(sandbox.wrap(prof, [python, "-E", "-s", "-m", "shaft.plugin_runner", plugin]),
                                cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True, preexec_fn=limits)
        try:
            out, err = proc.communicate(json.dumps(job["args"]), timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            timed_out = True
        code = proc.returncode
    finally:
        broker.stop()
        prof.unlink(missing_ok=True)
        _update(name, plugin, running=None, pid=None)
    seconds = round(time.monotonic() - started, 1)
    ok = code == 0 and not timed_out
    # done and hash: when its outcome became known, and under which schedule — what a trial replay needs to show the
    # record exactly as it stood at a moment (describe(as_of=…)); ts stays the start, as shown
    row = {"ts": stamp.isoformat(timespec="seconds"), "plugin": plugin, "seconds": seconds,
           "exit": None if timed_out else code, "timed_out": timed_out, "calls": broker.calls, "hash": job["hash"]}
    if long:
        row["resumed"] = resumed
    if ok:
        message, wake, note, files = _outcome(out, job)
        published = _publish(name, root, job)
        delivered = _deliver_job(name, plugin, message, note, files)
        row.update(out_chars=len(out), delivered=delivered, wake=wake)
        if published:
            row["published"] = published
        if wake:
            request_wake(name, plugin)
        _update(name, plugin, fails=0, hash=job["hash"], last_ok=row["ts"], last_error=None, status="done")
    elif long and timed_out and resumed < LONG_RESUMES and _checkpointed(root, plugin, since_ns):
        # not a failure: it saved its place, and goes on at the next tick
        row.update(error=f"stopped after {timeout} s; resumed from its checkpoint at the next tick", resuming=True)
        _update(name, plugin, hash=job["hash"], resume=resumed + 1, status=f"resuming {resumed + 1} of {LONG_RESUMES}")
    else:
        error = f"killed after {timeout} s" if timed_out else f"exit {code}: {(err or '').strip()[-1200:]}"
        e = entry(name, plugin)
        fails = (e.get("fails", 0) if e.get("hash") == job["hash"] else 0) + 1
        gave_up = long and timed_out and (resumed > 0 or _checkpointed(root, plugin, since_ns))
        row.update(error=error[:1500], **({"gave_up": True} if gave_up else {}))
        _update(name, plugin, fails=fails, hash=job["hash"], last_error=error[:1500],
                status="gave up" if gave_up else "failed")
        if gave_up:
            _deliver(name, "scheduler", f"My long job {plugin} gave up: it was stopped after {timeout} s "
                                        f"{resumed + 1} time(s) in a row ({resumed} resume(s) from its checkpoint; at "
                                        f"most {LONG_RESUMES}). Its checkpoint stays in .jobs/{plugin}/{CHECKPOINT}; "
                                        "it starts afresh at its next interval.")
        if fails == FAIL_REPORT:
            nxt = interval(job, {"fails": fails, "hash": job["hash"]})
            _deliver(name, "scheduler", f"My scheduled job {plugin} failed {fails} times in a row; it now runs every "
                                        f"{int(nxt.total_seconds() // 60)} minutes until it succeeds or I change "
                                        f"its schedule. Last error:\n\n```\n{error[-1500:]}\n```")
    row["done"] = datetime.now().isoformat(timespec="seconds")   # its delivery and state are written: now it is known
    _log_row(name, row)
    log(f"{name}: job {plugin} {'ok' if ok else 'resuming' if row.get('resuming') else 'FAILED'} in {seconds}s"
        + (f" (resume {resumed})" if resumed else "")
        + (f", delivered {row.get('out_chars', 0)} chars" if row.get("delivered") else "")
        + (f", published {', '.join(row['published'])}" if row.get("published") else "")
        + (", asked to wake me" if row.get("wake") else "") + ("" if ok else f" — {row['error'][:200]}"))
    return 0 if ok else 1


CHECKPOINT = "checkpoint.json"


def _checkpointed(root: Path, plugin: str, since_ns: int) -> bool:
    """Whether the long job wrote its checkpoint (a plain file, not a link) since its run started."""
    try:
        fd = dir_beneath(root, (".jobs", plugin))
    except (OSError, UnsafePath):
        return False
    try:
        st = os.stat(CHECKPOINT, dir_fd=fd, follow_symlinks=False)
    except OSError:
        return False
    finally:
        os.close(fd)
    return stat.S_ISREG(st.st_mode) and st.st_mtime_ns >= since_ns


def _outcome(out: str, job: dict) -> tuple[str, bool, str, object]:
    """(message, wake, note, files) from what run() returned: a plain string, or {"message": …, "wake": …,
    "files": […]}. The message is whole: delivery cuts it (_deliver_job), keeping all of it beside the message."""
    text, wake, note, files = out.strip(), False, "", None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and ("message" in data or "wake" in data or "files" in data):
            text = str(data.get("message") or "").strip()
            files = data.get("files")
            asked = data.get("wake") is True
            if asked and not job["can_wake"]:
                note = ("\n\n[This job asked to wake me, but its schedule does not say can_wake: true — so it did not; "
                        "this message waited for my next wake.]")
            wake = asked and job["can_wake"]
    return text, wake, note, files


def _handoff(root: Path, plugin: str, files) -> tuple[list[tuple[str, bytes]], list[str]]:
    """The files a run hands over, read from its own state folder without following a link, and why each refused one
    is: ([(name, bytes)], ["path: why"])."""
    if files is None or files == []:
        return [], []
    if not isinstance(files, list):
        return [], [f"`files` must be a list of paths in .jobs/{plugin}/, not {type(files).__name__}"]
    loaded, refused = [], []
    for i, f in enumerate(files):
        shown = str(f)[:200]
        if i >= 4 * MAX_FILES:   # a runaway list is not read to its end
            refused.append(f"and {len(files) - i} more paths, not read")
            break
        if len(loaded) >= MAX_FILES:
            refused.append(f"{shown}: more than {MAX_FILES} files in one run")
            continue
        rel = f.removeprefix(f".jobs/{plugin}/") if isinstance(f, str) else f
        if not _plain(rel):
            refused.append(f"{shown}: not a path inside my job's own folder .jobs/{plugin}/")
            continue
        try:
            data = safe_read_bytes(root, f".jobs/{plugin}/{rel}", limit=MAX_FILE_BYTES)
        except TooBig:
            refused.append(f"{shown}: over {MAX_FILE_BYTES // 1024} KB")
            continue
        except UnsafePath:
            refused.append(f"{shown}: a link, or not a plain file (links are not followed)")
            continue
        except OSError as e:
            refused.append(f"{shown}: {e.strerror or e}")
            continue
        loaded.append((Path(rel).name, data))
    return loaded, refused


def _deliver_job(name: str, plugin: str, text: str, note: str, files) -> bool:
    """A run's message to the inbox, with the files it handed over beside it (inbox/<stem>.files/) and, past
    MAX_OUTPUT, its whole text there too, named by the cut. Nothing to say and no file: nothing delivered."""
    from . import messages
    root = instance_root(name)
    loaded, refused = _handoff(root, plugin, files)
    if not text and not loaded and not refused:
        return False
    _prune_files(root, plugin)
    stamp = datetime.now()
    stem = _stem(stamp, f"job {plugin}")
    whole = len(text) > MAX_OUTPUT
    atts = messages.store_attachments(root, f"inbox/{stem}.files",
                                      ([("output.txt", text[:MAX_KEPT].encode())] if whole else []) + loaded)
    body = text
    if whole:
        kept = atts[0]["rel"]
        body = (text[:MAX_OUTPUT] + f"\n[… cut at {MAX_OUTPUT} of {len(text)} characters; the whole text: {kept}"
                + (f" (its first {MAX_KEPT})" if len(text) > MAX_KEPT else "") + "]")
    body += note
    handed = atts[1:] if whole else atts
    if handed:
        body += ("\n\nFiles this job handed me, whole (read them with offsets; they are removed once this message is "
                 "archived and the job delivers again — copy what I keep into notes/):\n"
                 + "\n".join(f"- {a['rel']} ({messages.size_text(a['bytes'])})" for a in handed))
    if refused:
        body += "\n\nFiles this job named that were not delivered:\n" + "\n".join(f"- {r}" for r in refused)
    _deliver(name, f"job {plugin}", body, stamp=stamp, attachments=atts)
    return True


def _prune_files(root: Path, plugin: str) -> None:
    """The file folders of this job's earlier messages that have left the inbox (archived once read): removed, so
    hand-offs do not pile up in the instance's inbox (and its mind store). Through descriptors: a link is removed
    itself, never followed."""
    import shutil
    suffix = f"-from-job-{plugin.replace(' ', '-')}.files"
    names = list_beneath(root, "inbox")
    try:
        fd = dir_beneath(root, ("inbox",))
    except (OSError, UnsafePath):
        return
    try:
        for n in names:
            if n.endswith(suffix) and n.removesuffix(".files") + ".md" not in names:
                try:
                    st = os.stat(n, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        shutil.rmtree(n, dir_fd=fd)
                    else:
                        os.unlink(n, dir_fd=fd)
                except OSError as e:
                    log(f"{root.name}: job {plugin}: could not remove inbox/{n}: {e}")
    finally:
        os.close(fd)


def _publish(name: str, root: Path, job: dict) -> list[str]:
    """After a successful run: each declared source that changed since it was last published, copied onto its notes/
    target atomically (never a half-written note for a cycle to read). The targets published; a problem reaches the
    instance's inbox once (until it changes)."""
    plugin = job["plugin"]
    if not job["publishes"]:
        return []
    e = entry(name, plugin)
    last = e.get("published") if isinstance(e.get("published"), dict) else {}
    now, done, problems = {}, [], []
    for target, source in job["publishes"].items():
        if target in last:
            now[target] = last[target]
        try:
            data = safe_read_bytes(root, f".jobs/{plugin}/{source}", limit=MAX_FILE_BYTES)
        except FileNotFoundError:
            continue   # nothing to publish yet
        except TooBig:
            problems.append(f"{target}: its source .jobs/{plugin}/{source} is over {MAX_FILE_BYTES // 1024} KB")
            continue
        except (OSError, UnsafePath) as err:
            problems.append(f"{target}: its source .jobs/{plugin}/{source} cannot be read ({err})")
            continue
        digest = hashlib.sha256(data).hexdigest()
        if last.get(target) == digest:
            continue
        try:
            _replace_beneath(root, target, data)
        except (OSError, UnsafePath) as err:
            problems.append(f"{target}: not written ({err})")
            continue
        now[target], done = digest, done + [target]
    key = hashlib.sha256("\n".join(problems).encode()).hexdigest()[:16] if problems else None
    _update(name, plugin, published=now, publish_problem=key)
    if problems and key != e.get("publish_problem"):
        _deliver(name, "scheduler", f"My scheduled job {plugin} ran, but did not publish:\n\n"
                 + "\n".join(f"- {p}" for p in problems))
    return done


def _replace_beneath(root: Path, rel: str, data: bytes) -> None:
    """root/rel replaced by `data` in one rename, through its folder's descriptor (made if missing, no link followed on
    the way; a link at rel is replaced itself, never followed)."""
    parts = Path(rel).parts
    fd = dir_beneath(root, parts[:-1], make=True)
    tmp = f".{parts[-1]}.shaft-publish-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        try:
            if stat.S_ISDIR(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode):
                raise UnsafePath(f"{rel}: a folder stands there")
        except FileNotFoundError:
            pass
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as f:
                f.write(data)
            os.rename(tmp, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=fd)
            raise
    finally:
        os.close(fd)


def _stem(stamp: datetime, sender: str) -> str:
    return f"{stamp:%Y%m%d-%H%M%S-%f}-from-{sender.replace(' ', '-')}"


def _deliver(name: str, sender: str, text: str, stamp: datetime | None = None,
             attachments: list[dict] | None = None) -> Path:
    from . import messages
    stamp = stamp or datetime.now()
    path = safe_write(instance_root(name), f"inbox/{_stem(stamp, sender)}.md",
                      f"From: {sender} (my own scheduled job, run by the supervisor)\nTo: {name}\nKind: job\n"
                      f"Date: {stamp:%Y-%m-%d %H:%M}\n\n{text.strip()}\n")
    messages.record(SUP_DIR, path, sender, name, "job", stamp, text, attachments)
    return path


def _log_row(name: str, row: dict) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    with (_dir() / f"{name}.jsonl").open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# -- waking ----------------------------------------------------------------------------------------------------------------
def request_wake(name: str, plugin: str, not_before: datetime | None = None) -> None:
    """Kept once: several requests before the instance is idle make one wake, not a queue. `plugin`: the job that asked,
    or an event (autonomy.event_wake: "message from wren", "commitment c… due") — an event's name has a space.
    `not_before`: the wake may not start earlier (another instance's message inside the recipient's wake floor: paced,
    not dropped); a request without one makes the wake due now. `last`: when the latest request came — a cycle that
    began after it has seen what it asked for (tick)."""
    now = datetime.now().isoformat(timespec="seconds")
    with _editing_state() as st:
        new = name not in st.setdefault("wake", {})
        req = st["wake"].setdefault(name, {"since": now, "by": []})
        req["last"] = now
        if plugin not in req["by"]:
            req["by"].append(plugin)
        if not_before is None:
            req.pop("not_before", None)
        elif new or req.get("not_before"):
            at = not_before.isoformat(timespec="seconds")
            req["not_before"] = min(at, req.get("not_before") or at)


def take_wake(name: str) -> dict | None:
    with _editing_state() as st:
        req = st.get("wake", {}).pop(name, None)
    return req


def pending_wakes() -> dict[str, dict]:
    return dict(_load_state().get("wake", {}))


def _wakers(by: list[str]) -> str:
    """'its job(s) watcher; message from wren': who asked for a wake, jobs and events (request_wake) apart."""
    plugins, events = [b for b in by if " " not in b], [b for b in by if " " in b]
    return "; ".join(([f"its job(s) {', '.join(plugins)}"] if plugins else []) + events)


def results_since(name: str, since: datetime) -> int:
    """Runs of the instance's jobs that delivered a message or published a file after `since` (its last cycle): a
    timed wake has something to do (autonomy.wake_gate)."""
    cut = since.isoformat(timespec="seconds")
    return sum(1 for r in _rows(name, 500) if (r.get("delivered") or r.get("published")) and _known(r) > cut)


# -- what the scheduler does each tick ---------------------------------------------------------------------------------
def tick(children: dict, now: datetime, start, names: list[str], awake_of) -> None:
    """Start due jobs (never one whose previous run is still going: skipped, logged) and the wakes jobs asked for.
    `start(args)` starts a child; `children` maps job keys to (process, started)."""
    from .core import paused
    for name in names:
        if paused(name) or jobs_off(name) or f"battery {name}" in children:   # an exam restores the real folder
            continue
        try:
            jobs, problems = declared(name)
        except OSError:
            continue
        report_problems(name, problems)
        long_running = any(f"job {name} {j['plugin']}" in children for j in jobs if j["long"])
        for job in jobs:
            key = f"job {name} {job['plugin']}"
            if not due(name, job, now):
                continue
            if key in children:
                _update(name, job["plugin"], last_start=now.isoformat(timespec="seconds"))   # a full interval later
                _log_row(name, {"ts": now.isoformat(timespec="seconds"), "plugin": job["plugin"], "skipped":
                                "its previous run is still going"})
                log(f"{name}: job {job['plugin']} skipped — its previous run is still going")
                continue
            if job["long"] and long_running:
                continue   # one long job at a time per instance: still due, it starts once the other has ended
            children[key] = (start(["job", "run", name, job["plugin"]]), time.monotonic())
            long_running = long_running or job["long"]
    from .autonomy import _state as schedule
    cycles = schedule().get("cycles") or {}
    for name, req in pending_wakes().items():
        if name not in names and not (INSTANCES / name / "shaft").is_dir():   # deleted (shaft delete)
            take_wake(name)
            continue
        if paused(name):
            take_wake(name)
            log(f"{name}: wake asked by {_wakers(req['by'])} dropped — {name} is paused")
            continue
        if f"wake {name}" in children or awake_of(name):
            continue   # kept: it starts a cycle once the instance is idle
        if (cycles.get(name) or "") > req.get("last", req["since"]):
            # A cycle began after the last request (a timed wake, `shaft live`): it saw what was asked for. Without
            # this, a deferred request (not_before) or one kept while that cycle ran woke the instance again for it.
            take_wake(name)
            log(f"{name}: wake asked by {_wakers(req['by'])} dropped — its cycle at {cycles[name][11:16]} came after")
            continue
        if req.get("not_before") and now.isoformat(timespec="seconds") < req["not_before"]:
            continue   # kept: paced (request_wake's not_before)
        take_wake(name)
        from .autonomy import record
        record("wake", name)   # counts as its wake: the next scheduled one is a full interval later
        log(f"{name}: woken by {_wakers(req['by'])} (asked since {req['since']})")
        children[f"wake {name}"] = (start(["wake", name]), time.monotonic())


# -- what the operator and the instance see --------------------------------------------------------------------------------
def _parsed(lines: list[str]) -> list[dict]:
    """Run-log rows, skipping a line that is not a dated row (a torn write must not break the record)."""
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("ts"), str) and isinstance(row.get("plugin"), str):
            rows.append(row)
    return rows


def _known(row: dict) -> str:
    """When a run's outcome became known: logged (done) since 2026-10-25, else its start plus its duration."""
    if isinstance(row.get("done"), str):
        return row["done"]
    try:
        return (datetime.fromisoformat(row["ts"]) + timedelta(seconds=float(row.get("seconds") or 0))
                ).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):   # a corrupt duration: known from its start
        return row["ts"]


def _rows(name: str, limit: int, before: datetime | None = None) -> list[dict]:
    """The job run log, oldest first: its last `limit` rows, or (`before`) every run whose outcome was known by then."""
    path = _dir() / f"{name}.jsonl"
    lines = path.read_text().splitlines() if path.is_file() else []
    if before is None:
        return _parsed(lines[-limit:])
    cut = before.isoformat(timespec="seconds")
    return [r for r in _parsed(lines) if _known(r) < cut]


def _entry_then(rows: list[dict], plugin: str, job_hash: str) -> dict:
    """A job's state as its runs in `rows` left it — what entry() held at that moment (the state file keeps only now):
    the same transitions as run(), a failure count restarting when the schedule changed. Rows logged before
    2026-10-25 carry no schedule hash: they count as the schedule declared then."""
    e = {"last_ok": None, "fails": 0, "hash": None, "last_error": None, "status": None}
    for r in rows:
        if r.get("plugin") != plugin or r.get("skipped"):
            continue
        h = r.get("hash") or job_hash
        if r.get("exit") == 0 and not r.get("timed_out"):
            e.update(last_ok=r["ts"], fails=0, hash=h, last_error=None, status="done")
        elif r.get("resuming"):   # a long job stopped after saving its place: not a failure
            n = int(r.get("resumed") or 0) + 1
            e.update(hash=h, status=f"resuming {n} of {max(n, LONG_RESUMES)}")
        else:
            e.update(fails=(e["fails"] if e["hash"] == h else 0) + 1, hash=h, last_error=r.get("error"),
                     status="gave up" if r.get("gave_up") else "failed")
    return e


def _running(e: dict) -> bool:
    """A run of the job is going now: its process is alive (a scheduler restart kills it without clearing the mark)."""
    try:
        return bool(e.get("running")) and isinstance(e.get("pid"), int) and (os.kill(e["pid"], 0) or True)
    except OSError:
        return False


def describe(name: str, limit: int = 20, as_of: datetime | None = None, root: Path | None = None) -> str:
    """The jobs an instance declared, their state and recent runs. `as_of` and `root` (a trial replay): the record as
    it stood when the replayed cycle began — its declared jobs from the replay's copy, their state and runs from the
    run log before then — in the same shape as a live answer, so nothing in it says it is a replay."""
    root = Path(root or instance_root(name))
    try:
        jobs, problems = declared(name, root, committed=as_of is not None)
    except OSError:
        return f"{name}: no folder"
    rows = _rows(name, limit, as_of)
    lines = [f"{name}: jobs {'OFF (shaft jobs ' + name + ' on)' if jobs_off(name) else 'on'}"]
    for j in jobs:
        e = entry(name, j["plugin"]) if as_of is None else _entry_then(rows, j["plugin"], j["hash"])
        state = (f"last ok {e['last_ok']}" if e.get("last_ok") else "not run yet")
        if e.get("fails") and e.get("hash") == j["hash"]:
            state += f" · {e['fails']} failure(s) in a row, now every {int(interval(j, e).total_seconds() // 60)} min"
        # status: running now, or (a long job) how its last run ended — done, resuming n of N, gave up
        status = (f"running since {e['running']}" if as_of is None and _running(e) else
                  e.get("status") if j["long"] and e.get("status") and e.get("hash") == j["hash"] else "")
        args = json.dumps(j["args"], ensure_ascii=False)
        lines.append(f"  {j['plugin']}: every {j['every_minutes']} min{' · long' if j['long'] else ''}"
                     f"{' · can wake' if j['can_wake'] else ''} · {state}" + (f" · {status}" if status else "")
                     + f" · args {args if len(args) <= 200 else args[:200] + '…'}"
                     + (f" · publishes {', '.join(j['publishes'])}" if j["publishes"] else "")
                     + (f"\n    last error: {e['last_error'][:200]}" if e.get("last_error") else ""))
    lines += [f"  not run: {p}" for p in problems]
    if as_of is None:   # what is changing or waiting now: a replay's moment has passed, and neither is recorded
        changing = uncommitted(root) or set()
        lines += [f"  {p}: being changed (not committed yet) — runs once its cycle commits it"
                  for p in sorted(changing) if '"schedule"' in _manifest_text(name, p)]
    if not jobs and not problems:
        lines.append("  (no plugin has a schedule)")
    if as_of is None and (req := pending_wakes().get(name)):
        lines.append(f"  wake asked by {_wakers(req['by'])} since {req['since']} (starts once it is idle"
                     + (f", not before {req['not_before'][11:16]})" if req.get("not_before") else ")"))
    if rows:
        lines.append("  recent runs:")
        for r in rows[-limit:]:
            what = (f"skipped ({r['skipped']})" if r.get("skipped") else
                    f"{'ok' if r.get('exit') == 0 else 'stopped' if r.get('resuming') else 'FAILED'} {r.get('seconds')}s"
                    + (f" (resume {r['resumed']})" if r.get("resumed") else "")
                    + (", gave up" if r.get("gave_up") else "")
                    + (f", delivered {r.get('out_chars')} chars" if r.get("delivered") else "")
                    + (f", published {', '.join(map(str, r['published']))}" if isinstance(r.get("published"), list)
                       else "")
                    + (", woke me" if r.get("wake") else "") + (f" — {r['error'][:120]}" if r.get("error") else ""))
            lines.append(f"    {r['ts']} {r['plugin']}: {what}")
    return "\n".join(lines)
