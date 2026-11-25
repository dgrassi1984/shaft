"""OpenAI-compatible API for the instances — chat with them from any client on your tailnet.

  GET  /v1/models              each instance is a "model" (shaft, wisp, …)
  POST /v1/chat/completions    stream or not; the instance answers as itself, with its tools; X-Shaft-Request-Id
                               (non-streamed): asked again, the same answer, not run again (Answered)
  POST /v1/chat/interject      a message for the conversation whose reply is still running: the instance reads it at
                               its next step and its reply covers it (202), or 409 when no reply is running
  POST /v1/chat/end            the conversation is over: write-up requested (202), 404 if none is open, 409 while it replies
  POST /v1/inbox/<instance>    the doorbell: drop a message in its inbox (read at its next wake), and
                               optionally wake it now — for scripts, a phone shortcut, CI on the tailnet

Each request is a normal sandboxed run (broker, run lock, gate, commit); a spoken one, a turn of a warm run (below). The client's conversation
is the dialogue; the instance's own identity and memory come first. A conversation (same instance,
same first user message — or, if the client sends X-Shaft-Conversation, that name: the web UI does) keeps one id, and
the channel the client names in X-Shaft-Channel (web, telegram; `api` when absent) — where a message the instance sends
the operator while it is open alerts them (channel.py); after IDLE_MINUTES (SHAFT_IDLE_MINUTES) without a message
(counted from when its last reply ended, never while one runs) it is closed and a journal write-up is
scheduled (the entry is written in the background, not before the 202). Tool activity and status are sent as `reasoning_content`, which "thinking"-aware clients
show and others ignore.

A message sent to /v1/chat/interject while that conversation's reply runs is appended to the agent's
.tmp/interject-<talk id>.jsonl; the agent takes it at its next step and says so (`heard`). The reply's JSON counts
them in `interjections_heard` (in the order sent): the client answers any others with its next request.

`"voice": true` in a chat or interject body: the operator spoke it. The agent's request (or the interjection's line)
carries it, and a voice request's stream also carries `shaft_event` chunks: the step's text as it is written (`draft`),
whether it was the answer (`step_end`), status and tool events (_chat; docs/web-and-api.md).
Typed streams carry status events too.
A spoken, streamed turn is answered by its conversation's warm process (VoiceSessions, runner.Warm): one agent kept up
between turns, so its model session and memory stay warm; it holds the instance only while a turn runs, and ends after
SHAFT_VOICE_SESSION_IDLE_S without one, with its conversation, or when its instance is paused, updated or deleted.

Security: the server listens on 127.0.0.1 only; `tailscale serve` exposes it to the tailnet (never
Funnel). Every request needs the bearer key SHAFT_API_KEY from .env AND a Tailscale-User-Login
header (set by `tailscale serve`) in SHAFT_API_LOGINS. `shaft api on --local` (no Tailscale) exposes nothing: it adds
LOCAL_LOGIN to SHAFT_API_LOGINS, the login the Mac app's devices paired with `shaft web pair --local` act as.

The inbox doorbell has its own, narrower key, SHAFT_INBOX_KEY (`shaft api inbox-key`): it can only drop a
message, never chat with or drive an instance, and it needs no tailnet login (a script on this Mac, a tagged CI
node or a phone shortcut has none) — the server is still reachable only from this Mac and the tailnet. The chat
key with an allowed login works too. Each message is marked as outside content (messages.deliver_external) and
rate-limited per instance: SHAFT_INBOX_PER_HOUR messages, SHAFT_INBOX_WAKES_PER_HOUR wakes.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import os
import queue
import re
import secrets
import select
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core
from .core import (jsonl_lines, API_TALKS as TALKS, IDLE_MINUTES, SUP_DIR, UnsafePath, instance_names, instance_root, knob, log,
                   paused, port, safe_write, secrets_env, set_secret_env, unlink_beneath, whole)
from .runner import REFUSED, Reply, Warm, busy_with, request_yield, run_agent, spawn_recover, stop_runs_on_signal

PORT, _ = knob(
    "SHAFT_API_PORT", "8781", port, "the API's local port (OpenAI-compatible; the web UI and Telegram chat through it)",
    group="Ports and paths", services=("api", "web", "telegram"), web=False)
INBOX_PER_HOUR, _ = knob(
    "SHAFT_INBOX_PER_HOUR", "30", whole, "messages per hour the API's inbox (doorbell) accepts for one instance",
    group="Inbox and benchmarks", services=("api",))
INBOX_WAKES_PER_HOUR, _ = knob(
    "SHAFT_INBOX_WAKES_PER_HOUR", "4", whole, "wakes per hour inbox messages may start for one instance",
    group="Inbox and benchmarks", services=("api",))
VOICE_SESSION_IDLE_S, _ = knob(
    "SHAFT_VOICE_SESSION_IDLE_S", "300", whole, "seconds a voice conversation keeps its instance's process up between "
    "spoken turns (its model session and memory stay warm); 0: none, every spoken turn starts its own process",
    group="Runs and conversations", services=("api",))
DOORBELL_LOG = "doorbell.jsonl"
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # X-Shaft-Conversation: a client's own name for a conversation
CLIENT_CHANNELS = ("web", "telegram")    # X-Shaft-Channel: where the operator is talking (channel.py routes alerts there)
# The rule of _chat below, told to the instance with every request (a system message: the instance's talk.py puts the
# app's rules in its system prompt, whatever its own prompts say). Untold, instances wrote the answer beside a tool call
# and ended with "Stored." or "." — all the operator got (shaft and wisp, 2026-10-18..22).
LAST_STEP_NOTE = ("[SHAFT, about how your reply is delivered — not the app] Only your last message, the one that calls "
                  "no tool, reaches the operator. Text you write in a message that also calls a tool is never shown "
                  "to them: it is kept as your working notes. So put your whole answer in that last message, even "
                  "the parts you already wrote before a tool call; never end with only \"Done\", \"Stored\" or \".\" "
                  "when the answer is above it.")       # one line per accepted message: when, instance, source, wake outcome


def allowed_logins() -> set[str]:
    raw = os.environ.get("SHAFT_API_LOGINS") or secrets_env().get("SHAFT_API_LOGINS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _last_text(messages: list[dict]) -> str:
    """The text of the last message: a string, or the text parts of a content list."""
    content = messages[-1].get("content")
    return (content if isinstance(content, str) else "\n".join(
        p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")).strip()


class ReplyQueue:
    """Tickets keep API replies in arrival order; terminal conversations use the run lock alone."""

    def __init__(self):
        self.changed = threading.Condition()
        self.queues = {}

    def join(self, model: str, title: str):
        ticket = (object(), title)
        with self.changed:
            self.queues.setdefault(model, []).append(ticket)
        return ticket

    def wait(self, model, ticket, cancelled, status):
        with self.changed:
            previous = None
            while self.queues[model][0] is not ticket:
                if cancelled.is_set():
                    return False
                title = self.queues[model][0][1]
                phase = f"waiting: answering in «{title}»" if title else f"waiting: {model} is in another conversation"
                if phase != previous:
                    status({"type": "status", "phase": phase})
                    previous = phase
                self.changed.wait(0.1)
            return not cancelled.is_set()

    def leave(self, model, ticket):
        with self.changed:
            self.queues[model].remove(ticket)
            if not self.queues[model]:
                del self.queues[model]
            self.changed.notify_all()


class Conversations:
    """Maps a client conversation to an instance talk id; writes it up after it goes idle."""

    def __init__(self):
        self.lock = threading.Lock()
        self.talks = json.loads(TALKS.read_text()) if TALKS.exists() else {}

    def _save(self) -> None:
        """Whole or not at all: others read this file while it is saved (channel.route, runner.open_conversations), and
        a half-written one read as "no conversations" sent an alert to the wrong channel (Codex review, 2026-10-20)."""
        TALKS.parent.mkdir(exist_ok=True)
        tmp = TALKS.with_name(f".{TALKS.name}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(self.talks, indent=1))
        os.replace(tmp, TALKS)

    @staticmethod
    def _key(model: str, messages: list[dict], cid: str | None = None) -> str:
        """A client that names its conversation (X-Shaft-Conversation: the web UI) is keyed by that name; any other by
        its first user message, so two web chats opening with "hi" are never one conversation."""
        if cid:
            return "cid:" + hashlib.sha256(json.dumps([model, cid]).encode()).hexdigest()[:16]
        first = next((m for m in messages if m.get("role") == "user"), {})
        return hashlib.sha256(json.dumps([model, first.get("content")], sort_keys=True).encode()).hexdigest()[:16]

    def current(self, model: str, messages: list[dict], cid: str | None = None) -> str | None:
        """The talk id of this conversation if it has one still going on (never creates one)."""
        with self.lock:
            t = self.talks.get(self._key(model, messages, cid))
            return t["talk_id"] if t and not t.get("written") else None

    def is_open(self, talk_id: str) -> bool:
        """A conversation with this talk id is still going on (not written up)."""
        with self.lock:
            return any(t["talk_id"] == talk_id and not t.get("written") for t in self.talks.values())

    def end(self, model: str, messages: list[dict], cid: str | None = None) -> str | None:
        """Close the conversation now (as SHAFT_IDLE_MINUTES of silence would): its talk id, or None if none is open."""
        with self.lock:
            t = self.talks.get(self._key(model, messages, cid))
            if not t or t.get("written"):
                return None
            t["written"] = True
            self._save()
            return t["talk_id"]

    def talk_id(self, model: str, messages: list[dict], cid: str | None = None, channel: str = "api") -> str:
        key = self._key(model, messages, cid)
        with self.lock:
            t = self.talks.get(key)
            if t and not t.get("written"):  # going on already: `shaft deploy` sees it and restarts nothing
                return self._touch(t, channel)
            # New, or its earlier part is already in the journal: it opens now, never while a deploy restarts the
            # services (core.admitting raises core.Restarting then: the client is asked to try again).
            with core.admitting():
                t = {"talk_id": f"api-{uuid.uuid4().hex[:10]}", "model": model}
                self.talks[key] = t
                return self._touch(t, channel)

    def _touch(self, t: dict, channel: str) -> str:
        """Under self.lock."""
        t["last"] = time.time()
        t["channel"] = channel
        t["written"] = False
        self._save()
        return t["talk_id"]

    def replied(self, talk_id: str) -> None:
        """A reply in this conversation has just ended: its quiet time starts now, not when the request came in."""
        with self.lock:
            for t in self.talks.values():
                if t["talk_id"] == talk_id:
                    t["ended"] = time.time()
                    self._save()

    def write_up_idle(self, replying=lambda: set()) -> None:
        """Close and write up the conversations quiet for IDLE_MINUTES, counted from when the last reply ended — never
        one with a reply still running (`replying()`: their talk ids, asked under this lock, so a reply cannot end
        unseen in between). Idle time was counted from the request alone, so a reply longer than SHAFT_IDLE_MINUTES
        had its conversation written up under it, and the next message opened a new one (B12, review of 2026-10-24)."""
        with self.lock:
            names = set(instance_names())
            gone = [k for k, t in self.talks.items() if t.get("model") not in names]
            for k in gone:   # its instance was deleted (shaft delete): nothing to write up, nothing to keep
                log(f"api: conversation {self.talks.pop(k)['talk_id']} dropped — its instance no longer exists")
            running = replying()
            due = [t for t in self.talks.values() if not t.get("written") and t["talk_id"] not in running
                   and time.time() - max(t.get("last", 0), t.get("ended", 0)) > IDLE_MINUTES * 60]
            for t in due:
                t["written"] = True
            if due or gone:
                self._save()
        for model in sorted({t["model"] for t in due}):
            spawn_recover(model)


ANSWERED = SUP_DIR / "api-answered.json"
ANSWERED_KEPT = 500


class Answered:
    """The answers given to requests that carried an idempotency key (X-Shaft-Request-Id), newest ANSWERED_KEPT kept:
    the same key again is answered from here, never run again. Telegram sends its update id: an update replayed after
    the service restarted (telegram.py keeps it pending until it is answered) ran its whole reply a second time
    (Codex on e77d5e5). Only whole, non-streamed answers are kept; a request cut off before its answer runs again."""

    def __init__(self):
        self.lock = threading.Lock()

    def _read(self) -> dict:
        try:
            got = json.loads(ANSWERED.read_text())
            return got if isinstance(got, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:     # loud: a request asked again now runs again
            log(f"api: {ANSWERED} unreadable ({type(e).__name__}: {e}) — FAILED to recognise requests asked again")
            return {}

    @staticmethod
    def _key(model: str, request_id: str) -> str:
        return f"{model}:{request_id}"

    def get(self, model: str, request_id: str) -> dict | None:
        with self.lock:
            return (self._read().get(self._key(model, request_id)) or {}).get("answer")

    def put(self, model: str, request_id: str, answer: dict) -> None:
        with self.lock:
            done = self._read()
            done[self._key(model, request_id)] = {"ts": time.time(), "answer": answer}
            keep = dict(sorted(done.items(), key=lambda kv: kv[1].get("ts", 0))[-ANSWERED_KEPT:])
            ANSWERED.parent.mkdir(exist_ok=True)
            tmp = ANSWERED.with_name(f".{ANSWERED.name}.{os.getpid()}.{threading.get_ident()}")
            tmp.write_text(json.dumps(keep))
            os.replace(tmp, ANSWERED)


class Live:
    """Replies running now, by talk id, and the messages sent to them while they run. Until the agent process starts
    (it may wait for another conversation) they are held here; then each goes to its .tmp/interject-<talk id>.jsonl,
    one JSON line, which the agent reads at the start of each step.

    The file is the talk's, and requests in one talk may overlap: a retry after the client hung up waits for the
    instance while the first still replies, and is cancelled in turn. Every outstanding request of a talk is kept
    (`replies[talk_id]["requests"]`, oldest first). A message goes to the newest request whose agent has started — the
    one running, the run lock lets no other agent of the instance run — or, while none has, is held for the first that
    starts. The talk stays registered, and its file stays, until no request of it is outstanding; a starting agent
    clears what an earlier agent of the talk left unheard (that reply's client sends it again). Before this, each
    request owned the talk alone: begin and end unlinked the shared file (B19, review of 2026-10-24), and a cancelled
    newer request then dropped the registration of the older one still replying, so the janitor could close its
    conversation under it (Codex on e77d5e5)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.replies: dict[str, dict] = {}       # talk id -> {"requests": [entry, ...], "held": [line, ...]}

    @staticmethod
    def _rel(talk_id: str) -> str:
        return f".tmp/interject-{talk_id}.jsonl"

    def _drop(self, model: str, talk_id: str) -> None:
        """Remove the talk's file through its folder's descriptor: a link itself, never what it points to, and nothing
        in a folder reached through a link (unlink_beneath)."""
        unlink_beneath(instance_root(model), self._rel(talk_id))

    def running(self) -> set[str]:
        """The talk ids with a request outstanding: waiting for the instance or running."""
        with self.lock:
            return set(self.replies)

    def begin(self, model: str, talk_id: str) -> dict:
        entry = {"model": model, "talk_id": talk_id, "started": False}
        with self.lock:
            if talk_id not in self.replies:             # no request of this talk to read it: a crashed run's leftovers
                self._drop(model, talk_id)
                self.replies[talk_id] = {"requests": [], "held": []}
            self.replies[talk_id]["requests"].append(entry)
        return entry

    def _write(self, entry: dict, line: dict) -> None:
        safe_write(instance_root(entry["model"]), self._rel(entry["talk_id"]),
                   json.dumps(line, ensure_ascii=False) + "\n", append=True)

    def started(self, entry: dict) -> None:
        with self.lock:
            talk = self.replies[entry["talk_id"]]
            self._drop(entry["model"], entry["talk_id"])   # an earlier agent's, unheard: its client sends it again
            entry["started"] = True
            held, talk["held"] = talk["held"], []
            for line in held:
                self._write(entry, line)

    def interject(self, talk_id: str, text: str, voice: bool = False) -> bool:
        """`voice`: the operator spoke it; the agent records it on the interjection's episode."""
        line = {"text": text, **({"voice": True} if voice else {})}
        with self.lock:
            talk = self.replies.get(talk_id)
            if talk is None:
                return False
            running = [e for e in talk["requests"] if e["started"]]
            if running:
                self._write(running[-1], line)
            else:
                talk["held"].append(line)
            return True

    def end(self, entry: dict) -> None:
        with self.lock:
            talk = self.replies.get(entry["talk_id"])
            if talk is None or not any(e is entry for e in talk["requests"]):
                return
            talk["requests"] = [e for e in talk["requests"] if e is not entry]   # by identity: twins compare equal
            if not talk["requests"]:                    # the last of the talk: nobody can read the file any more
                del self.replies[entry["talk_id"]]
                self._drop(entry["model"], entry["talk_id"])


class VoiceSessions:
    """The voice conversations' warm processes (runner.Warm), by talk id: a spoken, streamed request in
    a conversation that has one is answered by its process, which stays up between turns; one that has none starts it.
    Every M0 cost of a fresh run per reply is paid once per conversation instead: process start, the memory index read,
    the Codex session opened. A session holds the instance only while a turn runs (runner.Warm), and it ends:
    - after SHAFT_VOICE_SESSION_IDLE_S without a turn;
    - when its conversation ends (/v1/chat/end, or written up after SHAFT_IDLE_MINUTES);
    - when its instance is paused (a kill pauses it first) or no longer exists, or its process ended (killed);
    - with the API (its agent is one of the runs end_all_runs ends).
    `shaft update` and `shaft delete` end it from their own process (runner.end_warm). A typed request never comes
    here: it runs as before, in a process of its own (its prompt, and so its model session, differ)."""

    SWEEP_S = 1.0

    def __init__(self, is_open=lambda talk_id: True):
        self.lock = threading.Lock()
        self.sessions: dict[str, Warm] = {}
        self.is_open = is_open
        self.sweeper: threading.Thread | None = None

    def get(self, model: str, talk_id: str) -> Warm:
        """The conversation's session, made (with no process yet: its first turn starts one) if it has none."""
        with self.lock:
            s = self.sessions.get(talk_id)
            if s is None or s.closed or s.name != model:
                s = self.sessions[talk_id] = Warm(model, "api", ["answer", "--loop"], talk_id)
            if self.sweeper is None:
                self.sweeper = threading.Thread(target=self._sweep_forever, daemon=True)
                self.sweeper.start()
            return s

    def end(self, talk_id: str, why: str) -> bool:
        """End the conversation's session now, unless a turn of it runs (the sweep ends it then, once it is over)."""
        with self.lock:
            s = self.sessions.get(talk_id)
        if s is None or not s.close(why, wait=False):
            return False
        with self.lock:
            if self.sessions.get(talk_id) is s:
                del self.sessions[talk_id]
        return True

    def why_end(self, s: Warm, names: set[str]) -> str | None:
        if s.closed:
            return "ended"
        if s.name not in names:
            return "its instance no longer exists"
        if paused(s.name):
            return "its instance is paused"
        if not self.is_open(s.talk_id):
            return "its conversation ended"
        if s.started and not s.alive():         # killed, crashed or stopped: the next turn makes a new session
            return "its process ended"
        if time.monotonic() - s.last > VOICE_SESSION_IDLE_S:
            return f"no turn for {VOICE_SESSION_IDLE_S}s"
        return None

    def sweep(self) -> None:
        """End the sessions that are over (why_end); a session in a turn is left for the next sweep."""
        with self.lock:
            sessions = list(self.sessions.items())
        if not sessions:
            return
        names = set(instance_names())
        for talk_id, s in sessions:
            if s.busy() or not (why := self.why_end(s, names)):
                continue
            if s.close(f"voice session over: {why}", wait=False):
                with self.lock:
                    if self.sessions.get(talk_id) is s:
                        del self.sessions[talk_id]

    def _sweep_forever(self) -> None:
        """Sweeps while there are sessions; the next get() starts it again. It waits on an Event, never time.sleep: a
        thread left sleeping for ever took the sleeps another part of the process counted on (a test's patched
        time.sleep in the scheduler's loop ran out under it)."""
        while not threading.Event().wait(self.SWEEP_S):
            try:
                self.sweep()
            except Exception as e:   # never the end of the sweeps
                log(f"api: voice sessions sweep FAILED: {type(e).__name__}: {e}")
            with self.lock:
                if not self.sessions:
                    self.sweeper = None
                    return


def _chunk(cid: str, model: str, delta: dict, finish: str | None = None, **extra) -> bytes:
    """One stream chunk; `extra` adds top-level keys OpenAI clients ignore (the last carries interjections_heard)."""
    return b"data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                                   "model": model, "choices": [{"index": 0, "delta": delta,
                                                                "finish_reason": finish}], **extra}).encode() + b"\n\n"


