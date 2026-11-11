"""The native chat apps' JSON API, /app/v1 on the web service (docs/app-api.md).
Stdlib only.

It shows a chat exactly as the PWA's chat page shows it: both draw from web_chat.build (the HTML renders it, this
serialises it with to_json), and every action goes through the function the web route calls — web_chat.send, queue,
background, stop, end, clear; web.feedback_action, save_doc, delete_doc, comment_action; web._serve_attachment.

Who may use it (§1.2): web._route has checked the tailnet login (SHAFT_API_LOGINS) and the Host before it hands a path
under /app/v1/ here, and never reads a cookie for it; here every route but /pair needs `Authorization: Bearer
<device>.<secret>`, the pair a paired browser's cookie holds, checked against devices.json by the cookie's own check
(web.paired_device). A header sent only by the app carries no ambient credential, so there is no CSRF token: a page
of another site can neither send it nor read an answer (no route sends CORS headers).

The long-poll (§4): GET /app/v1/chat/<n>?since=&live=&wait= holds the request — one thread of the web server's
ThreadingHTTPServer — until either revision of the feed moves (web_chat.build: `version`, `live`), woken by every
web_chat.save() and looking again every LOOK_S, or `wait` seconds pass (304). Held for the current chat, it marks the
device as watching it (web_chat.watching). Only a poll with `at` refreshes that mark during the hold, so a later
away cannot be undone by a held poll without `at`.

The stream (§6.1): GET /app/v1/chat/<n>/stream?after= is Server-Sent Events of the running reply, from the relay's own
memory (web_chat.Relay.events), on its own thread of the same server for as long as the reply runs (web.Handler._pour).
Held, it counts as watching the chat.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import urllib.parse
from datetime import datetime

from . import apns, web
from .core import NAME_RE, log
from .web import Request, Response
from .watch import CONFIG_KEYS, MODES

PREFIX = "/app/v1"
WAIT_MAX = 25                       # a long-poll is held at most this long (s): under URLSession's 60 s timeout
LOOK_S = 0.5                        # a held long-poll looks at the feed again this often, saved or not
WATCH_EVERY_S = 2.0                 # and marks its device watching again this often (web_chat.WATCH_S is 6)
KEEPALIVE_S = 2.0                   # a held stream (§6.1) writes a comment this often with nothing else to send: its
#                                     client's leaving is noticed and its watching refreshed even while the reply is quiet
MID = re.compile(r"[A-Za-z0-9_-]{1,64}")
PLATFORMS = ("ios", "macos")


class Refusal(Exception):
    """A request answered with an error (§1.5): raised anywhere below a route, answered by route()."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.response = error(status, code, message, **extra)


