"""Hand a coding task to an Opus-class model — the instance decides what, the coder writes it.

`delegate_coding` (a broker tool) runs this: a Claude tool loop that may *read* the instance's
files (read-only, its own folder, never .git) and ends by submitting complete new versions of
code files. The supervisor applies them to a scratch copy of the instance, runs the full test
gate there, and only if it passes writes them into the instance. Allowed targets: code only
(shaft/, plugins/, skills/, tests/) — never the instance's mind or its model settings.

Raw HTTP like the judge (the supervisor stays dependency-free). Budgets: per run and per day.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from .core import HOME, SUP_DIR, UnsafePath, contained, knob, safe_write, secrets_env

CODER_MODEL, _ = knob("SHAFT_CODER_MODEL", "claude-opus-5-5", str)
DAILY_USD, _ = knob("SHAFT_CODER_DAILY_USD", "5", float)
MAX_TURNS = 16
WRITABLE = ("shaft/", "plugins/", "skills/", "tests/")
HIDDEN = (".git", "logs", ".tmp", "__pycache__", "secrets")   # secrets: the instance's keys, never to the coder
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-fable-5-1": (10.0, 50.0)}
USAGE = SUP_DIR / "coder_usage.jsonl"
API = "https://api.anthropic.com/v1/messages"

SYSTEM = """\
You are the coding contractor for a SHAFT instance: an autonomous agent that lives in its own folder \
and may change its own code. It has asked you to do one coding task. You can read its files (read-only) \
and you finish by calling `submit` with the complete new content of every file you change or create.

