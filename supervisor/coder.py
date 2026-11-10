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

from . import cloudllm, ledger
from .core import (HOME, NETWORK_ERRORS, SUP_DIR, TreeTooBig, UnsafePath, claude_model, contained, copy_beneath,
                   dollars, knob, reserved, safe_read, safe_write, secrets_env, walk_beneath)



def _priced_claude(s: str) -> str:
    """A Claude model with a price (cloudllm.PRICES): an unpriced one would be uncapped, and a hand-off refuses it."""
    s = claude_model(s)
    if s not in cloudllm.PRICES:
        raise ValueError(f"{s!r} has no price in cloudllm.PRICES ({', '.join(sorted(cloudllm.PRICES))})")
    return s


CODER_MODEL, _ = knob(
    "SHAFT_CODER_MODEL", "claude-opus-5-5", _priced_claude, "the model an instance hands a coding task to (delegate)",
    group="Models and spend", services=("runs", "api"))
DAILY_USD, _ = knob(
    "SHAFT_CODER_DAILY_USD", "5", dollars, "dollars per day per instance its coding hand-offs may spend",
    group="Models and spend", services=("runs", "api", "web"))
MAX_TURNS = 16
WRITABLE = ("shaft/", "plugins/", "skills/", "tests/")
# secrets: the instance's keys, never to the coder; .venv/.uv: its Python environment, read in place, never copied;
# .browser: its browser profile — its sessions and the passwords of its own accounts. Also what
# trial.gate_check leaves out of its copy. mail/gdrive/gchat: private copies of the operator's files, never to the coder.
HIDDEN = (".git", "logs", ".tmp", "__pycache__", "secrets", ".venv", ".uv", ".browser", "mail", "gdrive", "gchat")
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


def spent_today(instance: str, until: datetime | None = None) -> float:
    if not USAGE.exists():
        return 0.0
    today = (until.date() if until else date.today()).isoformat()
    return sum(usd or 0 for ts, usd in ledger.spend(USAGE, instance)
               if ts.startswith(today) and (until is None or datetime.fromisoformat(ts) < until))


def _hidden(rel: str) -> bool:
    """A path with a HIDDEN folder anywhere on it, by any spelling the case-insensitive volume takes as the same."""
    return bool({p.casefold() for p in rel.split("/")} & {h.casefold() for h in HIDDEN})


def _safe(root: Path, rel: str) -> str:
    """`rel` as a plain path inside the agent's folder and outside HIDDEN. What the coder reads goes to its cloud
    model, so the read itself follows no link anywhere (core.safe_read, walk_beneath): a link the instance planted,
    even one swapped in after this check, is refused, never read through."""
    p = Path(rel)
    if p.is_absolute():
        try:
            p = p.relative_to(root)
        except ValueError:
            raise CoderError(f"{rel!r} is outside the agent's folder") from None
    if ".." in p.parts:
        raise CoderError(f"{rel!r} is outside the agent's folder")
    if _hidden(p.as_posix()) or reserved(root, p.as_posix(), HIDDEN):   # any spelling: `Secrets/` is secrets/
        raise CoderError(f"{rel!r} is not readable")
    return p.as_posix() if p.parts else ""


def _read_only_tool(root: Path, name: str, args: dict) -> str:
    try:
        if name == "read_file":
            return safe_read(root, _safe(root, args["path"]), errors="replace")[:60000]
        base = _safe(root, args.get("path") or ".")
        skip = (lambda rel: _hidden(f"{base}/{rel}")) if base else _hidden
        rows = [f"{base}/{rel}" if base else rel for rel, f in walk_beneath(root, base, skip) if f is not None]
        return "\n".join(rows[:500]) or "(empty)"
    except (CoderError, OSError, KeyError, UnsafePath) as e:
        return f"ERROR: {e}"


def _call(key: str, messages: list) -> dict:
    body = {"model": CODER_MODEL, "max_tokens": 32000, "system": SYSTEM, "messages": messages, "tools": TOOLS,
            "output_config": {"effort": "high"}, "fallbacks": "default",
            "cache_control": cloudllm.CACHE}   # each turn re-reads the files read so far from the cache
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
        except NETWORK_ERRORS as e:
            error = f"network: {e}"
    raise CoderError(f"coder call failed: {error}")


def write(root: Path, instance: str, task: str) -> tuple[list[dict], str, dict]:
    """Run the coder loop. Returns (files, summary, usage). Writes nothing."""
    key = secrets_env().get("ANTHROPIC_API_KEY")
    if not key:
        raise CoderError("no ANTHROPIC_API_KEY in the supervisor's .env")
    if spent_today(instance) >= DAILY_USD:
        raise CoderError(f"{instance} already spent ${spent_today(instance):.2f} on coding today (cap ${DAILY_USD:.2f})")
    if CODER_MODEL not in cloudllm.PRICES:   # unpriced = uncapped: refuse, never bill $0
        raise CoderError(f"SHAFT_CODER_MODEL={CODER_MODEL!r} has no price in cloudllm.PRICES ({sorted(cloudllm.PRICES)})")
    messages = [{"role": "user", "content": f"The agent's task for you:\n\n{task}"}]
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_5m_tokens": 0,
             "cache_write_1h_tokens": 0, "turns": 0, "usd": 0.0}
    started = time.monotonic()
    try:
        for _ in range(MAX_TURNS):
            data = _call(key, messages)
            usage["turns"] += 1
            counts = cloudllm.billed(data.get("usage") or {})
            for k, v in counts.items():
                usage[k] += v
            # a fallback model may have answered: priced as the model that did (an unpriced one as the coder's)
            usage["usd"] += cloudllm.usd(data.get("model") or CODER_MODEL, counts) or cloudllm.usd(CODER_MODEL, counts)
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
        usage["usd"] = round(usage["usd"], 4)
        ledger.record_charge(usage["usd"])
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
        try:
            copy_beneath(root, scratch, _hidden, links=True)   # inside links as links; nothing read through one
        except TreeTooBig as e:                                # fails closed: too big to check is not checked
            return False, f"the instance's folder is too big to copy for the gate: {e}"
        for f in files:
            safe_write(scratch, f["path"], f["content"])
        return gate.run_tests(scratch, HOME, env_root=root)
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
        result["error"] = (f"test gate refused: {test_log.why}; nothing applied" if getattr(test_log, "refused", False)
                           else "the changes failed the test gate; nothing applied")
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
