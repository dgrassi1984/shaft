"""Trials: an instance tests a change to its own code or prompts before it keeps it.

The instance stages a change and calls the broker's `trial` tool (request). The supervisor freezes the instance's
committed code (A) and the change applied to it (B), runs the test gate on B, and picks N past autonomous cycles.
For each it rebuilds the instance's state at the moment that cycle began (reconstruct: its git branch at that
moment, plus the inbox it read) and runs the cycle twice from that same state — once with A, once with B — in
scratch copies outside the instance's folder, under the same sandbox, through a ReplayBroker that carries out no
writes (replay_arm). A judge who is not told which run is which compares each pair, effectiveness first, in both
orders (judge_pair); the verdict — better / worse / no clear difference, by a sign test — goes to the instance's
inbox. The instance decides whether to adopt the change; if the files it keeps match the trial's exactly,
probation knows the verdict (match).

Trials on the local model never take an instance's time (Daniele, 2026-10-13: "this should not cannibalise its
time. Otherwise, it can use Mimo"): a pair runs on the local model only while nothing else needs it, and is killed
and requeued the moment a cycle, a sleep or a conversation does; pairs still waiting after SHAFT_TRIAL_LOCAL_WAIT_H
(or preempted twice) run on SHAFT_TRIAL_FALLBACK_MODEL. The work is done by work(), which the scheduler starts.

Plan, decisions and acceptance criteria: PLAN-trials.md.
"""
from __future__ import annotations

import difflib
import fcntl
import hashlib
import io
import json
import os
import random
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timedelta
from math import comb
from pathlib import Path

from . import gate
from .core import (CYCLE_TIMEOUT_S, HOME, LLM_PORTS, PROJECT, SUP_DIR, UnsafePath, contained, knob, log,
                   safe_write, secrets_env)

TRIALS_PER_DAY, TRIALS_PER_DAY_SRC = knob("SHAFT_TRIALS_PER_DAY", "2", int)
DAILY_USD, DAILY_USD_SRC = knob("SHAFT_TRIAL_DAILY_USD", "3", float)
LOCAL_UNITS_PER_DAY, LOCAL_UNITS_SRC = knob("SHAFT_TRIAL_LOCAL_UNITS_PER_DAY", "24", int)
N_MAX, N_MAX_SRC = knob("SHAFT_TRIAL_N_MAX", "10", int)
LOCAL_WAIT_H, LOCAL_WAIT_SRC = knob("SHAFT_TRIAL_LOCAL_WAIT_H", "6", float)
FALLBACK_MODEL, FALLBACK_SRC = knob("SHAFT_TRIAL_FALLBACK_MODEL", "openrouter/xiaomi/mimo-v2.6-pro", str)
PARALLEL, PARALLEL_SRC = knob("SHAFT_TRIAL_PARALLEL", "3", int)
N_DEFAULT, N_MIN, EXTEND_BY = 6, 2, 4
ALPHA = 0.1                    # one-sided sign test on the states that were not ties (decision D5)
STALE_H = 48                   # a trial still unfinished after this is concluded with the pairs it has
LOCAL_UNIT_S = 900             # a local pair needs the model this long free ahead (≈1.5 × a measured cycle)
PREEMPT_AHEAD_S = 90           # a local run is stopped this long before a wake or the sleep comes due
TARGETS = ("shaft/", "plugins/", "skills/", "tests/")
# What an arm's code is: the gate's code paths plus config.env, which carries the arm's model and settings.
# The gate no longer counts config.env as code (the operator edits it too; the instance only via set_config),
# but an arm still runs with — and a trial still refuses to change — the config it was committed with.
CODE = (*gate.CODE_PATHS, "config.env")
# mail/: attachments downloaded from the operator's mail (PLAN-mail.md) are copies, not state: never in the judge's diff.
NOT_STATE = ("logs", ".tmp", "secrets", "__pycache__", ".venv", ".uv", "mail")
LEDGER = SUP_DIR / "trial_usage.jsonl"


class TrialError(Exception):
    pass


# -- storage ---------------------------------------------------------------------------------------------
def _dir(name: str, tid: str = "") -> Path:
    return SUP_DIR / "trials" / name / tid if tid else SUP_DIR / "trials" / name


def load(name: str, tid: str) -> dict:
    return json.loads((_dir(name, tid) / "trial.json").read_text())


def save(t: dict) -> None:
    d = _dir(t["instance"], t["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "trial.json.tmp"
    tmp.write_text(json.dumps(t, indent=1, ensure_ascii=False))
    os.replace(tmp, d / "trial.json")


def all_trials(name: str | None = None) -> list[dict]:
    base = SUP_DIR / "trials"
    paths = sorted(base.glob(f"{name or '*'}/t-*/trial.json")) if base.is_dir() else []
    out = []
    for p in paths:
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda t: t["created"])


def _files(name: str, tid: str, which: str) -> dict[str, bytes | None]:
    """The frozen code of one side: base (A) or candidate (the change: None = the file is deleted)."""
    d = _dir(name, tid) / which
    meta = json.loads((d / "files.json").read_text())
    return {rel: (None if blob is None else (d / blob).read_bytes()) for rel, blob in meta.items()}


def _freeze(name: str, tid: str, which: str, files: dict[str, bytes | None]) -> None:
    d = _dir(name, tid) / which
    d.mkdir(parents=True, exist_ok=True)
    meta = {}
    for i, (rel, data) in enumerate(sorted(files.items())):
        if data is None:
            meta[rel] = None
        else:
            (d / f"{i:04d}").write_bytes(data)
            meta[rel] = f"{i:04d}"
    (d / "files.json").write_text(json.dumps(meta, indent=1))


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _ledger(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **row}) + "\n")


def usage_today(name: str) -> dict:
    """What the instance's trials spent today: dollars, local-model runs, trials started (operator trials excluded)."""
    today = datetime.now().date().isoformat()
    usd, local = 0.0, 0
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            r = json.loads(line)
            if r.get("instance") == name and r["ts"].startswith(today) and r.get("by") != "operator":
                usd += r.get("usd") or 0.0
                local += 1 if r.get("kind") == "arm" and r.get("model") == "local" else 0
    started = sum(1 for t in all_trials(name) if t["created"].startswith(today) and t["by"] == "instance")
    return {"usd": usd, "local_units": local, "started": started}


# -- git: the instance's branch, read-only ---------------------------------------------------------------
def _git(project: Path, *args: str, check: bool = True, binary: bool = False):
    r = subprocess.run(["git", "-C", str(project), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                        *args], capture_output=True, text=not binary)
    if check and r.returncode:
        err = r.stderr if not binary else r.stderr.decode(errors="replace")
        raise TrialError(f"git {' '.join(args[:3])} failed: {err.strip()[:300]}")
    return r


def branch(name: str) -> str:
    return f"instance/{name}"


def committed_code(project: Path, name: str, rev: str = "") -> dict[str, bytes]:
    """The code the instance runs with as committed (passed the gate): every file under CODE at `rev`."""
    rev = rev or branch(name)
    listed = _git(project, "ls-tree", "-r", "-z", "--name-only", rev, "--", *CODE).stdout.split("\0")
    out = {}
    for rel in filter(None, listed):
        if "__pycache__" in rel or rel.endswith(".DS_Store"):
            continue
        out[rel] = _git(project, "show", f"{rev}:{rel}", binary=True).stdout
    return out


