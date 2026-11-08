"""OpenAI-compatible API for the instances — chat with them from any client on your tailnet.

  GET  /v1/models              each instance is a "model" (shaft, wren, …)
  POST /v1/chat/completions    stream or not; the instance answers as itself, with its tools; X-Shaft-Request-Id
                               (non-streamed): asked again, the same answer, not run again (Answered)
  POST /v1/chat/interject      a message for the conversation whose reply is still running: the instance reads it at
                               its next step and its reply covers it (202), or 409 when no reply is running
  POST /v1/chat/end            the conversation is over: write-up requested (202), 404 if none is open, 409 while it replies
  POST /v1/inbox/<instance>    the doorbell: drop a message in its inbox (read at its next wake), and
                               optionally wake it now — for scripts, a phone shortcut, CI on the tailnet

Each request is a normal sandboxed run (broker, run lock, gate, commit). The client's conversation
is the dialogue; the instance's own identity and memory come first. A conversation (same instance,
same first user message — or, if the client sends X-Shaft-Conversation, that name: the web UI does) keeps one id, and
the channel the client names in X-Shaft-Channel (web, telegram; `api` when absent) — where a message the instance sends
the operator while it is open alerts them (channel.py); after IDLE_MINUTES (SHAFT_IDLE_MINUTES) without a message
(counted from when its last reply ended, never while one runs) it is closed and a journal write-up is
scheduled (the entry is written in the background, not before the 202). Tool activity and status are sent as `reasoning_content`, which "thinking"-aware clients
show and others ignore.

A message sent to /v1/chat/interject while that conversation's reply runs is appended to the agent's
.tmp/interject-<talk id>.jsonl; the agent takes it at its next step and says so (`heard`). The reply's JSON counts
them in `interjections_heard` (in the order sent): the client answers any others with its next request
(docs/plans/PLAN-interject.md).

Security: the server listens on 127.0.0.1 only; `tailscale serve` exposes it to the tailnet (never
Funnel). Every request needs the bearer key SHAFT_API_KEY from .env AND a Tailscale-User-Login
header (set by `tailscale serve`) in SHAFT_API_LOGINS.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import tom
from .core import (API_TALKS as TALKS, IDLE_MINUTES, SUP_DIR, UnsafePath, instance_names, instance_root, knob, log,
                   port, safe_write, secrets_env, unlink_beneath, whole)
from .runner import REFUSED, Reply, busy_with, request_yield, run_agent, spawn_recover, stop_runs_on_signal

PORT, _ = knob(
    "SHAFT_API_PORT", "8781", port, "the API's local port (OpenAI-compatible; the web UI and Telegram chat through it)",
    group="Ports and paths", services=("api", "web", "telegram"), web=False)
INBOX_PER_HOUR, _ = knob(
    "SHAFT_INBOX_PER_HOUR", "30", whole, "messages per hour the API's inbox (doorbell) accepts for one instance",
    group="Inbox and benchmarks", services=("api",))
INBOX_WAKES_PER_HOUR, _ = knob(
    "SHAFT_INBOX_WAKES_PER_HOUR", "4", whole, "wakes per hour inbox messages may start for one instance",
    group="Inbox and benchmarks", services=("api",))
DOORBELL_LOG = "doorbell.jsonl"
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # X-Shaft-Conversation: a client's own name for a conversation
CLIENT_CHANNELS = ("web", "telegram")    # X-Shaft-Channel: where the operator is talking (channel.py routes alerts there)
# The rule of _chat below, told to the instance with every request (a system message: the instance's talk.py puts the
# app's rules in its system prompt, whatever its own prompts say). Untold, instances wrote the answer beside a tool call
# and ended with "Stored." or "." — all the operator got (shaft and wren, 2026-10-18..22).
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
            if not t or t.get("written"):   # new, or its earlier part is already in the journal
                t = {"talk_id": f"api-{uuid.uuid4().hex[:10]}", "model": model}
                self.talks[key] = t
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
        self.replies: dict[str, dict] = {}       # talk id -> {"requests": [entry, ...], "held": [text, ...]}

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

    def _write(self, entry: dict, text: str) -> None:
        safe_write(instance_root(entry["model"]), self._rel(entry["talk_id"]),
                   json.dumps({"text": text}, ensure_ascii=False) + "\n", append=True)

    def started(self, entry: dict) -> None:
        with self.lock:
            talk = self.replies[entry["talk_id"]]
            self._drop(entry["model"], entry["talk_id"])   # an earlier agent's, unheard: its client sends it again
            entry["started"] = True
            held, talk["held"] = talk["held"], []
            for text in held:
                self._write(entry, text)

    def interject(self, talk_id: str, text: str) -> bool:
        with self.lock:
            talk = self.replies.get(talk_id)
            if talk is None:
                return False
            running = [e for e in talk["requests"] if e["started"]]
            if running:
                self._write(running[-1], text)
            else:
                talk["held"].append(text)
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


def _chunk(cid: str, model: str, delta: dict, finish: str | None = None, **extra) -> bytes:
    """One stream chunk; `extra` adds top-level keys OpenAI clients ignore (the last carries interjections_heard)."""
    return b"data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                                   "model": model, "choices": [{"index": 0, "delta": delta,
                                                                "finish_reason": finish}], **extra}).encode() + b"\n\n"


def _note(ev: dict) -> str | None:
    """Tool activity and status as human-readable reasoning lines."""
    t = ev["type"]
    if t == "status" and ev.get("phase") not in ("reading my memory",):
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
    conversations: Conversations
    live: Live
    answered: Answered | None = None

    def log_message(self, fmt, *args):
        log("api " + (fmt % args))

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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
            rows = [json.loads(l) for l in (SUP_DIR / DOORBELL_LOG).read_text().splitlines() if l.strip()]
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
        if route == "/v1/chat/interject":
            return self._interject(model, messages, cid, channel)
        rid = self.headers.get("X-Shaft-Request-Id") or None
        if rid is not None and not CONVERSATION_ID.match(rid):
            return self._error(400, "X-Shaft-Request-Id is 1-64 letters, digits, - or _")
        self._chat(model, messages, bool(body.get("stream")), self.headers.get("User-Agent", "an app"), cid, channel,
                   rid)

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
        spawn_recover(model)
        log(f"api: conversation {talk_id} with {model} ended by its client")
        self._json(202, {"ended": talk_id})

    def _interject(self, model: str, messages: list[dict], cid: str | None = None, channel: str = "api") -> None:
        """The last user message joins the reply running in this conversation (same first user message)."""
        text = _last_text(messages)
        if not text:
            return self._error(400, "the message to add is empty")
        talk_id = self.conversations.current(model, messages, cid)
        try:
            ok = bool(talk_id) and self.live.interject(talk_id, text)
        except (OSError, UnsafePath) as e:
            return self._error(500, f"could not pass it to {model}: {e}", "server_error")
        if not ok:
            return self._error(409, f"{model} is not replying in this conversation now: send it as a new message",
                               "conflict")
        tom.heard(model, text, channel)
        log(f"api: a message for {model}'s running reply ({talk_id})")
        self._json(202, {"interjected": True, "talk_id": talk_id})

    def _client_gone(self) -> bool:
        """The client hung up: its socket is readable and a peek returns no data."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _chat(self, model: str, messages: list[dict], stream: bool, app: str, cid: str | None = None,
              channel: str = "api", rid: str | None = None) -> None:
        events: queue.Queue = queue.Queue()
        procs: list = []
        if rid and not stream:
            if Handler.answered is None:
                Handler.answered = Answered()
            if (done := Handler.answered.get(model, rid)) is not None:
                log(f"api: {model}: request {rid} was answered already — the same answer, not run again")
                return self._json(200, {**done, "replayed": True})   # its client tells a kept answer from a new one
        if text := _last_text(messages):
            tom.heard(model, text, channel)         # what the operator said, for scoring reply predictions (tom.py)
        talk_id = self.conversations.talk_id(model, messages, cid, channel)
        live = self.live.begin(model, talk_id)
        request = {"messages": [*(m for m in messages if m.get("role") == "system"),
                                {"role": "system", "content": LAST_STEP_NOTE},
                                *(m for m in messages if m.get("role") != "system")],
                   "talk_id": talk_id, "client": app.split()[0][:40]}

        def driver(proc):
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

        def work():
            reply = Reply(model, "api")      # a running cycle of this instance pauses for this reply, then resumes
            try:
                reply.__enter__()
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
                code = run_agent(model, "api", ["answer"], interactive=True, driver=driver)
                if code == REFUSED:
                    events.put({"type": "error", "message": f"the supervisor refused to start {model}'s run: its "
                                "code could not be checked (the reason is in the panel's alerts and supervisor.log)"})
                elif not answered.is_set() and not errored.is_set():   # keep the agent's own reason if it gave one
                    err = (procs[0].stderr.read() if procs and procs[0].stderr else "")[-800:].strip()
                    events.put({"type": "error", "message": f"{model} stopped without answering (exit {code})"
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
                if reply.f is not None:
                    reply.__exit__(None, None, None)
                events.put({"type": "done"})

        threading.Thread(target=work, daemon=True).start()
        cid = f"chatcmpl-{uuid.uuid4().hex[:20]}"
        # Only the reply's last step is its answer. Text the model writes before calling a tool ("let me check my
        # notes…") is narration: it goes to reasoning_content with the tool activity, never into the reply — it
        # reached the operator's Telegram as if the instance had said it (wren, 2026-10-14: 25 of 44 Telegram replies had
        # tool steps). So a step's text is held until the next event tells which it was.
        content, step, notes, error, ended, heard = [], [], [], None, None, 0
        tools_ran = False
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        try:
            if stream:
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
                    continue
                if t in ("tool_start", "tool_end"):
                    tools_ran = True
                if t == "tool_start" and step:           # that step called tools: its text was narration
                    said = "".join(step).strip()
                    step = []
                    if said:
                        notes.append(f"{said}\n")
                        if stream:
                            self.wfile.write(_chunk(cid, model, {"reasoning_content": f"{said}\n"}))
                if t == "turn_end":
                    ended = ev
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
                if stream:
                    self.wfile.flush()
            if not content and step:                     # interrupted before a turn_end: the text so far
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
                          "interjections_heard": heard}
                if rid:
                    try:
                        Handler.answered.put(model, rid, answer)   # made in the check above
                    except OSError as e:
                        log(f"api: {model}: request {rid}'s answer FAILED to be kept ({e}); asked again, it runs again")
                self._json(200, answer)
        except (BrokenPipeError, ConnectionResetError):
            cancelled.set()
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
        print("no SHAFT_API_LOGINS — run: shaft api on")
        return 2
    from .version import record_service
    record_service("api", {"port": PORT, "idle_minutes": IDLE_MINUTES})
    Handler.conversations = Conversations()
    Handler.live = Live()
    stop_runs_on_signal()                        # stopped (launchd), it ends its replies' agents first
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
    exe = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    return subprocess.run([exe, *args], capture_output=True, text=True)


def _set_secret(name: str, value: str) -> None:
    path = SUP_DIR.parent / ".env"
    lines = [l for l in (path.read_text().splitlines() if path.exists() else []) if not l.startswith(f"{name}=")]
    path.write_text("\n".join(lines + [f"{name}={value}"]) + "\n")
    path.chmod(0o600)


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


def url() -> str:
    st = _tailscale_status()
    if st is None:
        return "(the tailnet address is not available from this session)"
    host = (st.get("Self", {}).get("DNSName") or "").rstrip(".")
    return f"https://{host}:{HTTPS_PORT}/v1" if host else "(tailscale is not running)"


def command(action: str) -> int:
    from . import autonomy
    if action == "serve":
        return serve()
    if action == "key":
        key = secrets_env().get("SHAFT_API_KEY")
        print(key or "no key yet — run: shaft api on")
        return 0 if key else 1
    if action == "inbox-key":
        key = secrets_env().get("SHAFT_INBOX_KEY")
        if not key:
            key = "sk-shaft-inbox-" + secrets.token_urlsafe(32)
            _set_secret("SHAFT_INBOX_KEY", key)
            log("api: inbox key created")
        print(key)
        print(f"  POST {url().removesuffix('/v1')}/v1/inbox/<instance>   (Authorization: Bearer <this key>)\n"
              f"  JSON {{\"text\": \"…\", \"source\": \"phone\", \"wake\": true}} or any body (a webhook's), "
              f"source from X-Shaft-Source", file=sys.stderr)
        return 0
    if action == "on":
        env = secrets_env()
        if not env.get("SHAFT_API_KEY"):
            _set_secret("SHAFT_API_KEY", "sk-shaft-" + secrets.token_urlsafe(32))
        if not env.get("SHAFT_INBOX_KEY"):
            _set_secret("SHAFT_INBOX_KEY", "sk-shaft-inbox-" + secrets.token_urlsafe(32))
        if not env.get("SHAFT_API_LOGINS"):
            st = _tailscale_status() or {}
            me = st.get("User", {}).get(str(st.get("Self", {}).get("UserID")), {}).get("LoginName")
            if not me:
                print("cannot read your Tailscale login (is Tailscale running?)")
                return 1
            _set_secret("SHAFT_API_LOGINS", me)
        was_running = autonomy.running("api")
        if autonomy.service("api", "on"):
            return 1
        r = _tailscale("serve", "--bg", f"--https={HTTPS_PORT}", f"http://127.0.0.1:{PORT}")
        if _outside_gui(r):
            if was_running:       # the existing mapping stays in place on a restart
                log("api restarted; tailscale serve left as it was (its CLI refuses to run outside the GUI session)")
                print("API restarted; tailscale serve left as it was (its CLI refuses to run outside the GUI session)")
                return 0
            message = ("API started but is not exposed on the tailnet because Tailscale's CLI refuses to run "
                       "outside the GUI session; run shaft api on in a terminal of the logged-in session.")
            print(message, file=sys.stderr)
            log(message)
            return 1
        if r.returncode != 0:
            print(f"tailscale serve failed: {(r.stderr or r.stdout).strip()}")
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