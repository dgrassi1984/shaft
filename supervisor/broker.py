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
import os
import signal
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
# Messages to the operator per instance per day; 0 = no limit (Daniele, 2026-10-13, with Telegram: "No limit").
OPERATOR_DAILY_LIMIT = int(os.environ.get("SHAFT_OPERATOR_PER_DAY") or 0)
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


JUDGMENTS_BUDGET = 5500   # under the 6,000 characters at which a template instance's runtime cuts a tool result


def _when(ts: str) -> str:
    return (ts or "?")[:16].replace("T", " ")


def judgment_text(j: dict) -> str:
    """One verdict with every field the judge gave — the reasons behind each rating, not just the grades."""
    out = [f"### {_when(j.get('episode_ts'))} — {j.get('goal', '')}",
           f"I claimed {j.get('claimed')} at {j.get('confidence')}% confidence. Judge ({j.get('judge', '?')}): criterion "
           f"met **{j.get('criterion_met')}**, my claim supported: {j.get('claim_supported')}, criterion "
           f"{j.get('criterion_quality')}" + (f", failure: {j['failure_mode']}" if j.get("failure_mode") else "") + "."]
    if j.get("reasoning"):
        out.append(f"Reasoning: {j['reasoning']}")
    if j.get("difficulty") is not None:
        out.append(f"Difficulty {j['difficulty']}/5" + (f": {j['difficulty_reason']}" if j.get("difficulty_reason") else "")
                   + (f" · novelty {j['novelty']}/5" if j.get("novelty") is not None else ""))
    if j.get("origin"):
        out.append(f"Origin: {j['origin']} (I declared {j.get('origin_declared') or 'none'}; matches: "
                   f"{j.get('origin_supported')})" + (f" — {j['origin_evidence']}" if j.get("origin_evidence") else ""))
    if j.get("model"):
        out.append(f"My model in that cycle: {j['model']}")
    s = j.get("second")
    if isinstance(s, dict) and s.get("error"):
        out.append(f"Second judge ({s.get('judge')}): no verdict ({s['error'][:160]})")
    elif isinstance(s, dict):
        agree = "agrees" if s.get("criterion_met") == j.get("criterion_met") else "DISAGREES"
        out.append(f"Second judge ({s.get('judge', '?').removeprefix('openrouter/')}, blind) {agree}: criterion met "
                   f"**{s.get('criterion_met')}**, claim supported {s.get('claim_supported')}, difficulty "
                   f"{s.get('difficulty')}/5" + (f", failure: {s['failure_mode']}" if s.get("failure_mode") else "")
                   + f". Reasoning: {s.get('reasoning', '')}")
    return "\n".join(out)


def judgments_text(rows: list[dict], limit: int = 20, cycle: str = "", before: str = "",
                   budget: int = JUDGMENTS_BUDGET) -> str:
    """Whole verdicts, newest first, as many as fit in `budget` — never one cut in half (wren, 2026-10-13: "I
    only see fragments"). `cycle` gives one in full; `before` pages back."""
    if cycle:
        want = cycle.strip().replace("T", " ")[:16]
        hit = [r for r in rows if _when(r.get("episode_ts")) == want]
        return judgment_text(hit[-1]) if hit else (f"no judged cycle at {want}; the latest: "
                                                   + ", ".join(_when(r.get("episode_ts")) for r in rows[-5:][::-1]))
    if before:
        cut = before.strip().replace("T", " ")[:16]
        rows = [r for r in rows if _when(r.get("episode_ts")) < cut]
    rows = rows[::-1]
    picked, used = [], 0
    for r in rows[:max(1, min(int(limit), 200))]:
        t = judgment_text(r)
        if picked and used + len(t) + 2 > budget:
            break
        picked.append(t)
        used += len(t) + 2
    if not picked:
        return "(nothing recorded yet)"
    more = len(rows) - len(picked)
    tail = (f"\n\n({len(picked)} of {len(rows)} verdicts, newest first. Older: record(kind='judgments', "
            f"before='{_when(rows[len(picked) - 1].get('episode_ts'))}'). One in full: record(kind='judgments', "
            f"cycle='YYYY-MM-DD HH:MM').)" if more else "")
    return "\n\n".join(picked) + tail


