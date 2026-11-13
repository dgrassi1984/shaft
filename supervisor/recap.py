"""One line per instance for `shaft top`: what it is doing right now and what it did in the last hour, written on
demand (the overview's `a`) by a small model — SHAFT_RECAP_MODEL, default Claude Haiku 5.5, or any model the
supervisor can call (modelcall: openrouter/…, a priced custom/…, codex/…).

The model reads only the instance's records, never asks the instance: its live status (.tmp/activity.json, through
the panel's snapshot), the cycles and conversations of the last hour (memory/episodes, panel.transcripts), the
broker's log of its supervisor-tool calls (which covers a cycle still running: its episode is written when it
ends), and the messages it sent or received. Each recap is kept in .supervisor/recaps.json with the time it was
written, so the panel shows how old it is and a restarted panel keeps it; a recap that failed says why. Every call
is logged in .supervisor/recap_usage.jsonl and counted in the panel's costs (panel.costs, "recaps").
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime

from . import modelcall
from .core import SUP_DIR, any_model, knob

MODEL, _ = knob(
    "SHAFT_RECAP_MODEL", "claude-haiku-5-5", any_model, "the model that writes the overview's one-line recap of "
    "each instance", group="Models and spend")
STORE = SUP_DIR / "recaps.json"
USAGE = SUP_DIR / "recap_usage.jsonl"
WINDOW_S = 3600                 # "the last hour"
MAX_CONTEXT = 14000             # characters of record per instance: an hour of cycles fits; the oldest goes first
MAX_TOKENS = 4000               # adaptive thinking shares it with a ~60-token answer
SYSTEM = (
    "You write the status line an operator's dashboard shows for one autonomous AI instance, from its records "
    "only, in two short parts. now: what it is doing right now (or idle, sleeping, paused), at most 10 words. "
    "last_hour: the one or two things in the last hour that matter — which task, what came out of it, who it "
    "talked to — at most 15 words; not tool names or counts. Plain and factual, no preamble, no subject (never "
    "its name or 'it'), nothing beyond the records; if the records show nothing in the last hour, say so.")
LAST = "\n\nNow the two parts: now at most 10 words, last_hour at most 15 words."
SHORTEN = ("Shorten this dashboard status line, keeping its two parts and only what matters most: now at most "
           "10 words, last_hour at most 15 words. Keep the facts, drop detail; add nothing.")
LONG = 30                       # words in all: a longer first answer gets the SHORTEN pass
SCHEMA = {"type": "object", "properties": {"now": {"type": "string"}, "last_hour": {"type": "string"}},
          "required": ["now", "last_hour"], "additionalProperties": False}

SECRETS = [(re.compile(r'(?i)("authorization"\s*:\s*")[^"]*'), r"\1[redacted]"),     # the whole header value
           (re.compile(r'(?i)(bearer\s+|(?:api[_-]?key|token|secret|password)["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+'),
            r"\1[redacted]"),
           (re.compile(r"\b(?:eyJ[\w-]{8,}(?:\.[\w-]*)*|gh[pousr]_\w{8,}|sk-[\w-]{8,})"), "[redacted]")]   # JWT, GitHub, keys


def redact(text: str) -> str:
    for pattern, repl in SECRETS:
        text = pattern.sub(repl, text)
    return text


_lock = threading.Lock()
_running: set[str] = set()


class RecapError(Exception):
    pass


# -- what the model reads -----------------------------------------------------------------------------------------
def _since(now: float) -> str:
    return datetime.fromtimestamp(now - WINDOW_S).strftime("%Y-%m-%dT%H:%M:%S")


def _one(text, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _episodes(name: str, since: str) -> list[str]:
    """The window's finished cycles and conversations, oldest first, each a header line and its indented details."""
    from . import panel
    groups = []
    for e in panel.transcripts(name):              # newest first
        if e["kind"] == "conversation":
            turns = [t for t in e["turns"] if t.get("ts", "") >= since]
            if not turns:
                continue
            g = [f"conversation via {e['via'] or 'terminal'} from {e['ts'][11:16]}, {len(e['turns'])} turns in all:"]
            g += [f"  {t.get('ts', '')[11:16]} {'it' if t.get('role') == 'assistant' else t.get('speaker') or 'operator'}: "
                  f"{_one(t.get('content'), 300)}" for t in turns[-6:]]
            groups.append(g)
            continue
        if e["ts"] < since:
            continue
        r = e["record"]
        tools = Counter(c.get("tool") for c in r.get("tools") or [])
        intent, finish = r.get("intent") or {}, r.get("finish") or {}
        g = [f"{e['kind']} ended {e['ts'][11:16]} ({r.get('ended_by') or '?'}): "
             f"goal {_one(intent.get('goal'), 300) or '(none declared)'}"
             + (f" · criterion met: {finish['criterion_met']}" if finish.get("criterion_met") else "")
             + (f" · tools {', '.join(f'{t}×{n}' if n > 1 else str(t) for t, n in tools.most_common(8))}" if tools else "")]
        g += [f"  {key.replace('_', ' ')}: {_one(finish[key], 600)}" for key in ("what_happened", "summary", "next")
              if finish.get(key)]
        if not finish and r.get("content"):
            g.append(f"  text: {_one(r['content'], 600)}")
        groups.append(g)
    return [line for g in groups[::-1] for line in g]