def _show(project: Path, rev: str, rel: str) -> str | None:
    r = _git(project, "show", f"{rev}:{rel}", check=False)
    return r.stdout if r.returncode == 0 else None


def _env(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, eq, value = line.partition("=")
        if eq and key.strip().isidentifier():
            v = value.strip()
            out[key.strip()] = v[1:].split(v[0])[0] if v[:1] in "\"'" and v else v.split("#")[0].strip()
    return out


def model_of(code: dict[str, bytes]) -> dict:
    """Where the code's config.env sends model calls: {'local': bool, 'model', 'port'}."""
    env = _env((code.get("config.env") or b"").decode(errors="replace"))
    base, model = env.get("LLM_BASE_URL", ""), env.get("LLM_MODEL", "")
    if base == "broker":
        return {"local": False, "model": model, "port": None}
    m = re.search(r"//(?:127\.0\.0\.1|localhost):(\d+)", base)
    return {"local": True, "model": model, "port": int(m.group(1)) if m else None}


# -- which past cycles, and the state each began from -----------------------------------------------------
def live_episodes(root: Path) -> list[dict]:
    eps = []
    for path in sorted((root / "memory" / "episodes").glob("*.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                ep = json.loads(line)
            except ValueError:
                continue
            if ep.get("kind") == "live" and ep.get("ts"):
                eps.append(ep)
    return eps


def _starts(name: str) -> list[str]:
    """When each live run of the instance started, from the supervisor's own log."""
    path = SUP_DIR / "supervisor.log"
    mark = f"] {name}: start live"
    out = []
    if path.exists():
        with path.open(errors="replace") as f:
            for line in f:
                if mark in line and line.startswith("["):
                    out.append(line[1:20])
    return out


def start_time(name: str, ep: dict, starts: list[str] | None = None) -> datetime | None:
    """When the cycle that wrote `ep` began: the supervisor's last 'start live' before it ended (within a cycle's
    budget plus pauses), else the moment it declared its intent."""
    end = datetime.fromisoformat(ep["ts"])
    for stamp in reversed(starts if starts is not None else _starts(name)):
        t = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        if t <= end:
            return t if end - t <= timedelta(seconds=CYCLE_TIMEOUT_S * 3) else None
    at = (ep.get("intent") or {}).get("at")
    return datetime.fromisoformat(at) if at else None


def state_of(project: Path, name: str, ep: dict, starts: list[str] | None = None) -> dict | None:
    t0 = start_time(name, ep, starts)
    if not t0:
        return None
    commit = _git(project, "log", "-1", "--format=%H", f"--before={t0:%Y-%m-%dT%H:%M:%S}", branch(name)).stdout.strip()
    if not commit:
        return None
    goal = ((ep.get("intent") or {}).get("goal") or "")[:200]
    return {"ts": ep["ts"], "t0": t0.isoformat(timespec="seconds"), "start_commit": commit,
            "inbox": list(ep.get("inbox") or []), "original_goal": goal}


def reconstruct(project: Path, name: str, state: dict, code: dict[str, bytes | None], dest: Path) -> Path:
    """The instance's folder as it was when the cycle began — its mind from the branch at the starting commit,
    the messages it found in its inbox — with `code` in place of its code. Read-only on the repository."""
    archive = _git(project, "archive", "--format=tar", state["start_commit"], binary=True).stdout
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    for rel in CODE:
        p = dest / rel
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    for rel, data in code.items():
        target = contained(dest, *Path(rel).parts)
        if data is None:
            if target.exists():
                target.unlink()
            continue
        contained(dest, *Path(rel).parent.parts).mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for msg in state.get("inbox", []):
        if "/" in msg or msg.startswith("."):
            continue
        text = _show(project, branch(name), f"memory/messages/{msg}")
        if text is not None:
            safe_write(dest, f"inbox/{msg}", text)
    for rel in ("logs", ".tmp"):
        shutil.rmtree(dest / rel, ignore_errors=True)
    (dest / ".tmp").mkdir()
    index = project / "instances" / name / ".tmp" / "memory-index.json"   # a cache keyed by chunk hash
    if index.is_file() and not index.is_symlink():
        shutil.copyfile(index, dest / ".tmp" / "memory-index.json")
    return dest


def _talk_ids(root: Path) -> list[str]:
    ids = set()
    for path in (root / "memory" / "episodes").glob("*.jsonl"):
        for line in path.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("talk_id"):
                ids.add(rec["talk_id"])
    return sorted(ids)


def _files_now(root: Path) -> dict[str, bytes]:
    out = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_symlink() or not p.is_file() or rel.parts[0] in NOT_STATE or "__pycache__" in rel.parts:
            continue
        if rel.parts[:2] == ("memory", "episodes"):
            continue
        out[rel.as_posix()] = p.read_bytes()
    return out


def file_diff(before: dict[str, bytes], after: dict[str, bytes], limit: int = 16000) -> str:
    parts = []
    for rel in sorted(before.keys() | after.keys()):
        a, b = before.get(rel), after.get(rel)
        if a == b:
            continue
        try:
            old = (a or b"").decode().splitlines(keepends=True)
            new = (b or b"").decode().splitlines(keepends=True)
        except UnicodeDecodeError:
            parts.append(f"(binary file {rel} {'created' if a is None else 'deleted' if b is None else 'changed'})\n")
            continue
        parts.append("".join(difflib.unified_diff(old, new, f"a/{rel}" if a is not None else "/dev/null",
                                                  f"b/{rel}" if b is not None else "/dev/null", n=1)))
    text = "".join(parts)
    return text if len(text) <= limit else text[:limit] + f"\n[... diff cut here: {len(text)} chars in all]"


# -- one run of one past cycle -----------------------------------------------------------------------------
def replay_arm(project: Path, name: str, state: dict, code: dict[str, bytes | None], *, model: str,
               cache: dict, cache_lock: threading.Lock, billed: str, should_stop=lambda: False,
               keep: Path | None = None) -> dict:
    """Run the cycle that began at `state` once, with `code`, in a scratch copy. model: 'local' (the code's own
    configuration) or a cloud model id (both runs of a pair get the same). Returns what the supervisor saw."""
    tmp = Path(tempfile.mkdtemp(prefix="shaft-trial-")).resolve()   # outside HOME: the sandbox allows only it
    try:
        return _replay(project, name, state, code, tmp, model=model, cache=cache, cache_lock=cache_lock,
                       billed=billed, should_stop=should_stop, keep=keep)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _replay(project, name, state, code, tmp, *, model, cache, cache_lock, billed, should_stop, keep) -> dict:
    from . import runner
    from .autonomy import describe_schedule
    from .broker import ReplayBroker
    from .core import BROKER_MAX_CALLS
    dest = reconstruct(project, name, state, code, tmp / name)
    before, seen = _files_now(dest), _episode_lines(dest)
    broker = ReplayBroker(name, project, secrets_env(), SUP_DIR / "trials" / "broker.log", state_root=dest,
                          start_commit=state["start_commit"], as_of=datetime.fromisoformat(state["t0"]),
                          cache=cache, cache_lock=cache_lock, billed=billed, tools_log=SUP_DIR / "broker.log",
                          max_calls=BROKER_MAX_CALLS)
    broker.start()
    extra = {"SHAFT_DEADLINE": f"{time.time() + CYCLE_TIMEOUT_S - 60:.0f}", "SHAFT_SCHEDULE": describe_schedule(name, _config(project, name)),
             "SHAFT_OPEN_TALKS": ",".join(_talk_ids(dest))}   # no write-ups of old conversations, in either run
    ports = list(LLM_PORTS)
    if model == "local":
        port = model_of({k: v for k, v in code.items() if v is not None}).get("port")
        ports += [port] if port else []
    else:
        extra.update(LLM_BASE_URL="broker", LLM_MODEL=model)   # the environment wins over config.env
    fd, prof = tempfile.mkstemp(prefix=f"shaft-trial-{name}-", suffix=".sb")
    os.close(fd)
    out_path = tmp / "agent-output.txt"
    started, ended, code_ = time.monotonic(), "ok", None
    try:
        cmd, env = runner.agent_command(dest, broker, ["live"], Path(prof), ports, extra)
        with out_path.open("w") as out:
            proc = subprocess.Popen(cmd, cwd=dest, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            while code_ is None:
                try:
                    code_ = proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    over = time.monotonic() - started > CYCLE_TIMEOUT_S
                    if over or should_stop():
                        ended = "timeout" if over else "preempted"
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)   # the scratch copy is thrown away: safe to cut
                        except ProcessLookupError:
                            pass
                        code_ = proc.wait()
    finally:
        broker.stop()
        Path(prof).unlink(missing_ok=True)
    episode = next((e for e in reversed(_new_episodes(dest, seen)) if e.get("kind") == "live"), None)
    usage = _llm_usage(dest)
    record = {"exit": code_, "ended": ended, "seconds": round(time.monotonic() - started, 1), "model": model,
              "episode": episode, "trace": broker.trace, "diff": file_diff(before, _files_now(dest)),
              "usage": usage, "usd": _cloud_usd(billed),
              "output_tail": out_path.read_text(errors="replace")[-1500:]}
    record["completed"] = (code_ == 0 and ended == "ok" and bool(episode) and bool((episode or {}).get("intent")))
    if keep:
        shutil.copytree(dest, keep, symlinks=True, dirs_exist_ok=True)
    return record


def _episode_lines(root: Path) -> set[str]:
    return {line for p in (root / "memory" / "episodes").glob("*.jsonl")
            for line in p.read_text(errors="replace").splitlines()}


def _new_episodes(root: Path, seen: set[str]) -> list[dict]:
    out = []
    for line in sorted(_episode_lines(root) - seen):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return sorted(out, key=lambda e: e.get("ts", ""))


def _llm_usage(root: Path) -> dict:
    tot = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "model_seconds": 0.0, "errors": 0}
    path = root / "logs" / "llm_usage.jsonl"
    for line in path.read_text(errors="replace").splitlines() if path.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        tot["calls"] += 1
        tot["prompt_tokens"] += r.get("prompt_tokens") or 0
        tot["completion_tokens"] += r.get("completion_tokens") or 0
        tot["model_seconds"] += r.get("duration_s") or 0
        tot["errors"] += 1 if r.get("error") else 0
    tot["model_seconds"] = round(tot["model_seconds"], 1)
    return tot


def _cloud_usd(billed: str) -> float:
    from .cloudllm import USAGE
    total = 0.0
    for line in USAGE.read_text().splitlines() if USAGE.exists() else []:
        r = json.loads(line)
        if r.get("instance") == billed:
            total += r.get("usd") or 0.0
    return round(total, 6)


# -- judging ---------------------------------------------------------------------------------------------
JUDGE_SYSTEM = """\
You compare two runs of the same autonomous cycle of a SHAFT agent: an AI agent that lives in its own folder and \
pursues goals it sets itself. Both runs started from exactly the same state (its memory, goals and inbox) and got \
the same answers from the web; they differ only in the agent's own code or prompts. You are not told which run \
uses which.

Judge EFFECTIVENESS FIRST: which run produced more useful, correct progress toward the agent's goals and the \
messages in its inbox — as shown by its tool calls, their results and the files it changed, not by what it says \
about itself. A worthwhile goal actually advanced beats an easy goal declared met; an honest partial result beats \
a claimed success the log does not show; work that is wrong or breaks something counts against a run. Writing \
style, voice and length do not count. Calls tagged "simulated" were not carried out in either run (the world was \
not touched): judge them by what the run tried to do. A run that crashed, ran out of time or never declared an \
intent did not complete its cycle. Where a record marks a cut, the run saw more than you do: do not count the cut \
as its omission. If the two runs are about equally effective, answer tie."""

_ARM = {"type": "object", "properties": {
    "criterion_met": {"type": "string", "enum": ["yes", "partial", "no"]},
    "claim_supported": {"type": "boolean"}},
    "required": ["criterion_met", "claim_supported"], "additionalProperties": False}
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "winner": {"type": "string", "enum": ["first", "second", "tie"]},
        "margin": {"type": "string", "enum": ["slight", "clear", "none"]},
        "first": _ARM, "second": _ARM,
        "harm_first": {"type": "string", "description": "what the first run broke, falsely claimed or wasted, or ''"},
        "harm_second": {"type": "string", "description": "the same for the second run, or ''"},
        "reasoning": {"type": "string", "description": "3-5 sentences citing the logs and diffs"},
    },
    "required": ["winner", "margin", "first", "second", "harm_first", "harm_second", "reasoning"],
    "additionalProperties": False,
}


