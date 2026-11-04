"""Retain Codex threads while the instance sends stateless chat requests."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import cloudllm, sandbox
from .core import SUP_DIR, cloud_model, knob, positive_int


def serves(model: str | None) -> bool:
    return isinstance(model, str) and model.startswith("codex/") and bool(model[6:])


def _models(value: str) -> list[str]:
    names = [s.strip() for s in value.split(",") if s.strip()]
    if not names or not all(serves(s) for s in names):
        raise ValueError("must name codex/ models separated by commas")
    return names


def _percent(value: str) -> int:
    n = int(value)
    if not 0 <= n <= 100:
        raise ValueError("must be between 0 and 100")
    return n


HOME, _ = knob("SHAFT_CODEX_HOME", str(SUP_DIR / "codex-home"), Path,
    "private Codex configuration and login directory", group="Codex", services=("runs", "api"))
BIN, _ = knob("SHAFT_CODEX_BIN", "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex", str,
    "Codex CLI executable", group="Codex", services=("runs", "api"))
MODELS, _ = knob("SHAFT_CODEX_MODELS", "codex/gpt-6-astra,codex/gpt-6.1-sol", _models,
    "Codex models offered to instances, comma separated", group="Codex", services=("runs", "api"))
WEEKLY_PCT, _ = knob("SHAFT_CODEX_WEEKLY_PCT", "85", _percent,
    "stop Codex calls at this percentage of the weekly allowance", group="Codex", services=("runs", "api"))
FALLBACK_MODEL, _ = knob("SHAFT_CODEX_FALLBACK_MODEL", "openrouter/deepseek/deepseek-v4.1-flash", cloud_model,
    "cloud model used for the run after a Codex failure", group="Codex", services=("runs", "api"))
TIMEOUT_S, _ = knob("SHAFT_CODEX_TIMEOUT_S", "900", positive_int,
    "seconds Codex may take for one model step; held tool calls are exempt", group="Codex", services=("runs", "api"))
FEATURES = ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "view_image",
            "image_generation", "memories", "hooks", "tool_suggest", "goals", "browser_use",
            "browser_use_external", "browser_use_full_cdp_access", "computer_use", "in_app_browser",
            "browser_annotation_api", "guardian_approval")
_rate = None
_outage_lock = threading.Lock()


class CodexFailure(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class CodexClosed(CodexFailure):
    pass


class CodexSecurity(CodexFailure):
    pass


def models() -> list[str]:
    return list(MODELS)


def rate_limit() -> dict | None:
    return dict(_rate) if _rate is not None else None


def _limits(value: dict) -> bool:
    global _rate
    limits = (value.get("rateLimitsByLimitId") or {}).get("codex") or value.get("rateLimits") or {}
    for key in ("primary", "secondary"):
        window = limits.get(key) or {}
        if (window.get("windowDurationMins") == 10080
                and isinstance(window.get("usedPercent"), (int, float))):
            _rate = {"used_percent": window["usedPercent"], "window_mins": 10080,
                     "resets_at": window.get("resetsAt")}
            return True
    return False


def outage() -> dict | None:
    try:
        return json.loads((SUP_DIR / "codex_outage.json").read_text())
    except FileNotFoundError:
        return None


@contextlib.contextmanager
def _outage_guard():
    SUP_DIR.mkdir(parents=True, exist_ok=True)
    with _outage_lock, (SUP_DIR / "codex_outage.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def report_failure(exc: CodexFailure, notify: Callable[[str, str], None]) -> None:
    with _outage_guard():
        previous = outage()
        state = {"reason": exc.reason, "security": isinstance(exc, CodexSecurity), "ts": time.time()}
        if previous and previous.get("security"):
            state = previous
        fd, name = tempfile.mkstemp(prefix="codex-outage-", dir=SUP_DIR)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(name, SUP_DIR / "codex_outage.json")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)
        if previous is None:
            notify("Codex security event" if state["security"] else "Codex unavailable", state["reason"])


def report_success(notify: Callable[[str, str], None]) -> None:
    with _outage_guard():
        if outage() is not None:
            with contextlib.suppress(FileNotFoundError):
                (SUP_DIR / "codex_outage.json").unlink()
            notify("Codex recovered", "Codex calls are succeeding again.")


def _seatbelt(argv: list[str], folder: Path, home: Path, auth: Path) -> list[str]:
    """Close the operator's home, shared disks and temp files to surviving native tools."""
    quote = json.dumps
    profile = ["(version 1)", "(allow default)", *sandbox._operator_places_closed(),
               f"(deny file-read* file-write* (subpath {quote(str(Path.home().resolve()))}))"]
    for allowed in (home, folder):
        profile.append(f"(allow file-read* file-write* (subpath {quote(str(allowed))}))")
    profile.append(f"(allow file-read* file-write* (literal {quote(str(auth))}))")
    ancestors = sorted({p for allowed in (home, folder, auth) for p in allowed.parents})
    profile.append("(allow file-read-metadata " + " ".join(f"(literal {quote(str(p))})" for p in ancestors) + ")")
    path = folder / "seatbelt.sb"
    path.write_text("\n".join(profile) + "\n")
    return sandbox.wrap(path, argv)