def _broker(name: str, since: str, n: int = 40) -> list[str]:
    """The instance's supervisor-tool calls in the window (model calls left out: they say nothing of the task)."""
    try:
        with (SUP_DIR / "broker.log").open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            lines = f.read().decode(errors="replace").splitlines()[1:]
    except OSError:
        return []
    stamp = since.replace("T", " ")
    mine = [l for l in lines if l[1:20] >= stamp and l[22:].startswith(f"{name} ") and not l[22:].startswith(f"{name} llm ")]
    return [f"{l[12:17]} {_one(redact(l[22 + len(name) + 1:]), 200)}" for l in mine[-n:]]


def _messages(name: str, since: str) -> list[str]:
    from . import messages
    return [f"{m['ts'][11:16]} {m['from']} → {m['to']}: {_one(m['text'], 240)}"
            for m in messages.history() if not m.get("cancelled") and m["ts"] >= since
            and name in (m["from"], m["to"])][-12:]


def context(inst: dict, now: float | None = None) -> str:
    """Everything the model reads about one instance (a snapshot row from dashboard.instance_state)."""
    now = now or time.time()
    since, name = _since(now), inst["name"]
    head = [f"Instance {name}. Time now {datetime.fromtimestamp(now):%H:%M}. Window: since {since[11:16]}.",
            f"State now: {inst['state']}" + (f" since {inst['since']}" if inst.get("since") else ""),
            f"Doing now: {inst.get('doing') or '—'}", f"Current cycle goal: {inst.get('goal') or '—'}"]
    sections = [("Finished cycles and conversations in the window, oldest first", _episodes(name, since)),
                ("Its supervisor-tool calls in the window (a cycle still running included; some come from its scheduled jobs)", _broker(name, since)),
                ("Messages it sent or received in the window", _messages(name, since))]
    body = []
    for title, lines in sections:
        body += [f"\n## {title}"] + (lines or ["(none)"])
    text = redact("\n".join(head + body))     # episodes and conversations too, not only the broker log
    if len(text) > MAX_CONTEXT:                    # keep the head and the newest record
        keep = "\n".join(head)
        text = keep + "\n[… older records cut …]\n" + text[len(text) - (MAX_CONTEXT - len(keep) - 30):]
    return text


# -- the call -----------------------------------------------------------------------------------------------------
def _log(call: modelcall.Call) -> None:
    """One line per model call in recap_usage.jsonl, with its dollars (modelcall's: priced Claude tokens, OpenRouter's
    own cost, a custom/ model's declared price, $0 for plan usage; None when unknown)."""
    usage = call.usage or {}
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": call.model,
           "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
           "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
           "seconds": round(call.seconds, 1), "error": call.error, "usd": call.usd}
    SUP_DIR.mkdir(exist_ok=True)
    with USAGE.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _parts(out: object) -> tuple[str, str]:
    """(now, last_hour) of an answer, each one line; ValueError when either is missing or empty."""
    try:
        now, hour = (" ".join(str(out[k]).split()).rstrip(".") for k in ("now", "last_hour"))
    except (KeyError, TypeError) as e:
        raise ValueError(f"no recap: {e!r}") from e
    if not (now and hour):
        raise ValueError("empty recap")
    return now, hour


def _check(out: object, schema: dict) -> str:
    """modelcall's check off Anthropic: the schema, then both parts present and not empty (else asked again)."""
    if (problem := modelcall.valid(out, schema)):
        return problem
    try:
        _parts(out)
    except ValueError as e:
        return str(e)
    return ""