# -- answers ----------------------------------------------------------------------------------------------------------
def _json(status: int, data: dict, headers: dict | None = None) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False).encode(),
                    {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", **(headers or {})})


def error(status: int, code: str, message: str, **extra) -> Response:
    """{"error": {code, message, …}} (§1.5): `message` in words for the screen, never empty."""
    return _json(status, {"error": {"code": code, "message": message or code, **extra}})


def stamp(ts: str | float | None) -> str | None:
    """A stored time with the server's UTC offset in effect at that moment, at second precision (§1.6): the supervisor
    stores local times without one (2026-11-03T14:02:11 → 2026-11-03T14:02:11+01:00), a document its mtime as epoch
    seconds. None when there is none."""
    if ts is None or ts == "":
        return None
    try:
        dt = datetime.fromtimestamp(ts) if isinstance(ts, (int, float)) else datetime.fromisoformat(ts)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return dt.astimezone().isoformat(timespec="seconds")


def file_url(path: str) -> str:
    """Where the apps fetch a feed item's file (web_chat.build's `path`): /app/v1/file/chat/<cid>/<k>/<j>, or
    /app/v1/file/message/<file>/<j> with the message's file name URL-encoded."""
    kind, _, rest = path.partition("/")
    if kind == "message":
        file, _, j = rest.rpartition("/")
        rest = f"{urllib.parse.quote(file, safe='')}/{j}"
    return f"{PREFIX}/file/{kind}/{rest}"


def _reply(reply: dict | None) -> dict | None:
    return reply and {**reply, "started": stamp(reply["started"])}


def to_json(model: dict) -> dict:
    """A chat's model (web_chat.build) as the apps get it (§4.2): its times stamped (stamp), each file's `path` made
    its URL (file_url)."""
    out = json.loads(json.dumps(model))           # a copy: the model is the HTML's too
    out.pop("last_count", None)                  # server-only unread tie-breaker
    out["reply"] = _reply(out["reply"])
    out["last"] = stamp(out["last"])
    if out["chat"]:
        out["chat"].update(created=stamp(out["chat"]["created"]), ended=stamp(out["chat"]["ended"]))
    for it in out["items"]:
        it["ts"] = stamp(it["ts"])
        if it.get("cancelled"):             # a next-cycle note taken back: a time like any other (§1.6)
            it["cancelled"] = stamp(it["cancelled"])
        for a in it.get("attachments", []):
            a["url"] = file_url(a.pop("path"))
        if (r := it.get("rating")) and r["sent"]:
            r["sent"]["ts"] = stamp(r["sent"]["ts"])
    for p in out["pending_feedback"]:
        p["reply_ts"] = stamp(p["reply_ts"])
    return out


def partial(model: dict) -> dict:
    """Only `live` moved (§4.2): the running reply alone, not the whole feed again each second."""
    return {"instance": model["instance"], "partial": True, "version": model["version"], "live": model["live"],
            "reply": _reply(model["reply"])}


# -- the entry point --------------------------------------------------------------------------------------------------
def device(req: Request) -> str | None:
    """The paired device of the request's bearer (web.paired_device: the cookie's own check, last_seen refreshed the
    same way), or None."""
    scheme, _, cred = (req.headers.get("authorization") or "").partition(" ")
    dev, dot, secret = cred.strip().partition(".")
    if scheme.lower() != "bearer" or not dot or not dev or not secret:
        return None
    return dev if web.paired_device(dev, secret) else None


def route(req: Request) -> Response:
    """Every path under /app/v1/, once web._route has checked the login and the host. Never an empty body: what broke
    is said (500 internal) and logged."""
    try:
        return _route(req)
    except Refusal as e:
        return e.response
    except (Exception, SystemExit) as e:          # SystemExit: core.instance_root's way to say an instance is gone
        log(f"app api: {req.method} {req.path} failed: {type(e).__name__}: {e}")
        return error(500, "internal", f"{req.path} failed: {type(e).__name__}: {e}")


N, CID = r"(?P<n>[A-Za-z0-9_-]+)", r"(?P<cid>web-[A-Za-z0-9]+)"


def _route(req: Request) -> Response:
    path = req.path.removeprefix(PREFIX)
    if path == "/pair":
        if req.method != "POST":
            raise Refusal(405, "method", f"{req.method} is not supported here: POST")
        return _pair(req)
    dev = device(req)
    if not dev:
        log(f"app api refused a request without a paired bearer ({req.method} {req.path})")
        raise Refusal(401, "unpaired", "This device is not paired: pair it again with a link from "
                                       "shaft web pair --name <device>.")
    req.device = dev
    for pattern, methods in ROUTES:
        if m := re.fullmatch(pattern, path):
            if req.method not in methods:
                raise Refusal(405, "method", f"{req.method} is not supported here: {', '.join(methods)}")
            args = m.groupdict()
            if "n" in args and args["n"] not in web._names():
                raise Refusal(404, "not_found", f"No instance named {args['n']}.")
            return methods[req.method](req, **args)
    raise Refusal(404, "not_found", f"Nothing at {req.path}.")


def _body(req: Request) -> dict:
    try:
        data = json.loads(req.body or b"{}")
    except ValueError as e:
        raise Refusal(400, "bad_request", f"The body is not JSON: {e}") from None
    if not isinstance(data, dict):
        raise Refusal(400, "bad_request", "The body must be a JSON object.")
    return data


def _str(data: dict, key: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise Refusal(400, "bad_request", f"{key} must be a string.")
    return value


def _login(req: Request) -> str:
    return (req.headers.get("tailscale-user-login") or "").lower()


def _chat_of(name: str, cid: str) -> dict:
    from . import web_chat
    # "from": the From conversation (none until its first message)
    chat = (web_chat.from_chat(name) if cid == web_chat.FROM else web_chat.load(cid)) if isinstance(cid, str) else None
    if not chat or chat.get("instance") != name:
        raise Refusal(404, "not_found", f"{name} has no chat {cid}.")
    return chat


def _said(said: str, **more) -> Response:
    return _json(200, {"said": said, **more})


# -- pairing (§2) -----------------------------------------------------------------------------------------------------
def _pair(req: Request) -> Response:
    data = _body(req)
    platform = data.get("platform")
    if platform not in PLATFORMS:
        raise Refusal(400, "bad_request", f"platform must be one of {', '.join(PLATFORMS)}.")
    token = _str(data, "token", "") or web._token_from_link(_str(data, "link", ""))
    if not token:
        raise Refusal(400, "bad_request", "Send the pairing link (link) or its token (token).")
    got = web._use_pairing(token, _login(req), platform)
    if not got:
        raise Refusal(403, "pair_expired", "This pairing link is used, unknown or older than "
                                           f"{web.PAIR_MINUTES} minutes: make a new one with shaft web pair.")
    dev, secret = got
    return _json(200, {"device": dev, "secret": secret, "name": web.devices()[dev]["name"], "login": _login(req)})


def _unpair(req: Request) -> Response:
    web.unpair(req.device)
    return _said("This device is unpaired.")


# -- the chat list (§3) -----------------------------------------------------------------------------------------------
def _instances(req: Request) -> Response:
    from . import core, messages, opconfig, web_chat, web_seen, web_views
    history = messages.history()
    names = web._names()
    summaries = {n: web_chat.summary(n, history) for n in names}
    unread = web_seen.unread(req.device, summaries)
    rows = []
    for n in names:
        live, s = web._live(n), summaries[n]
        state = live.get("state") or "?"
        threads = web_chat.threads(n)
        replying = any(c.get("state") == "replying" for c in web_chat.open_chats(n))
        rows.append({"name": n, "state": state, "since": live.get("since") or "",
                     "busy_note": None if replying else web_views.busy_note(n, state),
                     "model": live.get("model") or "", "usd_today": live.get("usd_today") or 0,
                     "line": s.get("line") or "", "last": stamp(s.get("last")), "replying": replying,
                     "threads": len(threads), "unread": unread.get(n, False), "next_cycle": web._next_cycle(n),
                     "voice_mode": opconfig.voice_mode(core.instance_config(n))})
    return _etagged(req, {"instances": rows, "badge": sum(unread.values())})


def _etagged(req: Request, data: dict) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode()
    tag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
    if req.headers.get("if-none-match") == tag:
        return Response(304, b"", {"ETag": tag, "Cache-Control": "no-store"})
    return Response(200, body, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
                                "ETag": tag})


# -- cycles -----------------------------------------------------------------------------------------------------------
def _due_stamp(value: str | None) -> str | None:
    """Keep a valid plain date; offset a timestamp; leave unparseable text out of date fields."""
    parsed = stamp(value)
    return value if parsed and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else parsed


def _plan(req: Request, n: str) -> Response:
    from . import cycles
    out = cycles.plan(n, web._live(n))
    since = out["now"]["since"]
    if since and re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", since):
        out["now"]["since"] = stamp(since)
    for key in ("next_step", "improvement"):
        if out[key] is not None:
            out[key]["at"] = stamp(out[key].pop("ts"))
    for item in out["commitments"]:
        item["due"] = _due_stamp(item["due"])
    for row in out["queue"]["rows"]:
        due = row["due_at"]
        row["due_at"] = _due_stamp(due)
        if due is not None and row["due_at"] is None:
            row["due_at_text"] = due
    return _etagged(req, out)


def _cycles(req: Request, n: str) -> Response:
    from . import cycles
    filt, before = req.query.get("filter", ""), req.query.get("before")
    if filt not in cycles.FILTERS:
        raise Refusal(400, "bad_request", 'filter must be "", "yours", "short" or "delivered".')
    if before:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", before):
                raise ValueError
            datetime.fromisoformat(before)
        except ValueError:
            raise Refusal(400, "bad_request", "before must be a valid date in YYYY-MM-DD form.") from None
    out = cycles.days(n, before=before, filt=filt)
    for day in out["days"]:
        day["rows"] = [{**r, "at": stamp(r["ts"])} for r in day["rows"]]
        for row in day["rows"]:
            row.pop("ts")
    return _etagged(req, out)


def _cycle(req: Request, n: str, ts: str) -> Response:
    from . import cycles
    out = cycles.cycle(n, ts)
    if out is None:
        raise Refusal(404, "not_found", f"{n} has no cycle {ts}.")
    out.pop("where")
    out["at"] = stamp(out.pop("ts"))
    if (planned := out["planned"]) is not None:
        predictions = planned["predictions"]
        planned["predictions"] = [p for p in predictions if isinstance(p, str)] if isinstance(predictions, list) else []
        planned["attention"] = cycles.note_lines(planned["attention"], ("source", "why"))
    if (result := out["result"]) is not None:
        result["surprises"] = cycles.note_lines(result["surprises"], ("expected", "observed", "so_what"))
        result["doubts"] = cycles.note_lines(result["doubts"], ("step", "what"))
    return _etagged(req, out)


# -- the feed (§4) and earlier chats (§5) -----------------------------------------------------------------------------
def _feed(req: Request, n: str, cid: str | None = None) -> Response:
    from . import web_chat
    # blank values kept: `live=` is the revision of no reply running (web.Request.query drops them)
    qs = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(req.target).query,
                                                     keep_blank_values=True).items()}
    try:
        wait = int(qs.get("wait", "0"))
    except ValueError:
        wait = -1
    if not 0 <= wait <= WAIT_MAX:
        raise Refusal(400, "bad_request", f"wait is a number of seconds from 0 to {WAIT_MAX}.")
    since, live = qs.get("since"), qs.get("live")
    if (since is None) != (live is None):
        raise Refusal(400, "bad_request", "Send since and live together, from the same answer, or neither.")
    whole, at = qs.get("all") == "1", web._page_ms(qs.get("at"))
    model = web_chat.feed(n, cid, whole)
    if model is None:
        raise Refusal(404, "not_found", f"{n} has no chat {cid}.")
    watch_cid = (model.get("chat") or {}).get("id")
    watch = not model["archived"] and qs.get("watch") != "0"
    (web_chat.watching if watch else web_chat.unwatch)(n, req.device, at, cid=watch_cid)
    if since is None:
        return _json(200, to_json(model))
    end = marked = time.monotonic()
    end += wait
    while True:
        if model["version"] != since:
            return _json(200, to_json(model))
        if model["live"] != live:
            return _json(200, partial(model))
        left = end - time.monotonic()
        if left <= 0:
            return Response(304, b"", {"Cache-Control": "no-store"})
        if watch and at is not None and time.monotonic() - marked >= WATCH_EVERY_S:
            web_chat.watching(n, req.device, at, cid=watch_cid)  # not after the app said it went away (`at` older than its away)
            marked = time.monotonic()
        web_chat.wait_change(min(LOOK_S, left))
        model = web_chat.feed(n, cid, whole)
        if model is None:                         # cannot happen to a chat that existed: chats are never deleted
            raise Refusal(404, "not_found", f"{n} has no chat {cid}.")


