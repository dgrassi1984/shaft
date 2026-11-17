"""Codex app-server backend for the shared retained plan channel."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import contextlib
import fcntl
import sqlite3
import subprocess
import tempfile
import time
from typing import Callable

from . import core, planchannel, sandbox
from .core import SUP_DIR, channel_fallback, knob, positive_int


def serves(model: str | None) -> bool:
    return isinstance(model, str) and model.startswith("codex/") and bool(model[6:])


def _models(value: str) -> list[str]:
    names = [s.strip() for s in value.split(",") if s.strip()]
    if not all(serves(s) for s in names):
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
MODELS, _ = knob("SHAFT_CODEX_MODELS", "", _models,
    "only these of the models Codex lists, comma separated; empty: every model Codex lists", group="Codex", services=("runs", "api"))
WEEKLY_PCT, _ = knob("SHAFT_CODEX_WEEKLY_PCT", "85", _percent,
    "stop Codex calls at this percentage of the weekly allowance", group="Codex", services=("runs", "api"))
FALLBACK_MODEL, _ = knob("SHAFT_CODEX_FALLBACK_MODEL", "openrouter/deepseek/deepseek-v4.1-flash", channel_fallback,
    "cloud model used for the run after a Codex failure (claude-…, openrouter/… or a priced custom/…), or none: the "
    "failure ends the run", group="Codex", services=("runs", "api"))
TIMEOUT_S, _ = knob("SHAFT_CODEX_TIMEOUT_S", "900", positive_int,
    "seconds Codex may take for one model step; held tool calls are exempt", group="Codex", services=("runs", "api"))
LOG_HOURS, _ = knob("SHAFT_CODEX_LOG_HOURS", "24", positive_int,
    "hours of Codex's own logs to keep", group="Codex", services=("runs", "api"))
FEATURES = ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "view_image",
            "image_generation", "memories", "hooks", "tool_suggest", "goals", "browser_use",
            "browser_use_external", "browser_use_full_cdp_access", "computer_use", "in_app_browser",
            "browser_annotation_api", "guardian_approval")
LABEL = "Codex"
VENDOR = "openai"
SEES_IMAGES = True
STATE = planchannel.ProviderState("codex", LABEL)

# Codex appends its own permissions/environment text that says the session is read-only, so instances skip writes without trying.
PROMPT_CONTEXT_FLAGS = ("include_permissions_instructions=false",
                        "include_environment_context=false",
                        "include_collaboration_mode_instructions=false")
# 2026-11-05: display/bookkeeping items have no external effect; dynamic tools
# are mediated separately by item/tool/call. Native actions still fail closed.
ALLOWED_ITEM_TYPES = frozenset({
    "agentMessage", "reasoning", "userMessage", "dynamicToolCall",
    "sleep", "contextCompaction", "plan", "functionCallOutput",
})


class CodexFailure(planchannel.ChannelFailure):
    pass


class _NoActiveTurn(CodexFailure):
    pass


class CodexClosed(CodexFailure, planchannel.ChannelClosed):
    pass


class CodexSecurity(CodexFailure, planchannel.ChannelSecurity):
    pass


def models(refresh: bool = True) -> list[str]:
    from . import effortcatalog
    listed = effortcatalog.listed("codex", _effort_listing, refresh=refresh)
    if listed is None:
        return list(MODELS)
    return ["codex/" + m for m in listed if not MODELS or "codex/" + m in MODELS]


# Keep the Codex-specific entry points for operators and existing integrations.
def rate_limit() -> dict | None:
    return STATE.rate_limit()


def outage() -> dict | None:
    return STATE.outage()


def report_failure(exc: CodexFailure, notify: Callable[[str, str], None]) -> None:
    STATE.report_failure(exc, notify)


def report_success(notify: Callable[[str, str], None]) -> None:
    STATE.report_success(notify)


def _prune_logs(home):
    """Best-effort retention, coordinated by one persistent stamp/lock inode."""
    logger = logging.getLogger(__name__)
    try:
        with (home / "logs-pruned.stamp").open("a+") as stamp:
            try:
                fcntl.flock(stamp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            now = time.time()
            stat = os.fstat(stamp.fileno())
            if stat.st_size and now - stat.st_mtime < 600:
                return
            # Stamp attempts too, so a busy or changed database does not delay every start.
            stamp.seek(0)
            stamp.truncate()
            stamp.write("pruned\n")
            stamp.flush()
            os.utime(stamp.fileno(), (now, now))
            path = home / "logs_2.sqlite"
            if not path.is_file():
                logger.warning("Codex log retention: logs_2.sqlite is missing; skipped")
                return
            # mode=rw avoids creating a replacement if the file disappears after the check.
            with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                                   timeout=3, isolation_level=None)) as db:
                db.execute("PRAGMA secure_delete=ON")
                cutoff = now - LOG_HOURS * 3600
                while db.execute("DELETE FROM logs WHERE rowid IN "
                                 "(SELECT rowid FROM logs WHERE ts < ? LIMIT 1000)",
                                 (cutoff,)).rowcount:
                    pass
                if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    logger.warning("Codex log retention: wal_checkpoint(TRUNCATE) busy; skipped")
                    return
                free = db.execute("PRAGMA freelist_count").fetchone()[0]
                pages = db.execute("PRAGMA page_count").fetchone()[0]
                size = db.execute("PRAGMA page_size").fetchone()[0]
                if free > pages / 4 and free * size > 8 * 1024 * 1024:
                    db.execute("VACUUM")
                    # In WAL mode VACUUM's rebuilt database also needs checkpointing.
                    if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                        logger.warning("Codex log retention: wal_checkpoint(TRUNCATE) busy; skipped")
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Codex log retention: %s", str(exc).replace("\n", " "))


def _limits(value: dict) -> bool:
    limits = (value.get("rateLimitsByLimitId") or {}).get("codex") or value.get("rateLimits") or {}
    for key in ("primary", "secondary"):
        window = limits.get(key) or {}
        if (window.get("windowDurationMins") == 10080
                and isinstance(window.get("usedPercent"), (int, float))):
            STATE.rate = {"used_percent": window["usedPercent"], "window_mins": 10080,
                          "resets_at": window.get("resetsAt")}
            return True
    return False


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


class Channel(planchannel.Channel):
    prefix = "codex"
    label = LABEL
    failure = CodexFailure
    closed_error = CodexClosed
    effort_in_session = False   # Codex accepts effort per turn; keep the retained thread.

    @property
    def timeout_s(self):
        return TIMEOUT_S

    def _calls_ready(self, step, now):
        return (step.calls and now - step.last_call >= 0.15
                and (step.usage_received or now - step.last_call >= 1.0))

    def _check_rate(self):
        rate = STATE.rate
        if rate is not None and rate["used_percent"] >= WEEKLY_PCT:
            raise CodexFailure("weekly cap")

    def _start(self, model, effort):
        self._start_server()
        if not _limits(self._request("account/rateLimits/read", {})):
            raise CodexFailure("Codex weekly rate limit unavailable")

    def _start_server(self):
        from .runner import RunRefused
        home = Path(HOME).expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        (home / "config.toml").write_text("")
        auth = home / "auth.json"
        if not auth.is_file():
            raise CodexFailure(core.say("Codex login missing; run shaft codex login",
                                        "Codex is not signed in: sign in with ChatGPT on the Model step"))
        _prune_logs(home)
        self._folder = Path(tempfile.mkdtemp(prefix="shaft-codex-")).resolve()
        argv = [BIN]
        for feature in FEATURES:
            argv += ["--disable", feature]
        for flag in PROMPT_CONTEXT_FLAGS:
            argv += ["-c", flag]
        argv += ["-c", 'web_search="disabled"', "-c", "agents.enabled=false", "-c", "mcp_servers={}", "app-server"]
        env = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "TMPDIR": str(self._folder), "LANG": os.environ.get("LANG", "en_US.UTF-8"), "CODEX_HOME": str(home)}
        try:
            self._process = subprocess.Popen(_seatbelt(argv, self._folder, home, auth.resolve()),
                cwd=self._folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
        except (OSError, RunRefused) as exc:
            shutil.rmtree(self._folder)
            if isinstance(exc, RunRefused):
                from .runner import _refused
                _refused(self.broker_name, exc)
            raise CodexFailure(f"cannot start Codex: {exc}") from exc
        self._start_workers()
        self._request("initialize", {"clientInfo": {"name": "shaft", "version": "1"},
                                     "capabilities": {"experimentalApi": True}})
        self._send({"method": "initialized"})

    def _result(self, method, reply):
        if "error" in reply:
            error = reply["error"]
            detail = error.get("message", "unknown") if isinstance(error, dict) else "unknown"
            cls = (_NoActiveTurn if method == "turn/steer" and isinstance(detail, str)
                   and detail.lower().startswith("no active turn") else CodexFailure)
            raise self._failure("Codex JSON-RPC error: " + str(detail), cls)
        result = reply.get("result")
        if not isinstance(result, dict):
            raise self._failure("malformed Codex RPC result")
        key = {"thread/start": "thread", "turn/start": "turn"}.get(method)
        if key and (not isinstance(result.get(key), dict) or not isinstance(result[key].get("id"), str)):
            raise self._failure("malformed Codex RPC result")
        return result

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
        if kind in ALLOWED_ITEM_TYPES:
            return
        if kind == "mcpToolCall" and item.get("server") == "codex" and item.get("tool", "").startswith("list_mcp_"):
            return
        tool = item.get("tool")
        reason = "Codex native item forbidden: " + str(kind)
        if tool is not None:
            reason += "; tool: " + str(tool)
        raise self._failure(reason, CodexSecurity)

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
        if step is None or params.get("threadId") != self._session:
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
            self._append_text(step, delta)
        elif method == "item/completed" and params["item"]["type"] == "agentMessage" and not step.event.is_set():
            item = params["item"]
            previous = step.items.get(item["id"], "")
            if not item["text"].startswith(previous):
                raise self._failure("Codex agent text disagrees with deltas")
            self._append_text(step, item["text"][len(previous):])
            step.items[item["id"]] = item["text"]
        elif method == "turn/completed":
            step.turn_ended = True
            for item in params["turn"].get("items") or []:
                self._check_item(item)
                if item["type"] == "agentMessage":
                    self._dispatch({"method": "item/completed", "params": {
                        "threadId": self._session, "turnId": step.turn, "item": item}})
            if step.event.is_set():
                return
            if params["turn"]["status"] != "completed":
                step.error = self._failure("Codex turn " + params["turn"]["status"] + ": " +
                    str((params["turn"].get("error") or {}).get("message", "unknown")))
            self._finish(step, "stop")

    def _open_session(self, model, system, tools, effort):
        self._tool_names = {t["function"]["name"] for t in tools}
        result = self._request("thread/start", {"baseInstructions": system, "ephemeral": True,
            "sandbox": "read-only", "approvalPolicy": "never", "approvalsReviewer": "user",
            "cwd": str(self._folder), "model": model[6:], "allowProviderModelFallback": False,
            "dynamicTools": [{"type": "function", "name": t["function"]["name"], "description": t["function"].get("description", ""),
                              "inputSchema": t["function"].get("parameters", {"type": "object"})} for t in tools]})
        return result["thread"]["id"]

    def _start_turn(self, text, images, effort):
        params = {"threadId": self._session, "input": self._input(text, images), "approvalPolicy": "never"}
        if effort:
            params["effort"] = effort
        return self._request("turn/start", params)["turn"]["id"]

    @staticmethod
    def _input(text, images):
        return [{"type": "text", "text": text}, *({"type": "image", "url": url} for url in images)]

    def _answer_calls(self, pending, results, text, images):
        for ident, call in pending:
            self._send({"id": ident, "result": {"success": call in results, "contentItems": [
                {"type": "inputText", "text": results.get(call, "not run")}]}})
        if text or images:   # tool results reach the code host; a steer reaches the model
            try:
                self._request("turn/steer", {"threadId": self._session,
                    "expectedTurnId": self._step.turn, "input": self._input(text or "[message]", images)})
            except _NoActiveTurn:
                return False
        return True

    def _cancel_turn(self, turn):
        self._request("turn/interrupt", {"threadId": self._session, "turnId": turn})


# -- signing in without a terminal (`shaft codex login --json`, the Mac app's Model step) ------------------------------
# `codex login --device-auth` prints a page to open and a one-time code to enter there, then polls until the operator
# has entered it; it opens no browser itself and needs no terminal. The app opens the page and shows the code.
LOGIN_TIMEOUT_S = 600
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_LINK = re.compile(r"https?://[^\s<>\"'`]+")
_CODE = re.compile(r"[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+")


class LoginStopped(Exception):
    """SIGTERM or SIGHUP reached the sign-in: whoever started it (the app's Cancel) wants it gone."""


def _plain(line: str) -> str:
    return _ANSI.sub("", line).strip()


def _stop_login(proc: subprocess.Popen) -> None:
    """The CLI and anything it started (its own process group) end now: SIGTERM, then SIGKILL after 5 s."""
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def device_login(home: Path, emit: Callable[[dict], None], timeout: float | None = None) -> tuple[bool, str]:
    """`codex login --device-auth` with CODEX_HOME=home and stdin closed. `emit` gets `{"url": …}` and `{"code": …}` as
    soon as the CLI prints them (each once). (True, "") once it exits 0 with a login in `home` (what `shaft codex status`
    reads, auth.json); else (False, its real error). A timeout, SIGTERM/SIGHUP or the parent's exit (the app quit) stops
    the CLI first: nothing of this sign-in keeps running."""
    import queue
    import threading
    limit = LOGIN_TIMEOUT_S if timeout is None else timeout
    lines: queue.Queue = queue.Queue()
    handlers = {}
    if threading.current_thread() is threading.main_thread():
        def stop(signum, frame):
            raise LoginStopped(signal.Signals(signum).name)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, stop)
    parent = os.getppid()
    proc = None
    readers: list = []
    try:
        proc = subprocess.Popen([BIN, "login", "--device-auth"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env={**os.environ, "CODEX_HOME": str(home), "NO_COLOR": "1"},
                                start_new_session=True)

        def read(name, stream):
            with contextlib.suppress(ValueError, OSError):      # closed under it once the sign-in is over
                for raw in iter(stream.readline, b""):
                    lines.put((name, raw.decode("utf-8", "replace")))
            lines.put((name, None))
        for name, stream in (("out", proc.stdout), ("err", proc.stderr)):
            readers.append(threading.Thread(target=read, args=(name, stream), daemon=True))
            readers[-1].start()
        deadline, open_streams, exited = time.monotonic() + limit, 2, None
        url = code = None
        expect_url = expect_code = None                # the stream whose prompt announced a link, a code
        # what each stream printed besides the page and the code, and where the code was on the stream that printed it
        # (only one stream's order is known: the two are read apart)
        said: dict[str, list[str]] = {"out": [], "err": []}
        code_at: tuple[str, int] | None = None
        def over() -> str:
            """Why the sign-in must end now, whatever Codex is doing: '' while it may go on."""
            if time.monotonic() > deadline:
                return (f"Codex sign-in timed out after {round(limit / 60) or 1} min: the code was not entered in time. "
                        "Sign in again for a new code.")
            return "the app that started this sign-in quit" if os.getppid() != parent else ""

        while open_streams:
            if why := over():
                return False, why
            if proc.poll() is not None:
                exited = exited or time.monotonic()
                if time.monotonic() - exited > 2:      # something it started keeps its output open: it is done
                    break
            try:
                name, line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                open_streams -= 1
                continue
            text = _plain(line)
            if not text:
                continue
            # the page: a link on its own line, or the first one after "Open this link" (never one inside an error)
            found = _LINK.search(text)
            if url is None and found and (expect_url == name or _LINK.fullmatch(text)):
                url = found.group(0).rstrip(".,;:)")
                emit({"url": url})
                continue
            # the code: the line right after "one-time code", or one shaped like a device code (ABCD-1234)
            if code is None and (expect_code == name and re.fullmatch(r"[A-Za-z0-9-]{4,32}", text)
                                 or _CODE.fullmatch(text)):
                code, code_at = text, (name, len(said[name]))
                emit({"code": code})
                continue
            low = text.lower()
            if "open this link" in low:
                expect_url = name
            expect_code = name if "one-time code" in low else None if expect_code == name else expect_code
            said[name].append(text)
        # Its output closed, but Codex may still run (and log in later): it stays under the same limits until it exits.
        while (status := proc.poll()) is None:
            if why := over():
                return False, why
            time.sleep(0.1)
        if status == 0 and (home / "auth.json").is_file():
            return True, ""
        def told(stream):          # its own words: on the stream with the prompt, only what came after the code
            rows = said[stream][code_at[1]:] if code_at and code_at[0] == stream else said[stream]
            return [r for r in rows if not r.startswith("Continue only if you started this login")]
        words = told("err") or told("out")
        return False, (" ".join(words) or f"codex login --device-auth exited with status {status}"
                       + ("" if status else f", leaving no login in {home}"))
    except OSError as e:
        return False, f"could not run {BIN}: {e}"
    finally:
        if proc is not None:
            _stop_login(proc)
            for reader in readers:
                reader.join(1)
            for stream in (proc.stdout, proc.stderr):
                stream.close()
        for sig, previous in handlers.items():
            signal.signal(sig, previous)


class _CatalogChannel(Channel):
    @property
    def timeout_s(self):
        return max(0, self.deadline - time.monotonic())


def _effort_listing() -> dict:
    from . import effortcatalog
    if not (Path(HOME).expanduser() / "auth.json").is_file():
        raise CodexFailure(core.say("Codex login missing; run shaft codex login",
                                    "Codex is not signed in: sign in with ChatGPT on the Model step"))
    channel = _CatalogChannel("effort-catalog", "effort-catalog", lambda message: None)
    channel.deadline = time.monotonic() + 15  # leave time for process cleanup within 20 seconds
    try:
        channel._start_server()
        models, cursor, seen = {}, None, set()
        while True:
            params = {"includeHidden": True}
            if cursor is not None:
                params["cursor"] = cursor
            result = channel._request("model/list", params)
            for item in result["data"]:
                models[item["model"]] = effortcatalog.entry(
                    [e["reasoningEffort"] for e in item["supportedReasoningEfforts"]],
                    item.get("defaultReasoningEffort"), hidden=item.get("hidden", False))
            cursor = result.get("nextCursor")
            if cursor is None:
                return models
            if cursor in seen:
                raise ValueError("repeated Codex model/list cursor")
            seen.add(cursor)
    finally:
        channel.close()


def efforts(model: str) -> tuple[tuple[str, ...], str | None] | None:
    from . import effortcatalog
    return effortcatalog.lookup("codex", model[6:], _effort_listing) if serves(model) else None