def _call(system: str, prompt: str) -> tuple[str, str]:
    """One call: (now, last_hour). Raises RecapError with the reason (no key, HTTP error, refusal, length, bad JSON,
    an empty recap). Every attempt is logged once the answer is judged, so a transport success whose recap is unusable
    is logged as a failed recap, with the reason."""
    calls: list[modelcall.Call] = []
    try:
        try:
            answer = modelcall.ask(MODEL, system, [{"role": "user", "content": prompt}], max_tokens=MAX_TOKENS,
                                   effort="low", schema=SCHEMA, purpose="recap", check=_check, log=calls.append)
        except modelcall.ModelCallError as e:   # no answer yet (max_tokens) is never shown as a recap
            raise RecapError(f"{e} (SHAFT_RECAP_MODEL={MODEL})") from e
        try:
            return _parts(json.loads(answer.text))
        except ValueError as e:
            if calls and calls[-1].error is None:
                calls[-1] = calls[-1]._replace(error=f"unusable recap: {e}: {answer.text[:200]}")
            raise RecapError(f"{MODEL} returned no usable recap ({e}): {answer.text[:120]!r}") from e
    finally:
        for call in calls:
            _log(call)


def ask(prompt: str) -> str:
    """The recap line for one instance's records. A first answer longer than LONG words is shortened once by a second
    call that sees only that answer (measured 2026-10-16: on a busy hour Haiku wrote 40-57 words against a 25-word
    instruction); what comes back is shown whole, never cut."""
    now, hour = _call(SYSTEM, prompt + LAST)
    if len(f"{now} {hour}".split()) > LONG:
        now, hour = _call(SHORTEN, json.dumps({"now": now, "last_hour": hour}, ensure_ascii=False))
    return f"{now} · last hour: {hour}"


# -- the store the panel reads --------------------------------------------------------------------------------------
def load() -> dict:
    try:
        return json.loads(STORE.read_text())
    except (OSError, ValueError):
        return {}


def _save(name: str, rec: dict) -> None:
    with _lock:
        all_ = load()
        all_[name] = rec
        SUP_DIR.mkdir(exist_ok=True)
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(all_, ensure_ascii=False, indent=1))
        tmp.replace(STORE)


def write(inst: dict, now: float | None = None) -> dict:
    """Write and store one instance's recap; a failure is stored too (with the reason), never swallowed."""
    now = now or time.time()
    at = datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        rec = {"ts": at, "text": ask(context(inst, now)), "model": MODEL}
    except Exception as e:
        rec = {**load().get(inst["name"], {}), "error": f"{type(e).__name__}: {e}", "error_ts": at}
    _save(inst["name"], rec)
    return rec


def _lock_path(name: str):
    return SUP_DIR / "locks" / f"recap-{name}.lock"


def _claim(name: str) -> int | None:
    """Hold the instance's recap lock (one file lock, across processes: `shaft top`, the web UI and `shaft recap`
    never pay twice for the same recap). The fd to close when done, or None if a recap is being written elsewhere."""
    path = _lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def start(instances: list[dict], wait: bool = False) -> str:
    """Write every instance's recap in the background, one thread each (wait=True: until all are written); what to
    tell the operator. An instance whose recap is already being written — here or in another process — is skipped."""
    todo, held = [], {}
    with _lock:
        for inst in instances:
            if inst["name"] in _running:
                continue
            fd = _claim(inst["name"])
            if fd is None:
                continue
            _running.add(inst["name"])
            held[inst["name"]] = fd
            todo.append(inst)

    def run(inst):
        try:
            write(inst)
        finally:
            with _lock:
                _running.discard(inst["name"])
                os.close(held.pop(inst["name"]))
    threads = [threading.Thread(target=run, args=(inst,), daemon=True, name=f"recap-{inst['name']}") for inst in todo]
    for th in threads:
        th.start()
    if wait:
        for th in threads:
            th.join()
    return (f"recapping {', '.join(i['name'] for i in todo)} with {MODEL}…" if todo
            else "a recap is already being written")


def running(name: str) -> bool:
    """A recap of this instance is being written, in this process or another."""
    with _lock:
        if name in _running:
            return True
    path = _lock_path(name)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def command(names: list[str]) -> int:
    """`shaft recap [names]`: write the recaps now (all instances by default) and print them."""
    from . import dashboard
    from .core import instance_names
    names = names or instance_names()
    unknown = [n for n in names if n not in instance_names()]
    if unknown:
        print(f"no such instance: {', '.join(unknown)}")
        return 1
    print(start([dashboard.instance_state(n) for n in names], wait=True))
    stored, worst = load(), 0
    for n in names:
        r = stored.get(n, {})
        if r.get("error") and r.get("error_ts", "") >= r.get("ts", ""):
            print(f"{n}: recap failed: {r['error']}")
            worst = 1
        elif r.get("text"):
            print(f"{n}: {r['text']}")
    return worst