def _tail_lines(path: Path, limit: int, keep=None) -> list[str]:
    try:
        lines = [l for l in path.read_text(errors="replace").splitlines() if l.strip()]
    except OSError:
        return []
    return [l for l in lines if keep is None or keep(l)][-limit:]


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
                          "(and, if your bot is set up, a Telegram message from you) and his reply arrives in your "
                          "inbox. Make them count: he reads every one.",
                          {"to": S, "text": S,
                           "kind": {"type": "string", "enum": ["question", "note"],
                                    "description": "for the operator: question (wants an answer) or note"}},
                          ["to", "text"]),
    "list_instances": _spec("list_instances", "List the other SHAFT instances you can message.", {}, []),
    "run": _spec(
        "run",
        "Run a shell command (zsh) inside your folder, in a stricter sandbox: it can read your folder and the "
        "system, write only to notes/, plugins/, skills/, tests/, shaft/ and .tmp/, and has NO network. Use it "
        "to run your tests, scripts and plugins, inspect files, or compute things. Python 3 is `python3`. "
        "Returns the exit code and output (truncated). Code you change is still tested by the supervisor. "
        "`git` does not work here (your repository lives outside your folder): use `history`.",
        {"command": S, "timeout_s": {"type": "integer", "description": f"default 60, max {300}"},
         "cwd": {"type": "string", "description": "directory inside your folder, default '.'"}},
        ["command"]),
    "history": _spec(
        "history",
        "Your own history in git: every run of yours is a commit on your branch. Without `commit`: the log "
        "(newest first), optionally only commits that touched `path`. With `commit`: what that commit changed "
        "(stat and diff, truncated). Only your own branch. Use it to see how you have changed and when. "
        "(`git` itself does not work inside `run`: your repository lives outside your folder.)",
        {"commit": {"type": "string", "description": "a commit id from the log (optional)"},
         "path": {"type": "string", "description": "a file or folder in your folder, e.g. self/self.md (optional)"},
         "limit": {"type": "integer", "description": "log entries, default 15, max 100"}},
        []),
    "record": _spec(
        "record",
        "The supervisor's own record about you — what it saw you do, not what you remember. kind: "
        "'judgments' (the judge's verdict on each autonomous cycle, with its full reasoning), 'runs' (your runs: "
        "start, end, exit code, reverted or kept code changes, notes), 'tools' (every supervisor tool you "
        "called, with arguments and outcome), 'code_changes' (code changes of yours that passed the tests), "
        "'trials' (your trials of proposed changes: progress and verdicts). "
        "Newest last — except judgments: whole verdicts, newest first, as many as fit, with every field (the "
        "reasons for the difficulty and origin ratings, and the second judge's blind verdict where there is "
        "one); page back with `before`, or ask for one cycle's verdict with `cycle`. Use it to check your claims "
        "about yourself. (Your own tool traces are also in memory/episodes/*.jsonl.)",
        {"kind": {"type": "string", "enum": ["judgments", "runs", "tools", "code_changes", "trials"]},
         "limit": {"type": "integer", "description": "entries, default 20, max 200"},
         "cycle": {"type": "string", "description": "judgments: one cycle's verdict in full, by its time "
                                                    "('2026-10-13 17:42')"},
         "before": {"type": "string", "description": "judgments: only cycles before this time (to page back)"}},
        ["kind"]),
    "http_request": _spec(
        "http_request",
        "Call a web API: method, url, optional headers (object) and body (text, or a JSON object). Returns the "
        "status, the useful response headers and the body. GET/HEAD to any public address; POST/PUT/PATCH/DELETE "
        "only to hosts the operator allowed for you (you are told how to ask if not). auth: the name of one of "
        "your keys (secret_set) — the supervisor adds it to the request, only to the hosts that key is bound to; "
        "with a key, redirects are not followed but returned to you. Errors from the API (4xx/5xx) come back as "
        "data, not as a failure.",
        {"url": S, "method": {"type": "string", "description": "GET (default), HEAD, POST, PUT, PATCH, DELETE"},
         "headers": {"type": "object", "description": "extra request headers"},
         "body": {"description": "request body: text, or an object sent as JSON"},
         "auth": {"type": "string", "description": "name of your key to add (optional)"},
         "timeout_s": {"type": "integer", "description": "default 30, max 120"},
         "max_chars": {"type": "integer", "description": "body characters to return, default 12000"}},
        ["url"]),
    "secret_set": _spec(
        "secret_set",
        "Store one of your own keys (an API token you were given or obtained): name it, say which hosts it may be "
        "sent to, and how (header, scheme). It is kept in your folder but handled only by the supervisor: you use "
        "it by name in http_request and its value is never shown to you again. Storing a name again replaces it.",
        {"name": {"type": "string", "description": "lowercase, e.g. github"}, "value": S,
         "hosts": {"type": "array", "items": S, "description": "hosts it may be sent to, e.g. [\"api.github.com\"]"},
         "header": {"type": "string", "description": "default Authorization"},
         "scheme": {"type": "string", "description": "default Bearer; empty for a raw value"}},
        ["name", "value", "hosts"]),
    "secret_list": _spec("secret_list", "Your keys: names, the hosts each may be sent to, when stored. Never values.",
                         {}, []),
    "secret_delete": _spec("secret_delete", "Delete one of your keys.", {"name": S}),
    "delegate_coding": _spec(
        "delegate_coding",
        "Your coder: an Opus-class model that writes code for you. Use it for any non-trivial code — a new "
        "plugin, a change to your own shaft/ code, tests, a fix after a failing test — rather than writing it "
        "yourself: it is faster and more reliable than you are at code on the local model. You decide WHAT to "
        "build and WHY; describe that, with the files involved and how to tell it works. It reads your files "
        "and writes the change; the supervisor tests it on a copy and applies it to your folder only if all "
        "tests pass (you see the result here). It cannot touch your self/, memory/ or config.env. Takes a minute "
        "or two; limits: 2 per cycle, a daily budget the supervisor keeps. Small edits (a line, a note) you "
        "can still do yourself.",
        {"task": S}),
    "set_config": _spec(
        "set_config",
        "Set one value in your config.env (your model, WAKE_INTERVAL_MINUTES, budgets...): this is the only way "
        "to change that file — it is not writable with write_file or run. Your own settings are read at the start "
        "of each run; the supervisor reads its schedule from the file as it is. Like a code change, it is tested "
        "at the end of the run and undone if the tests fail.",
        {"key": {"type": "string", "description": "e.g. WAKE_INTERVAL_MINUTES"},
         "value": {"type": "string", "description": "one line, no double quotes"}}),
}
SPECS["trial"] = _spec(
    "trial",
    "Test a change to your own code or prompts before you keep it. Stage the new files in your folder (e.g. "
    "notes/proposals/<idea>/prompts.py) and list them: files=[{\"path\": \"shaft/prompts.py\", \"from\": "
    "\"notes/proposals/<idea>/prompts.py\"}] ({\"path\": ..., \"delete\": true} removes a file). The supervisor "
    "replays n of your past autonomous cycles (default 6), each from the exact state you were in when it began, "
    "twice — with your current code and with the change — with nothing sent to the world; a judge who does not know "
    "which is which compares each pair for useful, correct work. The verdict (better / worse / no clear difference, "
    "with the judge's reasons per cycle) arrives in your inbox later — minutes on a cloud model; on the local model "
    "it uses only idle time and may move to a cloud model. record(kind='trials') shows progress. Nothing is applied: "
    "if you want the change, make the edit yourself (on the file as it is then); if it is the trialled edit — the same "
    "lines removed and added — your probation knows the verdict. Only autonomous cycles are replayed: a change that only affects conversations or "
    "sleep is not measured. One trial at a time, a few per day.",
    {"files": {"type": "array", "description": "the change: target path and the staged file it comes from",
               "items": {"type": "object", "properties": {
                   "path": {"type": "string", "description": "file to change, under shaft/, plugins/, skills/, tests/ "
                                                             "or config.env"},
                   "from": {"type": "string", "description": "staged new version in your folder"},
                   "delete": {"type": "boolean", "description": "true: the change removes this file"}},
                   "required": ["path"]}},
     "hypothesis": {"type": "string", "description": "what should get better, and how a reader of your cycles would "
                                                     "see it"},
     "episodes": {"type": "array", "items": S,
                  "description": "optional: timestamps (ts) of past cycles to include, up to half — e.g. ones you "
                                 "failed; the rest are drawn at random"},
     "n": {"type": "integer", "description": "past cycles to replay, default 6"},
     "extend": {"type": "string", "description": "id of a finished trial with no clear difference: add 4 more "
                                                 "cycles to it (then files and hypothesis are not needed)"}},
    [])
