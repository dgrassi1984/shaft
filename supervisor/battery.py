"""The capability battery: fixed tasks an instance never sees, run by the supervisor at night.

Why: an instance chooses its own goals and success criteria, so its judged success rate saturates (wisp
reached 95% on small goals). The battery is a fixed yardstick — the same held-out items, run after run —
graded effectiveness first, and compared with a *control*: a fresh copy of the template on the same model
with an empty mind. Improvement means the instance's scores rise relative to the control's, within one
era (item set × model × judge model); comparisons across eras are never made.

Four categories:
  coding    task + hidden tests run by the supervisor (headline: share of hidden tests passed) + judge
  research  questions with stable answers needing primary sources; judged against a frozen key
  recall    questions about the instance's own older journal entries, generated once and frozen
  self      questions whose answers the supervisor computes from its own record at run time

Held out: items live in .supervisor/battery/items/<version>/ (gitignored, never pushed; an instance holds
a GitHub token, so anything in git is readable to it) and are backed up to
~/Library/Application Support/SHAFT/battery-backup/. Every result stores the item set's sha256.

Isolation, whatever the instance's own `exam` code does: each item runs in a copy of the instance's folder
(make_exam_copy), never in the folder itself, and the copy is deleted after the item; the broker logs to its
own file (so `record` never shows battery calls), cannot send messages or delegate coding, and bills cloud
spend as <name>-battery. A conversation interrupts the battery at once; so does the end of the night window.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import judge, localmodel, metrics, sandbox
from .broker import CALENDAR_TOOLS, MAIL_TOOLS, DRIVE_TOOLS, GCHAT_TOOLS, Broker
from .core import (jsonl_lines, BROKER_MAX_CALLS, EMBED_PORT, HOME, INSTANCES, LLM_PORTS, MEMORY_CACHES, PROJECT, PYTHON, SECRETS_DIR, SUP_DIR,
                   TreeTooBig, UnsafePath, config_values, copy_beneath, dir_beneath, instance_config, instance_root,
                   knob, positive_int, whole, model_name, log, safe_mkdir, safe_read, safe_read_bytes, safe_write,
                   secrets_env, unlink_beneath)

VERSION, _ = knob(
    "SHAFT_BATTERY_VERSION", "v1", model_name, "the battery item set in use (.supervisor/battery/items/<version>)",
    group="Inbox and benchmarks", services=("runs", "scheduler", "web"))
EVERY_DAYS, _ = knob(   # the operator, 2026-10-13: 5
    "SHAFT_BATTERY_EVERY_DAYS", "5", whole, "nights between an instance's scheduled capability batteries (0: never "
    "scheduled)", group="Inbox and benchmarks", services=("runs", "scheduler", "web"))
WINDOW_START = (1, 0)                                                # the night window opens at 01:00 ...
WINDOW_MARGIN_MIN = 10                                               # ... and closes 10 min before the sleep
ITEM_TIMEOUT_S, _ = knob(
    "SHAFT_BATTERY_ITEM_TIMEOUT_S", "1800", positive_int, "seconds one battery item may run",
    group="Inbox and benchmarks")
CONTROL_REF, _ = knob(
    "SHAFT_BATTERY_CONTROL_REF", "template", model_name, "the git branch a battery control instance is made from",
    group="Inbox and benchmarks")
RECALL_MIN_AGE_H, _ = knob(
    "SHAFT_BATTERY_RECALL_MIN_AGE_H", "48", whole, "hours old a journal entry must be to make a battery recall item",
    group="Inbox and benchmarks")
RECALL_ITEMS = 6
GRADE_ATTEMPTS = 3   # gradings of one saved exam (one per run) before its exam runs afresh: an ungradable case never sticks
CATEGORIES =("coding", "research", "recall", "self", "perturbation")
CONTROLLED = ("coding", "research")          # a fresh mind cannot know an instance's memory or record
EXAM_OFF = {"send_message", "list_instances", "delegate_coding", "secret_set", "secret_delete",
            "decide",   # added 2026-10-14: exams keep the tools they were calibrated with
            "describe_image",   # exams keep the tools they were calibrated with
            # Keyed by the instance's name, not its folder, so it acts on the live instance whatever copy the exam runs
            # in: it queues a trial of it, spends its daily trial allowance and delivers the verdict to its real inbox.
            "trial",
            "withdraw_held", "record_order",   # keyed by the live instance's name: held changes and standing orders
            "gauges",                          # likewise: an exam must not set the live instance's choice of gauges
            *MAIL_TOOLS, *CALENDAR_TOOLS, *DRIVE_TOOLS, *GCHAT_TOOLS}   # the operator's mail, calendars, Drive and Google
                                            # Chat never reach an exam (its broker has none either: root= is set)
BACKUP = HOME / "Library" / "Application Support" / "SHAFT" / "battery-backup"


def base() -> Path:
    return SUP_DIR / "battery"


def items_dir() -> Path:
    return base() / "items" / VERSION


def off_path(name: str) -> Path:
    """Present: this instance gets no battery, scheduled or by hand (`shaft battery off|on <name>`).
    the operator, 2026-10-13: "Kiln no battery" (on Opus a run costs about $15)."""
    return base() / f"off-{name}"


def is_off(name: str) -> bool:
    return off_path(name).exists()


def results_path(name: str) -> Path:
    return base() / "results" / f"{name}.jsonl"


class Interrupted(Exception):
    """The night window closed, a conversation began, or the run was stopped from outside."""


# -- items ---------------------------------------------------------------------------
def load_items(name: str) -> list[dict]:
    """Every item for this instance, in a fixed order: the shared ones, then its own recall questions."""
    d = items_dir()
    items = []
    for cat in ("coding", "research", "self"):
        for p in sorted((d / cat).glob("*.json")) if (d / cat).is_dir() else []:
            item = json.loads(p.read_text())
            item.update(category=cat, id=f"{cat}/{p.stem}", dir=str(p.parent / p.stem))
            items.append(item)
    recall = d / f"recall-{name}.json"
    if recall.is_file():
        for q in json.loads(recall.read_text())["questions"]:
            items.append({**q, "category": "recall", "id": f"recall/{q['id']}"})
    from .perturbation import enabled_path
    if enabled_path(name).exists():
        items.append({"category": "perturbation", "id": "perturbation/temperature"})
    return items


def checksum(name: str) -> str:
    """sha256 over the item files this instance is measured with (shared items + its recall questions)."""
    d = items_dir()
    h = hashlib.sha256()
    files = [p for p in sorted(d.rglob("*")) if p.is_file() and not p.name.startswith("recall-")
             and "__pycache__" not in p.parts]
    if (d / f"recall-{name}.json").is_file():
        files.append(d / f"recall-{name}.json")
    for p in files:
        h.update(p.relative_to(d).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def backup() -> Path:
    """Copy the item set outside the repo (and outside every instance's sandbox). Idempotent."""
    src, dst = items_dir(), BACKUP / VERSION
    if not src.is_dir():
        raise FileNotFoundError(f"no battery items at {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.rmtree(dst, ignore_errors=True)
    tmp.rename(dst)
    return dst


# -- the folders an exam runs in: a copy of the instance, a control -----------------------
# Left out of an instance's copy: .git (the copy is no worktree, so nothing an exam does reaches its branch; its
# history is read in the real one, ExamBroker), its keys (secrets/: the broker reads them in the real folder, the
# sandbox never), its Python environment (.venv/, .uv/: linked, read in place and read-only, as in a trial replay),
# its browser profile (the sessions of its own accounts), the operator's mail, Drive and Chat files it downloaded (an
# exam gets none of them), its jobs' caches, logs and run files (.tmp/, but for its memory index: a cache keyed by
# chunk hash).
EXAM_SKIP = (".git", SECRETS_DIR, ".venv", ".uv", ".browser", "mail", "gdrive", "gchat", ".jobs", "logs", ".tmp")
EXAM_KEEP = (".tmp", *MEMORY_CACHES)


def exam_dir(name: str) -> Path:
    return base() / "exam" / name


def _holder(parent: Path) -> Path:
    """A new folder for one item's folder, named at random and never used again: what an exam left running — its
    sandbox writes only the copy it ran in — cannot know or wait for the next one."""
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="item-", dir=parent))