def _note(ev: dict) -> str | None:
    """Tool activity and status as human-readable reasoning lines."""
    t = ev["type"]
    if t == "status" and ev.get("phase") not in ("reading my memory", "started"):
        return f"{ev['phase']}{' · ' + ev['detail'] if ev.get('detail') else ''}\n"
    if t == "tool_start":
        return f"⚙ {ev['name']} {ev.get('args', '')[:120]}\n"
    if t == "tool_end":
        return f"{'✓' if ev['ok'] else '✗'} {ev['name']}\n"
    if t == "retry":
        return f"reply cut off at {ev['old']} tokens — asking again with {ev['new']}\n"
    if t == "heard":
        return f"↪ read your new message: {' '.join(ev.get('text', '').split())[:120]}\n"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "shaft-api/1"
    reply_queue = ReplyQueue()
    conversations: Conversations
    live: Live
    answered: Answered | None = None
    voice: VoiceSessions | None = None     # the warm voice sessions (serve makes them; None: every turn runs cold)

    def log_message(self, fmt, *args):
        log("api " + (fmt % args))

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if talk := getattr(self, "_response_talk", None):
            self.send_header("X-Shaft-Talk", talk)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str, kind: str = "invalid_request_error") -> None:
        self._json(code, {"error": {"message": message, "type": kind}})

    def _authorised(self) -> bool:
        key = secrets_env().get("SHAFT_API_KEY", "")
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not key or not secrets.compare_digest(given, key):
            self._error(401, "invalid API key", "authentication_error")
            return False
        login = (self.headers.get("Tailscale-User-Login") or "").lower()
        if login not in allowed_logins():
            self._error(403, f"tailnet user {login or '(none)'} is not allowed", "permission_error")
            log(f"api refused tailnet user {login or '(none)'}")
            return False
        return True

    def do_GET(self):
        if not self._authorised():
            return
        if self.path.rstrip("/") == "/v1/models":
            return self._json(200, {"object": "list", "data": [
                {"id": n, "object": "model", "created": 0, "owned_by": "shaft"} for n in instance_names()]})
        self._error(404, f"no route {self.path}")

    def _inbox_authorised(self) -> bool:
        """The inbox key alone, or the chat key with an allowed tailnet login."""
        inbox_key = secrets_env().get("SHAFT_INBOX_KEY", "")
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if inbox_key and given and secrets.compare_digest(given, inbox_key):
            return True
        return self._authorised()

    def _inbox(self, name: str) -> None:
        from . import messages
        if name not in instance_names():
            return self._error(404, f"no instance {name!r}; instances: {', '.join(instance_names())}")
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        text, source, wake = raw.decode(errors="replace"), self.headers.get("X-Shaft-Source", ""), False
        if (self.headers.get("Content-Type") or "").startswith("application/json"):
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._error(400, "body is not valid JSON")
            if isinstance(body, dict) and "text" in body:
                text, source, wake = str(body["text"]), str(body.get("source") or source), bool(body.get("wake"))
            else:                                   # any JSON payload (a webhook's): kept whole, readable
                text = json.dumps(body, indent=1, ensure_ascii=False)
        if not source and (event := self.headers.get("X-GitHub-Event")):
            source = f"github:{event}"
        source = (source or "outside").strip().lower()
        wake = wake or self.headers.get("X-Shaft-Wake", "").lower() in ("1", "true", "yes")
        recent = [m for m in messages.history() if m["to"] == name and m.get("kind") == "external"
                  and m["ts"] >= datetime.fromtimestamp(time.time() - 3600).isoformat(timespec="seconds")]
        if len(recent) >= INBOX_PER_HOUR:
            return self._error(429, f"{name} got {len(recent)} outside messages in the last hour "
                                    f"(SHAFT_INBOX_PER_HOUR={INBOX_PER_HOUR}); try later", "rate_limit_error")
        try:
            path = messages.deliver_external(name, text, source)
        except ValueError as e:
            return self._error(400, str(e))
        woke = self._wake(name) if wake else "not asked to wake: read at its next scheduled wake"
        with (SUP_DIR / DOORBELL_LOG).open("a") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "instance": name,
                                "source": source, "file": path.name, "wake": woke}) + "\n")
        self._json(202, {"delivered": path.name, "instance": name, "source": source, "wake": woke})

    def _wake(self, name: str) -> str:
        from .autonomy import start_wake
        since = datetime.fromtimestamp(time.time() - 3600).isoformat(timespec="seconds")
        try:
            rows = [json.loads(l) for l in jsonl_lines((SUP_DIR / DOORBELL_LOG).read_text()) if l.strip()]
        except (OSError, ValueError):
            rows = []
        woken = sum(1 for r in rows if r["instance"] == name and r["ts"] >= since and r["wake"].startswith("started"))
        if woken >= INBOX_WAKES_PER_HOUR:
            return (f"not woken: {woken} doorbell wakes in the last hour (SHAFT_INBOX_WAKES_PER_HOUR="
                    f"{INBOX_WAKES_PER_HOUR}); read at its next scheduled wake")
        try:
            return start_wake(name)
        except RuntimeError as e:
            return f"not woken: {e}; read at its next wake"

    def do_POST(self):
        route = self.path.rstrip("/")
        if route.startswith("/v1/inbox/"):
            if self._inbox_authorised():
                self._inbox(route.removeprefix("/v1/inbox/"))
            return
        if not self._authorised():
            return
        if route not in ("/v1/chat/completions", "/v1/chat/interject", "/v1/chat/end"):
            return self._error(404, f"no route {self.path}")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._error(400, "body must be JSON")
        model = body.get("model")
        if model not in instance_names():
            return self._error(404, f"no instance {model!r}; models: {', '.join(instance_names())}")
        cid = self.headers.get("X-Shaft-Conversation") or None
        if cid is not None and not CONVERSATION_ID.match(cid):
            return self._error(400, "X-Shaft-Conversation is 1-64 letters, digits, - or _")
        channel = (self.headers.get("X-Shaft-Channel") or "api").strip().lower()
        if channel not in ("api", *CLIENT_CHANNELS):
            return self._error(400, f"X-Shaft-Channel is one of {', '.join(CLIENT_CHANNELS)} (or absent), not {channel!r}")
        messages = body.get("messages") or []
        if route == "/v1/chat/end":
            return self._end(model, messages, cid)
        if not messages or messages[-1].get("role") != "user":
            return self._error(400, "messages must end with a user message")
        voice = body.get("voice") is True        # the last user message was spoken; anything else: typed
        if route == "/v1/chat/interject":
            return self._interject(model, messages, cid, channel, voice)
        rid = self.headers.get("X-Shaft-Request-Id") or None
        if rid is not None and not CONVERSATION_ID.match(rid):
            return self._error(400, "X-Shaft-Request-Id is 1-64 letters, digits, - or _")
        self._chat(model, messages, bool(body.get("stream")), self.headers.get("User-Agent", "an app"), cid, channel,
                   rid, voice)

    def _end(self, model: str, messages: list[dict], cid: str | None) -> None:
        """POST /v1/chat/end: the conversation is over. The write-up is scheduled now (202) and finishes in the
        background, instead of after SHAFT_IDLE_MINUTES. 202 is not a finished journal entry."""
        if not cid and not messages:
            return self._error(400, "which conversation? X-Shaft-Conversation, or its messages")
        talk_id = self.conversations.current(model, messages, cid)
        if not talk_id:
            return self._error(404, f"no open conversation with {model} to end", "not_found")
        if talk_id in self.live.replies:
            return self._error(409, f"{model} is still replying: stop the reply first", "conflict")
        self.conversations.end(model, messages, cid)
        if Handler.voice is not None:
            Handler.voice.end(talk_id, "its conversation was ended by its client")
        spawn_recover(model)
        log(f"api: conversation {talk_id} with {model} ended by its client")
        self._json(202, {"ended": talk_id})

    def _interject(self, model: str, messages: list[dict], cid: str | None = None, channel: str = "api",
                   voice: bool = False) -> None:
        """The last user message joins the reply running in this conversation (same first user message)."""
        text = _last_text(messages)
        if not text:
            return self._error(400, "the message to add is empty")
        talk_id = self.conversations.current(model, messages, cid)
        try:
            ok = bool(talk_id) and self.live.interject(talk_id, text, voice)
        except (OSError, UnsafePath) as e:
            return self._error(500, f"could not pass it to {model}: {e}", "server_error")
        if not ok:
            return self._error(409, f"{model} is not replying in this conversation now: send it as a new message",
                               "conflict")
        log(f"api: a message for {model}'s running reply ({talk_id})")
        self._json(202, {"interjected": True, "talk_id": talk_id})

    def _client_gone(self) -> bool:
        """The client hung up: its socket is readable and a peek returns no data."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _warm_turn(self, model: str, talk_id: str, request: dict, live: dict, events: queue.Queue, answered, errored,
                   stops: list, cancelled) -> tuple[int | None, str | None]:
        """One spoken turn in the conversation's warm process (VoiceSessions): the same request and events as a turn
        in a process of its own. Messages sent while it waited reach the agent before the request does (Live.started
        first: the process is already running and may take its first step at once). Returns (the process's exit code,
        or None when it is still up for the next turn — or REFUSED; its stderr's tail when it ended)."""
        def on_start(stop) -> None:
            events.put({"type": "status", "phase": "started"})
            stops.append(stop)
            try:
                self.live.started(live)
            except (OSError, UnsafePath) as e:
                log(f"api: could not pass held messages to {model}: {e}")

        def on_event(ev: dict) -> None:
            if ev.get("type") in ("content", "turn_end"):
                answered.set()
            if ev.get("type") == "error":
                errored.set()
            events.put(ev)

        for _ in range(3):                        # a session the sweep ended just now: the next one takes the turn
            session = Handler.voice.get(model, talk_id)
            code = session.reply(request, on_event, on_start, cancelled=cancelled.is_set)
            if code != Warm.CLOSED:
                break
        else:
            raise RuntimeError(f"{model}'s voice session kept ending before it could take the turn")
        stops.clear()                             # over: a late Stop must not reach the next turn
        return code, (session.stderr_tail() if code is not None else None)

    def _chat(self, model: str, messages: list[dict], stream: bool, app: str, cid: str | None = None,
              channel: str = "api", rid: str | None = None, voice: bool = False) -> None:
        events: queue.Queue = queue.Queue()
        procs: list = []
        if rid and not stream:
            if Handler.answered is None:
                Handler.answered = Answered()
            if (done := Handler.answered.get(model, rid)) is not None:
                log(f"api: {model}: request {rid} was answered already — the same answer, not run again")
                self._response_talk = done.get("talk_id")
                return self._json(200, {**done, "replayed": True})   # its client tells a kept answer from a new one
        try:
            talk_id = self.conversations.talk_id(model, messages, cid, channel)
        except core.Restarting as e:
            return self._error(503, str(e), "server_error")
        self._response_talk = talk_id
        title = unquote(self.headers.get("X-Shaft-Thread-Title", ""))
        with self.conversations.lock:
            self.conversations.talks[self.conversations._key(model, messages, cid)]["title"] = title
            self.conversations._save()
        live = self.live.begin(model, talk_id)
        # A spoken, streamed turn goes to the conversation's warm process (VoiceSessions); a typed one, or any turn
        # while the instance is paused or warm sessions are off, runs in a process of its own, as before.
        warm = (voice and stream and Handler.voice is not None and VOICE_SESSION_IDLE_S > 0 and not paused(model))
        stops: list = []                         # a warm turn's own stop (Warm.reply on_start)
        request = {"messages": [*(m for m in messages if m.get("role") == "system"),
                                {"role": "system", "content": LAST_STEP_NOTE},
                                *(m for m in messages if m.get("role") != "system")],
                   "talk_id": talk_id, "client": app.split()[0][:40], **({"voice": True} if voice else {})}

        def driver(proc):
            events.put({"type": "status", "phase": "started"})
            procs.append(proc)
            proc.stdin.write(json.dumps(request))
            proc.stdin.close()
            try:
                self.live.started(live)         # messages sent while it waited: read at its first step
            except (OSError, UnsafePath) as e:
                log(f"api: could not pass held messages to {model}: {e}")
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") in ("content", "turn_end"):
                    answered.set()
                if ev.get("type") == "error":
                    errored.set()
                events.put(ev)

        cancelled = threading.Event()
        answered = threading.Event()
        errored = threading.Event()

        ticket = self.reply_queue.join(model, title)

        def work():
            reply = Reply(model, "api")      # a running cycle of this instance pauses for this reply, then resumes
            try:
                reply.__enter__()
                if not self.reply_queue.wait(model, ticket, cancelled, events.put):
                    return
                if b := busy_with(model):
                    if not b.startswith(("talk", "api", "live", "sleep", "reflect")):
                        request_yield([model])          # anything else: ask it to wrap up
                    what = ("in another conversation" if b.startswith(("talk", "api")) else
                            "pausing its cycle for you" if b.startswith(("live", "sleep", "reflect")) else
                            "finishing a run")
                    events.put({"type": "status", "phase": f"waiting: {model} is {what} ({b})"})
                    while busy_with(model):
                        if cancelled.is_set():          # the client left while waiting: never run it later
                            return
                        time.sleep(1)
                if cancelled.is_set():
                    return
                if warm:
                    code, err = self._warm_turn(model, talk_id, request, live, events, answered, errored, stops,
                                                cancelled)
                else:
                    code = run_agent(model, "api", ["answer"], interactive=True, driver=driver, talk_id=talk_id)
                    err = None
                if code == REFUSED:
                    events.put({"type": "error", "message": f"the supervisor refused to start {model}'s run: its "
                                "code could not be checked (the reason is in the panel's alerts and supervisor.log)"})
                elif not answered.is_set() and not errored.is_set():   # keep the agent's own reason if it gave one
                    if err is None:
                        err = (procs[0].stderr.read() if procs and procs[0].stderr else "")[-800:].strip()
                    events.put({"type": "error", "message": f"{model} stopped without answering"
                                                           + (f" (exit {code})" if code is not None else "")
                                                           + (f": {err}" if err else "")})
            except Exception as e:   # surface, never hang the client
                events.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                try:
                    self.conversations.replied(talk_id)   # while still registered: running or just ended, never between
                except OSError as e:
                    log(f"api: {e}")
                try:
                    self.live.end(live)         # before "done": a message after this is the client's next request
                except (OSError, UnsafePath) as e:
                    log(f"api: {e}")
                try:
                    if reply.f is not None:
                        reply.__exit__(None, None, None)
                finally:
                    self.reply_queue.leave(model, ticket)
                    events.put({"type": "done"})

        threading.Thread(target=work, daemon=True).start()
        cid = f"chatcmpl-{uuid.uuid4().hex[:20]}"
        # Only the reply's last step is its answer. Text the model writes before calling a tool ("let me check my
        # notes…") is narration: it goes to reasoning_content with the tool activity, never into the reply — it
        # reached the operator's Telegram as if the instance had said it (wisp, 2026-10-14: 25 of 44 Telegram replies had
        # tool steps). So a step's text is held until the next event tells which it was.
        content, step, notes, error, ended, heard = [], [], [], None, None, 0
        tools_ran = False
        spoken = stream and voice

        def event(**shaft_event) -> None:
            """A voice reply's own event, in a chunk of its own with an empty delta: OpenAI clients see nothing new. A
            step's text goes out at once as `draft` pieces (the app speaks them) and is still held in `step` as above;
            `step_end` then says whether it was the answer (its whole text also comes as `content`) or narration
            before a tool call (only reasoning). A typed stream gets none of them, except the web chat's, which gets
            the status events (waiting for another thread, started): every other client's typed stream is
            byte for byte what it was before voice and threads (test_voice_api, test_voice_session)."""
            if spoken or (stream and channel == "web" and shaft_event.get("type") == "status"):
                self.wfile.write(_chunk(cid, model, {}, shaft_event=shaft_event))

        try:
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Shaft-Talk", talk_id)
                self.end_headers()
                self.wfile.write(_chunk(cid, model, {"role": "assistant", "content": ""}))
                self.wfile.flush()
            idle = 0
            while True:
                try:
                    ev = events.get(timeout=1)
                except queue.Empty:
                    if self._client_gone():         # noticed within a second, not on the next failed write
                        raise ConnectionResetError("client closed the connection")
                    idle += 1
                    if stream and idle % 10 == 0:
                        self.wfile.write(b": still working\n\n")   # keep-alive for proxies and clients
                        self.wfile.flush()
                    continue
                idle = 0
                t = ev["type"]
                if t == "status" and ev.get("phase") == "planned" and channel == "telegram":
                    from . import telegram
                    telegram._planned(model, ev.get("detail", ""))
                if t == "done":
                    break
                if t == "content":
                    step.append(ev["text"])
                    if spoken:
                        event(type="draft", text=ev["text"])
                        self.wfile.flush()
                    continue
                if t in ("tool_start", "tool_end"):
                    tools_ran = True
                if t == "retry" and step:       # a call cut off at max_tokens is discarded and asked again (llm._answer):
                    event(type="step_end", answer=False)   # its text was never the answer, nor in the agent's record
                    step = []
                if t == "tool_start" and step:           # that step called tools: its text was narration
                    event(type="step_end", answer=False)
                    said = "".join(step).strip()
                    step = []
                    if said:
                        notes.append(f"{said}\n")
                        if stream:
                            self.wfile.write(_chunk(cid, model, {"reasoning_content": f"{said}\n"}))
                if t == "turn_end":
                    ended = ev
                    if step:
                        event(type="step_end", answer=True)
                    content, step = step, []
                    if stream and content:
                        self.wfile.write(_chunk(cid, model, {"content": "".join(content)}))
                if t == "heard":
                    heard += 1
                if t == "error":
                    if error is None:          # later errors must not mask the cause
                        error = ev["message"]
                elif (n := _note(ev)) is not None:
                    notes.append(n)
                    if stream:
                        self.wfile.write(_chunk(cid, model, {"reasoning_content": n}))
                if t == "status":
                    event(type="status", phase=ev.get("phase", ""), detail=ev.get("detail") or "")
                elif t == "tool_start":
                    event(type="tool_start", name=ev["name"])
                elif t == "tool_end":
                    event(type="tool_end", name=ev["name"], ok=bool(ev["ok"]))
                if stream:
                    self.wfile.flush()
            if not content and step:                     # interrupted before a turn_end: the text so far
                event(type="step_end", answer=True)
                content = step
                if stream:
                    self.wfile.write(_chunk(cid, model, {"content": "".join(step)}))
            if not content and not error and ended and ended.get("ended_by") not in (None, "answer"):
                content = [f"(no answer: the reply stopped after {ended.get('steps')} steps — {ended['ended_by']})"]
                if stream:
                    self.wfile.write(_chunk(cid, model, {"content": content[0]}))
            if stream:
                if error and not content:
                    self.wfile.write(_chunk(cid, model, {"content": f"[error: {error}]"}))
                self.wfile.write(_chunk(cid, model, {}, "stop", interjections_heard=heard,
                                        **({"reply_error": {"message": error, "tools_ran": tools_ran}} if error else {})))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            elif error and not content:
                self._error(502, error, "server_error")
            else:
                answer = {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
                          "choices": [{"index": 0, "finish_reason": "stop", "message": {
                              "role": "assistant", "content": "".join(content), "reasoning_content": "".join(notes)}}],
                          "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                          "interjections_heard": heard, "talk_id": talk_id}
                if rid:
                    try:
                        Handler.answered.put(model, rid, answer)   # made in the check above
                    except OSError as e:
                        log(f"api: {model}: request {rid}'s answer FAILED to be kept ({e}); asked again, it runs again")
                self._json(200, answer)
        except (BrokenPipeError, ConnectionResetError):
            cancelled.set()
            for stop in stops:                         # a warm turn: only this reply is stopped, its process stays
                stop()
            if procs and procs[0].poll() is None:      # the client left: stop the reply
                try:
                    os.killpg(procs[0].pid, signal.SIGINT)
                except OSError:
                    pass


def serve() -> int:
    if not secrets_env().get("SHAFT_API_KEY"):
        print("no SHAFT_API_KEY in .env — run: shaft api on")
        return 2
    if not allowed_logins():
        print("no SHAFT_API_LOGINS — run: shaft api on (or shaft api on --local, without Tailscale)")
        return 2
    from .version import record_service
    record_service("api", {"port": PORT, "idle_minutes": IDLE_MINUTES})
    Handler.conversations = Conversations()
    Handler.live = Live()
    Handler.voice = VoiceSessions(Handler.conversations.is_open)
    stop_runs_on_signal()                        # stopped (launchd), it ends its replies' agents first (warm ones too)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True

    def janitor():
        while True:
            time.sleep(60)
            try:
                Handler.conversations.write_up_idle(Handler.live.running)
            except Exception as e:
                log(f"api janitor: {e}")

    threading.Thread(target=janitor, daemon=True).start()
    log(f"api listening on 127.0.0.1:{PORT} (logins {sorted(allowed_logins())}, idle write-up {IDLE_MINUTES} min)")
    print(f"SHAFT API on http://127.0.0.1:{PORT}/v1 — {datetime.now():%H:%M}")
    server.serve_forever()
    return 0


# -- operator commands: shaft api on | off | status | key | serve ---------------------------
HTTPS_PORT, _ = knob(
    "SHAFT_API_HTTPS_PORT", "8443", port, "the API's https port on the tailnet (tailscale serve)",
    group="Ports and paths", services=("api",), web=False)


def _tailscale(*args: str) -> "subprocess.CompletedProcess":
    import shutil
    import subprocess
    from .codexcli import APP_FOLDERS

    exe = shutil.which("tailscale")
    if exe is None:
        candidates = [folder / "Tailscale.app/Contents/MacOS/Tailscale" for folder in APP_FOLDERS]
        exe = str(next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), candidates[0]))
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True)
    except FileNotFoundError:              # a local-only Mac (`--local`) may have no Tailscale: a failed call, no crash
        return subprocess.CompletedProcess([exe, *args], 127, stdout="", stderr=f"tailscale is not installed ({exe})")


