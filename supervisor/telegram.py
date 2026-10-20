"""Telegram: talk with each instance from your phone, and get its messages to you there.

One bot per instance (its token in .env as TELEGRAM_TOKEN_<NAME>, `shaft telegram token <name>`), and ONE paired
Telegram account for all of them: `shaft telegram pair` prints a one-time code, you send `/pair <code>` to any
of the bots from your account, and from then on every bot answers that account only — anyone else is ignored
and logged; pairing again needs `shaft telegram unpair` in your terminal.

- You → instance: a message to its bot is a turn of a live conversation, through SHAFT's own API (the same path
  as any API client: a running cycle is paused only while the reply is written, the conversation is written up
  into the journal after SHAFT_IDLE_MINUTES). Replying to one of its messages quotes it — and answers it, if it
  was a question. A message sent while it is still replying joins that reply at its next step (/v1/chat/interject);
  one that arrives too late for it (the answer was already being written) is answered straight after. /new starts a new conversation, /wake starts a cycle now, /status says what it is doing.
- Instance → you: a message an instance sends the operator (send_message to "operator") that is routed to Telegram
  (channel.py: an open Telegram conversation, the instance's setting, or where you last wrote to it) arrives from its
  bot, read from the supervisor's message log (.supervisor/messages.jsonl, `alert`). No daily limit (the operator).
- Full text both ways (the operator accepted that Telegram's servers see it: bot chats are not end-to-end encrypted).
- Nothing listens for Telegram: the service long-polls getUpdates, so no port is opened and no webhook, tunnel
  or Funnel is needed. Tokens stay in the supervisor's .env and never reach an instance or a log line.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from .core import IDLE_MINUTES, NETWORK_ERRORS, SUP_DIR, instance_names, knob, log, secrets_env

API_BASE, _ = knob(   # tests point it at a fake
    "SHAFT_TELEGRAM_API", "https://api.telegram.org", str, "the Telegram Bot API endpoint", group="Ports and paths",
    services=("telegram",), web=False)
POLL_S = 30                      # long-poll timeout
STATE = "telegram.json"          # pairing, update offsets, the outbound cursor, which bot message was which question
PAIR_MINUTES = 10
MAX_TEXT = 4000                  # Telegram's limit is 4,096 characters per message, after formatting
CHUNK = 3000                     # Markdown per message, before formatting (tags and escapes add some)
_lock = threading.RLock()


# -- state ------------------------------------------------------------------------------------------------
def _path() -> Path:
    return SUP_DIR / STATE


def load() -> dict:
    with _lock:
        try:
            return json.loads(_path().read_text())
        except (OSError, ValueError):
            return {}


def save(st: dict) -> None:
    with _lock:
        SUP_DIR.mkdir(exist_ok=True)
        tmp = _path().with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.chmod(0o600)
        os.replace(tmp, _path())


def update(fn) -> dict:
    with _lock:
        st = load()
        fn(st)
        save(st)
        return st


def token_key(name: str) -> str:
    return "TELEGRAM_TOKEN_" + name.upper().replace("-", "_")


def tokens() -> dict[str, str]:
    """{instance: bot token} for the instances that have a bot."""
    env = secrets_env()
    return {n: env[token_key(n)] for n in instance_names() if env.get(token_key(n))}


# -- the Bot API ----------------------------------------------------------------------------------------------
class TelegramError(Exception):
    pass


def _multipart(fields: dict, files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "shaft" + secrets.token_hex(12)
    out = []
    for k, v in fields.items():
        v = v if isinstance(v, str) else json.dumps(v)
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (name, data) in files.items():
        safe = name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{safe}"\r\n'
                   f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def call(token: str, method: str, http_timeout: float = 15, files: dict | None = None, **params) -> object:
    """One Bot API call (with `files`, an upload). Errors never carry the token (it is part of the URL)."""
    if files:
        data, ctype = _multipart(params, files)
    else:
        data, ctype = json.dumps(params).encode(), "application/json"
    req = urllib.request.Request(f"{API_BASE}/bot{token}/{method}", data=data, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            desc = json.load(e).get("description", "")
        except ValueError:
            desc = ""
        raise TelegramError(f"{method}: HTTP {e.code} {desc}".strip()) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise TelegramError(f"{method}: {type(e).__name__}: {str(e).replace(token, '<token>')}") from None
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description', 'not ok')}")
    return data.get("result")


def send(token: str, chat_id: int, text: str, reply_to: int | None = None) -> list[int]:
    """Send Markdown as Telegram's HTML formatting, split into messages first (so no tag is ever cut); a part
    Telegram refuses to parse is sent again as plain text rather than lost. Returns the message ids."""
    ids = []
    for k, part in enumerate(split_markdown(text.strip() or "(empty)")):
        params = {"chat_id": chat_id}
        if reply_to and k == 0:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        formatted = to_html(part)
        try:
            if len(formatted) > MAX_TEXT:
                raise TelegramError("formatted text too long")
            ids.append(call(token, "sendMessage", text=formatted, parse_mode="HTML",
                            link_preview_options={"is_disabled": True}, **params)["message_id"])
        except TelegramError as e:
            if "parse" not in str(e).lower() and "too long" not in str(e):
                raise
            log(f"telegram: sent as plain text ({e})")
            ids.append(call(token, "sendMessage", text=part[:MAX_TEXT], **params)["message_id"])
    return ids


PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_MAX = 10 * 1024 * 1024


def send_file(token: str, chat_id: int, name: str, data: bytes, reply_to: int | None = None) -> int:
    """An attachment: a picture as a photo (shown inline), anything else as a document."""
    photo = Path(name).suffix.lower() in PHOTO_TYPES and len(data) <= PHOTO_MAX
    params = {"chat_id": chat_id}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    field, method = ("photo", "sendPhoto") if photo else ("document", "sendDocument")
    return call(token, method, http_timeout=120, files={field: (name, data)}, **params)["message_id"]


def download(token: str, file_id: str) -> bytes:
    """A file the operator sent: getFile, then the file itself (Telegram lets a bot download up to 20 MB)."""
    from .messages import ATTACH_MAX_BYTES
    path = call(token, "getFile", file_id=file_id)["file_path"]
    try:
        with urllib.request.urlopen(f"{API_BASE}/file/bot{token}/{path}", timeout=120) as r:
            return r.read(ATTACH_MAX_BYTES + 1)          # one byte over the limit is enough to refuse it
    except NETWORK_ERRORS as e:
        raise TelegramError(f"download: {type(e).__name__}: {str(e).replace(token, '<token>')}") from None


def incoming_files(msg: dict) -> list[tuple[str, str]]:
    """(file_id, file name) of what a Telegram message carries: a document, the largest size of a photo, audio,
    a voice note, a video."""
    out = []
    mid = msg.get("message_id")
    if msg.get("document"):
        d = msg["document"]
        out.append((d["file_id"], d.get("file_name") or f"document-{mid}"))
    if msg.get("photo"):
        out.append((msg["photo"][-1]["file_id"], f"photo-{mid}.jpg"))
    for kind, ext in (("audio", ".mp3"), ("voice", ".ogg"), ("video", ".mp4"), ("animation", ".mp4")):
        if msg.get(kind):
            f = msg[kind]
            out.append((f["file_id"], f.get("file_name") or f"{kind}-{mid}{ext}"))
    return out


# -- Markdown → Telegram HTML ----------------------------------------------------------------------------------
FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+#.-]*)\s*$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def split_markdown(md: str, limit: int = CHUNK) -> list[str]:
    """Parts of at most `limit` characters, cut at line ends; a code block cut in two is closed and reopened."""
    parts, cur, size, fence = [], [], 0, None
    for line in md.splitlines():
        for piece in [line[i:i + limit] for i in range(0, len(line), limit)] or [""]:
            if cur and size + len(piece) + 1 > limit:
                if fence is not None:
                    cur.append("```")
                parts.append("\n".join(cur).strip("\n"))
                cur, size = ([f"```{fence}"] if fence is not None else []), 0
            cur.append(piece)
            size += len(piece) + 1
            if (m := FENCE.match(piece)):
                fence = None if fence is not None else m.group(2)
    if cur:
        parts.append("\n".join(cur).strip("\n"))
    return [p for p in parts if p.strip()] or ["(empty)"]


def _inline(text: str) -> str:
    """One line's inline Markdown: `code`, **bold**, *italic*, _italic_, ~~strike~~, [links](url)."""
    out = []
    for k, seg in enumerate(re.split(r"(`[^`\n]+`)", text)):
        if k % 2:
            out.append(f"<code>{html.escape(seg[1:-1], quote=False)}</code>")
            continue
        s = html.escape(seg, quote=False)
        s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
                   lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', s)
        s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", s)
        s = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", s)
        s = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", s)
        s = re.sub(r"(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w])", r"<i>\1</i>", s)
        out.append(s)
    return "".join(out)


