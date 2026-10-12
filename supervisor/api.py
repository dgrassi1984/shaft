"""OpenAI-compatible API for the instances — chat with them from any client on your tailnet.

  GET  /v1/models              each instance is a "model" (shaft, wren, …)
  POST /v1/chat/completions    stream or not; the instance answers as itself, with its tools

Each request is a normal sandboxed run (broker, run lock, gate, commit). The client's conversation
is the dialogue; the instance's own identity and memory come first. A conversation (same instance,
same first user message) keeps one id; after IDLE_MINUTES without a message its journal entry is
written. Tool activity and status are sent as `reasoning_content`, which "thinking"-aware clients
show and others ignore.

Security: the server listens on 127.0.0.1 only; `tailscale serve` exposes it to the tailnet (never
Funnel). Every request needs the bearer key SHAFT_API_KEY from .env AND a Tailscale-User-Login
header (set by `tailscale serve`) in SHAFT_API_LOGINS.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import signal
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import SUP_DIR, instance_names, knob, log, secrets_env
from .runner import busy_with, request_yield, run_agent, spawn_recover

PORT, _ = knob("SHAFT_API_PORT", "8781", int)
IDLE_MINUTES, _ = knob("SHAFT_API_IDLE_MINUTES", "20", int)
TALKS = SUP_DIR / "api_talks.json"


def allowed_logins() -> set[str]:
    raw = os.environ.get("SHAFT_API_LOGINS") or secrets_env().get("SHAFT_API_LOGINS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


class Conversations:
    """Maps a client conversation to an instance talk id; writes it up after it goes idle."""

    def __init__(self):
        self.lock = threading.Lock()
        self.talks = json.loads(TALKS.read_text()) if TALKS.exists() else {}

    def _save(self) -> None:
        TALKS.parent.mkdir(exist_ok=True)
        TALKS.write_text(json.dumps(self.talks, indent=1))

    def talk_id(self, model: str, messages: list[dict]) -> str:
        first = next((m for m in messages if m.get("role") == "user"), {})
        key = hashlib.sha256(json.dumps([model, first.get("content")], sort_keys=True).encode()).hexdigest()[:16]
        with self.lock:
            t = self.talks.get(key)
            if not t or t.get("written"):   # new, or its earlier part is already in the journal
                t = {"talk_id": f"api-{uuid.uuid4().hex[:10]}", "model": model}
                self.talks[key] = t
            t["last"] = time.time()
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


def _chunk(cid: str, model: str, delta: dict, finish: str | None = None) -> bytes:
    return b"data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                                   "model": model, "choices": [{"index": 0, "delta": delta,
                                                                "finish_reason": finish}]}).encode() + b"\n\n"


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
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "shaft-api/1"
    conversations: Conversations

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

    def do_POST(self):
        if not self._authorised():
            return
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self._error(404, f"no route {self.path}")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._error(400, "body must be JSON")
        model = body.get("model")
        if model not in instance_names():
            return self._error(404, f"no instance {model!r}; models: {', '.join(instance_names())}")
        messages = body.get("messages") or []
        if not messages or messages[-1].get("role") != "user":
            return self._error(400, "messages must end with a user message")
        self._chat(model, messages, bool(body.get("stream")), self.headers.get("User-Agent", "an app"))

    def _chat(self, model: str, messages: list[dict], stream: bool, app: str) -> None:
        events: queue.Queue = queue.Queue()
        procs: list = []
        talk_id = self.conversations.talk_id(model, messages)
        request = {"messages": messages, "talk_id": talk_id, "client": app.split()[0][:40]}

        def driver(proc):
            procs.append(proc)
            proc.stdin.write(json.dumps(request))
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    events.put(json.loads(line))
                except ValueError:
                    pass

        def work():
            try:
                if (b := busy_with(model)) and not b.startswith(("talk", "api")):
                    request_yield([model])
                    events.put({"type": "status", "phase": f"waiting for {model} to finish its {b.split()[0]}"})
                run_agent(model, "api", ["answer", "--json"], interactive=True, driver=driver)
            except Exception as e:   # surface, never hang the client
                events.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
            events.put({"type": "done"})

        threading.Thread(target=work, daemon=True).start()
        cid = f"chatcmpl-{uuid.uuid4().hex[:20]}"
        content, notes, error = [], [], None
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        try:
            if stream:
                self.wfile.write(_chunk(cid, model, {"role": "assistant", "content": ""}))
                self.wfile.flush()
            while True:
                try:
                    ev = events.get(timeout=10)
                except queue.Empty:
                    if stream:
                        self.wfile.write(b": still working\n\n")   # keep-alive for proxies and clients
                        self.wfile.flush()
                    continue
                t = ev["type"]
                if t == "done":
                    break
                if t == "content":
                    content.append(ev["text"])
                    if stream:
                        self.wfile.write(_chunk(cid, model, {"content": ev["text"]}))
                elif t == "error":
                    error = ev["message"]
                elif (n := _note(ev)) is not None:
                    notes.append(n)
                    if stream:
                        self.wfile.write(_chunk(cid, model, {"reasoning_content": n}))
                if stream:
                    self.wfile.flush()
            if stream:
                if error and not content:
                    self.wfile.write(_chunk(cid, model, {"content": f"[error: {error}]"}))
                self.wfile.write(_chunk(cid, model, {}, "stop"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            elif error and not content:
                self._error(502, error, "server_error")
            else:
                self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
                                 "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                     "role": "assistant", "content": "".join(content),
                                     "reasoning_content": "".join(notes)}}],
                                 "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
        except (BrokenPipeError, ConnectionResetError):
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
    Handler.conversations = Conversations()
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