def _threads(req: Request, n: str) -> Response:
    from . import web_chat, web_seen
    from .web_md import first_line
    chats = web_chat.threads(n)
    feeds = {c["id"]: web_chat.feed(n, c["id"]) for c in chats}
    feeds["from"] = web_chat.from_feed(n)
    unread = web_seen.unread_feeds(req.device, n, feeds)

    def preview(cid):
        model = feeds[cid]
        item = next((i for i in reversed(model["items"]) if i.get("text") or i.get("content")), {})
        return {"line": first_line(item.get("text", item.get("content", "")))[:80], "last": stamp(model["last"]),
                "unread": unread[cid]}

    return _json(200, {"threads": [
        {"id": c["id"], "title": web_chat.title(c), "created": stamp(c.get("created")),
         "state": c["state"], "waiting": (c.get("reply") or {}).get("waiting") or None,
         **preview(c["id"])} for c in chats],
        "from": {**preview("from"), "state": (web_chat.from_chat(n) or {}).get("state") or "idle",
                 "waiting": ((web_chat.from_chat(n) or {}).get("reply") or {}).get("waiting") or None}})


def _from(req: Request, n: str) -> Response:
    from . import web_chat
    model = web_chat.from_feed(n, whole=req.query.get("all") == "1")       # all of From, as a thread's ?all=1
    at = web._page_ms(req.query.get("at"))
    (web_chat.unwatch if req.query.get("watch") == "0" else web_chat.watching)(
        n, req.device, at, cid="from")
    return _json(200, to_json(model))


def _chats(req: Request, n: str) -> Response:
    from . import web_chat
    out = []
    for c in web_chat.chats(n):
        if c.get("state") == "ended":
            line, count = web_chat.excerpt(c)
            out.append({"id": c["id"], "created": stamp(c.get("created")), "ended": stamp(c.get("ended")),
                        "excerpt": line, "messages": count})
    return _json(200, {"chats": out})


# -- sending (§6) and the chat's actions (§7) -------------------------------------------------------------------------
def quote_text(text: str, quotes: list[dict]) -> str:
    """The ↩ Reply cards made into the message, exactly as the web's composer writes them (web.js quoteText and
    takeQuote), so both clients send the same text: each quote's lines as "> " lines (with "> (from notes/docs/<doc>)"
    first when it was quoted from a document), its answer after it; when any is answered the text goes first, else
    last; the non-empty pieces joined by a blank line."""
    if not quotes:
        return text
    pairs, answered = [], False
    for q in quotes:
        quoted = (f"> (from notes/docs/{q['doc']})\n" if q["doc"] else "") + "\n".join(
            f"> {line}".rstrip() for line in re.split(r"\r?\n", q["quote"].strip()))
        answer = q["answer"].strip()
        pairs.append(f"{quoted}\n\n{answer}" if answer else quoted)
        answered = answered or bool(answer)
    rest = text.strip()
    return "\n\n".join(p for p in ([rest, *pairs] if answered else [*pairs, rest]) if p)


