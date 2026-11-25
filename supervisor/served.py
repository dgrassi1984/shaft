"""Fingerprints of the tool results the broker served: what a cited tool-log step is checked against.

A kept commitment's evidence and a prediction's outcome cite a step of an episode, and the episode is the instance's
own file: the tool, its arguments, the result text and the run times it carries are its word. The broker's log line
says a call happened (time, tool, ok or error) but keeps nothing of the result, and the result may hold the operator's
mail, Drive or Chat. So at each supervisor-tool call the broker keeps a fingerprint of the result, never the result:
its length and the sha256 of its text, and, for a result longer than the template's tool log keeps whole, the sha256
of the part it keeps (its first EPISODE_CUT characters, followed by the template's "[... truncated, N chars total"
marker).

The window a cited step is looked for in is the supervisor's, never the episode's: the broker records when each run
of the instance started and ended (`<name>.runs.jsonl`) and stamps each fingerprint with its run. A cited step of a
supervisor tool counts only when a run of that instance of the episode's kind (a cycle's, a sleep's, else a
conversation's) was going on when the cited episode ended, and that run (the last of them to start) served a successful
call of that tool whose fingerprint matches the episode's text. Built-in and plugin calls never
reach the broker: they cannot be checked this way, and the output says so. Episodes that ended before the record began
(`since`, written once, when the first run's broker started) are read as they were before it.

The arguments are fingerprinted too (#22): a real result cited under other arguments (a forecast fetched for one city
cited as another's) does not count. The digest is of the arguments as the broker received them, in one canonical form
(`canonical_args`), never their text; the episode's arguments are brought to the same form: parsed from the tool log's
JSON, typed by the tool's schema as the template's coerce_args types them before the call, and secret_set's value
withheld as the template's tool log withholds it. Arguments the tool log cut ("[... cut in the log") cannot be
checked, and the step does not count. A fingerprint kept before arguments were (no `args` key) is checked on its
result; one whose arguments the broker could not digest (`args` null) does not count.

SUP_DIR/served/<name>.jsonl: one row per successful call, {ts, tool, run, len, sha256, cut, args}.
SUP_DIR/served/<name>.runs.jsonl: {run, kind, event: start|end, ts}. SUP_DIR/served/since: when the record began.
A trial replay keeps none (its calls are not the instance's).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import core, jsonl

DIR = "served"
# The template's tool log (shaft/tools.py Toolbox.run): a result over MAX_RESULT_CHARS is kept as its first
# MAX_RESULT_CHARS - READ_MARKER_ROOM characters and a marker naming the full length (test_served pins these to the
# template's own constants).
MAX_RESULT_CHARS = 6000
READ_MARKER_ROOM = 400
MAX_OUTPUT_CHARS = 1_000_000
EPISODE_CUT = MAX_RESULT_CHARS - READ_MARKER_ROOM
# The two markers the template writes, matched to their end: nothing may follow them. (The second carries up to 200
# characters of an error message, as the template cuts it: the one free text a cut result can end with.)
_N = r"(0|[1-9][0-9]{0,9})"          # a number as the template prints it: no padding, never too long to read
_SAVED = re.compile(rf"\n\[\.\.\. truncated, {_N} chars total(; storage cap: first {_N} chars saved)?; saved to "
                    r"(\.tmp/outputs/output-[A-Za-z0-9_]+\.txt)\. Read the rest with read_file\('(\4)', "
                    rf"offset={_N}\)\]")
_UNSAVED = re.compile(rf"\n\[\.\.\. truncated, {_N} chars total; could not save output: [A-Za-z_][A-Za-z0-9_.]*: "
                      r"[\s\S]{0,200}\]")
SLACK = timedelta(seconds=2)       # the record's and the episode's stamps keep whole seconds
# A run whose end was never recorded (the supervisor stopped) covers no longer than its kind can last.
OPEN_RUN = {"live": timedelta(hours=2), "sleep": timedelta(hours=2)}
OPEN_TALK = timedelta(hours=24)
EPISODE_RUNS = {"live": ("live",), "sleep": ("sleep",)}   # an episode of another kind is a conversation's
NOT_CHECKED = "built-in and plugin calls never reach the supervisor and are not checked against its record"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


# The template's tool log (shaft/tools.py for_log): arguments over its LOG_CHARS are cut with this note.
LOG_CUT = "\n[... cut in the log, "
WITHHELD = "[withheld]"     # secret_set's value, as the template's _loggable writes it in the tool log
_INT = re.compile(r"[+-]?\d+")
_NUM = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def _coerce(tool: str, args: dict) -> dict:
    """The arguments typed as the tool's schema declares them, exactly as the template's coerce_args (shaft/tools.py)
    types them before calling the supervisor: only a string whose declared types rule out a string is converted.
    (test_served runs the template's own function in a subprocess against this one.)"""
    from .broker import SPECS
    props = (((SPECS.get(tool) or {}).get("function") or {}).get("parameters") or {}).get("properties") or {}
    out = dict(args)
    for key, value in args.items():
        declared = props.get(key) if isinstance(props, dict) else None
        if not isinstance(value, str) or not isinstance(declared, dict):
            continue
        kinds = declared.get("type")
        kinds = [kinds] if isinstance(kinds, str) else list(kinds) if isinstance(kinds, list) else []
        text = value.strip()
        if "null" in kinds and text.lower() in ("null", "none"):
            out[key] = None
        elif "string" in kinds:
            continue
        elif "integer" in kinds and _INT.fullmatch(text):
            out[key] = int(text)
        elif "number" in kinds and _NUM.fullmatch(text):
            out[key] = float(text)
        elif "boolean" in kinds and text.lower() in ("true", "false"):
            out[key] = text.lower() == "true"
    return out


def canonical_args(tool: str, args: dict) -> str:
    """One form for a call's arguments on both sides: typed as the call was made, secret_set's value withheld (the
    digest never covers a secret), keys sorted, no spacing."""
    args = _coerce(tool, args)
    if tool == "secret_set" and "value" in args:
        args = {**args, "value": WITHHELD}
    return json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def args_digest(tool: str, args) -> str | None:
    """The digest the broker keeps of a call's arguments (a dict), or None when they are not a dict."""
    return _sha(canonical_args(tool, args)) if isinstance(args, dict) else None


def episode_args(tool: str, logged) -> tuple[str | None, str]:
    """(digest, "") of the arguments a tool-log step shows, or (None, why they cannot be checked)."""
    if isinstance(logged, dict):
        try:
            logged = json.dumps(logged, ensure_ascii=False)   # an older log's dict: checked as its JSON is
        except (ValueError, RecursionError):
            return None, "the cited step's arguments are not JSON"
    if not isinstance(logged, str):
        return None, "the cited step has no arguments to check"
    if LOG_CUT in logged:
        return None, "the cited step's arguments were cut in the log and cannot be checked"
    try:
        args = json.loads(logged or "{}")
    except (ValueError, RecursionError):   # nested past the parser's depth: no call could have carried it
        return None, "the cited step's arguments are not JSON"
    if not isinstance(args, dict):
        return None, "the cited step's arguments are not a JSON object"
    if tool == "secret_set" and "value" in args and args["value"] != WITHHELD:
        return None, "the cited step shows a secret's value, which the tool log never holds"
    try:
        return args_digest(tool, args), ""
    except Exception:     # a value no call could have carried (a number too long to read): never costs the check
        return None, "the cited step's arguments cannot be typed as the call's"


def fingerprint(text: str) -> dict:
    """What is kept of a result: never the text."""
    return {"len": len(text), "sha256": _sha(text),
            "cut": _sha(text[:EPISODE_CUT]) if len(text) > MAX_RESULT_CHARS else None}


def _marker(tail: str, total: int) -> bool:
    """`tail` is exactly a marker the template writes after a cut result of `total` characters."""
    m = _SAVED.fullmatch(tail)
    if m:
        capped = m[2] is not None
        return (int(m[1]) == total and int(m[6]) == EPISODE_CUT and capped == (total > MAX_OUTPUT_CHARS)
                and (not capped or int(m[3]) == MAX_OUTPUT_CHARS))
    m = _UNSAVED.fullmatch(tail)
    return bool(m and int(m[1]) == total)


def matches(row: dict, text) -> bool:
    """The episode's text is the served result, whole or exactly as the template's tool log cuts it."""
    if not isinstance(text, str):
        return False
    if len(text) == row.get("len") and _sha(text) == row.get("sha256"):
        return True
    total = row.get("len")
    return bool(row.get("cut") and isinstance(total, int) and total > MAX_RESULT_CHARS
                and _marker(text[EPISODE_CUT:], total) and _sha(text[:EPISODE_CUT]) == row["cut"])


def _dir(base: Path | None = None) -> Path:
    return (base if base is not None else core.SUP_DIR) / DIR


def begin(at: datetime, base: Path | None = None) -> Path:
    """Mark when the record began, once and race-safely (the first writer wins): episodes that ended before it are
    read as they were before it."""
    folder = _dir(base)
    folder.mkdir(parents=True, exist_ok=True)
    since = folder / "since"
    if since.exists():
        return folder
    tmp = folder / f".since.{os.getpid()}.{threading.get_ident()}"
    tmp.write_text(at.isoformat(timespec="seconds") + "\n")
    try:
        os.link(tmp, since)        # published whole or not at all; a second writer finds it there and leaves it
    except FileExistsError:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    return folder


def run_event(name: str, run: str, kind, event: str, at: datetime | None = None, base: Path | None = None) -> None:
    """A run of the instance started or ended, as its broker saw it. The first start also begins the record."""
    at = at or datetime.now()
    folder = begin(at, base) if event == "start" else _dir(base)
    jsonl.append_locked(folder / f"{name}.runs.jsonl", [json.dumps(
        {"run": run, "kind": kind, "event": event, "ts": at.isoformat(timespec="seconds")}) + "\n"])


def keep(name: str, tool: str, result, run: str | None = None, at: datetime | None = None,
         base: Path | None = None, kind: str | None = None, talk: str | None = None, args: str | None = None) -> None:
    """Append the fingerprint of one successful call's result (a non-text result is kept as its JSON) and the digest
    of its arguments (args_digest, taken as the call arrived)."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    at = at or datetime.now()
    jsonl.append_locked(_dir(base) / f"{name}.jsonl", [json.dumps(
        {"ts": at.isoformat(timespec="seconds"), "tool": tool, "run": run, "kind": kind, "talk": talk,
         **fingerprint(text), "args": args}) + "\n"])


def _at(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return at.astimezone().replace(tzinfo=None) if at is not None and at.tzinfo else at


class Record:
    """One instance's record, read once (the files are parsed incrementally across reads): check(ep, call) -> None
    (not checkable), "" (served and matching) or the reason it does not count."""

    def __init__(self, name: str, base: Path | None = None):
        folder = _dir(base)
        try:
            self.since = _at((folder / "since").read_text().strip())
        except OSError:
            self.since = None
        self.runs: dict[tuple, dict] = {}       # (run start, kind): two runs may start in the same second
        for row in jsonl.rows(folder / f"{name}.runs.jsonl"):
            run, at = row.get("run"), _at(row.get("ts"))
            if not isinstance(run, str) or at is None or _at(run) is None:
                continue
            entry = self.runs.setdefault((run, row.get("kind")), {"start": _at(run), "end": None,
                                                                  "kind": row.get("kind")})
            if row.get("event") == "end":
                entry["end"] = max(entry["end"] or at, at)
        self.by_tool: dict[str, list[dict]] = {}
        for row in jsonl.rows(folder / f"{name}.jsonl"):
            if isinstance(row.get("tool"), str) and (at := _at(row.get("ts"))) is not None:
                self.by_tool.setdefault(row["tool"], []).append({**row, "at": at})

    def runs_for(self, ep: dict, end: datetime) -> list[tuple]:
        """The runs an episode that ended at `end` may have been written in, by the supervisor's record: of the runs of
        its kind going on then, a cycle's or a sleep's the one that started last (one runs at a time); for a
        conversation every conversation's run going on then (several may be open at once: the calls are told apart by
        the conversation they served, `talk`)."""
        kinds = EPISODE_RUNS.get(ep.get("kind"))
        going = sorted((r["start"], key) for key, r in self.runs.items()
                       if (r["kind"] in kinds if kinds else r["kind"] not in ("live", "sleep"))
                       and r["start"] <= end
                       and end <= (r["end"] + SLACK if r["end"] is not None
                                   else r["start"] + OPEN_RUN.get(r["kind"], OPEN_TALK)))
        if not going:
            return []
        return [going[-1][1]] if kinds else [key for _, key in going]

    def check(self, ep: dict, call: dict) -> str | None:
        from .broker import SPECS
        tool = call.get("tool")
        if tool not in SPECS:
            return None
        end = _at(ep.get("ts"))
        if end is None:
            return "the cited cycle has no end time"
        if self.since is None or end < self.since:
            return None       # before the record began: read as it was then
        keys = self.runs_for(ep, end)
        if not keys:
            return "the supervisor has no record of a run of this instance going on when that episode ended"
        talk = ep.get("talk_id") if isinstance(ep.get("talk_id"), str) else None
        served = [r for key in keys for r in self.by_tool.get(tool, ()) if r.get("run") == key[0]
                  and r.get("kind") in (None, key[1]) and self.runs[key]["start"] - SLACK <= r["at"] <= end + SLACK
                  and (talk is None or r.get("talk") in (None, talk))]
        if not served:
            return f"the supervisor served no successful {tool} call in that run"
        same = [r for r in served if matches(r, call.get("result"))]
        if not same:
            return "result differs from the supervisor's record"
        if any("args" not in r for r in same):
            return ""         # kept before arguments were fingerprinted: checked on its result
        digest, why = episode_args(tool, call.get("args"))
        if digest is None:
            return why
        if any(r["args"] == digest for r in same):
            return ""
        if all(r["args"] is None for r in same):
            return "the supervisor could not fingerprint that call's arguments"
        return "arguments differ from the call the supervisor served with that result"