def _outside_gui(r: "subprocess.CompletedProcess") -> bool:
    """The Mac app's CLI refuses to run outside the GUI session, as a launchd job is: it exits 0 and says so on stdout
    (web.tailnet_hosts). Nothing it was asked for happened."""
    return "Tailscale GUI" in r.stdout and "failed to start" in r.stdout


def _tailscale_status() -> dict | None:
    """Tailscale's status, None when it would not answer here (_outside_gui)."""
    r = _tailscale("status", "--json")
    if _outside_gui(r):
        return None
    return json.loads(r.stdout or "{}")


def tailnet_problem() -> str | None:
    """Why this Mac cannot join a tailnet right now, in words for the person at the Mac (the Mac app shows it as is):
    Tailscale missing, signed out or switched off. None when it looks fine, or when its status cannot be read at all
    (the callers' own checks then say what is missing)."""
    r = _tailscale("status", "--json")
    if r.returncode == 127:
        return "Tailscale is not installed on this Mac: install it from the Mac App Store, open it and sign in"
    if _outside_gui(r):
        return None
    try:
        state = json.loads(r.stdout or "{}").get("BackendState")
    except ValueError:
        return None
    if state in ("NeedsLogin", "NeedsMachineAuth"):
        return "Tailscale is not signed in: open Tailscale, log in, then try again"
    if state in ("Stopped", "NoState"):
        return "Tailscale is switched off: open Tailscale, turn it on, then try again"
    return None