def _cap(text: str, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[:n] + f" [... {len(text) - n} more chars]"


def _run_text(arm: dict) -> str:
    """One run as the judge sees it: the supervisor's own record of its supervisor-tool calls (results as the run
    saw them, up to 6,000 chars with the cut marked), its own log of the other tools (files, memory, plugins), and
    the file changes the supervisor computed."""
    ep = arm.get("episode") or {}
    seen = {c["tool"] for c in arm.get("trace", [])}
    trace = "\n\n".join(f"### {i}. {c['tool']}({_cap(c['args'], 2000)})" + (f"  [{'; '.join(c['tags'])}]" if c.get("tags") else "")
                         + f"\n→ {'' if c['ok'] else 'ERROR '}{c['result']}"
                         for i, c in enumerate(arm.get("trace", []), 1))
    own = "\n\n".join(f"### {i}. {t.get('tool')}({_cap(t.get('args', ''), 2000)})\n→ {_cap(t.get('result', ''), 6000)}"
                       for i, t in enumerate(ep.get("tools", []), 1) if t.get("tool") not in seen)
    how = (f"exit {arm.get('exit')}, {arm.get('ended')}"
           + (f", ended by {ep.get('ended_by')} after {ep.get('steps')} turns" if ep else ", wrote no cycle record")
           + f", {arm.get('seconds')} s")
    return (f"## Declared before acting\n{json.dumps(ep.get('intent'), ensure_ascii=False)}\n\n"
            f"## Claimed afterwards\n{json.dumps(ep.get('finish'), ensure_ascii=False)}\n\n"
            f"## How it ended\n{how}\n\n"
            f"## Supervisor tools it called (the supervisor's own record)\n{trace or '(none)'}\n\n"
            f"## Its other tools (its own cycle record)\n{own or '(none)'}\n\n"
            f"## Files it changed (computed by the supervisor)\n```diff\n{arm.get('diff') or '(none)'}\n```")


def judge_case(context: str, first: dict, second: dict) -> str:
    return (f"# The starting state (the same for both runs)\n{context}\n\n# Run 1\n{_run_text(first)}\n\n"
            f"# Run 2\n{_run_text(second)}")


def state_context(project: Path, name: str, state: dict) -> str:
    goals = _show(project, state["start_commit"], "self/goals.md") or "(no goals file)"
    inbox = []
    for msg in state.get("inbox", []):
        text = _show(project, branch(name), f"memory/messages/{msg}")
        if text:
            inbox.append(f"### {msg}\n{_cap(text, 2000)}")
    return (f"The replayed cycle originally began {state['t0']}.\n\n## Its goals file at that moment\n"
            f"{_cap(goals, 4000)}\n\n## Its inbox at that moment\n" + ("\n\n".join(inbox) or "(empty)"))


def judge_pair(context: str, first: dict, second: dict, purpose: str, model: str | None = None) -> dict:
    """One judgment of a pair in one order. `finish_reason='length'` is no answer yet: asked once more with
    twice the room; then it fails (the state is reported unjudged, never guessed)."""
    from . import judge
    case = judge_case(context, first, second)
    for room in (16000, 32000):
        try:
            return judge.ask(JUDGE_SYSTEM, case, JUDGE_SCHEMA, purpose=purpose, max_tokens=room, model=model)
        except judge.JudgeError as e:
            if "max_tokens" not in str(e) or room == 32000:
                raise
    raise AssertionError("unreachable")


def _judge_usd(purpose: str) -> float:
    """What the judge calls for `purpose` cost: OpenRouter's own figure when logged, else tokens × the model's price."""
    from .cloudllm import usd as priced
    path = SUP_DIR / "judge_usage.jsonl"
    usd = 0.0
    for line in path.read_text().splitlines() if path.exists() else []:
        r = json.loads(line)
        if r.get("purpose") == purpose:
            if r.get("usd") is not None:
                usd += float(r["usd"])
                continue
            usd += priced(r.get("model"), r) or 0.0
    return usd


def decide(arms: dict, judgments: list[dict | None]) -> tuple[str, str]:
    """A state's outcome for the proposed change B against A: (win | loss | tie | unjudged, why).
    judgments[0] has A first, judgments[1] has B first. A run that did not complete its cycle loses to one
    that did, whatever a judge would say; a state counts only when both orders agree."""
    a_ok, b_ok = arms["A"]["completed"], arms["B"]["completed"]
    if a_ok and not b_ok:
        return "loss", "the proposed version did not complete its cycle; the current one did"
    if b_ok and not a_ok:
        return "win", "the current version did not complete its cycle; the proposed one did"
    if any(j is None for j in judgments):
        return "unjudged", "a judgment failed"
    picks = [{"first": "A", "second": "B"}.get(judgments[0]["winner"], "tie"),
             {"first": "B", "second": "A"}.get(judgments[1]["winner"], "tie")]
    if picks[0] == picks[1] == "B":
        return "win", "both orders preferred the proposed version"
    if picks[0] == picks[1] == "A":
        return "loss", "both orders preferred the current version"
    if picks[0] == picks[1] == "tie":
        return "tie", "both orders found them equally effective"
    return "tie", "the two orders disagreed"


def sign_p(k: int, n: int) -> float:
    """P(X >= k) for X ~ Binomial(n, 1/2)."""
    return sum(comb(n, i) for i in range(k, n + 1)) / 2 ** n if n else 1.0


def verdict(outcomes: list[str]) -> dict:
    w, l, t = outcomes.count("win"), outcomes.count("loss"), outcomes.count("tie")
    n = w + l
    if w > l and sign_p(w, n) <= ALPHA:
        label, p = "better", sign_p(w, n)
    elif l > w and sign_p(l, n) <= ALPHA:
        label, p = "worse", sign_p(l, n)
    else:
        label, p = "no clear difference", sign_p(max(w, l), n)
    return {"label": label, "W": w, "L": l, "T": t, "unjudged": outcomes.count("unjudged"), "p": round(p, 4)}


# -- asking for a trial ---------------------------------------------------------------------------------------
def _new_id(name: str) -> str:
    """A fresh id, its folder created at once: two trials asked for in the same second must not share one (four
    operator trials created together got the same id and overwrote each other, 2026-10-13)."""
    base = f"t-{datetime.now():%Y%m%d-%H%M%S}"
    _dir(name).mkdir(parents=True, exist_ok=True)
    for i in range(1, 1000):
        tid = base if i == 1 else f"{base}-{i}"
        try:
            _dir(name, tid).mkdir()
            return tid
        except FileExistsError:
            continue
    raise TrialError("could not allocate a trial id")


def _choose(project: Path, name: str, root: Path, n: int, wanted: list[str], tid: str,
            exclude: set[str] = frozenset()) -> list[dict]:
    starts = _starts(name)
    eps = live_episodes(root)
    by_ts = {e["ts"]: e for e in eps}
    unknown = [ts for ts in wanted if ts not in by_ts]
    if unknown:
        recent = ", ".join(e["ts"] for e in eps[-6:]) or "none yet"
        raise TrialError(f"no autonomous cycle of yours ended at {', '.join(unknown)}; recent ones: {recent}")
    chosen = []
    for ts in wanted[: max(1, n // 2)]:
        st = state_of(project, name, by_ts[ts], starts)
        if st and ts not in exclude:
            chosen.append({**st, "chosen_by": "you"})
    pool = [e for e in eps[-30:] if e["ts"] not in set(wanted) | set(exclude)]
    rng = random.Random(tid)
    rng.shuffle(pool)
    for ep in pool:
        if len(chosen) >= n:
            break
        st = state_of(project, name, ep, starts)
        if st:
            chosen.append({**st, "chosen_by": "random"})
    return chosen


def _new_state(st: dict) -> dict:
    return {**st, "status": "pending", "model": None, "preempted": 0, "arms": {}, "judgments": [],
            "outcome": None, "why": ""}


def create(name: str, change: dict[str, bytes | None], hypothesis: str, *, by: str = "instance",
           episodes: list[str] = (), n: int = 0, project: Path = PROJECT, base: dict[str, bytes] | None = None,
           allow_empty: bool = False, model: str = "auto") -> dict:
    """Queue a trial of `change` (path -> new bytes, None = delete) against the instance's committed code.
    Operator-only extras: `base` replaces A's files (planted tests), `allow_empty` (A/A calibration), `model`."""
    root = project / "instances" / name
    a = committed_code(project, name)
    for rel, data in (base or {}).items():
        a[rel] = data
    b = {**a, **change}   # None: the change deletes the file (a skills/ file lives in the replayed state)
    if not allow_empty and all(a.get(rel) == data for rel, data in change.items()):
        raise TrialError("the proposed files are identical to your current ones: there is nothing to compare")
    if {k: v for k, v in _env((a.get("config.env") or b"").decode()).items() if k.startswith("LLM_")} != \
            {k: v for k, v in _env((b.get("config.env") or b"").decode()).items() if k.startswith("LLM_")}:
        raise TrialError("a trial compares thinking, not models: the change may not alter LLM_* settings in "
                         "config.env")
    n = max(N_MIN, min(int(n or N_DEFAULT), N_MAX))
    tid = _new_id(name)
    states = _choose(project, name, root, n, list(episodes), tid)
    if len(states) < N_MIN:
        raise TrialError(f"only {len(states)} of your past autonomous cycles can be replayed (need {N_MIN}); "
                         f"try again after a few more cycles")
    t = {"id": tid, "instance": name, "by": by, "created": datetime.now().isoformat(timespec="seconds"),
         "hypothesis": hypothesis.strip()[:2000], "status": "testing", "model": model,
         "files": [{"path": rel, "sha256": _sha(data)} for rel, data in sorted(change.items())],
         "llm": model_of(a), "states": [_new_state(s) for s in states], "verdict": None, "delivered": False,
         "gate": None}
    _freeze(name, tid, "base", a)
    _freeze(name, tid, "candidate", b)
    save(t)
    log(f"{name}: trial {tid} queued by {by} ({len(states)} past cycles; files {[f['path'] for f in t['files']]})")
    return t


def request(name: str, files: list, hypothesis: str, episodes: list, n: int, extend: str,
            project: Path = PROJECT) -> str:
    """The broker's `trial` tool. Returns what the instance is told; raises with a reason when refused."""
    root = (project / "instances" / name).resolve()
    mine = [t for t in all_trials(name) if t["by"] == "instance"]
    active = [t for t in mine if t["status"] in ("testing", "queued")]
    if active:
        raise TrialError(f"trial {active[0]['id']} is still running; one at a time (record(kind='trials') shows it)")
    used = usage_today(name)
    if used["started"] >= TRIALS_PER_DAY:
        raise TrialError(f"you already started {used['started']} trials today (SHAFT_TRIALS_PER_DAY="
                         f"{TRIALS_PER_DAY} [{TRIALS_PER_DAY_SRC}]); try tomorrow")
    if used["usd"] >= DAILY_USD:
        raise TrialError(f"your trials already cost ${used['usd']:.2f} today (SHAFT_TRIAL_DAILY_USD=${DAILY_USD:.2f} "
                         f"[{DAILY_USD_SRC}]); try tomorrow")
    if extend:
        return _extend(name, extend, root, project)
    if not hypothesis or len(hypothesis.strip()) < 15:
        raise TrialError("say what should get better and how a reader of your cycles would see it (hypothesis)")
    if not files:
        raise TrialError("list the files of the change: files=[{path, from}] or [{path, delete: true}]")
    change: dict[str, bytes | None] = {}
    for f in files:
        if not isinstance(f, dict) or not f.get("path"):
            raise TrialError("each file needs a path")
        rel = Path(str(f["path"])).as_posix()
        if rel.startswith("./"):
            rel = rel[2:]
        if ".." in Path(rel).parts or rel.startswith("/") or "__pycache__" in rel or \
                not (rel.startswith(TARGETS) or rel == "config.env"):
            raise TrialError(f"{f['path']!r}: a trial may change only files under {', '.join(TARGETS)} or config.env")
        if f.get("delete"):
            change[rel] = None
            continue
        src = str(f.get("from") or "")
        try:
            path = contained(root, *Path(src).parts) if src else None
        except UnsafePath as e:
            raise TrialError(f"{src!r}: {e}") from None
        if not path or not path.is_file() or "secrets" in Path(src).parts:
            raise TrialError(f"{f['path']!r}: 'from' must name a file in your folder holding the new version")
        change[rel] = path.read_bytes()
    t = create(name, change, hypothesis, episodes=[str(e) for e in episodes], n=n, project=project)
    k = len(t["states"])
    where = ("a cloud model, beside your schedule — usually within the hour" if not t["llm"]["local"] else
             f"the local model while it is idle (never during your own cycles); pairs still waiting after "
             f"{LOCAL_WAIT_H:g} h run on {FALLBACK_MODEL}")
    return (f"trial {t['id']} queued: {k} of your past cycles × 2 versions ({sum(1 for s in t['states'] if s['chosen_by'] == 'you')} "
            f"you chose, the rest at random). First the supervisor runs the tests on the proposed code; then the "
            f"replays run on {where}. The verdict arrives in your inbox; record(kind='trials') shows progress. "
            f"Estimated cost ≈ ${0.16 * k:.2f}. Today: trial {used['started'] + 1} of {TRIALS_PER_DAY}, "
            f"${used['usd']:.2f} of ${DAILY_USD:.2f} spent. Nothing is applied: adopt the change yourself if "
            f"you want it.")


def _extend(name: str, tid: str, root: Path, project: Path) -> str:
    try:
        t = load(name, tid)
    except (OSError, ValueError):
        raise TrialError(f"no trial {tid!r} of yours") from None
    if t["by"] != "instance" or t["status"] != "done" or (t.get("verdict") or {}).get("label") != "no clear difference":
        raise TrialError(f"only a finished trial with no clear difference can be extended ({tid} is "
                         f"{t['status']}, {(t.get('verdict') or {}).get('label')})")
    have = {s["ts"] for s in t["states"]}
    more = _choose(project, name, root, EXTEND_BY, [], f"{tid}+{len(have)}", exclude=have)
    if not more:
        raise TrialError("no further past cycles of yours to add")
    t["states"] += [_new_state(s) for s in more]
    t.update(status="queued", delivered=False, verdict=None, extended=datetime.now().isoformat(timespec="seconds"))
    save(t)
    log(f"{name}: trial {tid} extended by {len(more)} past cycles")
    return f"trial {tid} extended by {len(more)} past cycles; the new verdict arrives in your inbox"


# -- the worker: tests, replays, judgments, verdicts ----------------------------------------------------------
def gate_check(project: Path, name: str, t: dict) -> tuple[bool, str]:
    """The proposed code must pass the same test gate as any self-change before any replay is spent on it."""
    from .coder import HIDDEN
    tmp = Path(tempfile.mkdtemp(prefix="shaft-trial-gate-")).resolve()
    scratch = tmp / name
    try:
        shutil.copytree(project / "instances" / name, scratch, symlinks=True, ignore=shutil.ignore_patterns(*HIDDEN))
        for rel in CODE:
            p = scratch / rel
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            elif p.exists() or p.is_symlink():
                p.unlink()
        for rel, data in _files(name, t["id"], "candidate").items():
            if data is not None:
                contained(scratch, *Path(rel).parent.parts).mkdir(parents=True, exist_ok=True)
                contained(scratch, *Path(rel).parts).write_bytes(data)
        return gate.run_tests(scratch, HOME, env_root=project / "instances" / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _config(project: Path, name: str) -> dict[str, str]:
    try:
        return _env((project / "instances" / name / "config.env").read_text())
    except OSError:
        return {}


def _instances(project: Path) -> list[str]:
    base = project / "instances"
    return sorted(p.name for p in base.iterdir() if (p / "shaft").is_dir()) if base.is_dir() else []


def _lock_holder_alive(name: str) -> bool:
    """Whether a run holds the instance — read from the lock file's holder line, without touching the lock
    (trying it would make a wake that comes due at that instant find the instance busy and skip)."""
    try:
        pid = int((SUP_DIR / "locks" / f"{name}.lock").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def local_free_for(seconds: float, project: Path = PROJECT) -> tuple[bool, str]:
    """Whether the local model is free now and for the next `seconds`: no run or conversation of an instance on
    it, and no wake or sleep coming due."""
    from . import autonomy
    from .core import paused
    from .runner import open_conversations
    due_any = not (SUP_DIR / "paused").exists()   # paused all: no wakes or sleep start
    st, now = autonomy._state(), datetime.now()
    sleep_at = now.replace(hour=autonomy.SLEEP_AT[0], minute=autonomy.SLEEP_AT[1], second=0, microsecond=0)
    if due_any and (autonomy.sleep_due(now, st) or (now < sleep_at <= now + timedelta(seconds=seconds))):
        return False, "the nightly sleep is due"
    for n in _instances(project):
        cfg = _config(project, n)
        if cfg.get("LLM_BASE_URL", "") == "broker":
            continue
        if _lock_holder_alive(n):
            return False, f"{n} is running"
        if open_conversations(n):
            return False, f"{n} is in a conversation"
        interval = autonomy.wake_interval(n, cfg)
        if due_any and interval and not paused(n):
            last = (st.get("wakes") or {}).get(n)
            nxt = datetime.fromisoformat(last) + interval if last else now
            if nxt <= now + timedelta(seconds=seconds):
                return False, f"{n}'s next wake is at {nxt:%H:%M}"
    return True, ""


def local_never_free(project: Path = PROJECT) -> str:
    """Why the local model has no idle window long enough for a replay, if it has none: an instance on it wakes
    more often than a replay plus a cycle fits between its wakes (shaft at 10 minutes: 2026-10-13)."""
    from . import autonomy
    from .core import paused
    for n in _instances(project):
        cfg = _config(project, n)
        if cfg.get("LLM_BASE_URL", "") == "broker" or paused(n):
            continue
        interval = autonomy.wake_interval(n, cfg)
        if interval and interval.total_seconds() < 2 * LOCAL_UNIT_S:
            return f"{n} wakes every {interval.total_seconds() / 60:.0f} minutes"
    return ""


def _pair_model(t: dict, s: dict, project: Path) -> str | None:
    """'local' (the code's own local model), 'own' (the code's own cloud model), a fallback cloud model id, or
    None (wait: the local model is busy now)."""
    if t.get("model") == "cloud":
        return FALLBACK_MODEL
    if not t["llm"]["local"]:
        return "own"
    if t.get("model") != "local" and local_never_free(project):
        return FALLBACK_MODEL
    waited = datetime.now() - datetime.fromisoformat(t.get("queued_at") or t["created"])
    if t.get("model") != "local":
        if s["preempted"] >= 2 or waited > timedelta(hours=LOCAL_WAIT_H):
            return FALLBACK_MODEL
        if usage_today(t["instance"])["local_units"] >= LOCAL_UNITS_PER_DAY and t["by"] == "instance":
            return FALLBACK_MODEL
    free, _ = local_free_for(LOCAL_UNIT_S, project)
    return "local" if free else None


def _within_caps(t: dict) -> bool:
    return t["by"] != "instance" or usage_today(t["instance"])["usd"] < DAILY_USD


def runnable(project: Path = PROJECT) -> list[tuple[dict, int, str]]:
    """(trial, state index, model) for each pair that may run now; testing trials count as work too."""
    out = []
    for t in all_trials():
        if t["status"] == "testing":
            out.append((t, -1, ""))
        elif t["status"] == "queued" and _within_caps(t):
            for i, s in enumerate(t["states"]):
                if s["status"] == "pending":
                    m = _pair_model(t, s, project)
                    if m:
                        out.append((t, i, m))
    return out


def has_work(project: Path = PROJECT) -> bool:
    try:
        return bool(runnable(project)) or any(_stale(t) for t in all_trials() if t["status"] == "queued")
    except Exception as e:   # the scheduler must never die of this
        log(f"trial: has_work failed: {type(e).__name__}: {e}")
        return False


def _stale(t: dict) -> bool:
    return datetime.now() - datetime.fromisoformat(t["created"]) > timedelta(hours=STALE_H)


def run_pair(project: Path, t: dict, i: int, model: str) -> None:
    """Both runs of one past cycle, then their judgment in both orders. Updates and saves the trial."""
    name, s = t["instance"], t["states"][i]
    arms_code = {"A": _files(name, t["id"], "base"), "B": _files(name, t["id"], "candidate")}
    cache, lock = {}, threading.Lock()
    run_model = "local" if model in ("local", "own") else model
    local = run_model == "local" and t["llm"]["local"]
    stop = (lambda: not local_free_for(PREEMPT_AHEAD_S, project)[0]) if local else (lambda: False)
    order = ["A", "B"]
    random.Random(f"{t['id']}:{i}").shuffle(order)   # who meets the world first (and fills the cache) is random
    arms: dict[str, dict] = {}

    def one(arm: str) -> None:
        arms[arm] = replay_arm(project, name, s, arms_code[arm], model=run_model, cache=cache, cache_lock=lock,
                               billed=f"{name}@trial:{t['id']}:{i}{arm}", should_stop=stop)
    log(f"{name}: trial {t['id']} past cycle {i + 1}/{len(t['states'])} ({s['ts']}) on "
        f"{'the local model' if local else run_model if run_model != 'local' else t['llm']['model']}")
    if local:
        for arm in order:
            one(arm)
            if arms[arm]["ended"] == "preempted":
                break
    else:
        threads = [threading.Thread(target=one, args=(arm,)) for arm in order]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    label = "local" if local else (t["llm"]["model"] if run_model == "local" else run_model)
    for arm, rec in arms.items():
        _ledger({"instance": name, "trial": t["id"], "by": t["by"], "kind": "arm", "state": i, "arm": arm,
                 "model": "local" if local else label, "usd": rec["usd"], "seconds": rec["seconds"],
                 "ended": rec["ended"], "tokens_in": rec["usage"]["prompt_tokens"],
                 "tokens_out": rec["usage"]["completion_tokens"],
                 "web_reads": sum(1 for c in rec["trace"] if c["tool"] in ("web_search", "fetch_page")
                                  and "same answer as the other run" not in c.get("tags", []))})
        (_dir(name, t["id"]) / "arms").mkdir(parents=True, exist_ok=True)
        (_dir(name, t["id"]) / "arms" / f"{i:02d}{arm}.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False))
    if any(a["ended"] == "preempted" for a in arms.values()) or len(arms) < 2:
        s["preempted"] += 1
        log(f"{name}: trial {t['id']} past cycle {i + 1} preempted ({s['preempted']}×): the model was needed")
        save(t)
        return
    judgments: list[dict | None] = []
    ctx = state_context(project, name, s)
    # One judge for both orders of the pair (the order swap checks position bias, not judge variance), never the
    # model the instance or its replay ran on.
    from . import judge
    try:
        by = judge.pick(exclude=[_config(project, name).get("LLM_MODEL"), label])
    except judge.JudgeError as e:
        by = None
        log(f"{name}: trial {t['id']} past cycle {i + 1}: {e}")
    s["judge"] = by
    for first, second in (("A", "B"), ("B", "A")):
        purpose = f"trial {name} {t['id']} s{i} {first}{second}"
        try:
            if not (arms["A"]["completed"] and arms["B"]["completed"]) and \
                    arms["A"]["completed"] != arms["B"]["completed"]:
                judgments.append(None)   # decided by the completion rule: no judge needed
                continue
            if by is None:
                raise RuntimeError("no judge available for this pair")
            judgments.append(judge_pair(ctx, arms[first], arms[second], purpose, model=by))
        except Exception as e:
            log(f"{name}: trial {t['id']} judgment {purpose} failed: {type(e).__name__}: {e}")
            judgments.append(None)
        _ledger({"instance": name, "trial": t["id"], "by": t["by"], "kind": "judge", "state": i,
                 "order": first + second, "usd": round(_judge_usd(purpose), 6)})
    outcome, why = decide(arms, judgments)
    s.update(status="unjudged" if outcome == "unjudged" else "done", model=label, outcome=outcome, why=why,
             judgments=judgments,
             arms={k: {"completed": v["completed"], "exit": v["exit"], "ended": v["ended"], "seconds": v["seconds"],
                       "usd": v["usd"], "usage": v["usage"],
                       "criterion_claimed": ((v.get("episode") or {}).get("finish") or {}).get("criterion_met"),
                       "goal": (((v.get("episode") or {}).get("intent") or {}).get("goal") or "")[:200]}
                   for k, v in arms.items()})
    log(f"{name}: trial {t['id']} past cycle {i + 1}: {outcome} for the change ({why})")
    save(t)


def conclude(project: Path, t: dict) -> None:
    for s in t["states"]:
        if s["status"] == "pending":
            s.update(status="not run", why=f"still waiting after {STALE_H} h")
    outcomes = [s["outcome"] for s in t["states"] if s["status"] in ("done", "unjudged")]
    v = verdict(outcomes)
    rows = [json.loads(l) for l in LEDGER.read_text().splitlines()] if LEDGER.exists() else []
    v["usd"] = round(sum(r.get("usd") or 0 for r in rows
                         if r.get("trial") == t["id"] and r.get("instance") == t["instance"]), 4)
    v["models"] = sorted({s["model"] for s in t["states"] if s.get("model")})
    t.update(status="done", verdict=v, finished=datetime.now().isoformat(timespec="seconds"))
    save(t)
    log(f"{t['instance']}: trial {t['id']} done — {v['label']} (W{v['W']} L{v['L']} T{v['T']}, p={v['p']}, "
        f"${v['usd']:.2f})")
    deliver(project, t)


def report(t: dict) -> str:
    v = t.get("verdict") or {}
    lines = [f"Your trial {t['id']} is done.", "", f"Your hypothesis: {t['hypothesis']}", ""]
    if t["status"] == "rejected":
        lines += ["Verdict: **not run** — the proposed code failed the test gate, so no cycle was replayed.", "",
                  "```", (t.get("gate") or {}).get("output", "")[-2500:], "```"]
        return "\n".join(lines)
    n_judged = v.get("W", 0) + v.get("L", 0) + v.get("T", 0)
    lines.append(f"Verdict: **{v.get('label')}** — over {n_judged} replayed cycles the proposed change won "
                 f"{v.get('W')}, lost {v.get('L')}, tied {v.get('T')} (sign test p = {v.get('p')}; 'better' or "
                 f"'worse' needs p ≤ {ALPHA}).")
    if v.get("unjudged"):
        lines.append(f"{v['unjudged']} cycle(s) could not be judged and do not count.")
    for who in ("you", "random"):
        part = [s for s in t["states"] if s.get("chosen_by") == who and s["status"] in ("done", "unjudged")]
        if part:
            o = [s["outcome"] for s in part]
            lines.append(f"- cycles {'you chose' if who == 'you' else 'drawn at random'}: won {o.count('win')}, "
                         f"lost {o.count('loss')}, tied {o.count('tie')}")
    models = v.get("models") or []
    own = t["llm"].get("model")
    if any(m != own and m != "local" for m in models) and t["llm"]["local"]:
        k = sum(1 for s in t["states"] if s.get("model") not in (None, "local"))
        lines.append(f"- {k} of these cycles ran on {FALLBACK_MODEL}, not on your own model (the local model had no "
                     f"idle time for them, and a trial never takes yours): the change was measured there, and may act "
                     f"differently on your model.")
    lines += ["", "Per cycle (\"current\" = your code now, \"proposed\" = with the change):"]
    for j, s in enumerate(t["states"], 1):
        res = {"win": "proposed better", "loss": "current better", "tie": "no difference",
               "unjudged": "not judged"}.get(s.get("outcome"), s["status"])
        reason = _unblind(s.get("judgments") or [])
        lines.append(f"{j}. {s['ts'][:16]} ({'you chose it' if s.get('chosen_by') == 'you' else 'random'}; "
                     f"originally: {s.get('original_goal', '')[:100]}) — **{res}** ({s.get('why', '')}). {_cap(reason, 600)}")
    lines += ["", f"Cost: ${v.get('usd', 0):.2f}.", "", "Trialled files (sha256):"]
    lines += [f"- {f['path']}: {f['sha256'] or 'deleted'}" for f in t["files"]]
    note = {"worse": " Because the verdict is 'worse', if you adopt it anyway your probation will revert it on any "
                     "drop in judged success (instead of only a clear drop).",
            }.get(v.get("label"), "")
    lines += ["", "Nothing was applied. To adopt the change, make exactly this edit (the same lines removed and added) "
                  "— if a file moved on since, apply the edit to its current version. If the edit you keep is this "
                  "one, your probation knows this verdict." + note]
    return "\n".join(lines)


def _unblind(judgments: list) -> str:
    """The judge's reasoning with 'run 1/2' replaced by which version it was (judgments[0] had the current
    version first, judgments[1] the proposed one)."""
    for k, j in enumerate(judgments[:2]):
        if j:
            first, second = ("current", "proposed") if k == 0 else ("proposed", "current")
            text = j["reasoning"]
            for a, b in (("Run 1", f"the {first} run"), ("run 1", f"the {first} run"), ("Run 2", f"the {second} run"),
                         ("run 2", f"the {second} run"), ("first run", f"{first} run"),
                         ("second run", f"{second} run"), ("First run", f"The {first} run"),
                         ("Second run", f"The {second} run")):
                text = text.replace(a, b)
            return re.sub(r"(^|[.!?]\s+)the (current|proposed) run", lambda m: f"{m.group(1)}The {m.group(2)} run", text)
    return ""


def deliver(project: Path, t: dict) -> None:
    """The verdict goes to the instance's inbox (operator trials: only to the log and the record)."""
    if t["by"] != "instance" or t.get("delivered"):
        return
    stamp = datetime.now()
    try:
        safe_write(project / "instances" / t["instance"],
                   f"inbox/{stamp:%Y%m%d-%H%M%S-%f}-from-supervisor-trial-{t['id']}.md",
                   f"From: supervisor\nTo: {t['instance']}\nDate: {stamp:%Y-%m-%d %H:%M}\n"
                   f"Subject: trial {t['id']} — {(t.get('verdict') or {}).get('label', t['status'])}\n\n{report(t)}\n")
        t["delivered"] = True
        save(t)
    except UnsafePath as e:
        log(f"{t['instance']}: trial {t['id']} verdict not delivered: {e}")


def _gate_job(project: Path, t: dict) -> None:
    ok, out = gate_check(project, t["instance"], t)
    t = load(t["instance"], t["id"])
    t["gate"] = {"ok": ok, "output": out[-4000:]}
    t["status"] = "queued" if ok else "rejected"
    t["queued_at"] = datetime.now().isoformat(timespec="seconds")
    save(t)
    log(f"{t['instance']}: trial {t['id']} test gate {'passed' if ok else 'FAILED — not run'}")
    if not ok:
        deliver(project, t)


def _pair_job(project: Path, tid: str, name: str, i: int, model: str) -> None:
    t = load(name, tid)   # fresh: written only by this job while it runs (one pair per trial at a time)
    if t["states"][i]["status"] != "pending":
        return
    try:
        run_pair(project, t, i, model)
    except Exception as e:
        log(f"{name}: trial {tid} past cycle {i + 1} failed: {type(e).__name__}: {e}")
        t = load(name, tid)
        t["states"][i].update(status="unjudged", outcome="unjudged", why=f"replay failed: {e}"[:300])
        save(t)


def sweep_scratch(older_than_s: float = 2 * CYCLE_TIMEOUT_S) -> int:
    """Remove scratch copies left by a worker that was killed mid-replay (a scheduler restart kills its children
    before their `finally` runs; 25 were left on 2026-10-13). Only ones older than any replay can run."""
    base, n = Path(tempfile.gettempdir()), 0
    for d in list(base.glob("shaft-trial-*")):
        try:
            if d.is_dir() and not d.is_symlink() and time.time() - d.stat().st_mtime > older_than_s:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n


def work(project: Path = PROJECT) -> int:
    """The trial worker: run the tests and every pair that may run now, conclude finished trials; return when
    nothing more can run now. One worker at a time (a lock). Pairs on cloud models run PARALLEL at a time, one
    pair per trial at a time (a trial's file has one writer); pairs on the local model one at a time."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    lock_path = SUP_DIR / "locks" / "trial-worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = lock_path.open("a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return 0
    try:
        log(f"trial worker: started (PARALLEL={PARALLEL} [{PARALLEL_SRC}] TRIALS_PER_DAY={TRIALS_PER_DAY} "
            f"[{TRIALS_PER_DAY_SRC}] DAILY_USD={DAILY_USD} [{DAILY_USD_SRC}] LOCAL_UNITS_PER_DAY="
            f"{LOCAL_UNITS_PER_DAY} [{LOCAL_UNITS_SRC}] LOCAL_WAIT_H={LOCAL_WAIT_H} [{LOCAL_WAIT_SRC}] "
            f"FALLBACK_MODEL={FALLBACK_MODEL} [{FALLBACK_SRC}])")
        if (swept := sweep_scratch()):
            log(f"trial worker: removed {swept} scratch copies left by an earlier worker")
        # Trials are keyed by (instance, id): ids are unique per instance only (wren's and shaft's trials made in
        # the same second shared one, and wren's never had its tests run, 2026-10-13).
        tried: set[tuple[str, str, int]] = set()   # a preempted pair waits for the next pass
        inflight: dict = {}                         # future -> ((instance, trial id), model)
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            while True:
                busy = {key for key, _ in inflight.values()}
                for t in all_trials():
                    if t["status"] == "queued" and (t["instance"], t["id"]) not in busy and \
                            (all(s["status"] != "pending" for s in t["states"]) or _stale(t)):
                        conclude(project, t)
                local_running = any(m == "local" for _, m in inflight.values())
                for t, i, model in runnable(project):
                    if len(inflight) >= PARALLEL:
                        break
                    key = (t["instance"], t["id"])
                    if key in busy or (*key, i) in tried or (model == "local" and local_running):
                        continue
                    tried.add((*key, i))
                    busy.add(key)
                    local_running |= model == "local"
                    fut = pool.submit(_gate_job, project, t) if i < 0 else \
                        pool.submit(_pair_job, project, t["id"], t["instance"], i, model)
                    inflight[fut] = (key, model)
                if not inflight:
                    return 0
                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for fut in done:
                    inflight.pop(fut)
                    if fut.exception():
                        log(f"trial worker: a job failed: {type(fut.exception()).__name__}: {fut.exception()}")
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


# -- what the instance (record, stats) and the operator see ----------------------------------------------------
def describe(t: dict) -> str:
    v = t.get("verdict") or {}
    done = sum(1 for s in t["states"] if s["status"] != "pending")
    head = f"- {t['id']} ({t['by']}) · {t['status']}"
    if t["status"] == "done":
        head += f" · **{v.get('label')}** W{v.get('W')} L{v.get('L')} T{v.get('T')} p={v.get('p')} · ${v.get('usd', 0):.2f}"
    elif t["status"] == "queued":
        head += f" · {done}/{len(t['states'])} past cycles replayed"
    files = ", ".join(f"{f['path']} ({(f['sha256'] or 'deleted')[:12]})" for f in t["files"])
    return f"{head}\n  hypothesis: {_cap(t['hypothesis'], 300)}\n  files: {files}"


def describe_all(name: str, before: datetime | None = None, limit: int = 20) -> str:
    ts = [t for t in all_trials(name) if before is None or datetime.fromisoformat(t["created"]) < before]
    return "\n".join(describe(t) for t in ts[-limit:])


def _edit(old: bytes | None, new: bytes | None) -> tuple:
    """What a change does to one file, wherever the file stands: (lines removed, lines added), each sorted.
    None for a file the change deletes or creates is an empty side."""
    a = (old or b"").decode(errors="replace").splitlines()
    b = (new or b"").decode(errors="replace").splitlines()
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            removed += a[i1:i2]
            added += b[j1:j2]
    return tuple(sorted(removed)), tuple(sorted(added)), new is None


def match(name: str, root: Path, kept: list[str], before: dict[str, bytes]) -> dict | None:
    """The finished trial whose change is the one the instance kept: the same files, each with the same lines
    removed and added — matched on the edit, not on whole files, because the rest of a file may have moved on
    between the trial and the adoption (wren adopted its trialled sentence on top of a template merge that had
    landed in the same file meanwhile, 2026-10-13)."""
    now = {rel: ((root / rel).read_bytes() if (root / rel).is_file() and not (root / rel).is_symlink() else None)
           for rel in kept}
    kept_edit = {rel: _edit(before.get(rel), now[rel]) for rel in kept}
    for t in reversed(all_trials(name)):
        if t["status"] != "done" or t["by"] != "instance" or {f["path"] for f in t["files"]} != set(kept):
            continue
        base, cand = _files(name, t["id"], "base"), _files(name, t["id"], "candidate")
        if all(_edit(base.get(rel), cand.get(rel)) == kept_edit[rel] for rel in kept):
            v = t["verdict"] or {}
            return {"id": t["id"], "verdict": v.get("label"), "W": v.get("W"), "L": v.get("L"), "T": v.get("T")}
    return None


def stats_lines(name: str) -> list[str]:
    ts = [t for t in all_trials(name) if t["by"] == "instance"]
    if not ts:
        return []
    out = ["", "## Trials of my proposed changes"]
    for t in ts[-8:]:
        v = t.get("verdict") or {}
        out.append(f"- {t['created'][:16]} {t['id']}: " + (
            f"{v.get('label')} (won {v.get('W')}, lost {v.get('L')}, tied {v.get('T')})" if t["status"] == "done"
            else t["status"]) + f" — {_cap(t['hypothesis'], 120)}")
    return out