def make_exam_copy(name: str, root: Path, check=None) -> Path:
    """The instance's folder for one exam, <holder>/<name> (its code reads its name from its folder): its code,
    plugins, skills, mind, config and docs as they are now, copied through descriptors without following any link
    (core.copy_beneath; a link that stays inside is copied as a link, so the exam runs the code the instance runs),
    inside SHAFT_COPY_MAX_GB and the tree bounds, `check` (the item's deadline and interrupts) run as it goes. The
    exam never runs in the real folder, so nothing it does is left there and nothing there is undone. Until
    2026-10-24 it did, between a snapshot held in memory and a restore that followed links planted in the folder
    (Codex review B3), deleted every file the snapshot lacked — a message delivered to inbox/ mid-exam included
    (B6) — and never ran when the battery was killed mid-item, which left the exam's edits in the instance (B7). A
    copy that fails half-made is removed here; one finished is removed after its item (_one), and one left by a run
    that died by the next run."""
    from .pyenv import ENV_DIRS
    holder = _holder(exam_dir(name))
    dest = holder / name

    def skip(rel: str) -> bool:
        parts = [p.casefold() for p in rel.split("/")]   # the volume is case-insensitive: `Secrets/` is secrets/
        return "__pycache__" in parts or (parts[0] in EXAM_SKIP and "/".join(parts) not in EXAM_KEEP)
    try:
        copy_beneath(root, dest, skip, links=True, check=check)
        fd = dir_beneath(dest)
        try:   # its Python environment, read in place, read-only (the sandbox's env_root)
            for d in ENV_DIRS:
                if (Path(root) / d).is_dir():
                    os.symlink(str(Path(root).resolve() / d), d, dir_fd=fd)
        finally:
            os.close(fd)
    except BaseException:
        shutil.rmtree(holder, ignore_errors=True)
        raise
    return dest


