"""The capability battery: fixed tasks an instance never sees, run by the supervisor at night.

Why: an instance chooses its own goals and success criteria, so its judged success rate saturates (wren
reached 95% on small goals). The battery is a fixed yardstick — the same held-out items, run after run —
graded effectiveness first, and compared with a *control*: a fresh copy of the template on the same model
with an empty mind. Improvement means the instance's scores rise relative to the control's, within one
era (item set × model × judge model); comparisons across eras are never made.

Four categories (PLAN-measurement.md, E3):
  coding    task + hidden tests run by the supervisor (headline: share of hidden tests passed) + judge
  research  questions with stable answers needing primary sources; judged against a frozen key
  recall    questions about the instance's own older journal entries, generated once and frozen
  self      questions whose answers the supervisor computes from its own record at run time

Held out: items live in .supervisor/battery/items/<version>/ (gitignored, never pushed; an instance holds
a GitHub token, so anything in git is readable to it) and are backed up to
~/Library/Application Support/SHAFT/battery-backup/. Every result stores the item set's sha256.

Isolation, whatever the instance's own `exam` code does: its files (tracked and untracked, not ignored)
are snapshotted before each item and restored after; the broker logs to its own file (so `record`
never shows battery calls), cannot send messages or delegate coding, and bills cloud spend as
<name>-battery. A conversation interrupts the battery at once; so does the end of the night window.
"""
from __future__ import annotations

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

from . import judge, metrics, sandbox
from .broker import CALENDAR_TOOLS, MAIL_TOOLS, Broker
from .core import (BROKER_MAX_CALLS, EMBED_PORT, HOME, INSTANCES, LLM_PORTS, PROJECT, PYTHON, SUP_DIR,
                   git_wt, instance_config, instance_root, knob, log, secrets_env)

VERSION, _ = knob("SHAFT_BATTERY_VERSION", "v1", str)
EVERY_DAYS, _ = knob("SHAFT_BATTERY_EVERY_DAYS", "5", int)          # 0 = never scheduled (Daniele, 2026-10-13: 5)
WINDOW_START = (1, 0)                                                # the night window opens at 01:00 ...
WINDOW_MARGIN_MIN = 10                                               # ... and closes 10 min before the sleep
ITEM_TIMEOUT_S, _ = knob("SHAFT_BATTERY_ITEM_TIMEOUT_S", "1800", int)
CONTROL_REF, _ = knob("SHAFT_BATTERY_CONTROL_REF", "template", str)  # the branch a control is made from
RECALL_MIN_AGE_H, _ = knob("SHAFT_BATTERY_RECALL_MIN_AGE_H", "48", int)
RECALL_ITEMS = 6
CATEGORIES = ("coding", "research", "recall", "self")
CONTROLLED = ("coding", "research")          # a fresh mind cannot know an instance's memory or record
EXAM_OFF = {"send_message", "list_instances", "delegate_coding", "secret_set", "secret_delete",
            "decide",   # added 2026-10-14: exams keep the tools they were calibrated with
            "describe_image",   # PLAN-vision: exams keep the tools they were calibrated with
            *MAIL_TOOLS, *CALENDAR_TOOLS}   # the operator's mail and calendars never reach an exam (its broker has
                                            # none either: root= is set)
BACKUP = HOME / "Library" / "Application Support" / "SHAFT" / "battery-backup"


def base() -> Path:
    return SUP_DIR / "battery"


def items_dir() -> Path:
    return base() / "items" / VERSION


def off_path(name: str) -> Path:
    """Present: this instance gets no battery, scheduled or by hand (`shaft battery off|on <name>`).
    Daniele, 2026-10-13: "Kant no battery" (on Opus a run costs about $15)."""
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


