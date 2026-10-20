"""OpenAI-compatible API for the instances — chat with them from any client on your tailnet.

  GET  /v1/models              each instance is a "model" (shaft, wren, …)
  POST /v1/chat/completions    stream or not; the instance answers as itself, with its tools
  POST /v1/chat/interject      a message for the conversation whose reply is still running: the instance reads it at
                               its next step and its reply covers it (202), or 409 when no reply is running
  POST /v1/chat/end            the conversation is over: written up now (202), 404 if none is open, 409 while it replies
  POST /v1/inbox/<instance>    the doorbell: drop a message in its inbox (read at its next wake), and
                               optionally wake it now — for scripts, a phone shortcut, CI on the tailnet

Each request is a normal sandboxed run (broker, run lock, gate, commit). The client's conversation
is the dialogue; the instance's own identity and memory come first. A conversation (same instance,
same first user message — or, if the client sends X-Shaft-Conversation, that name: the web UI does) keeps one id, and
the channel the client names in X-Shaft-Channel (web, telegram; `api` when absent) — where a message the instance sends
the operator while it is open alerts them (channel.py); after IDLE_MINUTES (SHAFT_IDLE_MINUTES) without a message it is closed and its journal entry is
written. Tool activity and status are sent as `reasoning_content`, which "thinking"-aware clients
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

from .core import (API_TALKS as TALKS, IDLE_MINUTES, SUP_DIR, UnsafePath, instance_names, instance_root, knob, log,
                   port, safe_write, secrets_env, whole)
from .runner import Reply, busy_with, request_yield, run_agent, spawn_recover

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
CLIENT_CHANNELS = ("web", "telegram")    # X-Shaft-Channel: where the operator is talking (channel.py routes alerts there)       # one line per accepted message: when, instance, source, wake outcome


def allowed_logins() -> set[str]:
    raw = os.environ.get("SHAFT_API_LOGINS") or secrets_env().get("SHAFT_API_LOGINS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


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

    def write_up_idle(self) -> None:
        with self.lock:
            due = [t for t in self.talks.values()
                   if not t.get("written") and time.time() - t.get("last", 0) > IDLE_MINUTES * 60]
            for t in due:
                t["written"] = True
            if due:
                self._save()
        for model in sorted({t["model"] for t in due}):
            spawn_recover(model)


class Live:
    """Replies running now, by talk id, and the messages sent to them while they run. Until the agent process starts
    (it may wait for another conversation) they are held here; then each goes to its .tmp/interject-<talk id>.jsonl,
    one JSON line, which the agent reads at the start of each step."""

    def __init__(self):
        self.lock = threading.Lock()
        self.replies: dict[str, dict] = {}

    @staticmethod
    def _rel(talk_id: str) -> str:
        return f".tmp/interject-{talk_id}.jsonl"

    def begin(self, model: str, talk_id: str) -> dict:
        entry = {"model": model, "talk_id": talk_id, "held": [], "started": False}
        with self.lock:
            self.replies[talk_id] = entry               # a second request in the same talk: the newest takes them
            (instance_root(model) / self._rel(talk_id)).unlink(missing_ok=True)   # a crashed run's leftovers
        return entry

    def _write(self, entry: dict, text: str) -> None:
        safe_write(instance_root(entry["model"]), self._rel(entry["talk_id"]),
                   json.dumps({"text": text}, ensure_ascii=False) + "\n", append=True)

    def started(self, entry: dict) -> None:
        with self.lock:
            for text in entry["held"]:
                self._write(entry, text)
            entry["held"], entry["started"] = [], True

    def interject(self, talk_id: str, text: str) -> bool:
        with self.lock:
            entry = self.replies.get(talk_id)
            if entry is None:
                return False
            if entry["started"]:
                self._write(entry, text)
            else:
                entry["held"].append(text)
            return True

    def end(self, entry: dict) -> None:
        with self.lock:
            if self.replies.get(entry["talk_id"]) is entry:
                del self.replies[entry["talk_id"]]
            (instance_root(entry["model"]) / self._rel(entry["talk_id"])).unlink(missing_ok=True)


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
            return self._interject(model, messages, cid)
        self._chat(model, messages, bool(body.get("stream")), self.headers.get("User-Agent", "an app"), cid, channel)

    def _end(self, model: str, messages: list[dict], cid: str | None) -> None:
        """POST /v1/chat/end: the conversation is over — written up now instead of after SHAFT_IDLE_MINUTES."""
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

    def _interject(self, model: str, messages: list[dict], cid: str | None = None) -> None:
        """The last user message joins the reply running in this conversation (same first user message)."""
        content = messages[-1].get("content")
        text = (content if isinstance(content, str) else "\n".join(
            p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")).strip()
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
              channel: str = "api") -> None:
        events: queue.Queue = queue.Queue()
        procs: list = []
        talk_id = self.conversations.talk_id(model, messages, cid, channel)
        live = self.live.begin(model, talk_id)
        request = {"messages": messages, "talk_id": talk_id, "client": app.split()[0][:40]}

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
                events.put(ev)

        cancelled = threading.Event()
        answered = threading.Event()

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
                if not answered.is_set():   # the agent ended without a reply: say why, never an empty answer
                    err = (procs[0].stderr.read() if procs and procs[0].stderr else "")[-800:].strip()
                    events.put({"type": "error", "message": f"{model} stopped without answering (exit {code})"
                                                           + (f": {err}" if err else "")})
            except Exception as e:   # surface, never hang the client
                events.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
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
                if t == "done":
                    break
                if t == "content":
                    step.append(ev["text"])
                    continue
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
                self.wfile.write(_chunk(cid, model, {}, "stop", interjections_heard=heard))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            elif error and not content:
                self._error(502, error, "server_error")
            else:
                self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
                                 "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                     "role": "assistant", "content": "".join(content),
                                     "reasoning_content": "".join(notes)}}],
                                 "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                 "interjections_heard": heard})
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
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True

    def janitor():
        while True:
            time.sleep(60)
            try:
                Handler.conversations.write_up_idle()
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


def url() -> str:
    st = json.loads(_tailscale("status", "--json").stdout or "{}")
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
            st = json.loads(_tailscale("status", "--json").stdout or "{}")
            me = st.get("User", {}).get(str(st.get("Self", {}).get("UserID")), {}).get("LoginName")
            if not me:
                print("cannot read your Tailscale login (is Tailscale running?)")
                return 1
            _set_secret("SHAFT_API_LOGINS", me)
        if autonomy.service("api", "on"):
            return 1
        r = _tailscale("serve", "--bg", f"--https={HTTPS_PORT}", f"http://127.0.0.1:{PORT}")
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