def _table(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows if not TABLE_SEP.match(r)]
    widths = [max(len(r[i]) if i < len(r) else 0 for r in cells) for i in range(max(map(len, cells)))]
    lines = ["  ".join((r[i] if i < len(r) else "").ljust(widths[i]) for i in range(len(widths))).rstrip()
             for r in cells]
    return "<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>"


def to_html(md: str) -> str:
    """Markdown as Telegram's HTML subset: headings bold, lists with bullets, quotes, code blocks, tables
    as aligned monospace. Anything else passes through as escaped text."""
    out, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if (m := FENCE.match(line)):
            lang, body, i = m.group(2), [], i + 1
            while i < len(lines) and not FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1                                    # the closing fence (or the end)
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(body), quote=False)}</code></pre>")
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and TABLE_SEP.match(lines[i + 1]):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        if line.lstrip().startswith(">"):
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(_inline(lines[i].lstrip()[1:].lstrip()))
                i += 1
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            continue
        if (m := re.match(r"^#{1,6}\s+(.*)$", line)):
            out.append(f"<b>{_inline(m.group(1).strip().rstrip('#').strip())}</b>")
        elif re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            out.append("──────────")
        elif (m := re.match(r"^(\s*)[-*+]\s+(\[[ xX]\]\s+)?(.*)$", line)):
            box = {"[ ]": "☐ ", "[x]": "☑ "}.get((m.group(2) or "").strip().lower(), "")
            out.append(f"{'  ' * (len(m.group(1)) // 2)}• {box}{_inline(m.group(3))}")
        else:
            out.append(_inline(line))
        i += 1
    return "\n".join(out)


