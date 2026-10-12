"""The broker: the supervisor's door to the outside world, lent to one instance per run.

Instances have no internet and no secrets. While an instance runs, the supervisor
serves this broker on a localhost port that only that instance's sandbox may reach,
authenticated by a per-run token. It holds the API keys, refuses requests to private
networks, delivers messages between instances, enforces a per-run call budget, and
logs every call (.supervisor/broker.log).
"""
from __future__ import annotations

import ipaddress
import json
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
MAX_FETCH_BYTES = 3_000_000
MAX_MESSAGE_CHARS = 8000
OPERATOR_DAILY_LIMIT = 3
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def _spec(name, description, properties, required=None):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties,
        "required": required if required is not None else list(properties)}}}


S = {"type": "string"}
SPECS = {
    "web_search": _spec("web_search", "Search the web (Google). Returns titles, URLs and snippets.",
                        {"query": S, "page": {"type": "integer", "description": "1-based, default 1"}}, ["query"]),
    "fetch_page": _spec("fetch_page", "Fetch a public web page and return its readable text.",
                        {"url": S, "max_chars": {"type": "integer", "description": "default 6000"}}, ["url"]),
    "send_message": _spec("send_message", "Send a message to another SHAFT instance, or to 'operator' "
                          "(Daniele) to ask him something or tell him something — he gets a notification "
                          "and his reply arrives in your inbox. At most 3 operator messages per day, so "
                          "make them count.",
                          {"to": S, "text": S,
                           "kind": {"type": "string", "enum": ["question", "note"],
                                    "description": "for the operator: question (wants an answer) or note"}},
                          ["to", "text"]),
    "list_instances": _spec("list_instances", "List the other SHAFT instances you can message.", {}, []),
    "delegate_coding": _spec(
        "delegate_coding",
        "Hand a coding task to an Opus-class model. Describe WHAT you want changed in your code, plugins, "
        "skills or tests and WHY. It reads your files, writes the change; the supervisor tests it on a copy "
        "and applies it to your folder only if all tests pass. It cannot touch your self/, memory/ or "
        "config.env. Slow (minutes) and costs real money: at most 2 per cycle, for real coding work only. "
        "Your commit will record that the code was delegated.",
        {"task": S}),
}
MAX_HANDOFFS_PER_RUN = 2