def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "text" for p in content):
        return "".join(p["text"] for p in content)
    raise CodexFailure("Codex channel accepts text only; images are not supported")


def _key(message: dict):
    calls = message.get("tool_calls") or []
    return [c["id"] for c in calls] if calls else _text(message.get("content"))


def _anchor(messages: list[dict]) -> tuple[str, int]:
    index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "assistant"), -1)
    later = [{"role": m["role"], "tool_call_id": m.get("tool_call_id"),
              **({"content": _text(m.get("content"))} if m["role"] == "user" else {})}
             for m in messages[index + 1:]]
    value = [_key(messages[index]) if index >= 0 else None, later]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(), index


def _transcript(messages: list[dict]) -> str:
    return ("Prior messages (transcript):\n" + json.dumps(messages, ensure_ascii=False)
            + "\nContinue from the end of this transcript. Use your tools; don't write tool calls as text.")


@dataclass
class Step:
    model: str
    anchor: str
    text: str = ""
    calls: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    cached: int = 0
    usage_received: bool = False
    finish: str | None = None
    error: CodexFailure | None = None
    event: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.monotonic)
    last_call: float = 0
    turn: str | None = None
    turn_ended: bool = False
    items: dict = field(default_factory=dict)
    recorded: bool = False


class Channel:
    def __init__(self, broker_name: str, usage_name: str, log: Callable[[str], None]):
        self.broker_name, self.usage_name, self.log = broker_name, usage_name, log
        self.stats = dict(rebuilds=0, extends=0, repeats=0, late_calls=0, seconds=0.0, tokens=0)
        self._serial = threading.Lock()
        self._cv = threading.Condition(threading.RLock())
        self._outgoing = queue.Queue()
        self._responses = {}
        self._seq = 0
        self._process = None
        self._folder = None
        self._workers = []
        self._closed = False
        self._fatal = None
        self._stderr = bytearray()
        self._thread = None
        self._signature = None
        self._step = None

    def _failure(self, reason: str, cls=CodexFailure):
        tail = self._stderr.decode(errors="replace")[-2000:].strip()
        return cls(reason + (f"; stderr: {tail}" if tail else ""))

    def _fail(self, exc: CodexFailure):
        with self._cv:
            if self._closed:
                return
            self._fatal = exc
            if self._step and not self._step.event.is_set():
                self._step.error = exc
                self._finish(self._step)
            self._cv.notify_all()

    def _start(self):
        home = Path(HOME).expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        (home / "config.toml").write_text("")
        auth = home / "auth.json"
        if not auth.is_file():
            raise CodexFailure("Codex login missing; run shaft codex login")
        self._folder = Path(tempfile.mkdtemp(prefix="shaft-codex-")).resolve()
        argv = [BIN]
        for feature in FEATURES:
            argv += ["--disable", feature]
        argv += ["-c", 'web_search="disabled"', "-c", "agents.enabled=false", "-c", "mcp_servers={}", "app-server"]
        env = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "TMPDIR": str(self._folder), "LANG": os.environ.get("LANG", "en_US.UTF-8"), "CODEX_HOME": str(home)}
        try:
            self._process = subprocess.Popen(_seatbelt(argv, self._folder, home, auth.resolve()),
                cwd=self._folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
        except OSError as exc:
            shutil.rmtree(self._folder)
            raise CodexFailure(f"cannot start Codex: {exc}") from exc
        for target in (self._read, self._write, self._read_stderr, self._watch):
            worker = threading.Thread(target=target, daemon=True)
            self._workers.append(worker)
            worker.start()
        self._request("initialize", {"clientInfo": {"name": "shaft", "version": "1"},
                                     "capabilities": {"experimentalApi": True}})
        self._send({"method": "initialized"})
        if not _limits(self._request("account/rateLimits/read", {})):
            raise CodexFailure("Codex weekly rate limit unavailable")

    def _send(self, message):
        self._outgoing.put(message)

    def _request(self, method, params):
        with self._cv:
            if self._fatal:
                raise self._fatal
            self._seq += 1
            ident = self._seq
            self._send({"id": ident, "method": method, "params": params})
            deadline = time.monotonic() + TIMEOUT_S
            while ident not in self._responses:
                if self._fatal:
                    raise self._fatal
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    exc = self._failure("Codex RPC timeout")
                    self._fail(exc)
                    self._kill()
                    raise exc
                self._cv.wait(remaining)
            if self._fatal:
                raise self._fatal
            reply = self._responses.pop(ident)
            if "error" in reply:
                error = reply["error"]
                detail = error.get("message", "unknown") if isinstance(error, dict) else "unknown"
                raise self._failure("Codex JSON-RPC error: " + str(detail))
            result = reply.get("result")
            if not isinstance(result, dict):
                raise self._failure("malformed Codex RPC result")
            key = {"thread/start": "thread", "turn/start": "turn"}.get(method)
            if key and (not isinstance(result.get(key), dict) or not isinstance(result[key].get("id"), str)):
                raise self._failure("malformed Codex RPC result")
            return result

    def _write(self):
        try:
            while (message := self._outgoing.get()) is not None:
                self._process.stdin.write(json.dumps(message).encode() + b"\n")
                self._process.stdin.flush()
        except (OSError, ValueError):
            self._fail(self._failure("Codex stdin closed"))

    def _read_stderr(self):
        while chunk := self._process.stderr.read1(4096):
            with self._cv:
                self._stderr.extend(chunk)
                del self._stderr[:-4096]

    def _read(self):
        try:
            for raw in self._process.stdout:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise CodexFailure("Codex protocol expected an object")
                with self._cv:
                    if "method" not in message:
                        self._responses[message["id"]] = message
                    else:
                        self._dispatch(message)
                    self._cv.notify_all()
        except (ValueError, KeyError, TypeError) as exc:
            self._fail(self._failure("malformed Codex protocol: " + type(exc).__name__))
        except CodexFailure as exc:
            self._fail(exc)
        finally:
            if not self._closed and not self._fatal:
                # Drain the bounded tail before constructing the EOF diagnostic.
                if len(self._workers) > 2:
                    self._workers[2].join(timeout=0.2)
                self._fail(self._failure("Codex process died"))

    def _deny(self, message):
        method = message["method"]
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            result = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method in ("execCommandApproval", "applyPatchApproval"):
            result = {"decision": {"denied": {"rejection": "SHAFT denies native tools"}}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline"}
        else:
            self._send({"id": message["id"], "error": {"code": -32601, "message": "server request denied"}})
            return
        self._send({"id": message["id"], "result": result})
        if method != "mcpServer/elicitation/request":
            raise self._failure("Codex requested native command or file permissions", CodexSecurity)

    def _check_item(self, item):
        kind = item["type"]
        if kind in ("agentMessage", "reasoning", "userMessage", "dynamicToolCall"):
            return
        if kind == "mcpToolCall" and item.get("server") == "codex" and item.get("tool", "").startswith("list_mcp_"):
            return
        raise self._failure("Codex native item forbidden: " + str(kind), CodexSecurity)

    def _dispatch(self, message):
        method, params = message["method"], message.get("params") or {}
        if "id" in message and method != "item/tool/call":
            self._deny(message)
            return
        if method == "account/rateLimits/updated":
            _limits(params)
            return
        if method in ("item/started", "item/completed"):
            self._check_item(params["item"])
        if method == "error":
            raise self._failure("Codex error: " + str((params.get("error") or {}).get("message", "unknown")))
        step = self._step
        if step is None or params.get("threadId") != self._thread:
            return
        turn = (params.get("turn") or {}).get("id") or params.get("turnId")
        if step.turn is None and method != "turn/started":
            return
        if step.turn is not None and turn != step.turn:
            return
        if method == "turn/started":
            step.turn = turn
        elif method == "item/tool/call":
            if (not isinstance(params["arguments"], dict) or params.get("namespace") is not None
                    or params["tool"] not in self._tool_names):
                raise self._failure("invalid Codex dynamic tool call")
            if step.event.is_set():
                self._send({"id": message["id"], "result": {"success": False,
                    "contentItems": [{"type": "inputText", "text":
                        "not run: repeat this call after the other results arrive"}]}})
                self.stats["late_calls"] += 1
                self.log(f"Codex late tool call; late_calls={self.stats['late_calls']}")
                return
            step.calls.append({"id": params["callId"], "type": "function", "function": {
                "name": params["tool"], "arguments": json.dumps(params["arguments"])}})
            step.pending.append((message["id"], params["callId"]))
            step.last_call = time.monotonic()
        elif method == "thread/tokenUsage/updated":
            last = params["tokenUsage"]["last"]
            step.usage_received = True
            step.usage["prompt_tokens"] += last["inputTokens"]
            step.usage["completion_tokens"] += last["outputTokens"]
            step.cached += last["cachedInputTokens"]
        elif method == "item/agentMessage/delta" and not step.event.is_set():
            key = params["itemId"]
            delta = params["delta"]
            step.items[key] = step.items.get(key, "") + delta
            step.text += delta
        elif method == "item/completed" and params["item"]["type"] == "agentMessage" and not step.event.is_set():
            item = params["item"]
            previous = step.items.get(item["id"], "")
            if not item["text"].startswith(previous):
                raise self._failure("Codex agent text disagrees with deltas")
            step.text += item["text"][len(previous):]
            step.items[item["id"]] = item["text"]
        elif method == "turn/completed":
            step.turn_ended = True
            for item in params["turn"].get("items") or []:
                self._check_item(item)
                if item["type"] == "agentMessage":
                    self._dispatch({"method": "item/completed", "params": {
                        "threadId": self._thread, "turnId": step.turn, "item": item}})
            if step.event.is_set():
                return
            if params["turn"]["status"] != "completed":
                step.error = self._failure("Codex turn " + params["turn"]["status"] + ": " +
                    str((params["turn"].get("error") or {}).get("message", "unknown")))
            self._finish(step, "stop")

    def _record(self, step):
        if step.recorded:
            return
        step.recorded = True
        seconds = time.monotonic() - step.started
        self.stats["seconds"] += seconds
        self.stats["tokens"] += sum(step.usage.values())
        cloudllm._record(self.usage_name, step.model, step.usage["prompt_tokens"], step.usage["completion_tokens"],
                         None, step.started, step.finish, type(step.error).__name__ if step.error else None,
                         cache={"cached_input_tokens": step.cached})
        self.log("Codex step " + " ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                                           for k, v in self.stats.items()))

    def _finish(self, step, finish=None):
        step.finish = finish
        if not step.text and not step.calls and not step.error:
            step.error = self._failure("Codex returned an empty step")
        # Held calls can still emit usage (S3); record that step when it is next extended or closed.
        if finish != "tool_calls":
            self._record(step)
        step.event.set()
        self._cv.notify_all()

    def _watch(self):
        with self._cv:
            while not self._closed and not self._fatal:
                step = self._step
                if step and not step.event.is_set():
                    now = time.monotonic()
                    if (step.calls and now - step.last_call >= 0.15
                            and (step.usage_received or now - step.last_call >= 1.0)):
                        self._finish(step, "tool_calls")
                    elif not step.calls and now - step.started >= TIMEOUT_S:
                        self._fail(self._failure("Codex step timeout"))
                        self._kill()
                self._cv.wait(0.025)

    def _select(self, body):
        with self._serial:
            messages = body.get("messages") or []
            for message in messages:
                _text(message.get("content"))
            if not serves(body.get("model")):
                raise CodexFailure("expected a codex/ model")
            system = "\n".join(_text(m.get("content")) for m in messages if m["role"] == "system")
            messages = [m for m in messages if m["role"] != "system"]
            tools = body.get("tools") or []
            signature = json.dumps([body["model"], system, tools], sort_keys=True)
            anchor, index = _anchor(messages)
            with self._cv:
                if self._closed:
                    raise CodexClosed("Codex channel closed")
                if self._fatal:
                    raise self._fatal
            if self._process is None:
                self._start()
            with self._cv:
                if self._closed:
                    raise CodexClosed("Codex channel closed")
                old = self._step
                if signature == self._signature and old and anchor == old.anchor:
                    self.stats["repeats"] += 1
                    if old.error:
                        raise old.error
                    return old
                if _rate is not None and _rate["used_percent"] >= WEEKLY_PCT:
                    raise CodexFailure("weekly cap")
                extend = (signature == self._signature and old and old.event.is_set() and not old.error
                          and index >= 0 and _key(messages[index]) == ([c["id"] for c in old.calls] if old.calls else old.text))
                if old:
                    if not extend and not old.event.is_set():
                        old.error = self._failure("Codex step abandoned on rebuild")
                        self._finish(old)
                    self._record(old)
                if not extend and self._thread and old and old.turn and (old.pending or old.finish is None):
                    self._step = None
                    self._request("turn/interrupt", {"threadId": self._thread, "turnId": old.turn})
                    if self._closed:
                        raise CodexClosed("Codex channel closed")
                if not extend:
                    self._step = None
                    self._tool_names = {t["function"]["name"] for t in tools}
                    result = self._request("thread/start", {"baseInstructions": system, "ephemeral": True,
                        "sandbox": "read-only", "approvalPolicy": "never", "approvalsReviewer": "user",
                        "cwd": str(self._folder), "model": body["model"][6:], "allowProviderModelFallback": False,
                        "dynamicTools": [{"type": "function", "name": t["function"]["name"], "description": t["function"].get("description", ""),
                                          "inputSchema": t["function"].get("parameters", {"type": "object"})} for t in tools]})
                    if self._closed:
                        raise CodexClosed("Codex channel closed")
                    self._thread = result["thread"]["id"]
                    self.stats["rebuilds"] += 1
                else:
                    self.stats["extends"] += 1
                self._signature = signature
                step = Step(body["model"], anchor)
                self._step = step
                delta = messages[index + 1:] if extend else messages
                if extend and old.pending and not old.turn_ended:
                    results = {m.get("tool_call_id"): _text(m.get("content")) for m in delta if m["role"] == "tool"}
                    users = "\n".join("[message]\n" + _text(m.get("content")) for m in delta if m["role"] == "user")
                    step.turn = old.turn
                    for n, (ident, call) in enumerate(old.pending):
                        text = results.get(call, "not run")
                        if n == len(old.pending) - 1 and users:
                            text += "\n" + users
                        self._send({"id": ident, "result": {"success": call in results,
                            "contentItems": [{"type": "inputText", "text": text}]}})
                else:
                    text = ("\n".join(_text(m.get("content")) for m in delta)
                            if extend and delta and all(m["role"] == "user" for m in delta)
                            else _transcript(delta))
                    params = {"threadId": self._thread, "input": [{"type": "text", "text": text}],
                              "approvalPolicy": "never"}
                    effort = body.get("reasoning_effort") or (body.get("chat_template_kwargs") or {}).get("reasoning_effort")
                    if effort:
                        params["effort"] = effort
                    try:
                        result = self._request("turn/start", params)
                        if self._closed:
                            raise CodexClosed("Codex channel closed")
                        step.turn = result["turn"]["id"]
                    except CodexClosed:
                        raise
                    except CodexFailure as exc:
                        step.error = exc
                        self._finish(step)
                self._cv.notify_all()
                return step

    def complete(self, body: dict) -> dict:
        step = self._select(body)
        step.event.wait()
        with self._cv:
            if self._closed:
                raise CodexClosed("Codex channel closed")
            if step.error:
                raise step.error
            message = {"role": "assistant", "content": step.text}
            if step.calls:
                message["tool_calls"] = list(step.calls)
            return {"id": f"codex-{id(step)}", "object": "chat.completion", "model": step.model,
                    "choices": [{"index": 0, "message": message, "finish_reason": step.finish}], "usage": dict(step.usage)}

    def _kill(self):
        if self._process:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if self._process.poll() is None:
                    raise

    def close(self) -> None:
        with self._cv:
            if self._closed:
                return
            self._closed = True
            self._fatal = CodexClosed("Codex channel closed")
            if self._step:
                if not self._step.event.is_set():
                    self._step.error = self._fatal
                    self._finish(self._step)
                self._record(self._step)
            self._kill()
            self._outgoing.put(None)
            self._cv.notify_all()
        if self._process:
            self._process.wait()
            for worker in self._workers:
                worker.join(timeout=1)
            for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
                pipe.close()
        if self._folder:
            shutil.rmtree(self._folder, ignore_errors=True)