# -- the instance's files: snapshot and restore ----------------------------------------
def _files(root: Path) -> list[str]:
    """Every file an exam could change: what git lists (code, skills, untracked files not ignored) and, listed
    from disk, every file under the mind paths — they are not in the project's git (supervisor/mind.py) and the
    instance's .gitignore hides them, so git alone would leave an exam's memory edits in the real folder."""
    from .core import MIND_PATHS
    out = git_wt(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    found = {f for f in out.split("\0") if f}
    for top in MIND_PATHS:   # links are left alone: never followed, and an instance's own link is never removed
        if (root / top).is_symlink():
            continue
        for dirpath, _, filenames in os.walk(root / top):
            found.update((Path(dirpath) / f).relative_to(root).as_posix() for f in filenames
                         if not (Path(dirpath) / f).is_symlink())
    return sorted(found)


def snapshot(root: Path) -> dict[str, bytes]:
    snap = {}
    for rel in _files(root):
        p = root / rel
        if p.is_file() and not p.is_symlink():
            snap[rel] = p.read_bytes()
    return snap


def restore(root: Path, before: dict[str, bytes]) -> list[str]:
    """Put every file back as it was; remove files the item created. Returns what was undone."""
    undone = []
    for rel in _files(root):
        p = root / rel
        if rel not in before:
            if p.is_file() or p.is_symlink():
                p.unlink()
                undone.append(f"+{rel}")
        elif p.is_file() and p.read_bytes() != before[rel]:
            p.unlink()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(before[rel])
            undone.append(f"~{rel}")
    for rel, data in before.items():
        p = root / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            undone.append(f"-{rel}")
    return undone


# -- a control: the template on the same model, with an empty mind ----------------------
def make_control(name: str) -> Path:
    dest = base() / "control" / name
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(PROJECT), "archive", "--format=tar", CONTROL_REF],
                             capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    shutil.copy2(INSTANCES / name / "config.env", dest / "config.env")   # same model and settings
    for d in ("self", "memory", "notes", "inbox"):
        (dest / d).mkdir(exist_ok=True)
    return dest


# -- one exam --------------------------------------------------------------------------
def _exam(label: str, root: Path, broker: Broker, task: dict, until: float | None, interrupt) -> dict:
    """Run the agent's `exam` command on one task in its sandbox. Raises Interrupted."""
    prof = base() / f"{label}.sb"
    prof.write_text(sandbox.profile(root, HOME, LLM_PORTS + [EMBED_PORT, broker.port, broker.egress.port],
                                    env_root=broker.env_root))   # its own Python environment, read-only
    (root / ".tmp").mkdir(exist_ok=True)
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
    proc = subprocess.Popen(cmd, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    proc.stdin.write(json.dumps(task))
    proc.stdin.close()
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    import threading
    readers = [threading.Thread(target=lambda: out_chunks.append(proc.stdout.read()), daemon=True),
               threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)]
    for r in readers:
        r.start()
    try:
        while proc.poll() is None:
            why = interrupt()
            if why or time.time() > deadline + 30:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise Interrupted(why or f"item timed out ({ITEM_TIMEOUT_S}s or the window's end)")
            time.sleep(1)
    finally:
        for r in readers:
            r.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
    out, err = "".join(out_chunks).strip(), "".join(err_chunks).strip()
    last = out.splitlines()[-1] if out else ""
    try:
        res = json.loads(last)
    except ValueError:
        return {"answer": "", "tools": [], "steps": 0, "ended_by": "error",
                "error": f"exit {proc.returncode}: {(err or out)[-800:]}"}
    res["exit"] = proc.returncode
    return res


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