def _quotes(raw: str) -> list[dict]:
    try:
        quotes = json.loads(raw or "[]")
    except ValueError:
        quotes = None
    if not isinstance(quotes, list) or not all(
            isinstance(q, dict) and all(isinstance(q.get(k, ""), str) for k in ("quote", "answer", "doc"))
            and isinstance(q.get("quote"), str) for q in quotes):
        raise Refusal(400, "bad_request", "quotes is a JSON list of {quote, answer, doc}.")
    return [{"quote": q["quote"], "answer": q.get("answer", ""), "doc": q.get("doc", "")} for q in quotes]


def _send(req: Request, n: str) -> Response:
    """The web's /chat/<n>/send path: web_chat.send (a new reply, or an interjection while one runs), queue (after the
    running reply) or background (its next cycle). The same `mid` again is not sent twice (web_chat)."""
    from . import web_chat, reply_links
    try:
        fields, files = web_chat.parse_multipart(req.headers.get("content-type", ""), req.body)
    except web_chat.Refused as e:
        raise Refusal(400, "bad_request", f"The body must be multipart/form-data: {e}") from None
    mid, mode, retry_of = fields.get("mid", ""), fields.get("mode") or "now", fields.get("retry_of", "")
    if not MID.fullmatch(mid):
        raise Refusal(400, "bad_request", "mid is required: 1–64 letters, digits, - or _ (a UUID).")
    if mode not in ("now", "queue", "background"):
        raise Refusal(400, "bad_request", "mode is now, queue or background.")
    if retry_of and (mode != "now" or not retry_of.isdigit()):
        raise Refusal(400, "bad_request", "retry_of is a reply's k, sent with mode now.")
    voice = fields.get("voice", "0")
    if voice not in ("0", "1", ""):
        raise Refusal(400, "bad_request", 'voice is "1" (spoken) or "0" (typed).')
    voice = voice == "1"
    if voice and mode == "background":
        raise Refusal(400, "bad_request", "voice is for a chat message (mode now or queue), not for its next cycle.")
    text = quote_text(fields.get("text", ""), _quotes(fields.get("quotes", "")))
    login = _login(req)
    try:
        if mode == "background":
            file, said = web_chat.background(n, login, text, files, mid=mid, cid=fields.get("cid", ""))
            reply_links.record(n, fields.get("in_reply_to"), f"send:{n}:{mid}")
            return _said(said, chat=None, file=file or None)
        if mode == "queue":
            chat, said = web_chat.queue(n, login, text, files, mid=mid, voice=voice, cid=fields.get("cid", ""))
        else:
            chat, said = web_chat.send(n, login, text, files, mid=mid, retry_of=retry_of, voice=voice, cid=fields.get("cid", ""))
    except web_chat.Refused as e:
        raise Refusal(409, "refused", str(e)) from None
    reply_links.record(n, fields.get("in_reply_to"), f"send:{n}:{mid}")
    return _said(said, chat=web_chat.public_id(n, (chat or {}).get("id")), file=None)


def _action(req: Request, n: str, act: str) -> Response:
    from . import web_chat, web_seen
    data = _body(req)
    try:
        if act == "new":
            chat = web_chat.new_thread(n, _login(req), title=_str(data, "title", ""))
            return _said("thread created", chat={"id": chat["id"], "title": web_chat.title(chat)})
        if act == "uncycle":
            return _said(web_chat.uncycle(n, _login(req), mid=_str(data, "mid", ""), file=_str(data, "file", "")))
        if act == "unqueue":
            cid = _str(data, "cid", "")
            if cid:
                _chat_of(n, cid)
            return _said(web_chat.unqueue(n, _str(data, "mid"), cid=cid))
        if act in ("seen", "away"):
            cid = _str(data, "cid", "") or None
            if cid and cid != "from":
                _chat_of(n, cid)
        if act == "seen":
            version = _str(data, "version")
            if not web_seen.seen(req.device, n, version, cid):
                now = web_chat.from_feed(n) if cid == "from" else web_chat.feed(n, cid)
                raise Refusal(409, "stale_version", "The chat moved since it was drawn: mark it seen once the new "
                                                    "version is on screen.", version=now["version"])
            return _json(200, {"unread": False, "badge": web_seen.badge(req.device)})
        if act == "away":
            at = data.get("at")
            if at is not None and not isinstance(at, (int, float)):
                raise Refusal(400, "bad_request", "at is the device's clock in Unix ms.")
            web_chat.unwatch(n, req.device, at, cid=cid)
            return _json(200, {"ok": True})
        cid = _str(data, "cid")
        chat = _chat_of(n, cid)
        if act == "rename":
            return _said(web_chat.rename(n, cid, _str(data, "title")))
        if act == "stop":                 # its relay is under the chat's id: "from" names the From conversation's
            return _said(web_chat.stop(chat["id"]))
        if act == "end":
            return _said(web_chat.end(chat["id"]))
        return _said(web_chat.clear(n, cid))
    except web_chat.Refused as e:
        raise Refusal(409, "refused", str(e)) from None


# -- the reply as it streams (§6.1) -------------------------------------------------------------------------------------
_held: dict[str, int] = {}                  # streams being held now, per instance
_held_lock = threading.Lock()


def held(n: str) -> int:
    """How many clients hold `n`'s stream now."""
    return _held.get(n, 0)