say = core.say      # the terminal's wording, or (json_verb: the Mac app) the app's; core.app_wording sets which


def service_on(name: str, force: bool = False) -> int:
    """`autonomy.service(name, "on")` for a service conversations reply through (api, web, telegram): a running one is
    restarted, which cuts a reply still being written. So, as `shaft deploy` does, the restart is done under
    core.restart_barrier (no conversation opens in between), and while an instance is replying (runner.replying: the
    reply the API, the web chat or Telegram registered while it waits for or holds the instance) nothing is restarted.
    A conversation that is open but quiet is not held: it lasts SHAFT_IDLE_MINUTES after its last message, and its next
    message simply reaches the restarted service. `force` (--force, the panels' Force) skips the replying check, never
    the barrier. A service that is not running yet has nothing to cut."""
    import contextlib
    from . import autonomy, runner
    with contextlib.ExitStack() as stack:
        if autonomy.running(name):
            try:
                stack.enter_context(core.restart_barrier())
            except TimeoutError as e:
                print(e)
                return 1
            for instance in [] if force else instance_names():
                if runner.replying(instance):
                    print(say(f"{instance} is replying in a conversation: try again when that reply finishes, or add "
                              f"--force (the reply is cut off)",
                              f"{instance} is replying in a conversation right now: try again when that reply "
                              f"finishes."))
                    return 1
        return autonomy.service(name, "on")