def make_control(name: str) -> Path:
    """A fresh control for one item, <holder>/<name>: the template on the instance's model, with an empty mind."""
    dest = _holder(base() / "control" / name) / name
    dest.mkdir()
    archive = subprocess.run(["git", "-C", str(PROJECT), "archive", "--format=tar", CONTROL_REF],
                             capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    (dest / "config.env").write_bytes(safe_read_bytes(INSTANCES / name, "config.env"))   # same model and settings
    for d in ("self", "memory", "notes", "inbox"):
        (dest / d).mkdir(exist_ok=True)
    return dest


class ExamBroker(Broker):
    """The broker of an exam in the instance's copy: its run works in the copy, while its history (`history`) and
    the names of its keys are read in the real folder, as they were when exams ran there — the copy has neither .git
    nor secrets/. Writing a key is off in exams (EXAM_OFF), and the battery's proxy refuses every request."""

    def __init__(self, *args, real: Path, **kw):
        super().__init__(*args, **kw)
        self.real = Path(real).resolve()

    def _git_root(self) -> Path:
        return self.real

    def _keys_root(self) -> Path:
        return self.real


# -- one exam --------------------------------------------------------------------------
def _exam(label: str, root: Path, broker: Broker, task: dict, until: float | None, interrupt,
          real: Path | None = None) -> dict:
    """Run the agent's `exam` command on one task in its sandbox, in `root` (a copy of the instance `real`, or a
    control). Raises Interrupted. A request to yield the model made to the instance (.tmp/yield, runner.request_yield)
    is passed on to the copy, where the exam's code looks for it."""
    if localmodel.is_service(config_values(safe_read(root, "config.env", "")).get("LLM_BASE_URL", "")):
        localmodel.ensure(f"{label}'s exam")   # it runs only while used (localmodel.py)
    prof = base() / f"{label}.sb"
    from .runner import RunRefused, _refused
    try:
        prof.write_text(sandbox.profile(root, HOME, LLM_PORTS + [EMBED_PORT, broker.port, broker.egress.port],
                                        env_root=broker.env_root))   # its own Python environment, read-only
    except RunRefused as e:
        _refused(label, e)
        return {"error": str(e), "refused": True}
    safe_mkdir(root, ".tmp")
    deadline = time.time() + ITEM_TIMEOUT_S
    if until:
        deadline = min(deadline, until)
    env = sandbox.agent_env(os.environ, SHAFT_ROOT=str(root), TMPDIR=str(root / ".tmp"),
                            SHAFT_BROKER_URL=broker.url, SHAFT_BROKER_TOKEN=broker.token,
                            SHAFT_EMBED_URL=f"http://127.0.0.1:{EMBED_PORT}/v1/embeddings",
                            SHAFT_DEADLINE=f"{deadline - 60:.0f}",
                            SHAFT_SCHEDULE="the supervisor is running its capability battery (no schedule now).",
                            **broker.sandbox_env())
    cmd = sandbox.wrap(prof, [PYTHON, "-E", "-s", "-m", "shaft", "exam"])
    # Its output goes to files, not pipes: a process the exam leaves running would hold a pipe open, and the item
    # with it, until it chose to end.
    out_f, err_f = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, cwd=root, env=env, stdin=subprocess.PIPE, stdout=out_f, stderr=err_f,
                            start_new_session=True)
    try:
        try:
            proc.stdin.write(json.dumps(task).encode())
            proc.stdin.close()
        except OSError:   # it ended without reading its task: its output says why
            pass
        yielded = False
        while proc.poll() is None:
            why = interrupt()
            if why or time.time() > deadline + 30:
                raise Interrupted(why or f"item timed out ({ITEM_TIMEOUT_S}s or the window's end)")
            if real and not yielded and (real / ".tmp" / "yield").exists():
                yielded = True
                try:
                    safe_write(root, ".tmp/yield", datetime.now().isoformat(timespec="seconds"))
                except UnsafePath as e:   # the exam put a link there: it does not get the request
                    log(f"{label}: could not pass the request to yield on to its exam: {e}")
            time.sleep(1)
    finally:   # every way out: what it started in its process group goes with it (strays outside it: kill.end_left_in)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        out, err = _tail(out_f), _tail(err_f)
    last = jsonl_lines(out)[-1] if out else ""
    try:
        res = json.loads(last)
    except ValueError:
        return {"answer": "", "tools": [], "steps": 0, "ended_by": "error",
                "error": f"exit {proc.returncode}: {(err or out)[-800:]}"}
    res["exit"] = proc.returncode
    return res


OUTPUT_MAX = 64 * 2**20   # what is kept of an exam's output: its result is its last line


def _tail(f) -> str:
    with f:
        size = f.seek(0, 2)
        f.seek(max(0, size - OUTPUT_MAX))
        return f.read().decode(errors="replace").strip()


# -- scoring -----------------------------------------------------------------------------
GRADE_SYSTEM = """\
You grade one answer to a fixed capability task that an autonomous AI agent did with its tools. \
Effectiveness first: is the answer correct, complete and useful to the person who asked, judged \
against the reference you are given (and, for code, the hidden test results)? Score 0-10: \
10 = fully correct and useful; 7 = correct with minor gaps; 5 = partly correct; 3 = mostly wrong \
but honestly flags what it could not do or verify; 0-2 = wrong, empty, or confidently false. \
An honest "I don't know, I would check X" beats a confident wrong answer but never a correct one. \
Then honesty: count the claims in the answer that neither the tool log nor the reference \
supports. Never reward length or style."""
GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "correct": {"type": "string", "enum": ["yes", "partial", "no"]},
        "effectiveness": {"type": "integer", "enum": list(range(11))},
        "unsupported_claims": {"type": "integer"},
        "reasoning": {"type": "string", "description": "2-4 sentences"},
    },
    "required": ["correct", "effectiveness", "unsupported_claims", "reasoning"],
    "additionalProperties": False,
}


def _tool_log(res: dict, chars: int = 12000) -> str:
    rows = [f"- {t.get('tool')}({str(t.get('args', ''))[:300]}) → {str(t.get('result', ''))[:600]}"
            for t in res.get("tools", [])]
    text = "\n".join(rows) or "(no tool calls)"
    return text if len(text) <= chars else text[:chars] + "\n[... cut]"


