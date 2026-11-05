"""Retained plan sessions behind the broker's stateless chat interface.

Backends subclass Channel: the shared selector owns steps; backends own protocol
messages and pending-call handles. Provider modules are loaded lazily so model
validation does not introduce an import cycle through core's knobs.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import cloudllm
from .core import SUP_DIR


class ChannelFailure(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ChannelClosed(ChannelFailure):
    pass


class ChannelSecurity(ChannelFailure):
    pass


# Prefix -> backend module. Adding a backend needs no changes in callers.
PROVIDERS = {"codex": ".codexcli", "grok": ".grokcli"}


def provider(model: str):
    prefix = model.partition("/")[0]
    return importlib.import_module(PROVIDERS[prefix], __package__)


def prefix(model: str | None) -> str | None:
    """Recognize a provider prefix even when the model suffix is empty (display/refusal paths)."""
    if isinstance(model, str):
        head, sep, _ = model.partition("/")
        if sep and head in PROVIDERS:
            return head
    return None


def serves(model: str | None) -> bool:
    return prefix(model) is not None and bool(model.partition("/")[2])


def models() -> list[str]:
    return [m for prefix in PROVIDERS for m in provider(prefix).models()]


def channel_for(model, name, usage_name, log):
    return provider(model).Channel(name, usage_name, log)


def label(model: str) -> str:
    return provider(model).LABEL


def fallback_model(model: str) -> str:
    return provider(model).FALLBACK_MODEL


def sees_images(model: str) -> bool:
    return provider(model).SEES_IMAGES


def vendor(model: str) -> str:
    return provider(model).VENDOR


def unprefix(model: str) -> str:
    return model.partition("/")[2] if prefix(model) else model


def outage(provider_name="codex") -> dict | None:
    return provider(provider_name).STATE.outage()


def rate_limit(provider_name="codex") -> dict | None:
    return provider(provider_name).STATE.rate_limit()


def saved_rate(provider_name="codex") -> dict | None:
    current = rate_limit(provider_name)
    if current is not None:
        return current
    prefix = provider_name.partition("/")[0]
    try:
        return json.loads((SUP_DIR / f"{prefix}_rate.json").read_text())
    except (OSError, ValueError):
        return None


def report_failure(exc, notify, provider_name="codex") -> None:
    provider(provider_name).STATE.report_failure(exc, notify)


def report_success(notify, provider_name="codex") -> None:
    provider(provider_name).STATE.report_success(notify)


def save_rate(previous, provider_name="codex"):
    rate = rate_limit(provider_name)
    if rate is not None and rate != previous:
        prefix = provider_name.partition("/")[0]
        SUP_DIR.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=f"{prefix}-rate-", dir=SUP_DIR)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(rate, f)
            os.replace(path, SUP_DIR / f"{prefix}_rate.json")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        return rate
    return previous


class ProviderState:
    def __init__(self, prefix: str, label: str):
        self.prefix, self.label = prefix, label
        self.rate = None
        self._outage_lock = threading.Lock()

    def rate_limit(self) -> dict | None:
        return dict(self.rate) if self.rate is not None else None

    def outage(self) -> dict | None:
        try:
            return json.loads((SUP_DIR / f"{self.prefix}_outage.json").read_text())
        except FileNotFoundError:
            return None

    @contextlib.contextmanager
    def _outage_guard(self):
        SUP_DIR.mkdir(parents=True, exist_ok=True)
        with self._outage_lock, (SUP_DIR / f"{self.prefix}_outage.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def report_failure(self, exc: ChannelFailure, notify: Callable[[str, str], None]) -> None:
        with self._outage_guard():
            previous = self.outage()
            state = {"reason": exc.reason, "security": isinstance(exc, ChannelSecurity), "ts": time.time()}
            if previous and previous.get("security"):
                state = previous
            fd, name = tempfile.mkstemp(prefix=f"{self.prefix}-outage-", dir=SUP_DIR)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(state, f)
                os.replace(name, SUP_DIR / f"{self.prefix}_outage.json")
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name)
            if previous is None:
                notify(f"{self.label} security event" if state["security"] else f"{self.label} unavailable", state["reason"])

    def report_success(self, notify: Callable[[str, str], None]) -> None:
        with self._outage_guard():
            if self.outage() is not None:
                with contextlib.suppress(FileNotFoundError):
                    (SUP_DIR / f"{self.prefix}_outage.json").unlink()
                notify(f"{self.label} recovered", f"{self.label} calls are succeeding again.")


def _split(content, label="channel", failure=ChannelFailure) -> tuple[str, list[str]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        raise failure(f"unsupported {label} content type: {type(content).__name__}")
    text, images = [], []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else type(part).__name__
        if kind == "text":
            text.append(part["text"])
        elif kind == "image_url":
            images.append(part["image_url"]["url"])
        else:
            raise failure(f"unsupported {label} content part type: {kind}")
    return "".join(text), images


def _text(content) -> str:
    return _split(content)[0]


def _user_key(content):
    text, images = _split(content)
    return [text, [hashlib.sha256(url.encode()).hexdigest() for url in images]]


def _key(message: dict):
    calls = message.get("tool_calls") or []
    return [c["id"] for c in calls] if calls else _text(message.get("content"))


def _anchor(messages: list[dict]) -> tuple[str, int]:
    index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "assistant"), -1)
    later = [{"role": m["role"], "tool_call_id": m.get("tool_call_id"),
              **({"content": _user_key(m.get("content"))} if m["role"] == "user" else {})}
             for m in messages[index + 1:]]
    value = [_key(messages[index]) if index >= 0 else None, later]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(), index


def _transcript(messages: list[dict]) -> str:
    numbered, count = [], 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if part["type"] == "image_url":
                    count += 1
                    part = {"type": "text", "text": f"[image {count}]"}
                parts.append(part)
            message = {**message, "content": parts}
        numbered.append(message)
    return ("Prior messages (transcript):\n" + json.dumps(numbered, ensure_ascii=False)
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
    error: ChannelFailure | None = None
    event: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.monotonic)
    last_call: float = 0
    turn: str | None = None
    turn_ended: bool = False
    items: dict = field(default_factory=dict)
    recorded: bool = False


class Channel:
    """Shared selection/lifecycle; protocol hooks below are implemented by a backend."""
    prefix = ""
    label = "Channel"
    failure = ChannelFailure
    closed_error = ChannelClosed
    effort_in_session = True

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
        self._session = None
        self._signature = None
        self._step = None

    def _failure(self, reason: str, cls=None):
        tail = self._stderr.decode(errors="replace")[-2000:].strip()
        return (cls or self.failure)(reason + (f"; stderr: {tail}" if tail else ""))

    def _fail(self, exc: ChannelFailure):
        with self._cv:
            if self._closed:
                return
            self._fatal = exc
            if self._step and not self._step.event.is_set():
                self._step.error = exc
                self._finish(self._step)
            self._cv.notify_all()

    def _send(self, message):
        self._outgoing.put(message)

    def _request(self, method, params):
        with self._cv:
            if self._fatal:
                raise self._fatal
            self._seq += 1
            ident = self._seq
            self._send({"id": ident, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout_s
            while ident not in self._responses:
                if self._fatal:
                    raise self._fatal
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    exc = self._failure(f"{self.label} RPC timeout")
                    self._fail(exc)
                    self._kill()
                    raise exc
                self._cv.wait(remaining)
            if self._fatal:
                raise self._fatal
            reply = self._responses.pop(ident)
            return self._result(method, reply)

    def _write(self):
        try:
            while (message := self._outgoing.get()) is not None:
                self._process.stdin.write(json.dumps(message).encode() + b"\n")
                self._process.stdin.flush()
        except (OSError, ValueError):
            self._fail(self._failure(f"{self.label} stdin closed"))

    def _read_stderr(self):
        while chunk := self._process.stderr.read1(4096):
            with self._cv:
                self._stderr.extend(chunk)
                del self._stderr[:-4096]

    def _response(self, message):
        # A backend may consume asynchronous turn responses before RPC correlation.
        self._responses[message["id"]] = message

    def _read(self):
        try:
            for raw in self._process.stdout:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise self.failure(f"{self.label} protocol expected an object")
                with self._cv:
                    if "method" not in message:
                        self._response(message)
                    else:
                        self._dispatch(message)
                    self._cv.notify_all()
        except (ValueError, KeyError, TypeError) as exc:
            self._fail(self._failure(f"malformed {self.label} protocol: " + type(exc).__name__))
        except ChannelFailure as exc:
            self._fail(exc)
        finally:
            if not self._closed and not self._fatal:
                # Drain the bounded tail before constructing the EOF diagnostic.
                if len(self._workers) > 2:
                    self._workers[2].join(timeout=0.2)
                self._fail(self._failure(f"{self.label} process died"))

    def _usage_cache(self, step):
        return {"cached_input_tokens": step.cached}

    def _record(self, step):
        if step.recorded:
            return
        step.recorded = True
        seconds = time.monotonic() - step.started
        self.stats["seconds"] += seconds
        self.stats["tokens"] += sum(step.usage.values())
        cloudllm._record(self.usage_name, step.model, step.usage["prompt_tokens"], step.usage["completion_tokens"],
                         None, step.started, step.finish, type(step.error).__name__ if step.error else None,
                         cache=self._usage_cache(step))
        self.log(f"{self.label} step " + " ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                                           for k, v in self.stats.items()))

    def _finish(self, step, finish=None):
        step.finish = finish
        if not step.text and not step.calls and not step.error:
            step.error = self._failure(f"{self.label} returned an empty step")
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
                    if self._calls_ready(step, now):
                        self._finish(step, "tool_calls")
                    elif not step.calls and now - step.started >= self.timeout_s:
                        self._fail(self._failure(f"{self.label} step timeout"))
                        self._kill()
                self._cv.wait(0.025)

    def _select(self, body):
        with self._serial:
            messages = body.get("messages") or []
            for message in messages:
                _split(message.get("content"), self.label, self.failure)
            if not serves(body.get("model")) or body["model"].partition("/")[0] != self.prefix:
                raise self.failure(f"expected a {self.prefix}/ model")
            system = "\n".join(_text(m.get("content")) for m in messages if m["role"] == "system")
            messages = [m for m in messages if m["role"] != "system"]
            tools = body.get("tools") or []
            effort = ((body.get("reasoning_effort") or (body.get("chat_template_kwargs") or {}).get("reasoning_effort"))
                      if self.effort_in_session else None)
            signature = json.dumps([body["model"], system, tools] + ([effort] if self.effort_in_session else []), sort_keys=True)
            anchor, index = _anchor(messages)
            with self._cv:
                if self._closed:
                    raise self.closed_error(f"{self.label} channel closed")
                if self._fatal:
                    raise self._fatal
            if self._process is None:
                self._start(body["model"], effort)
            with self._cv:
                if self._closed:
                    raise self.closed_error(f"{self.label} channel closed")
                old = self._step
                if signature == self._signature and old and anchor == old.anchor:
                    self.stats["repeats"] += 1
                    if old.error:
                        raise old.error
                    return old
                self._check_rate()
                extend = (signature == self._signature and old and old.event.is_set() and not old.error
                          and index >= 0 and _key(messages[index]) == ([c["id"] for c in old.calls] if old.calls else old.text))
                if old:
                    if not extend and not old.event.is_set():
                        old.error = self._failure(f"{self.label} step abandoned on rebuild")
                        self._finish(old)
                if not extend and self._session and old and old.turn and (old.pending or old.finish is None):
                    self._step = None
                    self._cancel_turn(old.turn)
                    if self._closed:
                        raise self.closed_error(f"{self.label} channel closed")
                if old:
                    self._record(old)
                if not extend:
                    self._step = None
                    session = self._open_session(body["model"], system, tools, effort)
                    if self._closed:
                        raise self.closed_error(f"{self.label} channel closed")
                    self._session = session
                    self.stats["rebuilds"] += 1
                else:
                    self.stats["extends"] += 1
                self._signature = signature
                step = Step(body["model"], anchor)
                self._step = step
                delta = messages[index + 1:] if extend else messages
                inputs = None
                if extend and old.pending and not old.turn_ended:
                    results = {m.get("tool_call_id"): _text(m.get("content")) for m in delta if m["role"] == "tool"}
                    users = "\n".join("[message]\n" + _text(m.get("content")) for m in delta if m["role"] == "user")
                    images = [url for m in delta if m["role"] == "user" for url in _split(m.get("content"))[1]]
                    step.turn = old.turn
                    try:
                        if not self._answer_calls(old.pending, results, users, images):
                            # A turn can end while answers/user delta are in flight.
                            self._record(step)
                            step = Step(body["model"], anchor)
                            self._step = step
                            inputs = (users or "[message]", images)
                    except ChannelClosed:
                        raise
                    except ChannelFailure as exc:
                        step.error = exc
                        self._finish(step)
                else:
                    text = ("\n".join(_text(m.get("content")) for m in delta)
                            if extend and delta and all(m["role"] == "user" for m in delta)
                            else _transcript(delta))
                    images = [url for m in delta for url in _split(m.get("content"))[1]]
                    inputs = (text, images)
                if inputs is not None:
                    effort = body.get("reasoning_effort") or (body.get("chat_template_kwargs") or {}).get("reasoning_effort")
                    try:
                        turn = self._start_turn(*inputs, effort)
                        if self._closed:
                            raise self.closed_error(f"{self.label} channel closed")
                        step.turn = turn
                    except ChannelClosed:
                        raise
                    except ChannelFailure as exc:
                        step.error = exc
                        self._finish(step)
                self._cv.notify_all()
                return step

    def complete(self, body: dict) -> dict:
        step = self._select(body)
        step.event.wait()
        with self._cv:
            if self._closed:
                raise self.closed_error(f"{self.label} channel closed")
            if step.error:
                raise step.error
            message = {"role": "assistant", "content": step.text}
            if step.calls:
                message["tool_calls"] = list(step.calls)
            return {"id": f"{self.prefix}-{id(step)}", "object": "chat.completion", "model": step.model,
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
            self._fatal = self.closed_error(f"{self.label} channel closed")
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

    def _start_workers(self):
        for target in (self._read, self._write, self._read_stderr, self._watch):
            worker = threading.Thread(target=target, daemon=True)
            self._workers.append(worker)
            worker.start()

    def _calls_ready(self, step, now):
        return step.calls and now - step.last_call >= 0.15

    def _check_rate(self):
        pass

    @property
    def timeout_s(self):
        raise NotImplementedError

    def _start(self, model, effort):
        raise NotImplementedError

    def _open_session(self, model, system, tools, effort):
        raise NotImplementedError

    def _start_turn(self, text, images, effort):
        raise NotImplementedError

    def _answer_calls(self, pending, results, text, images):
        """Answer calls plus user delta; False means start a new turn with that delta.

        Pending handles belong to the backend. A bridge backend may include text
        and images in a tool result; the selector knows nothing about RPC steering.
        """
        raise NotImplementedError

    def _cancel_turn(self, turn):
        raise NotImplementedError

    def _result(self, method, reply):
        raise NotImplementedError

    def _dispatch(self, message):
        raise NotImplementedError