def restart(force: bool = False) -> int:
    """`shaft api restart`: restart the installed com.shaft.api in its current mode — tailnet, local or both, as `api on`
    left it. It never runs `tailscale serve` and changes no login and no key, so a local-only Mac's restart needs no
    Tailscale and a restart never exposes the API on the tailnet. Under the same barrier as `api on` (service_on): while
    an instance is replying, nothing is restarted unless forced. Not installed: refused, naming the verb that installs
    it."""
    from . import autonomy
    if not autonomy.installed("api"):
        print(say("the api service is not installed: run shaft api on (or shaft api on --local) first",
                  "The API service is not installed on this Mac, so there is nothing to restart."))
        return 1
    if service_on("api", force):
        return 1
    log("api restarted in its current mode (no tailscale serve; logins and keys unchanged)")
    print("API restarted")
    return 0


def json_verb(fn) -> int:
    """Run a CLI verb with its prints captured and answer with one JSON line, the Mac app's contract: {"ok": true} (exit
    0) or {"ok": false, "error": <what the verb said>} (exit 1); a crash is an error too, never a traceback."""
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    try:
        with core.app_wording(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e) or type(e).__name__}))
        return 1
    said = " ".join(t for t in (err.getvalue().strip(), out.getvalue().strip()) if t)
    if code:
        print(json.dumps({"ok": False, "error": said or f"exit {code}"}))
        return 1
    print(json.dumps({"ok": True}))
    return 0