def sse(event: dict) -> bytes:
    """One event as Server-Sent Events: its seq as the `id` (a reconnect may send it back as Last-Event-ID), the
    event as one line of JSON."""
    return f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def _stream(req: Request, n: str) -> Response:
    """The running reply's events, in order, from the relay's memory (web_chat.Relay): no chat-file write, no poll
    between the API's chunk and the app. Replays those after `after`, then follows live; ends after `end`."""
    from . import web_chat
    qs = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(req.target).query,
                                                     keep_blank_values=True).items()}
    raw = qs.get("after", req.headers.get("last-event-id", "0")) or "0"
    if not raw.isdigit():
        raise Refusal(400, "bad_request", "after is the seq of the last event the app has, 0 for none.")
    after, reply, at = int(raw), qs.get("reply"), web._page_ms(qs.get("at"))
    chat = _chat_of(n, qs["cid"]) if qs.get("cid") else web_chat.target(n, "", writing=False)   # none: Main
    relay = web_chat.stream_of(chat["id"]) if chat else None
    if relay is not None and reply is not None and reply != relay.id:
        after = 0                                  # a newer reply than the app's: all of it
    if relay is None or (relay.ended is not None and len(relay.events) <= after):
        raise Refusal(409, "not_replying", f"{n} is not replying: nothing to stream.")
    dev = req.device
    web_chat.watching(n, dev, at, cid=chat["id"])                  # listening to it: no push for this chat reaches this device

    def pour():
        sent, marked = after, time.monotonic()
        with _held_lock:
            _held[n] = _held.get(n, 0) + 1
        try:
            yield f": reply {relay.id}\n\n".encode()
            while True:
                events = relay.events_after(sent, KEEPALIVE_S)
                if time.monotonic() - marked >= WATCH_EVERY_S:
                    web_chat.watching(n, dev, at, cid=chat["id"])
                    marked = time.monotonic()
                if not events:
                    yield b": keepalive\n\n"
                    continue
                yield b"".join(sse(e) for e in events)
                sent = events[-1]["seq"]
                if events[-1]["type"] == "end":
                    return
        except Exception as e:                    # noqa: BLE001 — past route(): say what broke, end the stream
            log(f"app api: {n}'s stream failed: {type(e).__name__}: {e}")
        finally:                                  # the reply ended, or the client left (web.Handler._pour closes it)
            with _held_lock:
                _held[n] -= 1
    return Response(200, b"", {"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store"},
                    stream=pour())


# -- files (§8) -------------------------------------------------------------------------------------------------------
def _chat_file(req: Request, cid: str, k: str, j: str) -> Response:
    r = web._chat_file(cid, int(k), int(j))
    if r is None:
        raise Refusal(404, "not_found", "No such file (any more).")
    return r


def _message_file(req: Request, file: str, j: str) -> Response:
    r = web._file(urllib.parse.unquote(file), int(j))
    if r is None:
        raise Refusal(404, "not_found", "No such file (any more).")
    return r


# -- feedback (§9) ----------------------------------------------------------------------------------------------------
def _feedback(req: Request, n: str) -> Response:
    data = _body(req)
    op = data.get("op")
    if op not in ("rate", "clear", "send", "unpend"):
        raise Refusal(400, "bad_request", "op is rate, clear, send or unpend.")
    # web.feedback_action reads a form: every field a string, as a browser posts it
    form = {k: str(v) for k, v in data.items() if isinstance(v, (str, int)) and not isinstance(v, bool)}
    try:
        said, item = web.feedback_action(n, _login(req), form)
    except ValueError as e:
        raise Refusal(409, "refused", str(e)) from None
    return _said(said, id=item["id"] if item and op != "clear" else None)


# -- documents and comments (§10) -------------------------------------------------------------------------------------
def _doc_chat(req: Request, n: str) -> dict | None:
    from . import web_chat
    cid = req.query.get("cid")
    return _chat_of(n, cid) if cid else web_chat.target(n, "", writing=False)        # none: Main


def _docs(req: Request, n: str) -> Response:
    from . import doc_comments, docs, web_views
    marked = web_views.in_chat(_doc_chat(req, n))
    try:
        listing = docs.listing(n)
    except (Exception, SystemExit) as e:          # the documents never take the chat down with them: say what failed
        log(f"app api: {n}'s documents: {type(e).__name__}: {e}")
        raise Refusal(500, "internal", f"documents not readable: {e}") from None
    try:
        counts = doc_comments.open_counts(n)
    except (Exception, SystemExit) as e:          # comments never take the documents down with them
        log(f"app api: {n}'s comments: {type(e).__name__}: {e}")
        counts = {}
    return _json(200, {"dir": docs.DIR, "max_bytes": docs.MAX_BYTES, "docs": [
        {"path": d["path"], "bytes": d["bytes"], "mtime": stamp(d["mtime"]), "sha": d["sha"],
         "too_big": d["sha"] == "too-big", "open_comments": counts.get(d["path"], 0), "in_chat": d["path"] in marked}
        for d in listing]})


def _doc_path(req: Request) -> str:
    from . import docs
    path = req.query.get("path", "")
    if not path:
        raise Refusal(400, "bad_request", "Name the document: ?path=<path in notes/docs/>.")
    try:
        return docs._rel(path)
    except docs.Refused as e:
        raise Refusal(409, "refused", str(e)) from None


def _doc(req: Request, n: str) -> Response:
    from . import doc_comments, docs, web_views
    chat, rel = _doc_chat(req, n), _doc_path(req)
    try:
        text, sha = docs.read(n, rel)
    except docs.Refused as e:
        if any(d["path"] == rel and d["sha"] == "too-big" for d in docs.listing(n)):
            raise Refusal(409, "too_big", f"{e}: download it instead (/app/v1/docs/{n}/raw).") from None
        raise Refusal(409, "refused", str(e)) from None
    if text is None:
        raise Refusal(404, "not_found", f"notes/docs/{rel} is not there (any more).")
    over = web_views.overwritten(((chat or {}).get("saved_docs") or {}).get(rel), sha, text)
    return _json(200, {"path": rel, "text": text, "sha": sha, "in_chat": rel in web_views.in_chat(chat),
                       "overwritten": over and {**over, "ts": stamp(over["ts"])},
                       "threads": [_thread(t, sha) for t in doc_comments.threads(n, rel)]})


def _thread(t: dict, sha: str) -> dict:
    from . import doc_comments
    return {"id": t["id"], "doc": t["doc"], "by": t.get("by", "operator"), "state": t["state"],
            "created": stamp(t.get("created")), "anchor": t.get("anchor"), "doc_changed": t.get("sha") != sha,
            "unsectioned": list(t.get("unsectioned") or []),
            "posts": [{"pid": p["pid"], "by": p["by"], "text": p["text"], "ts": stamp(p.get("ts")),
                       "unsent": not doc_comments.delivered(p), "sent": p.get("sent") or None,
                       "turn": p.get("turn") or None, "seen": p.get("seen") or None} for p in t["posts"]]}