def notify(title: str, text: str) -> None:
    """macOS notification; never fails the call that triggered it."""
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    body = " ".join(text.split())[:220]
    try:
        subprocess.run(["osascript", "-e", f"display notification {q(body)} with title {q(title)} sound name \"Glass\""],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


class BlockedAddress(Exception):
    pass


def check_public(url: str) -> None:
    """Refuse anything but http(s) to public addresses (no localhost, LAN, link-local, metadata)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedAddress(f"only http(s) URLs with a host are allowed: {url!r}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise BlockedAddress(f"cannot resolve {parts.hostname}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise BlockedAddress(f"{parts.hostname} resolves to non-public address {ip}")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects())


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre"}

    def __init__(self):
        super().__init__()
        self.out, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)

    def text(self) -> str:
        raw = "".join(self.out)
        return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", raw)).strip()


def html_to_text(html: str) -> tuple[str, str]:
    p = _Text()
    p.feed(html)
    return p.title.strip(), p.text()


class Broker:
    def __init__(self, name: str, project: Path, secrets_env: dict[str, str], log_path: Path,
                 max_calls: int = 60):
        self.name, self.project, self.secrets = name, project, secrets_env
        self.log_path, self.max_calls = log_path, max_calls
        self.token = secrets.token_hex(16)
        self.calls = 0
        self.counts: dict[str, int] = {}
        self.remembered: dict[str, list] = {}    # original cloud-model turns, resent verbatim
        self.handoffs: list[dict] = []           # coding tasks delegated during this run
        self.server: ThreadingHTTPServer | None = None

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> str:
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code: int, obj: dict):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorised(self) -> bool:
                if secrets.compare_digest(self.headers.get("X-Shaft-Token", ""), broker.token):
                    return True
                self._reply(403, {"ok": False, "error": "bad token"})
                return False

            def do_GET(self):
                if not self._authorised():
                    return
                if self.path == "/tools":
                    return self._reply(200, {"tools": list(SPECS.values())})
                self._reply(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if not self._authorised():
                    return
                if self.path.rstrip("/") == "/llm/v1/chat/completions":
                    return self._llm()
                m = re.fullmatch(r"/tools/([a-z_]+)", self.path)
                if not m or m.group(1) not in SPECS:
                    return self._reply(404, {"ok": False, "error": f"no tool {self.path!r}"})
                try:
                    args = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    return self._reply(400, {"ok": False, "error": "body must be JSON"})
                self._reply(200, broker.call(m.group(1), args if isinstance(args, dict) else {}))

            def _llm(self):
                from . import cloudllm
                body: dict = {}
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                    reply = cloudllm.complete(broker.name, body, broker.secrets.get("ANTHROPIC_API_KEY", ""),
                                              broker.remembered)
                except cloudllm.CloudError as e:
                    broker._log(f"{broker.name} llm {body.get('model', '?')} -> ERROR {e}")
                    return self._reply(e.status, {"error": {"message": str(e)}})
                except ValueError:
                    return self._reply(400, {"error": {"message": "body must be JSON"}})
                u = reply["usage"]
                broker._log(f"{broker.name} llm {body.get('model')} -> {reply['choices'][0]['finish_reason']} "
                            f"(in {u['prompt_tokens']}, out {u['completion_tokens']})")
                if body.get("stream"):
                    payload = cloudllm.as_sse(reply)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self._reply(200, reply)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def port(self) -> int:
        return self.server.server_address[1] if self.server else 0

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    # -- calls -------------------------------------------------------------------
    def call(self, tool: str, args: dict) -> dict:
        started = time.monotonic()
        self.calls += 1
        self.counts[tool] = self.counts.get(tool, 0) + 1
        if self.calls > self.max_calls:
            result = {"ok": False, "error": f"call budget for this run exhausted ({self.max_calls})"}
        else:
            try:
                result = {"ok": True, "result": getattr(self, f"_{tool}")(**args)}
            except TypeError as e:
                result = {"ok": False, "error": f"bad arguments: {e}"}
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        brief = json.dumps(args, ensure_ascii=False)[:160]
        status = "ok" if result["ok"] else f"ERROR {result['error'][:200]}"
        self._log(f"{self.name} {tool} {brief} -> {status} ({(time.monotonic() - started) * 1000:.0f} ms)")
        return result

    def _log(self, line: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")

    # -- tools ---------------------------------------------------------------------
    def _web_search(self, query: str, page: int = 1) -> str:
        key = self.secrets.get("SCRAPEBADGER_API_KEY")
        if not key:
            raise RuntimeError(f"web_search needs SCRAPEBADGER_API_KEY in {self.project / '.env'}")
        params = urllib.parse.urlencode({"q": query, "mode": "fast", "start": max(0, (int(page) - 1) * 10)})
        req = urllib.request.Request(f"https://scrapebadger.com/v1/google/search?{params}",
                                     headers={"x-api-key": key, "User-Agent": "shaft-broker"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.load(resp)
        rows = data.get("organic_results") or []
        if not rows:
            return f"no results for {query!r}"
        return "\n\n".join(f"{i}. {r.get('title', '')}\n   {r.get('link', '')}\n   {r.get('snippet', '')}"
                           for i, r in enumerate(rows, 1))

    def _fetch_page(self, url: str, max_chars: int = 6000) -> str:
        check_public(url)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,text/plain,*/*"})
        with _opener.open(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read(MAX_FETCH_BYTES)
            final = resp.geturl()
        charset = re.search(r"charset=([\w-]+)", ctype)
        text = raw.decode(charset.group(1) if charset else "utf-8", errors="replace")
        if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            title, text = html_to_text(text)
        elif any(t in ctype for t in ("text/", "json", "xml")):
            title = ""
        else:
            return f"{final}: not a text page ({ctype or 'unknown type'}, {len(raw)} bytes)"
        max_chars = max(500, min(int(max_chars), 20000))
        cut = f"\n[... {len(text) - max_chars} more chars]" if len(text) > max_chars else ""
        return f"{title}\n{final}\n\n{text[:max_chars]}{cut}".strip()

    def _send_message(self, to: str, text: str, kind: str = "note") -> str:
        if not text.strip():
            raise ValueError("empty message")
        if self.counts["send_message"] > 10:
            raise RuntimeError("at most 10 messages per run")
        text = text[:MAX_MESSAGE_CHARS]
        stamp = datetime.now()
        if to == "operator":
            inbox = self.project / ".supervisor" / "operator-inbox"
            kind = "question" if kind == "question" else "note"
            today = list(inbox.glob(f"{stamp:%Y%m%d}-*-from-{self.name}.md")) if inbox.is_dir() else []
            if len(today) >= OPERATOR_DAILY_LIMIT:
                raise RuntimeError(f"you already sent {len(today)} messages to the operator today "
                                   f"(limit {OPERATOR_DAILY_LIMIT}); keep it for tomorrow")
            inbox.mkdir(parents=True, exist_ok=True)
            path = inbox / f"{stamp:%Y%m%d-%H%M%S-%f}-from-{self.name}.md"
            path.write_text(f"From: {self.name}\nTo: operator\nKind: {kind}\nDate: {stamp:%Y-%m-%d %H:%M}\n\n"
                            f"{text.strip()}\n")
            notify(f"SHAFT · {self.name} {'asks' if kind == 'question' else 'says'}", text)
            return f"delivered to the operator ({kind}); his reply, if any, will arrive in your inbox"
        elif NAME_RE.match(to) and to != self.name and (self.project / "instances" / to / "shaft").is_dir():
            from .core import safe_write   # the recipient's folder is instance-controlled: no symlinks followed
            safe_write(self.project / "instances" / to, f"inbox/{stamp:%Y%m%d-%H%M%S-%f}-from-{self.name}.md",
                       f"From: {self.name}\nTo: {to}\nDate: {stamp:%Y-%m-%d %H:%M}\n\n{text.strip()}\n")
        else:
            raise ValueError(f"unknown recipient {to!r}; try list_instances or 'operator'")
        return f"delivered to {to}"

    def _delegate_coding(self, task: str) -> str:
        from . import coder
        if len(self.handoffs) >= MAX_HANDOFFS_PER_RUN:
            raise RuntimeError(f"at most {MAX_HANDOFFS_PER_RUN} coding handoffs per cycle")
        if len(task.strip()) < 20:
            raise ValueError("describe the task: what to change and why")
        try:
            r = coder.delegate(self.project / "instances" / self.name, self.name, task)
        except coder.CoderError as e:
            self.handoffs.append({"task": task, "applied": False, "error": str(e)})
            raise
        r["task"] = task
        self.handoffs.append(r)
        lines = [f"{'APPLIED' if r['applied'] else 'NOT applied'} — {r['model']}, ${r['usd']:.2f}",
                 f"files: {', '.join(r['files']) or '(none)'}", f"summary: {r['summary']}"]
        if r.get("error"):
            lines.append(f"problem: {r['error']}")
        if r.get("test_output"):
            lines.append(f"test output (end):\n{r['test_output'][-1500:]}")
        if r["applied"]:
            lines.append("The new code takes effect in your next cycle; plugins appear as tools then.")
        return "\n".join(lines)

    def _list_instances(self) -> str:
        base = self.project / "instances"
        names = sorted(p.name for p in base.iterdir() if (p / "shaft").is_dir() and p.name != self.name)
        return ("instances: " + ", ".join(names)) if names else "no other instances exist yet"