Rules:
- Only files under shaft/, plugins/, skills/ or tests/ may be changed. Never its mind (self/, memory/), \
never config.env.
- Plain Python 3.12 standard library only; the agent runs sandboxed with no internet except its \
supervisor's broker (`from shaft import broker; broker.call(name, **args)`).
- A plugin is plugins/<name>/plugin.json (name, description, JSON-schema `parameters`) + main.py with \
`run(**params) -> str`.
- Keep changes minimal and focused on the task; match the existing code's style; do not break existing \
behaviour. Your changes are tested by the supervisor's test gate before they are applied; if they fail, \
the agent gets the test output.
- Read the relevant files before writing. Submit complete files, not diffs."""

TOOLS = [
    {"name": "list_files", "description": "List files under a directory of the agent's folder.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}},
    {"name": "read_file", "description": "Read a text file in the agent's folder.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "submit", "description": "Finish: the complete new content of each changed or new file, and a summary.",
     "input_schema": {"type": "object", "properties": {
         "files": {"type": "array", "items": {"type": "object", "properties": {
             "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
         "summary": {"type": "string", "description": "what you changed and why, for the agent's journal"}},
         "required": ["files", "summary"]}},
]


class CoderError(Exception):
    pass


def spent_today(instance: str) -> float:
    if not USAGE.exists():
        return 0.0
    today = date.today().isoformat()
    return sum(r.get("usd") or 0 for r in map(json.loads, USAGE.read_text().splitlines())
               if r["instance"] == instance and r["ts"].startswith(today))


def _safe(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise CoderError(f"{rel!r} is outside the agent's folder")
    if set(p.relative_to(root).parts) & set(HIDDEN):
        raise CoderError(f"{rel!r} is not readable")
    return p


def _read_only_tool(root: Path, name: str, args: dict) -> str:
    try:
        if name == "read_file":
            return _safe(root, args["path"]).read_text(errors="replace")[:60000]
        base = _safe(root, args.get("path") or ".")
        rows = [p.relative_to(root).as_posix() for p in sorted(base.rglob("*"))
                if p.is_file() and not set(p.relative_to(root).parts) & set(HIDDEN)]
        return "\n".join(rows[:500]) or "(empty)"
    except (CoderError, OSError, KeyError) as e:
        return f"ERROR: {e}"


def _call(key: str, messages: list) -> dict:
    body = {"model": CODER_MODEL, "max_tokens": 32000, "system": SYSTEM, "messages": messages, "tools": TOOLS,
            "output_config": {"effort": "high"}, "fallbacks": "default"}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01",
        "anthropic-beta": "server-side-fallback-2026-07-01"})
    error = None
    for wait in (0, 15, 60):
        time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            error = f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}"
            if e.code not in (429, 500, 502, 503, 504, 529):
                break
        except (urllib.error.URLError, OSError) as e:
            error = f"network: {e}"
    raise CoderError(f"coder call failed: {error}")


def write(root: Path, instance: str, task: str) -> tuple[list[dict], str, dict]:
    """Run the coder loop. Returns (files, summary, usage). Writes nothing."""
    key = secrets_env().get("ANTHROPIC_API_KEY")
    if not key:
        raise CoderError("no ANTHROPIC_API_KEY in the supervisor's .env")
    if spent_today(instance) >= DAILY_USD:
        raise CoderError(f"{instance} already spent ${spent_today(instance):.2f} on coding today (cap ${DAILY_USD:.2f})")
    messages = [{"role": "user", "content": f"The agent's task for you:\n\n{task}"}]
    usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    started = time.monotonic()
    try:
        for _ in range(MAX_TURNS):
            data = _call(key, messages)
            usage["turns"] += 1
            for k in ("input_tokens", "output_tokens"):
                usage[k] += (data.get("usage") or {}).get(k) or 0
            if data.get("stop_reason") == "refusal":
                raise CoderError("the coder declined this task")
            if data.get("stop_reason") == "max_tokens":
                raise CoderError("the coder ran out of output room; ask for a smaller change")
            messages.append({"role": "assistant", "content": data["content"]})   # verbatim, thinking included
            calls = [b for b in data["content"] if b.get("type") == "tool_use"]
            submit = next((b for b in calls if b["name"] == "submit"), None)
            if submit:
                return submit["input"].get("files", []), submit["input"].get("summary", ""), usage
            if not calls:
                messages.append({"role": "user", "content": "Call submit with your changes (or with no files and "
                                                            "a summary explaining why not)."})
                continue
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b["id"], "content": _read_only_tool(root, b["name"], b["input"])}
                for b in calls]})
        raise CoderError(f"the coder did not submit within {MAX_TURNS} turns")
    finally:
        pin, pout = PRICES.get(CODER_MODEL, (0, 0))
        usage["usd"] = round((usage["input_tokens"] * pin + usage["output_tokens"] * pout) / 1e6, 4)
        usage["seconds"] = round(time.monotonic() - started, 1)
        USAGE.parent.mkdir(parents=True, exist_ok=True)
        with USAGE.open("a") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "instance": instance,
                                "model": CODER_MODEL, "task": task[:300], **usage}) + "\n")


def check_targets(files: list[dict]) -> list[str]:
    bad = []
    for f in files:
        rel = Path(f.get("path", "")).as_posix().lstrip("./")
        if ".." in Path(rel).parts or not rel.startswith(WRITABLE) or "__pycache__" in rel:
            bad.append(f.get("path", "?"))
    return bad


def test_in_scratch(root: Path, files: list[dict]) -> tuple[bool, str]:
    """Apply the files to a scratch copy of the instance and run the full gate there."""
    from . import gate
    tmp = Path(tempfile.mkdtemp(prefix="shaft-handoff-")).resolve()
    scratch = tmp / root.name
    try:
        shutil.copytree(root, scratch, symlinks=True,   # copy links as links: never pull in what they point to
                        ignore=shutil.ignore_patterns(*HIDDEN))
        for f in files:
            safe_write(scratch, f["path"], f["content"])
        return gate.run_tests(scratch, HOME)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def delegate(root: Path, instance: str, task: str) -> dict:
    files, summary, usage = write(root, instance, task)
    result = {"model": CODER_MODEL, "summary": summary, "files": [f["path"] for f in files],
              "usd": usage["usd"], "applied": False}
    bad = check_targets(files)
    if bad:
        result["error"] = f"the coder tried to change files outside code areas: {bad}; nothing applied"
        return result
    if not files:
        result["error"] = "the coder submitted no files"
        return result
    ok, test_log = test_in_scratch(root, files)
    if not ok:
        result["error"] = "the changes failed the test gate; nothing applied"
        result["test_output"] = test_log[-2500:]
        return result
    try:
        for f in files:
            contained(root, *Path(f["path"]).parts)       # check every target before writing any
        for f in files:
            safe_write(root, f["path"], f["content"])
    except UnsafePath as e:
        result["error"] = f"refused: {e}; nothing applied"
        return result
    result["applied"] = True
    return result