def _raw(req: Request, n: str) -> Response:
    from . import docs
    rel = _doc_path(req)
    data = docs.raw(n, rel)
    if data is None:
        raise Refusal(404, "not_found", f"notes/docs/{rel} is not there (any more).")
    return Response(200, data, {"Content-Type": "text/markdown; charset=utf-8", "Content-Disposition":
                                "attachment; filename*=UTF-8''" + urllib.parse.quote(rel.rpartition("/")[2], safe="")})


def _save(req: Request, n: str) -> Response:
    from . import docs
    data = _body(req)
    form = {k: _str(data, k, "") for k in ("path", "text", "sha", "dir")}
    try:
        said, rel, sha = web.save_doc(n, _login(req), form)
    except docs.Refused as e:
        raise Refusal(409, "refused", f"refused: {e}") from None
    except web.DocStale as e:
        if not e.changed:
            raise Refusal(409, "refused", str(e)) from None
        creating = form["sha"] == "none"
        raise Refusal(409, "stale", str(e), sha=None if creating or e.sha == "none" else e.sha,
                      diff=None if creating else e.diff or None) from None
    return _said(said, path=rel, sha=sha)


def _delete(req: Request, n: str) -> Response:
    from . import docs
    data = _body(req)
    try:
        said = web.delete_doc(n, _login(req), {k: _str(data, k, "") for k in ("path", "sha")})
    except web.DocStale as e:
        raise Refusal(409, "stale", f"refused: {e}", sha=None if e.sha == "none" else e.sha, diff=None) from None
    except docs.Refused as e:
        raise Refusal(409, "refused", f"refused: {e}") from None
    except web.DocRefused as e:
        raise Refusal(409, "refused", str(e)) from None
    return _said(said)


SEND_WHEN = {"": "", "now": "1", "cycle": "cycle"}        # the apps' words for the web form's


def _comment(req: Request, n: str, act: str | None = None) -> Response:
    from . import doc_comments, web_chat
    data = _body(req)
    form = {k: _str(data, k, "") for k in ("pid", "text", "thread", "doc", "sha", "state", "when")}
    if act is None:
        if data.get("send", "") not in SEND_WHEN:
            raise Refusal(400, "bad_request", 'send is "", "now" or "cycle".')
        form["send"] = SEND_WHEN[data.get("send", "")]
        anchor = data.get("anchor")
        if anchor is not None and not isinstance(anchor, dict):
            raise Refusal(400, "bad_request", "anchor is null (the whole document) or {exact, prefix, suffix, start}.")
        form["anchor"] = json.dumps(anchor) if anchor is not None else ""
    elif act == "send" and form["when"] not in ("now", "cycle"):
        raise Refusal(400, "bad_request", 'when is "now" or "cycle".')
    try:
        said, tid = web.comment_action(n, _login(req), act, form)
    except (doc_comments.Refused, web_chat.Refused) as e:
        raise Refusal(409, "refused", f"refused: {e}") from None
    return _said(said, thread=tid)


# -- push (§11) -------------------------------------------------------------------------------------------------------
def _register(req: Request) -> Response:
    data = _body(req)
    if problem := apns.configured():
        raise Refusal(503, "push_off", problem)
    try:
        apns.register(req.device, *(_str(data, k) for k in ("token", "env", "topic", "platform")))
    except ValueError as e:
        raise Refusal(400, "bad_request", str(e)) from None
    return _json(200, {"push": apns.status(req.device)})


def _unregister(req: Request) -> Response:
    return _json(200, {"removed": bool(apns.unregister(req.device))})


def _push_status(req: Request) -> Response:
    return _json(200, {**apns.status(req.device), "problem": apns.configured()})


def _latest(req: Request) -> Response:
    """What the notification shows: the events queued for this device (webpush.take), with the chat each opens."""
    from . import web_seen, webpush
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.target).query, keep_blank_values=True)
    instance = query.get("instance", [None])[0]
    if instance is not None and not NAME_RE.fullmatch(instance):
        raise Refusal(400, "bad_request", "instance must be an instance name.")
    events = [{**e, "instance": m[1] if (m := re.fullmatch(r"/i/([A-Za-z0-9_-]+)/(?:chat|from)",
                                                       urllib.parse.urlsplit(e.get("url", "")).path)) else None}
              for e in webpush.take(req.device, instance=instance)]
    return _json(200, {"events": events, "badge": web_seen.badge(req.device)})


# -- instance settings and services (§15) -----------------------------------------------------------------------------
CONFIG_FIELDS = {"model": "LLM_MODEL", "effort": "LLM_REASONING_EFFORT",
                 "wake_interval": "WAKE_INTERVAL_MINUTES", "operator_name": "OPERATOR_NAME",
                 "voice_mode": "VOICE_MODE", "voice_effort": "VOICE_REASONING_EFFORT",
                 **{f"watch_{kind}": key for kind, key in CONFIG_KEYS.items()}}


def _who(req: Request) -> str:
    return f"app {web.devices()[req.device]['name']}"