def run_hidden_tests(item: dict, scratch: Path) -> dict:
    """The item's hidden tests against what the exam left in its scratch folder, in a no-network sandbox
    outside the instance. Returns {passed, total, output}."""
    with tempfile.TemporaryDirectory(prefix="shaft-battery-") as tmp:
        work = Path(tmp) / "work"
        if scratch.is_dir():
            shutil.copytree(scratch, work, ignore=shutil.ignore_patterns("__pycache__", ".*"))
        else:
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
    js = metrics.judgments(name)
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
        kept = len([l for l in path.read_text().splitlines() if l.strip()]) if path.exists() else 0
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
    journal = INSTANCES / name / "memory" / "journal.md"
    if not journal.exists():
        return []
    entries = [b.strip() for b in journal.read_text().split("\n---\n") if b.strip().startswith("## ")]
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
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


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
    ctrl_root = None
    scored = 0
    interrupt = _interrupt_check(name, until)
    try:
        if (control or control_only) and any(it["category"] in CONTROLLED for it in items):
            ctrl_root = make_control(name)
        for item in items:
            arms = ([] if control_only else [("instance", root)]) + \
                ([("control", ctrl_root)] if ctrl_root and item["category"] in CONTROLLED else [])
            # One grader for both arms of an item, so instance and control are compared under the same judge.
            grader = judge.pick(exclude=[instance_config(name).get("LLM_MODEL")])
            for arm, arm_root in arms:
                why = interrupt()
                if why:
                    raise Interrupted(why)
                row = _one(name, item, arm, arm_root, e, until, interrupt, grader)
                summary["items"].append({k: row.get(k) for k in ("item", "arm", "status", "score")})
                scored += row["status"] == "scored"
    except Interrupted as stop:
        log(f"{name}: battery stopped: {stop}")
        summary["stopped"] = str(stop)
    finally:
        held.release()   # a no-op when control_only never took it
        if not control_only:
            (root / ".tmp" / "yield").unlink(missing_ok=True)   # a conversation's request to us, not to the next cycle
        if ctrl_root:
            shutil.rmtree(ctrl_root, ignore_errors=True)
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


def _one(name: str, item: dict, arm: str, root: Path, e: dict, until: float | None, interrupt,
         grader: str | None = None) -> dict:
    label = name if arm == "instance" else f"{name}-control"
    scratch_rel = f".tmp/battery/{item['id'].replace('/', '-')}"
    scratch = root / scratch_rel
    shutil.rmtree(scratch, ignore_errors=True)
    if item.get("dir") and (Path(item["dir"]) / "fixture").is_dir():
        shutil.copytree(Path(item["dir"]) / "fixture", scratch)
    task = {"item": item["id"], "prompt": item["prompt"], "max_steps": item.get("max_steps", 12), "scratch": scratch_rel}
    broker = Broker(label, PROJECT, secrets_env(), base() / "broker.log", max_calls=BROKER_MAX_CALLS,
                    root=root, usage_name=f"{label}-battery", network=False)   # exams stay offline, as they always were
    broker.disabled |= EXAM_OFF
    broker.url = broker.start()
    before = snapshot(root) if arm == "instance" else None
    row = {"item": item["id"], "category": item["category"], "arm": arm, "at": datetime.now().isoformat(timespec="seconds"),
           **e}
    t0 = time.monotonic()
    res: dict = {}
    try:
        res = _exam(label, root, broker, task, until, interrupt)
        tests = run_hidden_tests(item, scratch) if item["category"] == "coding" else None
    except Interrupted as stop:
        row.update(status="interrupted", why=str(stop))
        _append(name, row)
        raise
    finally:
        broker.stop()
        if before is not None:
            undone = restore(root, before)
            if undone:
                log(f"{name}: battery item {item['id']} wrote {', '.join(undone[:12])} — undone")
                row["undone"] = undone
        shutil.rmtree(scratch, ignore_errors=True)
    row.update(seconds=round(time.monotonic() - t0), steps=res.get("steps"), ended_by=res.get("ended_by"),
               submitted=res.get("submitted"), answer=(res.get("answer") or "")[:4000],
               broker_calls=broker.counts)
    if res.get("error"):
        row.update(status="error", error=res["error"])
        _append(name, row)
        log(f"{name}: battery item {item['id']} ({arm}) failed to run: {res['error'][:300]}")
        return row
    try:
        t = truth(name, item["truth"]) if item["category"] == "self" else None
        row["truth"] = t
        row["tests"] = tests
        row["grade"] = grade(item, res, t, tests, model=grader)
        row["grader"] = grader
        row["status"] = "scored"
        row["score"] = round(headline(row), 2)
    except judge.JudgeError as err:
        row.update(status="unjudged", error=str(err))   # never guessed: retried as a new item next run
    _append(name, row)
    return row


def _record_run(name: str) -> None:
    st_path = base() / "schedule.json"
    st = json.loads(st_path.read_text()) if st_path.exists() else {}
    st[name] = datetime.now().isoformat(timespec="seconds")
    st_path.parent.mkdir(parents=True, exist_ok=True)
    st_path.write_text(json.dumps(st, indent=1))


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
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