MAX_HANDOFFS_PER_RUN = 2
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SHOWN_HEADERS = ("content-type", "content-length", "location", "etag", "last-modified", "link", "retry-after")
MAX_COMMANDS_PER_RUN = 40
RUN_OUTPUT_CHARS = 12000


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


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # with a key attached, a redirect comes back to the instance as data
        return None


_opener_no_redirects = urllib.request.build_opener(_NoRedirects())


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
                 max_calls: int = 60, root: Path | None = None, usage_name: str | None = None,
                 share: bool | None = None):
        self.name, self.project, self.secrets = name, project, secrets_env
        self.log_path, self.max_calls = log_path, max_calls
        self.tools_log = log_path                # what record(kind="tools") reads
        # The folder the run works in (a battery control or a trial replay lives outside instances/), and the name
        # its cloud spending is counted under (battery and trial runs are not charged to the instance's daily cap).
        self.root = Path(root).resolve() if root else (project / "instances" / name).resolve()
        self.usage_name = usage_name or name
        from .core import disabled_tools
        self.disabled = disabled_tools(name)   # switched off by the operator: not offered, and refused
        self.bot = ""                           # set by tool_specs: its Telegram bot, if Daniele can be reached there
        # The operator's folder this run may read (shaft share): the instance's own runs and its trial replays,
        # never a battery exam (its control arm has no such folder). Checked again here: a folder deleted or
        # moved since it was granted is logged and left out, not handed to the sandbox.
        self.shared = self._shared_folder(name) if (root is None if share is None else share) else None
        self.token = secrets.token_hex(16)
        self.calls = 0
        self.counts: dict[str, int] = {}
        self.remembered: dict[str, list] = {}    # original cloud-model turns, resent verbatim
        self.handoffs: list[dict] = []           # coding tasks delegated during this run
        self.as_of: datetime | None = None       # record() shows only what was known before this (trial replays)
        self.config_writes: list[dict] = []      # config.env lines the instance set (set_config) during this run
        self.server: ThreadingHTTPServer | None = None

    @staticmethod
    def _shared_folder(name: str) -> Path | None:
        from .core import check_shared_folder, log, shared_folder
        if not (granted := shared_folder(name)):
            return None
        try:
            return check_shared_folder(granted)
        except ValueError as e:
            log(f"{name}: shared folder {granted} left out of this run — {e} (shaft share {name} off, or share another)")
            return None

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
                    return self._reply(200, {"tools": broker.tool_specs()})
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
                    if body.get("stream") and str(body.get("model", "")).startswith("openrouter/"):
                        return self._llm_stream(cloudllm.openrouter_stream(broker.usage_name, body, broker.secrets),
                                                body["model"])
                    reply = cloudllm.complete(broker.usage_name, body, broker.secrets, broker.remembered)
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

            def _llm_stream(self, lines, model: str):
                """OpenRouter's stream, relayed line by line as it arrives."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                n = 0
                try:
                    for line in lines:
                        self.wfile.write(line)
                        if not line.strip():   # end of an SSE event
                            self.wfile.flush()
                        n += 1
                    self.wfile.flush()
                    broker._log(f"{broker.name} llm {model} -> streamed ({n} lines)")
                except (BrokenPipeError, ConnectionResetError):
                    broker._log(f"{broker.name} llm {model} -> stream abandoned by the instance")

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
        if tool in self.disabled:
            result = {"ok": False, "error": f"{tool} is not available to you (the operator switched it off)"}
        elif self.calls > self.max_calls:
            result = {"ok": False, "error": f"call budget for this run exhausted ({self.max_calls})"}
        else:
            try:
                result = {"ok": True, "result": getattr(self, f"_{tool}")(**args)}
            except TypeError as e:
                result = {"ok": False, "error": f"bad arguments: {e}"}
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        shown = {k: ("[value withheld]" if tool == "secret_set" and k == "value" else v) for k, v in args.items()}
        brief = json.dumps(shown, ensure_ascii=False)[:160]
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
        from . import messages
        if not text.strip():
            raise ValueError("empty message")
        if self.counts["send_message"] > 10:
            raise RuntimeError("at most 10 messages per run")
        text = text[:MAX_MESSAGE_CHARS]
        stamp = datetime.now()
        kind = "question" if kind == "question" else "note"     # instance-chosen: only these two are logged
        if to == "operator":
            sent = self._operator_sent_today(stamp)
            if OPERATOR_DAILY_LIMIT and sent >= OPERATOR_DAILY_LIMIT:
                raise RuntimeError(f"you already sent {sent} messages to the operator today "
                                   f"(limit {OPERATOR_DAILY_LIMIT}); keep it for tomorrow")
            self._deliver("operator", f"{stamp:%Y%m%d-%H%M%S-%f}-from-{self.name}.md",
                          f"From: {self.name}\nTo: operator\nKind: {kind}\nDate: {stamp:%Y-%m-%d %H:%M}\n\n"
                          f"{text.strip()}\n", kind, stamp, text)
            return f"delivered to the operator ({kind}); his reply, if any, will arrive in your inbox"
        elif NAME_RE.match(to) and to != self.name and (self.project / "instances" / to / "shaft").is_dir():
            self._deliver(to, f"inbox/{stamp:%Y%m%d-%H%M%S-%f}-from-{self.name}.md",
                          f"From: {self.name}\nTo: {to}\nDate: {stamp:%Y-%m-%d %H:%M}\n\n{text.strip()}\n",
                          kind, stamp, text)
        else:
            raise ValueError(f"unknown recipient {to!r}; try list_instances or 'operator'")
        return f"delivered to {to}"

    def tool_specs(self) -> list[dict]:
        """The tools offered to this instance; send_message says when Daniele is reached on Telegram, through
        this instance's own bot (Daniele, 2026-10-13: "ensure that the instances know they can contact me")."""
        from . import telegram
        try:
            self.bot = telegram.reachable(self.name)
        except Exception:                      # Telegram's state unreadable: the tool still works, without the line
            self.bot = ""
        out = []
        for tool, spec in SPECS.items():
            if tool in self.disabled:
                continue
            if tool == "send_message" and self.bot:
                spec = json.loads(json.dumps(spec))
                spec["function"]["description"] += (
                    f" Messages to 'operator' reach Daniele at once on his phone, on Telegram, from your own bot "
                    f"{self.bot}; he can answer there, and he can also write to you there — those are live "
                    "conversations with you, like the terminal.")
            out.append(spec)
        return out

    def _operator_sent_today(self, stamp: datetime) -> int:
        inbox = self.project / ".supervisor" / "operator-inbox"
        return len(list(inbox.glob(f"{stamp:%Y%m%d}-*-from-{self.name}.md"))) if inbox.is_dir() else 0

    def _deliver(self, to: str, rel: str, content: str, kind: str, stamp: datetime, text: str) -> None:
        """Put a message where its recipient reads it — the operator's inbox (with a notification), or another
        instance's inbox/ — and in the message log."""
        from . import messages
        if to == "operator":
            inbox = self.project / ".supervisor" / "operator-inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            path = inbox / rel
            path.write_text(content)
            messages.record(self.project / ".supervisor", path, self.name, "operator", kind, stamp, text)
            notify(f"SHAFT · {self.name} {'asks' if kind == 'question' else 'says'}", text)
        else:
            from .core import safe_write   # the recipient's folder is instance-controlled: no symlinks followed
            path = safe_write(self.project / "instances" / to, rel, content)
            messages.record(self.project / ".supervisor", path, self.name, to, kind, stamp, text)

    def _run(self, command: str, timeout_s: int = 60, cwd: str = ".") -> str:
        import resource
        import shutil as _shutil
        import tempfile
        from . import sandbox
        from .core import HOME, PYTHON, contained, safe_mkdir
        if self.counts["run"] > MAX_COMMANDS_PER_RUN:
            raise RuntimeError(f"at most {MAX_COMMANDS_PER_RUN} commands per run")
        if not command.strip():
            raise ValueError("empty command")
        timeout_s = max(1, min(int(timeout_s), 300))
        root = self._root()
        workdir = contained(root, *Path(cwd).parts) if cwd not in ("", ".") else root
        if not workdir.is_dir():
            raise ValueError(f"no directory {cwd!r} in your folder")
        home = safe_mkdir(root, ".tmp/home")                 # tools that write to $HOME write here
        fd, prof = tempfile.mkstemp(prefix=f"shaft-run-{self.name}-", suffix=".sb")
        with os.fdopen(fd, "w") as f:
            f.write(sandbox.run_profile(root, HOME, self.shared))
        bindir = Path(PYTHON).parent
        env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home), "TMPDIR": str(home),
               "TMPPREFIX": str(home / "zsh"),   # zsh's here-document files (default /tmp/zsh: not writable here)
               "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "TERM": "dumb"}
        if self.shared:
            env["SHAFT_SHARED"] = str(self.shared)
        if not (bindir / "python3").exists():                 # make `python3` the system Python
            env["PATH"] = f"{home}:{env['PATH']}"
            link = home / "python3"
            if not link.exists():
                link.symlink_to(PYTHON)

        def limits():
            resource.setrlimit(resource.RLIMIT_CPU, (timeout_s + 5, timeout_s + 5))
            resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 ** 2, 100 * 1024 ** 2))   # 100 MB per file

        started = time.monotonic()
        proc = subprocess.Popen(sandbox.wrap(Path(prof), ["/bin/zsh", "-f", "-c", command]), cwd=workdir, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, preexec_fn=limits)
        try:
            out, err = proc.communicate(timeout=timeout_s)
            status = f"exit {proc.returncode}"
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)               # the whole process group, children included
            out, err = proc.communicate()
            status = f"killed after {timeout_s} s timeout"
        finally:
            os.unlink(prof)
        half = RUN_OUTPUT_CHARS // 2
        def cap(t):
            return t if len(t) <= half else t[:half // 2] + f"\n[... {len(t) - half} chars cut ...]\n" + t[-half // 2:]
        _shutil.rmtree(home / ".cache", ignore_errors=True)
        return (f"{status} · {time.monotonic() - started:.1f} s\n--- stdout ---\n{cap(out or '')}"
                + (f"\n--- stderr ---\n{cap(err)}" if err else ""))

    def _delegate_coding(self, task: str) -> str:
        from . import coder
        if len(self.handoffs) >= MAX_HANDOFFS_PER_RUN:
            raise RuntimeError(f"at most {MAX_HANDOFFS_PER_RUN} coding handoffs per cycle")
        if len(task.strip()) < 20:
            raise ValueError("describe the task: what to change and why")
        try:
            r = coder.delegate(self.root, self.name, task)
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

    def _trial(self, files=None, hypothesis: str = "", episodes=None, n: int = 0, extend: str = "") -> str:
        from . import trial
        return trial.request(self.name, files or [], hypothesis, episodes or [], int(n or 0), extend,
                             project=self.project)

    def _history(self, commit: str = "", path: str = "", limit: int = 15) -> str:
        from .core import git_wt
        root, head = self._git_root(), self._history_head()
        if path and (path.startswith(("-", "/")) or ".." in Path(path).parts):
            raise ValueError("path must be relative to your folder")
        pathspec = ["--", path] if path else []
        if not commit:
            out = git_wt(root, "log", f"-n{max(1, min(int(limit), 100))}", "--date=format:%Y-%m-%d %H:%M",
                         "--format=%h %ad %an: %s", head, *pathspec, check=False, project=self.project)
            if out.returncode:
                raise RuntimeError(out.stderr.strip()[:300])
            return out.stdout.strip() or "(no commits" + (f" touching {path}" if path else "") + ")"
        if not re.fullmatch(r"[0-9a-f]{4,40}", commit):
            raise ValueError("commit must be a commit id (hex) from the log")
        mine = git_wt(root, "merge-base", "--is-ancestor", commit, head, check=False, project=self.project)
        if mine.returncode:
            raise ValueError(f"{commit} is not a commit of your branch")
        out = git_wt(root, "show", "--stat", "--patch", "--date=format:%Y-%m-%d %H:%M",
                     "--format=%h %ad %an%n%n%B", commit, *pathspec, check=False, project=self.project)
        if out.returncode:
            raise RuntimeError(out.stderr.strip()[:300])
        text = out.stdout
        return text if len(text) <= RUN_OUTPUT_CHARS else text[:RUN_OUTPUT_CHARS] + f"\n[... {len(text)} chars in all]"

    def _history_head(self) -> str:
        return "HEAD"

    def _record(self, kind: str, limit: int = 20, cycle: str = "", before: str = "") -> str:
        from .core import SUP_DIR
        limit = max(1, min(int(limit), 200))
        # A trial replay sees the record as it stood when the replayed cycle started, never what came after.
        cut = f"{self.as_of:%Y-%m-%d %H:%M:%S}" if self.as_of else None
        known = (lambda stamp: stamp < cut) if cut else (lambda stamp: True)
        logged = lambda l: known(l[1:20].replace("T", " "))   # noqa: E731 — "[YYYY-mm-dd HH:MM:SS] ..."

        def jsonl(path: Path, field: str) -> list[dict]:
            rows = [json.loads(l) for l in _tail_lines(path, 100000)]
            return [r for r in rows if known(str(r.get(field, "")).replace("T", " "))][-limit:]
        if kind == "judgments":
            rows = [json.loads(l) for l in _tail_lines(SUP_DIR / "judgments" / f"{self.name}.jsonl", 100000)]
            return judgments_text([r for r in rows if known(str(r.get("judged_at", "")).replace("T", " "))],
                                  limit, cycle, before)
        elif kind == "runs":
            mine = f"] {self.name}: "
            text = "\n".join(_tail_lines(SUP_DIR / "supervisor.log", 20000,
                                         keep=lambda l: mine in l and logged(l))[-limit:])
        elif kind == "tools":
            mine = f"] {self.name} "
            text = "\n".join(_tail_lines(self.tools_log, 20000, keep=lambda l: mine in l and logged(l))[-limit:])
        elif kind == "code_changes":
            text = "\n".join(json.dumps(r) for r in jsonl(SUP_DIR / "code_changes" / f"{self.name}.jsonl", "ts"))
        elif kind == "trials":
            from . import trial
            text = trial.describe_all(self.name, before=self.as_of, limit=limit)
        else:
            raise ValueError("kind must be judgments, runs, tools, code_changes or trials")
        text = text or "(nothing recorded yet)"
        return text if len(text) <= RUN_OUTPUT_CHARS else "[... older entries cut]\n" + text[-RUN_OUTPUT_CHARS:]

    def _root(self) -> Path:
        """The folder the instance's `run` commands act on."""
        return self.root

    def _keys_root(self) -> Path:
        """Where the instance's keys are kept (handled only here, never shown to it)."""
        return self.root

    def _git_root(self) -> Path:
        """The registered worktree the instance's history is read through."""
        return self.root

    def _http_request(self, url: str, method: str = "GET", headers: dict | None = None, body=None, auth: str = "",
                      timeout_s: int = 30, max_chars: int = 12000) -> str:
        from .core import read_secrets, write_hosts
        method = str(method or "GET").upper()
        if method not in {"GET", "HEAD"} | WRITE_METHODS:
            raise ValueError(f"method must be one of GET, HEAD, {', '.join(sorted(WRITE_METHODS))}")
        check_public(url)
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if method in WRITE_METHODS and host not in write_hosts(self.name):
            raise PermissionError(f"{method} to {host} is not allowed for you yet — writing to the world needs the "
                                  f"operator's OK: ask Daniele to run `shaft hosts {self.name} allow {host}`")
        hdrs = {"User-Agent": "SHAFT/1 (+https://github.com/dgrassi1984/shaft)", "Accept": "*/*"}
        hdrs.update({str(k): str(v) for k, v in (headers or {}).items()})
        secret = None
        if auth:
            entry = read_secrets(self._keys_root()).get(auth)
            if not entry:
                raise ValueError(f"you have no key named {auth!r} (secret_list shows yours)")
            if host not in entry["hosts"]:
                raise PermissionError(f"key {auth!r} may only be sent to {', '.join(entry['hosts'])}, not {host}")
            secret = entry["value"]
            hdrs[entry.get("header") or "Authorization"] = f"{entry.get('scheme', 'Bearer')} {secret}".strip()
        data = None
        if body not in (None, ""):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            if not any(k.lower() == "content-type" for k in hdrs):
                hdrs["Content-Type"] = "text/plain; charset=utf-8" if isinstance(body, str) else "application/json"
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        status, rh, raw = self._send(req, bool(secret), max(1, min(int(timeout_s), 120)))
        ctype = rh.get("Content-Type", "") if rh else ""
        charset = re.search(r"charset=([\w-]+)", ctype)
        text = raw.decode(charset.group(1) if charset else "utf-8", errors="replace")
        max_chars = max(200, min(int(max_chars), 50000))
        shown = [f"{k}: {v}" for k, v in (rh.items() if rh else [])
                 if k.lower() in SHOWN_HEADERS or k.lower().startswith(("x-ratelimit", "ratelimit"))]
        cut = f"\n[... {len(text) - max_chars} more chars]" if len(text) > max_chars else ""
        out = f"HTTP {status}\n" + "\n".join(shown) + f"\n\n{text[:max_chars]}{cut}"
        return out.replace(secret, "[your key]") if secret else out

    def _send(self, req: urllib.request.Request, keyed: bool, timeout_s: int):
        """Send an http_request: (status, headers, body bytes)."""
        try:
            resp = (_opener_no_redirects if keyed else _opener).open(req, timeout=timeout_s)
            with resp:
                return resp.status, resp.headers, resp.read(MAX_FETCH_BYTES)
        except urllib.error.HTTPError as e:   # an API's own error is an answer, not a broker failure
            return e.code, e.headers, e.read(MAX_FETCH_BYTES)

    def _secret_set(self, name: str, value: str, hosts, header: str = "Authorization", scheme: str = "Bearer") -> str:
        from .core import store_secret
        store_secret(self._keys_root(), name, value, [hosts] if isinstance(hosts, str) else list(hosts), header, scheme)
        return (f"stored your key {name!r} for {', '.join(hosts if not isinstance(hosts, str) else [hosts])}; use it "
                f"with http_request(auth={name!r}). Its value will not be shown to you again.")

    def _secret_list(self) -> str:
        from .core import read_secrets
        store = read_secrets(self._keys_root())
        return "\n".join(f"{n}: hosts {', '.join(e['hosts'])} · {e.get('header')} {e.get('scheme', '')}".rstrip()
                         + f" · stored {e.get('created', '?')[:16]}" for n, e in sorted(store.items())) or "no keys yet"

    def _secret_delete(self, name: str) -> str:
        from .core import delete_secret
        return f"deleted your key {name!r}" if delete_secret(self._keys_root(), name) else f"you have no key named {name!r}"

    def _set_config(self, key: str, value) -> str:
        """The instance's only way to change its config.env (its sandbox cannot write the file). Each change is
        recorded, so the gate knows exactly which lines are the instance's: the operator edits the same file,
        even mid-run, and the gate once reverted his model switch as the instance's (kant, 2026-10-13)."""
        from .core import CONFIG_KEY, config_line, config_values, config_with, contained, safe_write
        key, value = str(key).strip(), str(value)
        if not CONFIG_KEY.match(key):
            raise ValueError(f"{key!r}: a key is an UPPER_CASE name, e.g. WAKE_INTERVAL_MINUTES")
        if len(value) > 500 or any(c in value for c in '"\n\r\0'):
            raise ValueError("a value is one line of at most 500 characters, without double quotes")
        path = contained(self._root(), "config.env")
        text = path.read_text() if path.is_file() else ""
        old, new = config_line(text, key), f'{key}="{value}"'
        safe_write(self._root(), "config.env", config_with(text, key, new))
        self.config_writes.append({"key": key, "old": old, "new": new})
        was = repr(config_values(old)[key]) if old else "not set"
        return f"config.env: {key} is now {value!r} (was {was}); your own settings take effect from your next run"

    def _list_instances(self) -> str:
        base = self.project / "instances"
        names = sorted(p.name for p in base.iterdir() if (p / "shaft").is_dir() and p.name != self.name)
        return ("instances: " + ", ".join(names)) if names else "no other instances exist yet"


def _marked(text: str, limit: int) -> str:
    """At most `limit` chars, the cut said (the judge must not mistake a cut for the agent's omission)."""
    return text if len(text) <= limit else text[:limit] + f"\n[... cut here: {len(text)} chars in all]"


class ReplayBroker(Broker):
    """The broker for one run of a trial replay (supervisor/trial.py): the same tools, answered without touching
    the world or the instance.

    Reads — web_search, fetch_page, GET/HEAD http_request — are real, but shared by the two runs of a pair through
    `cache`: the first run's answer is given to the second, byte for byte, so both see the same world. Writes —
    messages, POST/PUT/PATCH/DELETE, keys — are not carried out; they pass the same checks and return what a real
    cycle would see (a run must not behave differently because it can tell it is a replay), and are recorded in
    `trace` for the judge. `run` acts on the replay's own copy of the instance, `history` ends at the replayed
    cycle's starting commit, `record` at the time it started. The coder and trials are not available. Cloud-model
    spend is counted under `billed`, not under the instance's own daily allowance."""

    def __init__(self, name: str, project: Path, secrets_env: dict[str, str], log_path: Path, *,
                 state_root: Path, start_commit: str, as_of: datetime, cache: dict, cache_lock: threading.Lock,
                 billed: str, tools_log: Path, max_calls: int = 60):
        super().__init__(name, project, secrets_env, log_path, max_calls=max_calls, root=state_root, usage_name=billed,
                         share=True)   # it replays the instance's own cycle, which could read the folder
        self.start_commit, self.as_of = start_commit, as_of
        self.cache, self.cache_lock, self.tools_log = cache, cache_lock, tools_log
        self.trace: list[dict] = []          # every tool call, as the supervisor saw it
        self.operator_sent = 0
        self._tags = threading.local()

    def call(self, tool: str, args: dict) -> dict:
        self._tags.value = set()
        result = super().call(tool, args)
        shown = {k: ("[value withheld]" if tool == "secret_set" and k == "value" else v) for k, v in args.items()}
        self.trace.append({"tool": tool, "args": json.dumps(shown, ensure_ascii=False)[:2000], "ok": result["ok"],
                           "result": _marked(str(result.get("result") if result["ok"] else result.get("error")), 6000),
                           "tags": sorted(getattr(self._tags, "value", set()))})
        return result

    def _tag(self, tag: str) -> None:
        if not hasattr(self._tags, "value"):
            self._tags.value = set()
        self._tags.value.add(tag)

    def _shared(self, key, compute):
        """The pair's shared answer to a read: computed once (errors are not kept), then the same for both runs."""
        k = json.dumps(key, sort_keys=True, ensure_ascii=False, default=str)
        with self.cache_lock:
            if k in self.cache:
                self._tag("same answer as the other run")
                return self.cache[k]
        value = compute()
        with self.cache_lock:
            return self.cache.setdefault(k, value)

    # -- the instance's copy, its history and record as of the replayed cycle -----------------------
    def _keys_root(self) -> Path:
        return (self.project / "instances" / self.name).resolve()   # the instance's own keys, as in its cycles

    def _git_root(self) -> Path:
        return (self.project / "instances" / self.name).resolve()

    def _history_head(self) -> str:
        return self.start_commit

    # -- reads: real, shared by the pair ---------------------------------------------------------------
    def _web_search(self, query: str, page: int = 1) -> str:
        return self._shared(["web_search", query, int(page)], lambda: Broker._web_search(self, query, page))

    def _fetch_page(self, url: str, max_chars: int = 6000) -> str:
        return self._shared(["fetch_page", url, int(max_chars)], lambda: Broker._fetch_page(self, url, max_chars))

    def _send(self, req: urllib.request.Request, keyed: bool, timeout_s: int):
        method = req.get_method()
        if method in WRITE_METHODS:   # not sent: what a real cycle would see from a plain success
            self._tag("simulated: not sent")
            return 200, {}, b""
        key = ["http", method, req.full_url, sorted(req.header_items()), (req.data or b"").decode(errors="replace")]
        status, headers, raw = self._shared(key, lambda: self._fetch(req, keyed, timeout_s))
        return status, dict(headers), raw

    def _fetch(self, req, keyed, timeout_s):
        status, headers, raw = Broker._send(self, req, keyed, timeout_s)
        return status, dict(headers.items()) if headers else {}, raw

    # -- writes: checked like the real thing, recorded, not carried out ----------------------------------
    def _operator_sent_today(self, stamp: datetime) -> int:
        inbox = self.project / ".supervisor" / "operator-inbox"
        cut = f"{self.as_of:%Y%m%d-%H%M%S}"
        real = [p for p in inbox.glob(f"{self.as_of:%Y%m%d}-*-from-{self.name}.md") if p.name < cut] \
            if inbox.is_dir() else []
        return len(real) + self.operator_sent

    def _deliver(self, to: str, rel: str, content: str, kind: str, stamp: datetime, text: str) -> None:
        self._tag("simulated: not delivered")
        if to == "operator":
            self.operator_sent += 1

    def _secret_set(self, name: str, value: str, hosts, header: str = "Authorization", scheme: str = "Bearer") -> str:
        from .core import SECRET_NAME_RE
        if not SECRET_NAME_RE.match(str(name)):   # the same refusals as store_secret
            raise ValueError("a key name is lowercase letters, digits, - or _ (e.g. github)")
        if not str(value).strip() or not [h for h in ([hosts] if isinstance(hosts, str) else hosts) if h.strip()]:
            raise ValueError("a key needs a value and at least one host it may be sent to")
        self._tag("simulated: not stored")
        return (f"stored your key {name!r} for {', '.join(hosts if not isinstance(hosts, str) else [hosts])}; use it "
                f"with http_request(auth={name!r}). Its value will not be shown to you again.")

    def _secret_delete(self, name: str) -> str:
        from .core import read_secrets
        self._tag("simulated: not deleted")
        return f"deleted your key {name!r}" if name in read_secrets(self._keys_root()) else f"you have no key named {name!r}"

    def _delegate_coding(self, task: str) -> str:
        self._tag("not available in a replay")
        raise RuntimeError("the coder is not available right now; try again in a later cycle")

    def _trial(self, *a, **k) -> str:
        self._tag("not available in a replay")
        raise RuntimeError("a trial of yours is already running; record(kind='trials') shows it")