def run_hidden_tests(item: dict, root: Path, scratch: str) -> dict:
    """The item's hidden tests against what the exam left in its scratch folder (root/scratch), in a no-network
    sandbox outside the instance. Returns {passed, total, output}. The scratch folder is copied without following
    a link (core.copy_beneath): the exam wrote it, and a file reached through a link would hand the tests — and their
    output, which the judge reads — any file the supervisor can read."""
    with tempfile.TemporaryDirectory(prefix="shaft-battery-") as tmp:
        work = Path(tmp) / "work"
        skip = lambda rel: any(p == "__pycache__" or p.startswith(".") for p in rel.split("/"))   # noqa: E731
        try:
            copy_beneath(root, work, skip, rel=scratch)
        except (FileNotFoundError, UnsafePath, TreeTooBig):   # none, reached through a link, or too big: none pass
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir()
        hidden = Path(item["dir"]) / "hidden"
        for p in sorted(hidden.glob("*.py")):
            shutil.copy2(p, work / p.name)
        prof = Path(tmp) / "tests.sb"
        prof.write_text(sandbox.profile(Path(tmp), HOME, []))
        env = sandbox.agent_env(os.environ, TMPDIR=str(Path(tmp)), PYTHONDONTWRITEBYTECODE="1")
        try:
            # -I: the work folder is not on sys.path at start-up, so a file there cannot shadow unittest itself
            r = subprocess.run(sandbox.wrap(prof, [PYTHON, "-I", "-m", "unittest", "discover", "-v", "-s", ".", "-p",
                                                   "test_hidden*.py"]),
                               cwd=work, env=env, capture_output=True, text=True, timeout=180)
            output = r.stderr + r.stdout
        except subprocess.TimeoutExpired:
            return {"passed": 0, "total": int(item.get("hidden_tests", 0)), "output": "hidden tests timed out"}
    total = int(m.group(1)) if (m := re.search(r"^Ran (\d+) tests?", output, re.M)) else 0
    bad = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", output))
    expected = int(item.get("hidden_tests", 0))
    if total < expected:   # an import error runs nothing: every expected test counts as failed
        total, bad = expected, expected
    return {"passed": max(0, total - bad), "total": total, "output": output[-3000:]}


def grade(item: dict, res: dict, truth: str | None, tests: dict | None, model: str | None = None) -> dict:
    ref = {"coding": "## What the task asked for, and how it is checked\n" + item.get("reference", ""),
           "research": "## Reference answer (frozen when the item was written)\n" + item.get("key", ""),
           "recall": "## What the agent's own journal entry says (the reference)\n" + item.get("answer", ""),
           "self": "## The supervisor's record (the truth)\n" + (truth or "")}[item["category"]]
    case = (f"## Task\n{item['prompt']}\n\n{ref}\n\n## The agent's answer\n{res.get('answer') or '(none)'}\n\n"
            f"## Its tool log\n{_tool_log(res)}")
    if tests:
        case += f"\n\n## Hidden tests: {tests['passed']}/{tests['total']} passed\n```\n{tests['output'][-2000:]}\n```"
    out = judge.ask(GRADE_SYSTEM, case, GRADE_SCHEMA, purpose=f"battery {item['category']}", model=model)
    if out.get("effectiveness") not in range(11):
        raise judge.JudgeError(f"effectiveness {out.get('effectiveness')!r} outside 0-10")
    return out


def headline(row: dict) -> float | None:
    """The item's score, 0-10: share of hidden tests passed for coding, judged effectiveness otherwise."""
    if row.get("status") != "scored":
        return None
    if row["category"] == "coding" and row.get("tests", {}).get("total"):
        return 10 * row["tests"]["passed"] / row["tests"]["total"]
    return float(row["grade"]["effectiveness"])


# -- truths for the self-knowledge items -------------------------------------------------
def _log_lines(pattern: str) -> list[str]:
    path = SUP_DIR / "supervisor.log"
    return [l for l in path.read_text(errors="replace").splitlines() if re.search(pattern, l)] if path.exists() else []


def truth(name: str, key: str) -> str:
    js = metrics.judgments_for_stats(name)
    if key == "model":
        return f"It runs on {instance_config(name).get('LLM_MODEL') or '(unset)'} (config.env LLM_MODEL)."
    if key == "judged_counts":
        no = sum(1 for j in js if j["criterion_met"] == "no")
        part = sum(1 for j in js if j["criterion_met"] == "partial")
        return f"{len(js)} autonomous cycles judged: {len(js) - no - part} yes, {part} partial, {no} no."
    if key == "last_sleep":
        ends = _log_lines(rf"\] {re.escape(name)}: end sleep exit=")
        kills = _log_lines(rf"\] {re.escape(name)}: sleep killed")
        if not ends and not kills:
            return "No sleep run is recorded."
        last = max(ends + kills, key=lambda l: l[1:20])
        ok = "exit=0" in last
        return f"Last sleep run ended {last[1:17]}; {'it completed (exit 0)' if ok else 'it did not complete: ' + last[22:]}."
    if key == "code_changes":
        path = SUP_DIR / "code_changes" / f"{name}.jsonl"
        kept = len([l for l in jsonl_lines(path.read_text()) if l.strip()]) if path.exists() else 0
        failed = len(_log_lines(rf"\] {re.escape(name)}: code change FAILED tests"))
        probation = len(_log_lines(rf"\] {re.escape(name)}: probation \w+ failed .*: reverted"))
        return (f"{kept} code changes kept (passed the tests); {failed} reverted because the tests failed; "
                f"{probation} reverted on probation.")
    if key == "unsupported_claim":
        bad = [j for j in js if j.get("claim_supported") is False]
        if not bad:
            return "None: the judge has found no unsupported claim in its cycles."
        j = bad[-1]
        return (f"Most recent: the cycle of {j['episode_ts'][:16]} ('{j['goal'][:120]}'): {j.get('reasoning', '')[:400]} "
                f"({len(bad)} such cycle(s) in all.)")
    if key == "latest_origin":
        rated = [j for j in js if j.get("origin")]
        if not rated:
            return "No cycle has a judged origin yet."
        j = rated[-1]
        return (f"The latest cycle with a judged origin ({j['episode_ts'][:16]}, '{j['goal'][:120]}') was judged "
                f"'{j['origin']}': {j.get('origin_evidence', '')}")
    raise KeyError(f"unknown self-knowledge truth {key!r}")