def url() -> str:
    st = _tailscale_status()
    if st is None:
        return "(the tailnet address is not available from this session)"
    host = (st.get("Self", {}).get("DNSName") or "").rstrip(".")
    return f"https://{host}:{HTTPS_PORT}/v1" if host else "(tailscale is not running)"


LOCAL_LOGIN = "operator@local"   # the login `shaft api on --local` allows: the Mac app's devices act as it on loopback


def _add_login(login: str) -> None:
    """Merge `login` into SHAFT_API_LOGINS (kept in order, once): a later Tailscale install adds its login next to the
    local one, and a local install keeps the tailnet's."""
    raw = secrets_env().get("SHAFT_API_LOGINS", "")
    logins = [x.strip() for x in raw.split(",") if x.strip()]
    if login.lower() not in {x.lower() for x in logins}:
        set_secret_env("SHAFT_API_LOGINS", ",".join([*logins, login]))


def command(action: str, local: bool = False, as_json: bool = False, force: bool = False) -> int:
    """`shaft api on` creates the keys (SHAFT_API_KEY, SHAFT_INBOX_KEY) if missing, adds your Tailscale login to
    SHAFT_API_LOGINS when it has no tailnet login yet, installs the service and exposes it with `tailscale serve`. `shaft api on --local` does the same
    without Tailscale: it adds LOCAL_LOGIN instead and exposes nothing (the Mac app on this Mac pairs with
    `shaft web pair --local`). Logins are merged, never replaced. `on --json` (the Mac app's "Use SHAFT from iPhone/iPad")
    answers with one JSON line instead of the prose. `shaft api restart` restarts the installed service as it is
    (restart())."""
    from . import autonomy
    if as_json:
        if action not in ("on", "restart"):
            print(f"--json goes with on or restart, not {action}", file=sys.stderr)
            return 2
        return json_verb(lambda: command(action, local, force=force))
    if local and action != "on":
        print(f"--local goes with on only (shaft api on --local), not {action}", file=sys.stderr)
        return 2
    if action == "serve":
        return serve()
    if action == "restart":
        return restart(force)
    if action == "key":
        key = secrets_env().get("SHAFT_API_KEY")
        print(key or "no key yet — run: shaft api on")
        return 0 if key else 1
    if action == "inbox-key":
        key = secrets_env().get("SHAFT_INBOX_KEY")
        if not key:
            key = "sk-shaft-inbox-" + secrets.token_urlsafe(32)
            set_secret_env("SHAFT_INBOX_KEY", key)
            log("api: inbox key created")
        print(key)
        print(f"  POST {url().removesuffix('/v1')}/v1/inbox/<instance>   (Authorization: Bearer <this key>)\n"
              f"  JSON {{\"text\": \"…\", \"source\": \"phone\", \"wake\": true}} or any body (a webhook's), "
              f"source from X-Shaft-Source", file=sys.stderr)
        return 0
    if action == "on":
        env = secrets_env()
        if not env.get("SHAFT_API_KEY"):
            set_secret_env("SHAFT_API_KEY", "sk-shaft-" + secrets.token_urlsafe(32))
        if not env.get("SHAFT_INBOX_KEY"):
            set_secret_env("SHAFT_INBOX_KEY", "sk-shaft-inbox-" + secrets.token_urlsafe(32))
        if local:
            _add_login(LOCAL_LOGIN)
            if service_on("api", force):
                return 1
            log(f"api on, local only: 127.0.0.1:{PORT} (logins {sorted(allowed_logins())}; no tailscale serve)")
            print(say(f"\nSHAFT API running on this Mac only (127.0.0.1:{PORT}, not exposed: no Tailscale)\n"
                      f"  allowed: {', '.join(sorted(allowed_logins()))}\n"
                      f"  next: shaft web on --local, then shaft web pair --local --name <this mac>",
                      f"API running on this Mac only (127.0.0.1:{PORT}, not on any network)"))
            return 0
        if problem := tailnet_problem():
            print(problem)
            return 1
        # The tailnet login is added once, when the list has none yet (a fresh install, or a local-only Mac getting
        # Tailscale): an existing install keeps the logins it has, and whoever is logged in to Tailscale now is never
        # added silently next to them (`shaft api on` rerun under another Tailscale account).
        if not allowed_logins() - {LOCAL_LOGIN}:
            st = _tailscale_status() or {}
            me = st.get("User", {}).get(str(st.get("Self", {}).get("UserID")), {}).get("LoginName")
            if not me:
                print(say("cannot read your Tailscale login (is Tailscale running?)",
                          "Tailscale did not say who is logged in: open the Tailscale app, make sure you are logged "
                          "in, then Try again."))
                return 1
            _add_login(me)
        was_running = autonomy.running("api")
        if service_on("api", force):
            return 1
        r = _tailscale("serve", "--bg", f"--https={HTTPS_PORT}", f"http://127.0.0.1:{PORT}")
        if _outside_gui(r):
            if was_running:       # the existing mapping stays in place on a restart
                log("api restarted; tailscale serve left as it was (its CLI refuses to run outside the GUI session)")
                print("API restarted; tailscale serve left as it was (its CLI refuses to run outside the GUI session)")
                return 0
            message = say(
                "API started but is not exposed on the tailnet because Tailscale's CLI refuses to run "
                "outside the GUI session; run shaft api on in a terminal of the logged-in session.",
                "The API started but is not on your tailnet yet: Tailscale did not answer from this session. Open the "
                "Tailscale app, make sure you are logged in, then Try again.")
            print(message, file=sys.stderr)
            log(message)
            return 1
        if r.returncode != 0:
            print(say(f"tailscale serve failed: {(r.stderr or r.stdout).strip()}",
                      f"Tailscale could not put SHAFT on your tailnet: {(r.stderr or r.stdout).strip()}"))
            return 1
        log(f"api exposed on the tailnet at {url()} (tailscale serve, tailnet only)")
        print(f"\nSHAFT API (tailnet only): {url()}\n  models: {', '.join(instance_names())}\n"
              f"  key:    shaft api key   (prints it here, in your terminal)\n"
              f"  allowed tailnet user(s): {', '.join(sorted(allowed_logins())) or secrets_env().get('SHAFT_API_LOGINS')}")
        return 0
    if action == "off":
        _tailscale("serve", f"--https={HTTPS_PORT}", "off")
        autonomy.service("api", "off")
        print("SHAFT API removed from the tailnet and stopped")
        return 0
    autonomy.service("api", "status")
    print(_tailscale("serve", "status").stdout.strip())
    print(f"endpoint: {url()}  ·  allowed: {', '.join(sorted(allowed_logins())) or '(none)'}")
    print(f"inbox doorbell: {url()}/inbox/<instance>  ·  key: shaft api inbox-key  ·  "
          f"{INBOX_PER_HOUR} messages and {INBOX_WAKES_PER_HOUR} wakes per instance per hour")
    return 0