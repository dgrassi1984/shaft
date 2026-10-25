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
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import resource
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from .core import (HOME, INSTANCES, PYTHON, SUP_DIR, UnsafePath, _tool_settings, _update_tool_settings, instance_root,
                   knob, list_beneath, positive_int, log, safe_mkdir, safe_read, safe_write)

MIN_MINUTES, _ = knob(
    "SHAFT_JOB_MIN_MINUTES", "5", positive_int, "the shortest interval an instance's own job (a plugin on a timer) may "
    "run at", group="Inbox and benchmarks", services=("runs", "api", "scheduler"))
MAX_MINUTES = 7 * 24 * 60
MAX_JOBS = 5
TIMEOUT_S = 120
MAX_CALLS = 30
MAX_OUTPUT = 4000
FAIL_REPORT = 3
BACKOFF_CAP = timedelta(days=1)
OFF_IN_JOBS = {"delegate_coding": "in a scheduled job (it is a script; your next cycle can delegate)",
               "trial": "in a scheduled job (your next cycle can propose one)",
               "send_message": "in a scheduled job: what it returns reaches your inbox; the cycle it wakes can "
                               "write to the operator",
               "secret_set": "in a scheduled job (keys are managed in a cycle)",
               "secret_delete": "in a scheduled job (keys are managed in a cycle)",
               "set_config": "in a scheduled job (your config is changed in a cycle)"}


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
                     "can_wake": bool(sched.get("can_wake", False)),
                     "hash": hashlib.sha256(json.dumps(sched, sort_keys=True).encode()).hexdigest()[:16]})
    if len(jobs) > MAX_JOBS:
        problems.append(f"{len(jobs)} scheduled plugins; at most {MAX_JOBS} run — not run: "
                        + ", ".join(j["plugin"] for j in jobs[MAX_JOBS:]))
        jobs = jobs[:MAX_JOBS]
    return jobs, problems


def _manifest_text(name: str, plugin: str) -> str:
    return safe_read(instance_root(name), f"plugins/{plugin}/plugin.json", "", errors="replace")


def _invalid(sched) -> str:
    if not isinstance(sched, dict):
        return "`schedule` must be an object: {\"every_minutes\": 15, \"args\": {...}, \"can_wake\": false}"
    extra = set(sched) - {"every_minutes", "args", "can_wake"}
    if extra:
        return f"unknown key(s) {sorted(extra)} (every_minutes, args, can_wake)"
    m = sched.get("every_minutes")
    if not isinstance(m, int) or isinstance(m, bool):
        return "every_minutes must be a whole number of minutes"
    if not MIN_MINUTES <= m <= MAX_MINUTES:
        return f"every_minutes {m} is outside {MIN_MINUTES}–{MAX_MINUTES} (SHAFT_JOB_MIN_MINUTES)"
    if not isinstance(sched.get("args", {}), dict):
        return "args must be an object (the arguments run() gets)"
    if not isinstance(sched.get("can_wake", False), bool):
        return "can_wake must be true or false"
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
    last = e.get("last_start")
    return not last or now - datetime.fromisoformat(last) >= interval(job, e)