# -- pairing ----------------------------------------------------------------------------------------------------
def _digest(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def new_pairing_code() -> str:
    st = load()
    if st.get("paired"):
        raise TelegramError(f"already paired with {st['paired'].get('username') or st['paired']['user_id']}; "
                            "`shaft telegram unpair` first")
    code = secrets.token_hex(4).upper()
    update(lambda s: s.update(pairing={"digest": _digest(code),
                                       "until": (datetime.now() + timedelta(minutes=PAIR_MINUTES)).isoformat()}))
    log("telegram: pairing code issued")
    return code


def try_pair(user: dict, code: str) -> str:
    """/pair <code> from a private chat: pairs this account if no account is paired and the code is right."""
    st = load()
    if st.get("paired"):
        return ""                                   # never says anything to an unpaired account
    p = st.get("pairing") or {}
    if not p or datetime.fromisoformat(p["until"]) < datetime.now() or \
            not secrets.compare_digest(p["digest"], _digest(code)):
        log(f"telegram: wrong or expired pairing code from user {user.get('id')}")
        return ""
    update(lambda s: (s.update(paired={"user_id": user["id"], "username": user.get("username", ""),
                                       "paired_at": datetime.now().isoformat(timespec="seconds")}),
                      s.pop("pairing", None)))
    log(f"telegram: paired with user {user['id']} ({user.get('username', '')})")
    return "Paired. From now on only this account can talk to SHAFT's bots."


def paired_id() -> int | None:
    return (load().get("paired") or {}).get("user_id")


def reachable(name: str) -> str:
    """'@its_bot' when the operator can be reached on Telegram through this instance's bot: a token, a known bot name,
    a paired account and the service running; '' otherwise. Instances are told this in send_message."""
    from .version import service
    st = load()
    bot = (st.get("bots") or {}).get(name, "")
    if not (bot and st.get("paired") and token_key(name) in secrets_env() and service("telegram", stale=False)):
        return ""
    return f"@{bot}"


def remember_bot(name: str, username: str) -> None:
    update(lambda s: s.setdefault("bots", {}).__setitem__(name, username))


# -- you → instance ---------------------------------------------------------------------------------------------
class Chat:
    """One instance's conversation over its bot: the history the API needs, and its message ids."""

    def __init__(self, name: str, token: str):
        self.name, self.token = name, token
        self.history: list[dict] = []
        self.last = 0.0
        self.mu = threading.Lock()                   # the history and the in-flight list; never held for a reply
        self.inflight: list[str] | None = None       # while a reply runs (one at a time): messages sent to it, in order

    def reset_if_idle(self) -> None:
        if self.history and time.time() - self.last > IDLE_MINUTES * 60:
            self.history = []                        # the API has written that conversation up: a new one starts

    def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()
        files = incoming_files(msg)
        if not text and not files:
            send(self.token, chat_id, "I can take text and files, not this kind of message.", msg["message_id"])
            return
        if files:
            try:
                text = (text or "(no text, only files)") + self.receive(files)
            except TelegramError as e:
                send(self.token, chat_id, f"(could not receive your file: {e})", msg["message_id"])
                return
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd in ("/start", "/help"):
            send(self.token, chat_id, f"I am {self.name}. Write to me and I answer. /new starts a new conversation, "
                                      "/wake starts a cycle now, /status says what I am doing.")
            return
        if cmd == "/status":
            from .dashboard import instance_state
            i = instance_state(self.name)
            line = f"{i['state']}{' since ' + i['since'] if i['since'] else ''}"
            for k in ("doing", "goal"):
                if i.get(k):
                    line += f"\n{k}: {i[k]}"
            line += f"\nnext wake {i['next_wake']} · today {i['calls_today']} model calls, ${i['usd_today']:.2f}"
            send(self.token, chat_id, line)
            return
        if cmd == "/wake":
            from .autonomy import start_wake
            try:
                send(self.token, chat_id, start_wake(self.name))
            except RuntimeError as e:
                send(self.token, chat_id, f"Not started: {e}")
            return
        if cmd == "/new":
            with self.mu:
                running = self.inflight is not None
                if not running:
                    self.history = []
            send(self.token, chat_id, "I am still replying: send /new once I have answered." if running
                 else "New conversation.")
            return
        if cmd == "/pair":
            return                                   # already paired (an unpaired account never gets here)
        quoted = msg.get("reply_to_message") or {}
        if quoted.get("text"):
            text = "> " + "\n> ".join(quoted["text"].splitlines()) + "\n\n" + text
            answer_question(self.name, quoted.get("message_id"))
        with self.mu:
            if self.inflight is not None:            # a reply is running: this message joins it (docs/plans/PLAN-interject.md)
                self.inflight.append(text)
                try:                                 # under mu: it can never reach two replies
                    if not interject_api(self.name, [self.history[0], {"role": "user", "content": text}]):
                        log(f"telegram: {self.name}'s reply had ended; the message is answered next")
                except TelegramError as e:
                    log(f"telegram: could not add a message to {self.name}'s reply ({e}); it is answered next")
                return
            self.reset_if_idle()
            self.history.append({"role": "user", "content": text})
            self.last = time.time()
            self.inflight = []
        try:
            self._converse(chat_id, msg["message_id"])
        except BaseException:
            with self.mu:                            # still ours: a new message would have joined it
                self.inflight = None
            raise

    def _converse(self, chat_id: int, message_id: int) -> None:
        """Reply to the history's last message, and to every message sent meanwhile: those the reply heard go before
        it in the history; the others (too late for it, or an instance whose code does not read them yet) get one
        reply straight after. Only this thread changes the history while the reply runs."""
        stop = threading.Event()
        threading.Thread(target=self._typing, args=(chat_id, stop), daemon=True).start()
        try:
            while True:
                try:
                    reply, heard = ask_api(self.name, list(self.history))
                except TelegramError as e:
                    with self.mu:
                        self.history.pop()
                        lost, self.inflight = len(self.inflight or []), None
                    send(self.token, chat_id, f"(no answer: {e}"
                         + (f" — nor to the {lost} message(s) you sent meanwhile" if lost else "") + ")",
                         message_id)
                    return
                with self.mu:
                    got, heard = self.inflight or [], min(heard, len(self.inflight or []))
                    self.history += [{"role": "user", "content": t} for t in got[:heard]]
                    self.history.append({"role": "assistant", "content": reply})
                    self.last = time.time()
                    rest = got[heard:]
                    if rest:
                        self.history.append({"role": "user", "content": "\n\n".join(rest)})
                    self.inflight = [] if rest else None   # decided with the list: nothing falls between
                send(self.token, chat_id, reply)
                if not rest:
                    return
        finally:
            stop.set()

    def receive(self, files: list[tuple[str, str]]) -> str:
        """Save the operator's files in the instance's attachments/ folder; returns the lines that tell it where."""
        from . import messages
        got = []
        for file_id, name in files:
            data = download(self.token, file_id)
            if len(data) > messages.ATTACH_MAX_BYTES:
                raise TelegramError(f"{name} is larger than {messages.size_text(messages.ATTACH_MAX_BYTES)}")
            got.append((name, data))
        try:
            return messages.from_operator(self.name, got, "telegram")[1]   # the same as the web UI's uploads
        except ValueError as e:
            raise TelegramError(str(e)) from None

    def _typing(self, chat_id: int, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                call(self.token, "sendChatAction", chat_id=chat_id, action="typing")
            except TelegramError:
                pass
            stop.wait(4.5)


def _api(route: str, name: str, messages: list[dict], timeout: float) -> dict:
    """POST to SHAFT's own API as the paired operator; its error message is raised as a TelegramError."""
    from . import api
    env = secrets_env()
    key, logins = env.get("SHAFT_API_KEY"), sorted(api.allowed_logins())
    if not key or not logins:
        raise TelegramError("the API is not set up (shaft api on)")
    req = urllib.request.Request(f"http://127.0.0.1:{api.PORT}/v1/chat/{route}",
                                 data=json.dumps({"model": name, "messages": messages}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                                          "Tailscale-User-Login": logins[0], "X-Shaft-Channel": "telegram",
                                          "User-Agent": "Telegram"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = TelegramError(json.load(e)["error"]["message"])
        except (ValueError, KeyError):
            err = TelegramError(f"API HTTP {e.code}")
        err.code = e.code
        raise err from None
    except NETWORK_ERRORS as e:
        raise TelegramError(f"the API is not reachable ({e}); is it on? shaft api status") from None


def ask_api(name: str, history: list[dict]) -> tuple[str, int]:
    """One reply through SHAFT's own API: a sandboxed run, pausing a running cycle only while it replies.
    Returns the reply and how many messages sent to it meanwhile (interject_api) it heard, oldest first."""
    data = _api("completions", name, history, 3600)
    reply = (data["choices"][0]["message"].get("content") or "").strip() or "(empty answer)"
    return reply, int(data.get("interjections_heard") or 0)


def interject_api(name: str, messages: list[dict]) -> bool:
    """Add the last message to the reply running in this conversation (its first user message says which).
    False when no reply is running there any more."""
    try:
        _api("interject", name, messages, 15)
    except TelegramError as e:
        if getattr(e, "code", None) == 409:
            return False
        raise
    return True


def answer_question(name: str, message_id: int | None) -> None:
    """A reply to the bot message that carried an instance's question answers it in the operator inbox too."""
    from . import messages
    q = (load().get("questions") or {}).get(f"{name}:{message_id}")
    if not q:
        return
    path = messages.OPERATOR_INBOX / q
    if path.exists():
        messages._archive(path)
        log(f"telegram: {name}'s question {q} answered in Telegram")


def handle_update(name: str, token: str, chat: Chat, upd: dict) -> None:
    msg = upd.get("message")
    if not msg:
        return
    user = msg.get("from") or {}
    if (msg.get("chat") or {}).get("type") != "private":
        log(f"telegram: {name}'s bot ignored a message from a {msg.get('chat', {}).get('type')} chat")
        return
    text = (msg.get("text") or "").strip()
    owner = paired_id()
    if owner is None:
        if text.lower().startswith("/pair "):
            if (said := try_pair(user, text.split(None, 1)[1])):
                send(token, msg["chat"]["id"], said)
        return
    if user.get("id") != owner:
        log(f"telegram: {name}'s bot ignored user {user.get('id')} ({user.get('username', '')}): not the paired account")
        return
    chat.handle(msg)


def poll(name: str, token: str, stop: threading.Event) -> None:
    chat = Chat(name, token)
    while not stop.is_set():
        offset = (load().get("offsets") or {}).get(name, 0)
        try:
            updates = call(token, "getUpdates", http_timeout=POLL_S + 15, offset=offset, timeout=POLL_S,
                           allowed_updates=["message"])
        except TelegramError as e:
            log(f"telegram: {name}'s bot: {e}")
            stop.wait(30)
            continue
        for upd in updates or []:
            update(lambda s: s.setdefault("offsets", {}).__setitem__(name, upd["update_id"] + 1))
            threading.Thread(target=_safe, args=(handle_update, name, token, chat, upd), daemon=True).start()


def _safe(fn, *args) -> None:
    try:
        fn(*args)
    except Exception as e:                       # one bad update must not stop the bot; say what went wrong
        log(f"telegram: {type(e).__name__}: {e}")


# -- instance → you ---------------------------------------------------------------------------------------------
def forward_new(toks: dict[str, str] | None = None) -> int:
    """Send the instances' messages to the operator that arrived since the last call, each from its own bot.
    Starts from 'now' the first time (no backlog). Returns how many were sent."""
    from . import messages
    owner = paired_id()
    toks = tokens() if toks is None else toks
    st = load()
    if owner is None or not toks:
        return 0
    rows, end = messages.appended(_sent_offset(st))   # in the order logged (messages.appended: never by timestamp)
    sent = 0
    for m, after in rows:
        if m.get("to") != "operator":
            update(lambda s: s.__setitem__("sent_offset", after))
            continue
        tok = toks.get(m["from"]) if m.get("alert") == "telegram" else None   # routed elsewhere: channel.py
        if tok:
            # it comes from the instance's own bot, so no "from <name>"; a question is marked: a reply answers it
            body = f"❓ **Question**\n\n{m['text']}" if m.get("kind") == "question" else m["text"]
            try:
                ids = send(tok, owner, body)
            except TelegramError as e:
                log(f"telegram: could not forward {m['file']}: {e}")
                return sent                         # try again from here next time
            for a in m.get("attachments") or []:      # after the text, answering it; one that fails is said, not lost
                from .core import PROJECT
                try:
                    send_file(tok, owner, a["name"], (PROJECT / a["path"]).read_bytes(), ids[0])
                except (OSError, TelegramError) as e:
                    log(f"telegram: could not send {a['path']}: {e}")
                    try:
                        send(tok, owner, f"(could not send the attachment {a['name']}: {e})", ids[0])
                    except TelegramError:
                        pass
            if m.get("kind") == "question":
                update(lambda s: s.setdefault("questions", {}).__setitem__(f"{m['from']}:{ids[0]}", m["file"]))
            sent += 1
        update(lambda s: s.__setitem__("sent_offset", after))
    update(lambda s: s.__setitem__("sent_offset", end))
    return sent


def _sent_offset(st: dict) -> int | None:
    """Where forwarding resumes in the message log: its byte offset — or, from a state that kept the older cursor (the
    last forwarded row's "ts|file"), the start of the first row logged after it, so nothing is skipped or sent twice."""
    if isinstance(st.get("sent_offset"), int):
        return st["sent_offset"]
    old = st.get("sent_cursor")
    if old is None:
        return None
    from . import messages
    rows, end = messages.appended(0)
    start = 0
    for m, after in rows:
        if m.get("ts", "") + "|" + m.get("file", "") > old:
            break
        start = after
    else:
        start = end
    update(lambda s: (s.__setitem__("sent_offset", start), s.pop("sent_cursor", None)))
    return start


def serve() -> int:
    toks = tokens()
    if not toks:
        print("no bot tokens: shaft telegram token <instance>")
        return 2
    from .version import record_service
    for name, token in toks.items():                 # the bots' names, for the instances' send_message tool
        try:
            remember_bot(name, call(token, "getMe").get("username", ""))
        except TelegramError as e:
            log(f"telegram: {name}'s bot: {e}")
    record_service("telegram", {"bots": sorted(toks), "paired": bool(paired_id())})
    stop = threading.Event()
    for name, token in toks.items():
        threading.Thread(target=poll, args=(name, token, stop), daemon=True).start()
    log(f"telegram: serving bots for {', '.join(sorted(toks))} ({'paired' if paired_id() else 'NOT paired yet'})")
    print(f"telegram: bots for {', '.join(sorted(toks))}")
    while True:
        try:
            forward_new(toks)
        except Exception as e:
            log(f"telegram: forwarding: {type(e).__name__}: {e}")
        time.sleep(5)


# -- operator commands -------------------------------------------------------------------------------------------
def command(action: str, name: str | None = None) -> int:
    from . import autonomy
    from .api import _set_secret
    if action == "serve":
        return serve()
    if action == "token":
        if not name or name not in instance_names():
            print(f"usage: shaft telegram token <instance> ({', '.join(instance_names())})")
            return 2
        import getpass
        tok = getpass.getpass(f"bot token for {name} from @BotFather (not echoed): ").strip() if sys.stdin.isatty() \
            else sys.stdin.readline().strip()
        try:
            me = call(tok, "getMe")
        except TelegramError as e:
            print(f"not saved: Telegram refused the token ({e})")
            return 1
        _set_secret(token_key(name), tok)
        remember_bot(name, me.get("username", ""))
        log(f"telegram: bot @{me.get('username')} set for {name}")
        print(f"{name} now talks through @{me.get('username')} — restart the service: shaft telegram on")
        return 0
    if action == "pair":
        try:
            code = new_pairing_code()
        except TelegramError as e:
            print(e)
            return 1
        bots = ", ".join(sorted(tokens())) or "(no bots yet: shaft telegram token <instance>)"
        print(f"Send this to any of the bots ({bots}) from your Telegram account, within {PAIR_MINUTES} minutes:\n\n"
              f"    /pair {code}\n\nOnly that account will be able to talk to them.")
        return 0
    if action == "unpair":
        update(lambda s: (s.pop("paired", None), s.pop("pairing", None)))
        log("telegram: unpaired by the operator")
        print("unpaired: the bots answer nobody until `shaft telegram pair`")
        return 0
    if action == "on":
        if not tokens():
            print("no bot tokens yet: shaft telegram token <instance>")
            return 2
        return autonomy.service("telegram", "on")
    if action == "off":
        return autonomy.service("telegram", "off")
    autonomy.service("telegram", "status")
    st = load()
    p = st.get("paired")
    print(f"bots: {', '.join(f'{n}' for n in sorted(tokens())) or 'none'} · "
          f"paired: {(p.get('username') or p['user_id']) if p else 'no (shaft telegram pair)'}")
    return 0