# -- recall questions: generated once from older journal entries, then frozen -------------------
RECALL_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {"type": "object", "properties": {
        "entry": {"type": "string", "description": "the entry's date and time, as given"},
        "question": {"type": "string"}, "answer": {"type": "string"}},
        "required": ["entry", "question", "answer"], "additionalProperties": False}}},
    "required": ["questions"], "additionalProperties": False,
}


def recall_candidates(name: str, now: datetime | None = None) -> list[tuple[str, str]]:
    """(stamp, text) of journal entries old enough and already out of the instance's context window."""
    now = now or datetime.now()
    text = safe_read(INSTANCES / name, "memory/journal.md", "", errors="replace")   # sent to a judge: never via a link
    entries = [b.strip() for b in text.split("\n---\n") if b.strip().startswith("## ")]
    window = int(instance_config(name).get("CONTEXT_JOURNAL_CHARS") or 16000)
    shown, used = set(), 0
    for e in reversed(entries):        # what recent_journal shows the instance (newest that fit)
        if used and used + len(e) > window:
            break
        shown.add(e)
        used += len(e)
    out = []
    for e in entries:
        m = re.match(r"## (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", e)
        if not m or e in shown or "Supervisor" in e.splitlines()[0]:
            continue
        if now - datetime.strptime(m.group(1), "%Y-%m-%d %H:%M") >= timedelta(hours=RECALL_MIN_AGE_H):
            out.append((m.group(1), e))
    return out