# -- one run ------------------------------------------------------------------------------------------------------------
def run(name: str, plugin: str) -> int:
    """One run of a job: the plugin in the job sandbox, its output to the inbox, a wake request kept for the
    scheduler. Returns 0 on success, 1 on a failed run, 2 if there is no such job."""
    from . import pyenv, sandbox
    from .broker import Broker
    from .core import secrets_env
    root = instance_root(name)
    job = next((j for j in declared(name, root)[0] if j["plugin"] == plugin), None)
    if job is None:
        changing = plugin in (uncommitted(root) or set())
        log(f"{name}: job {plugin} not run — " + ("it is being changed (not committed yet)" if changing
                                                  else "it is not (or no longer) a valid scheduled plugin"))
        return 2
    started, stamp = time.monotonic(), datetime.now()
    _update(name, plugin, last_start=stamp.isoformat(timespec="seconds"))
    # The project the instance lives in (its broker's own folder, grants and environment are found from it).
    broker = Broker(name, root.parent.parent, secrets_env(), SUP_DIR / "broker.log", max_calls=MAX_CALLS)
    broker.unavailable = dict(OFF_IN_JOBS)
    broker.model_allowed = False
    broker.start()
    state_dir = safe_mkdir(root, f".jobs/{plugin}")
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
    for key, value in (("SHAFT_SHARED", str(broker.shared) if broker.shared else ""),
                       ("SHAFT_MAIL", broker.mail_env()), ("SHAFT_CALENDAR", broker.calendar_env())):
        if value:
            env[key] = value

    def limits():
        resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_S + 5, TIMEOUT_S + 5))
        resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 ** 2, 100 * 1024 ** 2))

    timed_out = False
    try:
        proc = subprocess.Popen(sandbox.wrap(prof, [python, "-E", "-s", "-m", "shaft.plugin_runner", plugin]),
                                cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True, preexec_fn=limits)
        try:
            out, err = proc.communicate(json.dumps(job["args"]), timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            timed_out = True
        code = proc.returncode
    finally:
        broker.stop()
        prof.unlink(missing_ok=True)
    seconds = round(time.monotonic() - started, 1)
    ok = code == 0 and not timed_out
    # done and hash: when its outcome became known, and under which schedule — what a trial replay needs to show the
    # record exactly as it stood at a moment (describe(as_of=…)); ts stays the start, as shown
    row = {"ts": stamp.isoformat(timespec="seconds"), "plugin": plugin, "seconds": seconds,
           "exit": None if timed_out else code, "timed_out": timed_out, "calls": broker.calls, "hash": job["hash"]}
    if ok:
        message, wake, note = _outcome(out, job)
        row.update(out_chars=len(out), delivered=bool(message), wake=wake)
        if message:
            _deliver(name, f"job {plugin}", message + note)
        if wake:
            request_wake(name, plugin)
        _update(name, plugin, fails=0, hash=job["hash"], last_ok=row["ts"], last_error=None)
    else:
        error = f"killed after {TIMEOUT_S} s" if timed_out else f"exit {code}: {(err or '').strip()[-1200:]}"
        e = entry(name, plugin)
        fails = (e.get("fails", 0) if e.get("hash") == job["hash"] else 0) + 1
        row.update(error=error[:1500])
        _update(name, plugin, fails=fails, hash=job["hash"], last_error=error[:1500])
        if fails == FAIL_REPORT:
            nxt = interval(job, {"fails": fails, "hash": job["hash"]})
            _deliver(name, "scheduler", f"My scheduled job {plugin} failed {fails} times in a row; it now runs every "
                                        f"{int(nxt.total_seconds() // 60)} minutes until it succeeds or I change "
                                        f"its schedule. Last error:\n\n```\n{error[-1500:]}\n```")
    row["done"] = datetime.now().isoformat(timespec="seconds")   # its delivery and state are written: now it is known
    _log_row(name, row)
    log(f"{name}: job {plugin} {'ok' if ok else 'FAILED'} in {seconds}s"
        + (f", delivered {row.get('out_chars', 0)} chars" if row.get("delivered") else "")
        + (", asked to wake me" if row.get("wake") else "") + ("" if ok else f" — {row['error'][:200]}"))
    return 0 if ok else 1


def _outcome(out: str, job: dict) -> tuple[str, bool, str]:
    """(message, wake, note) from what run() returned: a plain string, or {"message": …, "wake": …}."""
    text, wake, note = out.strip(), False, ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and ("message" in data or "wake" in data):
            text = str(data.get("message") or "").strip()
            asked = data.get("wake") is True
            if asked and not job["can_wake"]:
                note = ("\n\n[This job asked to wake me, but its schedule does not say can_wake: true — so it did not; "
                        "this message waited for my next wake.]")
            wake = asked and job["can_wake"]
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + f"\n[… cut at {MAX_OUTPUT} characters of {len(text)}]"
    return text, wake, note


def _deliver(name: str, sender: str, text: str) -> Path:
    from . import messages
    stamp = datetime.now()
    safe = sender.replace(" ", "-")
    path = safe_write(instance_root(name), f"inbox/{stamp:%Y%m%d-%H%M%S-%f}-from-{safe}.md",
                      f"From: {sender} (my own scheduled job, run by the supervisor)\nTo: {name}\nKind: job\n"
                      f"Date: {stamp:%Y-%m-%d %H:%M}\n\n{text.strip()}\n")
    messages.record(SUP_DIR, path, sender, name, "job", stamp, text)
    return path


def _log_row(name: str, row: dict) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    with (_dir() / f"{name}.jsonl").open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# -- waking ----------------------------------------------------------------------------------------------------------------
def request_wake(name: str, plugin: str) -> None:
    """Kept once: several requests before the instance is idle make one wake, not a queue."""
    with _editing_state() as st:
        st.setdefault("wake", {}).setdefault(name, {"since": datetime.now().isoformat(timespec="seconds"), "by": []})
        if plugin not in st["wake"][name]["by"]:
            st["wake"][name]["by"].append(plugin)


def take_wake(name: str) -> dict | None:
    with _editing_state() as st:
        req = st.get("wake", {}).pop(name, None)
    return req


def pending_wakes() -> dict[str, dict]:
    return dict(_load_state().get("wake", {}))


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
            children[key] = (start(["job", "run", name, job["plugin"]]), time.monotonic())
    for name, req in pending_wakes().items():
        if name not in names and not (INSTANCES / name / "shaft").is_dir():   # deleted (shaft delete)
            take_wake(name)
            continue
        if paused(name):
            take_wake(name)
            log(f"{name}: wake asked by job(s) {', '.join(req['by'])} dropped — {name} is paused")
            continue
        if f"wake {name}" in children or awake_of(name):
            continue   # kept: it starts a cycle once the instance is idle
        take_wake(name)
        from .autonomy import record
        record("wake", name)   # counts as its wake: the next scheduled one is a full interval later
        log(f"{name}: woken by its job(s) {', '.join(req['by'])} (asked since {req['since']})")
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
    e = {"last_ok": None, "fails": 0, "hash": None, "last_error": None}
    for r in rows:
        if r.get("plugin") != plugin or r.get("skipped"):
            continue
        h = r.get("hash") or job_hash
        if r.get("exit") == 0 and not r.get("timed_out"):
            e.update(last_ok=r["ts"], fails=0, hash=h, last_error=None)
        else:
            e.update(fails=(e["fails"] if e["hash"] == h else 0) + 1, hash=h, last_error=r.get("error"))
    return e


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
        lines.append(f"  {j['plugin']}: every {j['every_minutes']} min{' · can wake' if j['can_wake'] else ''} · {state}"
                     + (f"\n    last error: {e['last_error'][:200]}" if e.get("last_error") else ""))
    lines += [f"  not run: {p}" for p in problems]
    if as_of is None:   # what is changing or waiting now: a replay's moment has passed, and neither is recorded
        changing = uncommitted(root) or set()
        lines += [f"  {p}: being changed (not committed yet) — runs once its cycle commits it"
                  for p in sorted(changing) if '"schedule"' in _manifest_text(name, p)]
    if not jobs and not problems:
        lines.append("  (no plugin has a schedule)")
    if as_of is None and (req := pending_wakes().get(name)):
        lines.append(f"  wake asked by {', '.join(req['by'])} since {req['since']} (starts once it is idle)")
    if rows:
        lines.append("  recent runs:")
        for r in rows[-limit:]:
            what = (f"skipped ({r['skipped']})" if r.get("skipped") else
                    f"{'ok' if r.get('exit') == 0 else 'FAILED'} {r.get('seconds')}s"
                    + (f", delivered {r.get('out_chars')} chars" if r.get("delivered") else "")
                    + (", woke me" if r.get("wake") else "") + (f" — {r['error'][:120]}" if r.get("error") else ""))
            lines.append(f"    {r['ts']} {r['plugin']}: {what}")
    return "\n".join(lines)