def _settings(n: str, cfg: dict | None = None) -> dict:
    from . import autonomy, channel, cloudllm, core, opconfig
    cfg = core.instance_config(n) if cfg is None else cfg
    model = cfg.get("LLM_MODEL", "")
    choices = [{"model": c["model"], "label": c["label"]} for c in opconfig.model_choices()]
    if model not in [c["model"] for c in choices]:
        choices.append({"model": model, "label": model})
    efforts = opconfig.effort_choices(core.instance_root(n))
    raw = cfg.get("WAKE_INTERVAL_MINUTES", "")
    try:
        minutes = int(raw) if raw else autonomy.WAKE_DEFAULT_MIN
    except ValueError:
        minutes = autonomy.WAKE_DEFAULT_MIN
    interval = autonomy.wake_interval(n, cfg)
    cap = core._tool_settings(n).get("cloud_daily_usd")
    return {"instance": n, "paused": core.paused(n), "model": {"value": model, "choices": choices},
            **{f"watch_{kind}": {"value": cfg.get(key, "off"), "choices": list(MODES)}
               for kind, key in CONFIG_KEYS.items()},
            "effort": {"value": cfg.get("LLM_REASONING_EFFORT"), "choices": list(efforts) if efforts is not None else None},
            "wake_interval": {"value": minutes, "floor": autonomy.wake_floor(n),
                              "effective": int(interval.total_seconds() // 60) if interval else 0},
            "cloud_cap": {"value": float(cap) if cap is not None else None, "spent_today": cloudllm.spent_today(n)},
            "channel": {"value": channel.preference(n), "choices": ["auto", "web", "telegram"]},
            "operator_name": {"value": cfg.get("OPERATOR_NAME", "")},
            # As config.env has them (the defaults when it does not), so `expect` compares what was shown
            "voice_mode": {"value": cfg.get("VOICE_MODE", opconfig.VOICE_DEFAULTS["VOICE_MODE"]),
                           "choices": list(opconfig.VOICE_MODES)},
            "voice_effort": {"value": cfg.get("VOICE_REASONING_EFFORT", opconfig.VOICE_DEFAULTS["VOICE_REASONING_EFFORT"]),
                             "choices": [""] + list(efforts) if efforts is not None else None}}


def _get_settings(req: Request, n: str) -> Response:
    return _json(200, _settings(n))


def _setting_shape(field: str, value) -> bool:
    if field == "wake_interval":
        return type(value) is int
    if field == "cloud_cap":
        return value is None or (type(value) in (int, float) and math.isfinite(value))
    if field == "effort":
        return value is None or isinstance(value, str)
    return isinstance(value, str)


def _stale(n: str, settings: dict) -> None:
    raise Refusal(409, "stale", f"{n}'s settings changed: reload before saving.", settings=settings)


def _set_settings(req: Request, n: str) -> Response:
    from . import core, opconfig
    data = _body(req)
    field = data.get("field")
    if (set(data) != {"field", "value", "expect"} or not isinstance(field, str)
            or field not in (*CONFIG_FIELDS, "cloud_cap", "channel")
            or not all(_setting_shape(field, data[k]) for k in ("value", "expect"))):
        raise Refusal(400, "bad_request", "Send one field, its value and its expect value, with the setting's type.")
    cfg = core.instance_config(n)
    current = _settings(n, cfg)
    if current[field]["value"] != data["expect"]:
        _stale(n, current)
    who = _who(req)
    log(f"{who}: operator setting {field} for {n}")
    try:
        if field in CONFIG_FIELDS:
            key = CONFIG_FIELDS[field]
            value = data["value"]
            opconfig.set_values(n, {key: str(value) if value is not None else None}, {key: cfg.get(key)})
        elif field == "cloud_cap":
            value = "off" if data["value"] is None else str(data["value"])
            core._update_tool_settings(n, expect={"cloud_daily_usd": data["expect"]},
                                       cloud_daily_usd=core.limit_value("cloud-cap", value))
            log(f"{n}: cloud-cap set to {value} by the operator ({who})")
        else:
            from . import channel
            channel.set_preference(n, data["value"], expect=data["expect"])
            log(f"{n}: channel set to {data['value']} by the operator ({who})")
    except core.StaleSetting:
        _stale(n, _settings(n))
    except opconfig.Refused as e:
        fresh = _settings(n)
        if fresh[field]["value"] != data["expect"]:
            _stale(n, fresh)
        raise Refusal(409, "refused", str(e)) from None
    except ValueError as e:
        raise Refusal(409, "refused", str(e)) from None
    return _json(200, {"settings": _settings(n),
                       "applies": "next run" if field in ("model", "effort", "operator_name", "voice_effort") else "now"})


# -- voices (§17) -----------------------------------------------------------------------------------------------------
# supervisor.voices keeps them; imported in each route, as every route here imports what it uses.
VOICE_FIELDS = ("identifier", "name", "language", "quality")


def _voices_of(n: str) -> dict:
    from . import voices

    def stamped(row: dict | None) -> dict | None:
        return row and {**row, **({"ts": stamp(row["ts"]) or row["ts"]} if row.get("ts") else {})}
    return {"voices": {lang: stamped(v) for lang, v in voices.get(n).items()},
            "proposals": [stamped(p) for p in voices.proposals(n)], "available": voices.available()}


def _voices(req: Request, n: str) -> Response:
    return _json(200, _voices_of(n))


def _refused_voice(e: Exception) -> Refusal:
    return Refusal(409, "refused", str(e) or type(e).__name__)


def _set_voice(req: Request, n: str) -> Response:
    """The operator picks a voice for a language ({language, identifier}), or clears it ({language, clear: true})."""
    from . import voices
    data = _body(req)
    language = _str(data, "language")
    if data.get("clear") is True and set(data) == {"language", "clear"}:
        act = lambda: voices.clear(n, language)                                    # noqa: E731
    elif set(data) == {"language", "identifier"}:
        identifier = _str(data, "identifier")
        act = lambda: voices.assign(n, language, identifier, "operator")           # noqa: E731
    else:
        raise Refusal(400, "bad_request", "Send {language, identifier} to pick a voice, or {language, clear: true}.")
    log(f"{_who(req)}: operator voice {language} for {n}: {data.get('identifier') or 'cleared'}")
    try:
        act()
    except voices.Refused as e:
        raise _refused_voice(e) from None
    return _json(200, _voices_of(n))


def _decide_voice(req: Request, n: str, pid: str) -> Response:
    """The operator accepts or rejects the instance's proposal; the instance hears either way (voices.decide)."""
    from . import voices
    data = _body(req)
    if not isinstance(data.get("accept"), bool) or not isinstance(data.get("reason", ""), str):
        raise Refusal(400, "bad_request", "Send {accept: true|false, reason: str}.")
    log(f"{_who(req)}: operator {'accepted' if data['accept'] else 'rejected'} {n}'s voice proposal {pid}")
    try:
        voices.decide(n, pid, data["accept"], data.get("reason", ""))
    except voices.Refused as e:
        raise _refused_voice(e) from None
    return _json(200, _voices_of(n))


def _report_voices(req: Request) -> Response:
    """The device's installed voices (AVSpeechSynthesisVoice.speechVoices()), replacing its platform's list: what the
    instance may propose from and the operator may pick from."""
    from . import voices
    data = _body(req)
    platform, rows = data.get("platform"), data.get("voices")
    if platform not in PLATFORMS:
        raise Refusal(400, "bad_request", f"platform must be one of {', '.join(PLATFORMS)}.")
    if not isinstance(rows, list) or not all(isinstance(v, dict) and all(isinstance(v.get(k), str) for k in VOICE_FIELDS)
                                             for v in rows):
        raise Refusal(400, "bad_request", "voices is a list of {identifier, name, language, quality}, all strings.")
    try:
        voices.report(platform, [{k: v[k] for k in VOICE_FIELDS} for v in rows])
    except voices.Refused as e:
        raise _refused_voice(e) from None
    return _json(200, {"available": voices.available()})


def _pending_drain() -> dict | None:
    from . import autonomy, version
    pending = autonomy.restart_request()
    scheduler = version.service("scheduler", stale=False)
    return pending if pending and scheduler and pending.get("pid") == scheduler.get("pid") else None


def _pause_resume(req: Request, n: str, act: str) -> Response:
    from . import web_actions
    who = _who(req)
    log(f"{who}: operator {act} {n}")
    status, said = web_actions.pause_resume(act, n, who=who)
    if status != 200:
        raise Refusal(409, "refused", said)
    return _json(200, {"settings": _settings(n)})


def _services() -> list[dict]:
    from . import actions, autonomy, version
    rows = []
    for name in ("api", "scheduler"):
        rec = version.service(name, stale=False)
        row = {"name": name, "running": bool(rec), "pid": rec.get("pid") if rec else None,
               "started": stamp(rec.get("started")) if rec else None, "commit": rec.get("commit") if rec else None,
               "version": version.service_version(rec) if rec else None,
               "busy": actions.service_guard(name, "restart")}
        if name == "scheduler":
            drain = autonomy.drain_status()
            # A request is pending even before the scheduler's next tick writes its drain record.
            pending = _pending_drain()
            if pending and (not drain or pending.get("at") != drain.get("at")):
                drain = {**pending, "state": "draining", "waiting": []}
            row["drain"] = ({"state": drain["state"], "waiting": drain.get("waiting") or [],
                             "deadline": stamp(drain.get("deadline"))} if drain else None)
        rows.append(row)
    return rows


def _services_response() -> Response:
    from . import version
    return _json(200, {"services": _services(),
                       "version": version.service_version(version.service("web", stale=False))})


def _get_services(req: Request) -> Response:
    return _services_response()


def _service_action(req: Request, service: str, act: str) -> Response:
    from . import actions, autonomy
    lock = actions.restart_lock(service)
    if not lock.acquire(blocking=act == "cancel"):
        raise Refusal(409, "restarting", f"{service} is already restarting.")
    try:
        who = _who(req)
        log(f"{who}: operator {act} {service}")
        if act == "restart" and service == "api":
            if why := actions.service_guard("api", "restart"):
                raise Refusal(409, "busy", why)
            args, timeout = ["api", "on"], actions.SLOW_S
        else:
            if act == "restart" and (_pending_drain() or autonomy.draining_now()):
                return _services_response()
            args = ["autonomy", "restart"] + (["--cancel"] if act == "cancel" else [])
            timeout = actions.RUN_NOW_S
        ok, said = actions.run_now(args, timeout, who=who)
    finally:
        lock.release()
    if not ok:
        raise Refusal(409, "refused", said)
    return _services_response()


ROUTES: list[tuple[str, dict]] = [
    (r"/unpair", {"POST": _unpair}),
    (r"/instances", {"GET": _instances}),
    (rf"/plan/{N}", {"GET": _plan}),
    (rf"/cycles/{N}", {"GET": _cycles}),
    (rf"/cycles/{N}/(?P<ts>\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}})", {"GET": _cycle}),
    (rf"/settings/{N}", {"GET": _get_settings, "POST": _set_settings}),
    (rf"/settings/{N}/(?P<act>pause|resume)", {"POST": _pause_resume}),
    (r"/voices/report", {"POST": _report_voices}),            # before /voices/<n>: "report" is not an instance
    (rf"/voices/{N}", {"GET": _voices, "POST": _set_voice}),
    (rf"/voices/{N}/proposals/(?P<pid>[A-Za-z0-9_-]{{1,64}})", {"POST": _decide_voice}),
    (r"/services", {"GET": _get_services}),
    (r"/services/(?P<service>api|scheduler)/(?P<act>restart)", {"POST": _service_action}),
    (r"/services/(?P<service>scheduler)/(?P<act>cancel)", {"POST": _service_action}),
    (rf"/chat/{N}", {"GET": _feed}),
    (rf"/chat/{N}/send", {"POST": _send}),
    (rf"/chat/{N}/stream", {"GET": _stream}),
    (rf"/chat/{N}/(?P<act>new|rename|unqueue|uncycle|stop|end|clear|seen|away)", {"POST": _action}),
    (rf"/chat/{N}/{CID}", {"GET": _feed}),
    (rf"/chats/{N}", {"GET": _chats}),
    (rf"/threads/{N}", {"GET": _threads}),
    (rf"/from/{N}", {"GET": _from}),
    (rf"/file/chat/{CID}/(?P<k>\d+)/(?P<j>\d+)", {"GET": _chat_file}),
    (r"/file/message/(?P<file>[^/]+)/(?P<j>\d+)", {"GET": _message_file}),
    (rf"/feedback/{N}", {"POST": _feedback}),
    (rf"/docs/{N}", {"GET": _docs}),
    (rf"/docs/{N}/doc", {"GET": _doc}),
    (rf"/docs/{N}/raw", {"GET": _raw}),
    (rf"/docs/{N}/save", {"POST": _save}),
    (rf"/docs/{N}/delete", {"POST": _delete}),
    (rf"/docs/{N}/comment(?:/(?P<act>edit|delete|send|state))?", {"POST": _comment}),
    (r"/push/register", {"POST": _register, "DELETE": _unregister}),
    (r"/push/status", {"GET": _push_status}),
    (r"/push/latest", {"GET": _latest}),
]