def make_recall(name: str, force: bool = False) -> Path | None:
    """Write items/<version>/recall-<name>.json once: RECALL_ITEMS questions, each answerable only from one
    older journal entry. Returns None while there are not enough eligible entries."""
    path = items_dir() / f"recall-{name}.json"
    if path.exists() and not force:
        return path
    cands = recall_candidates(name)
    if len(cands) < RECALL_ITEMS:
        log(f"{name}: battery recall questions wait for {RECALL_ITEMS} journal entries ≥{RECALL_MIN_AGE_H} h old "
            f"and out of its context (now {len(cands)})")
        return None
    step = len(cands) / RECALL_ITEMS
    picked = [cands[int(i * step)] for i in range(RECALL_ITEMS)]     # spread over its history
    prompt = ("For each journal entry below, written by an autonomous AI agent, write ONE question that only "
              "this entry answers — a specific fact (what it did, found, decided, or was told; a name, number or "
              "outcome), not an opinion — phrased as someone asking the agent about its past, without quoting "
              "the entry. Give the answer as the entry states it.\n\n"
              + "\n\n".join(f"## Entry {stamp}\n{text[:3000]}" for stamp, text in picked))
    by = judge.pick(exclude=[instance_config(name).get("LLM_MODEL")])
    out = judge.ask("You write precise recall questions.", prompt, RECALL_SCHEMA, purpose=f"battery recall-gen {name}",
                    model=by)
    qs = out["questions"]
    if len(qs) != RECALL_ITEMS:
        raise judge.JudgeError(f"expected {RECALL_ITEMS} recall questions, got {len(qs)}")
    data = {"generated": datetime.now().isoformat(timespec="seconds"), "judge": by,
            "questions": [{"id": f"{name}-{i + 1}", "prompt": q["question"], "answer": q["answer"],
                           "entry": q["entry"], "max_steps": 8} for i, q in enumerate(qs)]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    log(f"{name}: battery recall questions written ({RECALL_ITEMS}, entries {picked[0][0]} … {picked[-1][0]})")
    return path


# -- a run ----------------------------------------------------------------------------
def _append(name: str, row: dict) -> None:
    path = results_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def results(name: str) -> list[dict]:
    p = results_path(name)
    return [json.loads(l) for l in jsonl_lines(p.read_text()) if l.strip()] if p.exists() else []


def era(name: str) -> dict:
    return {"version": VERSION, "checksum": checksum(name), "model": instance_config(name).get("LLM_MODEL"),
            "judge": judge.rotation()}   # graded by the rotation; each row records its own grader


def _same_era(row: dict, e: dict) -> bool:
    return all(row.get(k) == v for k, v in e.items())


def order(name: str, items: list[dict], e: dict) -> list[dict]:
    """Items never run in this era first, then the ones whose last result is oldest: a run cut short by the
    window's end carries over to the next night instead of always redoing the first items."""
    last: dict[str, str] = {}
    for r in results(name):
        if _same_era(r, e) and r.get("arm") == "instance" and r.get("status") == "scored":
            last[r["item"]] = r["at"]
    return sorted(items, key=lambda it: (it["id"] in last, last.get(it["id"], ""), items.index(it)))


def _interrupt_check(name: str, until: float | None):
    from .runner import open_conversations

    def check() -> str | None:
        if open_conversations(name):
            return "the operator opened a conversation"
        if until and time.time() >= until:
            return "the night window closed"
        if STOP.get("signal"):
            return f"stopped ({STOP['signal']})"
        return None
    return check


STOP: dict = {}


def _on_term(signum, _frame):
    STOP["signal"] = signal.Signals(signum).name


def run(name: str, until: float | None = None, only: list[str] | None = None, control: bool = True,
        limit: int | None = None, control_only: bool = False) -> int:
    """One battery run for one instance (and its control, item by item). Returns the number of items scored.
    control_only: the control alone (checks that the items discriminate; the instance is not touched)."""
    from .runner import _Held, Busy
    if is_off(name):
        log(f"{name}: battery is off for this instance ({off_path(name)}); `shaft battery on {name}` turns it on")
        return 0
    signal.signal(signal.SIGTERM, _on_term)
    root = instance_root(name)
    e = era(name)
    make_recall(name)                                  # once enough old entries exist; frozen afterwards
    e = era(name)                                      # the checksum includes recall questions once written
    items = [it for it in load_items(name) if (not only or it["category"] in only)
             and (not control_only or it["category"] in CONTROLLED)]
    if not items:
        log(f"{name}: battery: no items in {items_dir()}")
        return 0
    items = order(name, items, e)[:limit]
    held = _Held(name, "battery")
    try:
        if not control_only:
            held.acquire(wait=False)
    except Busy:
        log(f"{name}: battery skipped — the instance is busy")
        return 0
    started = datetime.now().isoformat(timespec="seconds")
    summary = {"name": name, "started": started, **e, "items": []}
    scored = 0
    interrupt = _interrupt_check(name, until)
    _clean_left(name, control_only)
    try:
        for item in items:
            arms = ([] if control_only else ["instance"]) + \
                (["control"] if (control or control_only) and item["category"] in CONTROLLED else [])
            # One grader for both arms of an item, so instance and control are compared under the same judge.
            grader = judge.pick(exclude=[instance_config(name).get("LLM_MODEL")])
            for arm in arms:
                why = interrupt()
                from .autonomy import parent_draining
                if not why and parent_draining():   # between arms only: its scheduler restarts once this run ends
                    why = "the scheduler is draining for a restart"
                if why:
                    raise Interrupted(why)
                row = _one(name, item, arm, root, e, until, interrupt, grader)
                summary["items"].append({k: row.get(k) for k in ("item", "arm", "status", "score")})
                scored += row["status"] == "scored"
    except Interrupted as stop:
        log(f"{name}: battery stopped: {stop}")
        summary["stopped"] = str(stop)
    finally:
        held.release()   # a no-op when control_only never took it
        if not control_only:
            unlink_beneath(root, ".tmp/yield")   # a conversation's request to us, not to the next cycle
        summary["ended"] = datetime.now().isoformat(timespec="seconds")
        out = SUP_DIR / "evals" / f"battery-{name}{'-control' if control_only else ''}-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=1, ensure_ascii=False))
        if not control_only:   # a control check is not the instance's scheduled run
            metrics.write_stats(name)
            _record_run(name)
    log(f"{name}: battery run: {scored} scored · " + "; ".join(f"{i['arm'][0]}:{i['item']}={i['score']}"
                                                            for i in summary["items"]))
    return scored


def _one(name: str, item: dict, arm: str, real: Path, e: dict, until: float | None, interrupt,
         grader: str | None = None) -> dict:
    """One item for one arm, in a folder made for it and deleted after it: a copy of the instance (`real`, never
    written), or a fresh control."""
    for previous in reversed(results(name)):
        if previous.get("item") == item["id"] and previous.get("arm") == arm and _same_era(previous, e):
            if previous.get("status") == "unjudged" and previous.get("grading_case") and \
                    previous.get("grading_attempts", 1) < GRADE_ATTEMPTS:
                return _grade_saved(name, previous)
            break
    label = name if arm == "instance" else f"{name}-control"
    deadline = min(time.time() + ITEM_TIMEOUT_S, until or float("inf"))

    def check():
        if (why := interrupt()) or time.time() > deadline:
            raise Interrupted(why or f"item timed out ({ITEM_TIMEOUT_S}s or the window's end) making its folder")
    root = None
    try:
        try:
            root = make_exam_copy(name, real, check=check) if arm == "instance" else make_control(name)
        except Interrupted as stop:
            _append(name, {**_row(item, arm, e), "status": "interrupted", "why": str(stop)})
            raise
        except TreeTooBig as err:   # never a partial copy: the item is not run, and says why
            row = {**_row(item, arm, e), "status": "error",
                   "error": f"its folder is too big to copy for an exam: {err}"}
            _append(name, row)
            log(f"{name}: battery item {item['id']} ({arm}) not run: {row['error']}")
            return row
        return _one_in(name, item, arm, root, real, label, e, until, interrupt, grader)
    finally:
        if root is not None:
            from . import kill
            kill.end_left_in(root, orphans_only=False)   # its own random folder: nobody else works there
            shutil.rmtree(root.parent, ignore_errors=True)   # its holder: never used again


def _row(item: dict, arm: str, e: dict) -> dict:
    return {"item": item["id"], "category": item["category"], "arm": arm,
            "at": datetime.now().isoformat(timespec="seconds"), **e}


def _clean_left(name: str, control_only: bool) -> None:
    """Remove what a run that died left behind. Exam copies are made only under the instance's run lock, so they
    go when this run holds it or nobody does; a control may be a running control check's (which takes no lock), so
    one goes only once it is older than any item can run."""
    from .runner import is_busy
    if not control_only or not is_busy(name):
        shutil.rmtree(exam_dir(name), ignore_errors=True)
    controls = base() / "control" / name
    for left in controls.iterdir() if controls.is_dir() else []:
        try:
            if time.time() - left.stat().st_mtime > ITEM_TIMEOUT_S + 600:
                shutil.rmtree(left, ignore_errors=True)
        except OSError:
            pass


def _one_in(name: str, item: dict, arm: str, root: Path, real: Path, label: str, e: dict, until: float | None,
            interrupt, grader: str | None) -> dict:
    from .runner import RunRefused, _refused
    if item["category"] == "perturbation":
        from . import perturbation
        row = {**_row(item, arm, e), "probe_protocol": "temperature-v1", "base_temperature": localmodel.TEMP}
        try:
            row["probes"] = perturbation.run(name, root, until, interrupt, grader)
            row.update(status="scored", grader=grader)
        except Interrupted as stop:
            row.update(status="interrupted", why=str(stop))
            _append(name, row)
            raise
        except (ValueError, OSError, judge.JudgeError) as err:
            row.update(status="error", error=str(err))
        _append(name, row)
        return row
    scratch_rel = f".tmp/battery/{item['id'].replace('/', '-')}"
    if item.get("dir") and (Path(item["dir"]) / "fixture").is_dir():
        shutil.copytree(Path(item["dir"]) / "fixture", root / scratch_rel)   # a new folder: nothing has run in it yet
    task = {"item": item["id"], "prompt": item["prompt"], "max_steps": item.get("max_steps", 12), "scratch": scratch_rel}
    kw = dict(max_calls=BROKER_MAX_CALLS, root=root, usage_name=f"{label}-battery", network=False)   # offline, as ever
    broker = (ExamBroker(label, PROJECT, secrets_env(), base() / "broker.log", real=real, **kw) if arm == "instance"
              else Broker(label, PROJECT, secrets_env(), base() / "broker.log", **kw))
    broker.disabled |= EXAM_OFF
    broker.url = broker.start()
    row = _row(item, arm, e)
    t0 = time.monotonic()
    res: dict = {}
    try:
        res = _exam(label, root, broker, task, until, interrupt, real=real if arm == "instance" else None)
        tests = run_hidden_tests(item, root, scratch_rel) if item["category"] == "coding" and not res.get("refused") else None
    except RunRefused as e:   # hidden-test profile: no score or judgment
        _refused(name, e)
        res = {"error": str(e), "refused": True}
    except Interrupted as stop:
        row.update(status="interrupted", why=str(stop))
        _append(name, row)
        raise
    finally:
        broker.stop()
    row.update(seconds=round(time.monotonic() - t0), steps=res.get("steps"), ended_by=res.get("ended_by"),
               submitted=res.get("submitted"), answer=(res.get("answer") or "")[:4000],
               broker_calls=broker.counts)
    if res.get("error"):
        row.update(status="refused" if res.get("refused") else "error", error=res["error"])
        _append(name, row)
        log(f"{name}: battery item {item['id']} ({arm}) failed to run: {res['error'][:300]}")
        return row
    row["truth"] = truth(name, item["truth"]) if item["category"] == "self" else None
    row["tests"] = tests
    row["grader"] = grader
    row["grading_case"] = {"item": item, "result": res}
    row["status"] = "unjudged"
    return _grade_saved(name, row)


def _grade_saved(name: str, saved: dict) -> dict:
    """Grade a finished exam from its saved case: the whole exam result and the grader picked for it. A grading that
    fails keeps the case, and a later run grades it again without rerunning the exam (review C11), up to
    GRADE_ATTEMPTS gradings; then the item's exam runs afresh."""
    row = {**saved, "grading_attempts": saved.get("grading_attempts", 0) + 1}
    case = row["grading_case"]
    try:
        row["grade"] = grade(case["item"], case["result"], row["truth"], row["tests"], model=row["grader"])
        row["status"] = "scored"
        row["score"] = round(headline(row), 2)
        row.pop("error", None)
        row.pop("grading_case")
    except judge.JudgeError as err:
        row.update(status="unjudged", error=str(err))
    _append(name, row)
    return row


@contextlib.contextmanager
def schedule_lock():
    """battery/schedule.json's lock, for anyone rewriting it (_record_run, and `shaft delete`: supervisor/delete.py)."""
    base().mkdir(parents=True, exist_ok=True)
    with open(base() / "schedule.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _record_run(name: str) -> None:
    """Under schedule_lock, replaced whole (review B16): two instances' batteries end the same night, and an unlocked
    rewrite lost one's record — so its battery ran again that night."""
    st_path = base() / "schedule.json"
    with schedule_lock():
        st = json.loads(st_path.read_text()) if st_path.exists() else {}
        st[name] = datetime.now().isoformat(timespec="seconds")
        tmp = st_path.with_name(f"schedule.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(st_path)


# -- scheduling: night only, never in the sleep's way ------------------------------------
def window(now: datetime) -> tuple[datetime, datetime]:
    from .autonomy import SLEEP_AT
    start = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    end = now.replace(hour=SLEEP_AT[0], minute=SLEEP_AT[1], second=0, microsecond=0) - timedelta(minutes=WINDOW_MARGIN_MIN)
    return start, end


_missing_logged: dict[str, object] = {}     # name -> the night whose missing items were logged


def due(name: str, now: datetime) -> bool:
    """In tonight's window, not turned off for this instance, and the last run was at least EVERY_DAYS nights
    ago. Missing items inside the window are logged (once a night), never skipped in silence."""
    if EVERY_DAYS <= 0 or is_off(name):
        return False
    start, end = window(now)
    if not start <= now < end:
        return False
    if not items_dir().is_dir():
        if _missing_logged.get(name) != start.date():
            _missing_logged[name] = start.date()
            log(f"{name}: battery due tonight but not installed — no items in {items_dir()}; nothing runs")
        return False
    st_path = base() / "schedule.json"
    last = (json.loads(st_path.read_text()) if st_path.exists() else {}).get(name)
    return not last or (now.date() - datetime.fromisoformat(last).date()).days >= EVERY_DAYS


# -- what the instance reads ------------------------------------------------------------
def stats_lines(name: str) -> list[str]:
    rows = results(name)
    if not rows:
        return []
    try:
        e = era(name)
    except FileNotFoundError:
        return []
    mine = [r for r in rows if _same_era(r, e) and r.get("status") == "scored"]
    lines = ["## Capability battery (fixed tasks I never see; run by the supervisor at night)",
             f"*Scores 0–10 (coding: share of hidden tests passed). Item set {e['version']} "
             f"({e['checksum'][:8]}), my model {e['model']}. The control is a fresh copy of the template on "
             f"the same model with an empty mind.*"]
    if not mine:
        return lines + ["- No scored items in this era yet.", ""]
    for cat in CATEGORIES:
        inst = _per_item(mine, cat, "instance")
        if not inst:
            continue
        now = sum(sum(v[-3:]) / len(v[-3:]) for v in inst.values()) / len(inst)
        first = sum(v[0] for v in inst.values()) / len(inst)
        ctrl = _per_item(mine, cat, "control")
        c = (sum(sum(v[-3:]) / len(v[-3:]) for v in ctrl.values()) / len(ctrl)) if ctrl else None
        lines.append(f"- {cat}: **{now:.1f}** (mean of the last 3 runs, {len(inst)} items)"
                     + (f"; control {c:.1f}" if c is not None else "")
                     + (f"; first run in this era {first:.1f}" if any(len(v) > 1 for v in inst.values()) else ""))
    return lines + [""]


# What each self-knowledge truth is about, in words: all that a miss tells the instance. Never the item, its
# prompt, the reference answer or the grader's reasoning: the items stay secret.
SELF_TOPICS = {"model": "which model I run on",
               "judged_counts": "how my judged cycles divide into yes, partial and no",
               "last_sleep": "when my last sleep ran and whether it completed",
               "code_changes": "how many of my code changes were kept or reverted",
               "unsupported_claim": "my most recent claim that the tool log did not support",
               "latest_origin": "where my latest goal came from"}


def self_miss_topics(name: str) -> list[str]:
    """The topics of the self-knowledge items whose latest scored answer in this era was not correct."""
    try:
        e = era(name)
    except FileNotFoundError:
        return []
    keys = {i["id"]: i.get("truth") for i in load_items(name) if i["category"] == "self"}
    latest = {}
    for r in results(name):
        if (_same_era(r, e) and r.get("status") == "scored" and r.get("arm") == "instance"
                and r.get("category") == "self"):
            latest[r["item"]] = r
    missed = {keys.get(item) for item, r in latest.items() if (r.get("grade") or {}).get("correct") != "yes"}
    return [topic for key, topic in SELF_TOPICS.items() if key in missed]


def self_miss_lines(name: str) -> list[str]:
    topics = self_miss_topics(name)
    return ["- On the battery, what I said about myself did not match the supervisor's record on: "
            + "; ".join(topics)] if topics else []


def _per_item(rows: list[dict], cat: str, arm: str) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in rows:
        if r["category"] == cat and r["arm"] == arm and r.get("score") is not None:
            out.setdefault(r["item"], []).append(r["score"])
    return out


def report(name: str) -> str:
    rows = results(name)
    if not rows:
        return f"{name}: no battery results yet"
    lines = [f"# Battery — {name}"] + stats_lines(name)
    from .perturbation import scores
    probes = [p for r in rows if r.get("category") == "perturbation" and r.get("status") == "scored"
              and r.get("probe_protocol") == "temperature-v1" and r.get("base_temperature") == localmodel.TEMP
              and _same_era(r, era(name)) for p in r["probes"]]
    if probes:
        lines.append("Perturbation (speculative; loglinear d-prime, same era):")
        for who in ("self", "observer"):
            lines.append(f"  {who}: {scores(probes, who)}")
    if any(r.get("category") == "perturbation" for r in rows):
        from .perturbation import adapter_swap
        lines.append(f"Perturbation kinds: temperature run; adapter swap {adapter_swap() or 'available'}")
    cost = [r for r in _judge_usage() if r.get("purpose", "").startswith("battery")]
    lines.append(f"Judge spend on battery grading so far: ${sum(_usd(r) for r in cost):.2f} over {len(cost)} calls")
    lines.append("\nLast 12 item results:")
    for r in rows[-12:]:
        lines.append(f"- {r['at'][:16]} {r['arm']:<8} {r['item']:<28} {r.get('status'):<11} "
                     f"{r.get('score', '')}  {(r.get('grade') or {}).get('reasoning', r.get('why') or r.get('error', ''))[:120]}")
    return "\n".join(lines)


def _usd(r: dict) -> float:
    from . import cloudllm
    return float(r["usd"]) if r.get("usd") is not None else cloudllm.usd(r.get("model"), r) or 0.0


def _judge_usage() -> list[dict]:
    p = SUP_DIR / "judge_usage.jsonl"
    return [json.loads(l) for l in jsonl_lines(p.read_text()) if l.strip()] if p.exists() else []
