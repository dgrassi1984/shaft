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

On loopback (SHAFT_WEB_LOCAL on, Host 127.0.0.1 or localhost at the web port: web.local_hosts, and none of the headers
tailscale serve's proxy adds: web.PROXY_HEADERS) web._route checks no login and marks the request local: the bearer
is then the only credential, the identity is the device's recorded login (_login), never a header, and /pair is
refused (local_pair) — such a device is made by `shaft web pair --local`.

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
import io
import json
import math
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime

from . import apns, meetings, order, speakers, web
from .core import NAME_RE, log
from .web import Request, Response
from .watch import CONFIG_KEYS, MODES

PREFIX = "/app/v1"
API_VERSION = 1                     # GET /app/v1/version's `api`: bumped on every breaking change to /app/v1 (an app
#                                     refuses a server below the version it needs: docs/app-api.md §19)
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
        if it.get("answer"):
            it["answer"]["ts"] = stamp(it["answer"]["ts"])
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
    if path == "/local":
        return _local_identity(req)
    if path == "/pair":
        if req.local:                               # a loopback request has no tailnet login to pair as
            log(f"app api refused a loopback pairing ({req.method} {req.path})")
            raise Refusal(403, "local_pair", "This Mac's app pairs with the server on this Mac directly, not with a "
                                             "link: open SHAFT on this Mac and it pairs itself.")
        if req.method != "POST":
            raise Refusal(405, "method", f"{req.method} is not supported here: POST")
        return _pair(req)
    dev = device(req)
    if not dev:
        log(f"app api refused a request without a paired bearer ({req.method} {req.path}{', loopback' * req.local})")
        raise _unpaired(req)
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


def _unpaired(req: Request) -> Refusal:
    """Only the apps read /app/v1, and an app has no terminal: what to do is said in the apps' own terms."""
    return Refusal(401, "unpaired", "This app is no longer paired with the server on this Mac: pair it again on the "
                                    "screen SHAFT shows." if req.local else
                                    "This device is not paired: pair it again with a new link from SHAFT on your Mac "
                                    "(Settings, Services, Use SHAFT from iPhone/iPad).")


def _login(req: Request) -> str:
    """Who acts: the tailnet login web._route checked, or on loopback (req.local) the paired device's recorded login —
    never a header there, which any local process could send. A device unpaired since its bearer was checked (or a
    record without a login) is refused as unpaired, not acted for."""
    if req.local:
        login = (web.devices().get(req.device) or {}).get("login")
        if not isinstance(login, str) or not login:
            log(f"app api refused a loopback request: device {req.device} has no record now ({req.method} {req.path})")
            raise _unpaired(req)
        return login.lower()
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
                                           f"{web.PAIR_MINUTES} minutes: make a new one in SHAFT on your Mac "
                                           "(Settings, Services, Use SHAFT from iPhone/iPad).")
    dev, secret = got
    return _json(200, {"device": dev, "secret": secret, "name": web.devices()[dev]["name"], "login": _login(req)})


def _unpair(req: Request) -> Response:
    web.unpair(req.device)
    return _said("This device is unpaired.")


# -- the chat list (§3) -----------------------------------------------------------------------------------------------
def _instances(req: Request) -> Response:
    from . import core, dashboard, messages, opconfig, web_chat, web_seen, web_views
    history = messages.history()
    names = web._names()
    paused = []
    visible = []
    for n in names:
        if core.paused_alone(n):
            flag = core.SUP_DIR / f"paused-{n}"
            try:
                try:
                    since = stamp(flag.read_text().strip())
                except (OSError, UnicodeError):
                    since = None
                since = since or stamp(flag.stat().st_mtime)
            except OSError:
                since = None
            paused.append({"name": n, "since": since})
        else:
            visible.append(n)
    names = visible
    summaries = {n: web_chat.summary(n, history) for n in names}
    conversations = {n: web_seen.unread_conversations(req.device, n) for n in names}
    unread = {n: any(marks.values()) for n, marks in conversations.items()}
    rows = []
    for n in names:
        live, s = dashboard.live_state(n), summaries[n]
        state = live.get("state") or "?"
        threads = web_chat.threads(n)
        replying = any(c.get("state") == "replying" for c in web_chat.open_chats(n))
        rows.append({"name": n, "state": state, "since": live.get("since") or "",
                     "busy_note": None if replying else web_views.busy_note(n, state),
                     "model": live.get("model") or "", "usd_today": live.get("usd_today") or 0,
                     "line": s.get("line") or "", "last": stamp(s.get("last")), "replying": replying,
                     "threads": len(threads), "threads_rev": _threads_rev(n, threads), "unread": unread.get(n, False),
                     "unread_main": conversations[n].get("from", False),
                     "unread_threads": sum(v for cid, v in conversations[n].items() if cid != "from"),
                     "next_cycle": web._next_cycle(n),
                     "voice_mode": opconfig.voice_mode(core.instance_config(n))})
    return _etagged(req, {"instances": rows, "paused": paused, "instances_rev": order.instance_rev(),
                          "badge": sum(unread.values())})


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
    from . import cycles, dashboard
    out = cycles.plan(n, dashboard.live_state(n))
    since = out["now"]["since"]
    if since and re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", since):
        out["now"]["since"] = stamp(since)
    for key in ("next_step", "improvement"):
        if out[key] is not None:
            out[key]["at"] = stamp(out[key].pop("ts"))
    for item in out["commitments"]:
        item["due"] = _due_stamp(item["due"])
    for row in out["projects"]["rows"]:
        for key in ("opened_at", "deadline", "closed_at"):
            if row[key]:
                row[key] = stamp(row[key])
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


# -- the feed (§4), From (§4.10) and earlier chats (§5) ----------------------------------------------------------------
def _poll(req: Request, n: str, build, watch_cid, cid: str | None = None) -> Response:
    """One long-poll (§4), shared by a thread's feed and From: parse and validate since/live/wait (the same 400s), mark
    the device watching, and hold the request until either revision of the feed moves (`version`, `live`) or `wait`
    seconds pass (304). `build` makes the model each look (web_chat.feed or web_chat.from_feed, `whole` passed); `watch_cid`
    is the conversation watched — a callable over the model (a thread's chat id) or a literal ("from"); `cid` names it
    in a 404 (From never 404s: web_chat.from_feed always builds)."""
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
    model = build(whole)
    if model is None:
        raise Refusal(404, "not_found", f"{n} has no chat {cid}.")
    watch = not model["archived"] and qs.get("watch") != "0"
    wcid = watch_cid(model) if callable(watch_cid) else watch_cid
    (web_chat.watching if watch else web_chat.unwatch)(n, req.device, at, cid=wcid)
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
            web_chat.watching(n, req.device, at, cid=wcid)  # not after the app said it went away (`at` older than its away)
            marked = time.monotonic()
        web_chat.wait_change(min(LOOK_S, left))
        model = build(whole)
        if model is None:                         # cannot happen to a chat that existed: chats are never deleted
            raise Refusal(404, "not_found", f"{n} has no chat {cid}.")


def _feed(req: Request, n: str, cid: str | None = None) -> Response:
    from . import web_chat
    return _poll(req, n, lambda whole: web_chat.feed(n, cid, whole),
                 lambda model: (model.get("chat") or {}).get("id"), cid)


def _threads_rev(n: str, chats: list[dict]) -> str:
    """Refetch rows when titles, lifecycle, waiting state or manual order changes."""
    from . import web_chat
    rows = [(c["id"], web_chat.title(c),
             "waiting" if c["state"] == "replying" and (c.get("reply") or {}).get("waiting") else c["state"], web_chat.failed(c)) for c in chats]
    return hashlib.sha256(json.dumps([order.thread_rev(n), rows, web_chat.failed(web_chat.from_chat(n))]).encode()).hexdigest()[:16]


def _move(req: Request, n: str | None = None) -> Response:
    data = _body(req)
    after = None if data.get("after") is None else _str(data, "after")
    try:
        moved = (order.move_thread(n, _str(data, "id"), after) if n is not None
                 else order.move_instance(_str(data, "name"), after))
    except order.Refused as e:
        raise Refusal(409, "refused", str(e)) from None
    return _json(200, moved)


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

    return _json(200, {"threads_rev": _threads_rev(n, chats), "threads": [
        {"id": c["id"], "title": web_chat.title(c), "created": stamp(c.get("created")),
         "state": c["state"], "waiting": (c.get("reply") or {}).get("waiting") or None,
         **preview(c["id"]), "failed": web_chat.failed(c),
         "empty": web_chat.empty(n, c, feeds[c["id"]])} for c in chats],
        "from": {**preview("from"), "failed": web_chat.failed(web_chat.from_chat(n)), "state": (web_chat.from_chat(n) or {}).get("state") or "idle",
                 "waiting": ((web_chat.from_chat(n) or {}).get("reply") or {}).get("waiting") or None}})


def _notes(req: Request, n: str) -> Response:
    from . import web_chat
    return _json(200, {"notes": [{**row, "ts": stamp(row["ts"])} for row in web_chat.pending_notes(n)]})


def _from(req: Request, n: str) -> Response:
    from . import web_chat
    return _poll(req, n, lambda whole: web_chat.from_feed(n, whole), "from", "from")


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


def _answer(req: Request, n: str) -> Response:
    return _json(200, answer_question(n, _login(req), _body(req)))


def answer_question(n: str, login: str, data: dict) -> dict:
    """Answer a choice card through the ordinary Reply delivery path, once per question."""
    from . import questions, messages, web_chat, reply_links
    from .core import SUP_DIR
    import fcntl
    mid, cid, file = (data.get(k) for k in ("mid", "cid", "file"))
    if not isinstance(mid, str) or not MID.fullmatch(mid):
        raise Refusal(400, "bad_answer", "mid is required: 1–64 letters, digits, - or _.")
    if not isinstance(cid, str) or not cid:
        raise Refusal(400, "bad_answer", "cid is required: a thread id or from.")
    question = next((m for m in messages.history() if m["file"] == file and m["from"] == n
                     and m["to"] == "operator" and m["kind"] == "question"), None)
    if question is None:
        raise Refusal(404, "not_found", "No such question from this instance.")
    SUP_DIR.mkdir(parents=True, exist_ok=True)
    with (SUP_DIR / "question-answers.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = dict(questions.answers())
        if old := stored.get(file):
            if old["mid"] == mid:
                return {"said": old["said"], "answer": {**old["answer"], "ts": stamp(old["answer"]["ts"])}}
            raise Refusal(409, "answered", "This question was already answered.",
                          answer={**old["answer"], "ts": stamp(old["answer"]["ts"])})
        # Answered or closed another way (a quoted Reply, Telegram, the CLI): a late card or notification adds nothing
        if file not in messages._unread("operator"):
            raise Refusal(409, "closed", "This question was already answered or closed.")
        try:
            answer = questions.validate_answer(question.get("choices"), data)
        except ValueError as e:
            raise Refusal(400, "bad_answer", str(e)) from None
        chat = None
        if cid != "from":
            chat = web_chat.load(cid)
            if not chat or chat.get("instance") != n:
                raise Refusal(404, "not_found", "No such thread from this instance.")
        text = questions.reply_text(question, answer)
        # Recorded before it is delivered: a store that cannot be written stops the delivery, so a second device can
        # never deliver a different answer because the first one's was not kept. A refused delivery takes it back.
        stored[file] = {"mid": mid, "said": "", "answer": answer}
        questions.save_answers(stored)
        try:
            # From and ended threads use the next-cycle Reply path specified by the card contract. quoted=False: the
            # answer closes its own question only, never another whose text its quote happens to match
            if cid == "from" or chat.get("state") == "ended" or web_chat.is_from(chat):
                _, said = web_chat.background(n, login, text, [], mid=mid, cid=cid, in_reply_to=file, quoted=False)
            else:
                _, said = web_chat.send(n, login, text, [], mid=mid, cid=cid, quoted=False)
        except BaseException as e:
            stored.pop(file)
            questions.save_answers(stored)
            if isinstance(e, web_chat.Refused):
                raise Refusal(409, "refused", str(e)) from None
            raise
        messages._archive(messages.OPERATOR_INBOX / file, "reply")
        reply_links.record(n, file, f"send:{n}:{mid}")
        stored[file] = {"mid": mid, "said": said, "answer": answer}
        questions.save_answers(stored)
        return {"said": said, "answer": {**answer, "ts": stamp(answer["ts"])}}


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
            return _said(web_chat.end(chat["id"], if_empty=data.get("if_empty") is True))
        return _said(web_chat.clear(n, cid))
    except web_chat.NotEmpty as e:
        raise Refusal(409, "not_empty", str(e)) from None
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
    # Listening to it: no push for this chat reaches this device. `watch=0`: the device follows the reply for someone
    # else (the iPhone relaying it to the Apple Watch) and still wants its push, as a feed read with `watch=0` does.
    watch = qs.get("watch", "1") != "0"
    if watch:
        web_chat.watching(n, dev, at, cid=chat["id"])

    def pour():
        sent, marked = after, time.monotonic()
        with _held_lock:
            _held[n] = _held.get(n, 0) + 1
        try:
            yield f": reply {relay.id}\n\n".encode()
            while True:
                events = relay.events_after(sent, KEEPALIVE_S)
                if watch and time.monotonic() - marked >= WATCH_EVERY_S:
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
    """The paired device acting, for the log and the CLI's `who`. A device unpaired since its bearer was checked is
    refused as unpaired (401), not answered with a crash."""
    record = web.devices().get(req.device) or {}
    if not isinstance(record.get("name"), str) or not record["name"]:
        log(f"app api refused a request: device {req.device} has no record now ({req.method} {req.path})")
        raise _unpaired(req)
    return f"app {record['name']}"


def _settings(n: str, cfg: dict | None = None) -> dict:
    from . import autonomy, channel, cloudllm, core, opconfig
    cfg = core.instance_config(n) if cfg is None else cfg
    model = cfg.get("LLM_MODEL", "")
    choices = [{"model": c["model"], "label": c["label"]} for c in opconfig.model_choices()]
    if model not in [c["model"] for c in choices]:
        choices.append({"model": model, "label": model})
    efforts = opconfig.effort_choices(core.instance_root(n), model)
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
    notes = []
    try:
        if field in CONFIG_FIELDS:
            key = CONFIG_FIELDS[field]
            value = data["value"]
            notes = opconfig.set_values(n, {key: str(value) if value is not None else None}, {key: cfg.get(key)})["notes"]
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
                       "notes": notes,
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


# Who this web process is, for the Mac app paired over the tailnet with the server on its own Mac: /version gives it to
# a paired device with `local_port`, and GET /app/v1/local answers it on loopback with no bearer. The app sends its bearer
# to the loopback port only once the two match, so a process that took that port (the server being on another Mac)
# never receives it (Codex review 2026-11-25).
LOCAL_ID = __import__("secrets").token_hex(16)


def _local_identity(req: Request) -> Response:
    """GET /app/v1/local, loopback only and without a bearer: LOCAL_ID, nothing else. A tailnet request gets 404."""
    if not req.local:
        raise Refusal(404, "not_found", f"Nothing at {req.path}.")
    if req.method != "GET":
        raise Refusal(405, "method", f"{req.method} is not supported here: GET")
    return _json(200, {"local_id": LOCAL_ID})


def _version(req: Request) -> Response:
    """§19: what the app checks before anything else. `server` is the release this web process runs: its own service
    record when launchd runs it, else (a web started by hand, a test) the checkout's describe. On loopback only (the
    Mac app on the same Mac), `project` is the checkout it runs from: the app updates the server only from there. A
    tailnet client never learns a path on the Mac. With the loopback path on (SHAFT_WEB_LOCAL), every caller also gets
    `local_port` and `local_id`: a Mac app paired over the tailnet checks that 127.0.0.1:<local_port>/app/v1/local
    answers the same `local_id` before it sends its bearer there for the loopback-only routes (Server keys)."""
    from . import core, version
    rec = version.service("web", stale=False)
    server = (version.service_version(rec) if rec and rec.get("pid") == os.getpid()
              else version.commit_describe(version.supervisor_commit()))
    answer: dict = {"server": server, "api": API_VERSION}
    if web.LOCAL:
        answer.update(local_port=web.PORT, local_id=LOCAL_ID)
    if req.local:
        answer["project"] = str(core.PROJECT.resolve())
    return _json(200, answer)


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
            # `api restart`, never `api on`: on a local-only Mac that would need Tailscale, and on a tailnet-ready one
            # it would expose the API (tailscale serve) as a side effect of a restart
            args, timeout = ["api", "restart"], actions.SLOW_S
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


# -- setup (§18) ------------------------------------------------------------------------------------------------------
# The Mac app's setup wizard. Each state change runs the CLI verb an operator would type, as the paired device (_who):
# JSON verbs through actions.run_json, the others through actions.run_now. A secret goes on the verb's stdin (input=),
# never in argv, a log line or an answer.
SETUP_SECRETS = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "SCRAPEBADGER_API_KEY", "SHAFT_CUSTOM_API_KEY", "HF_TOKEN")
AUTONOMY = {"own": "resume", "talk": "stay"}      # §18.1: what the first reply does to the paused new instance
BROWSER_INSTALL_S = 900                           # `browser install` downloads Chromium: the request is held this long
PROVIDERS = ("openrouter", "codex", "custom")
PROVIDER_FLAGS = {"model": str, "judge_model": str, "base_url": str, "price_in": float, "price_out": float,
                  "judge_price_in": float, "judge_price_out": float, "vision": bool}
ALIAS = r"(?P<alias>[a-z][a-z0-9-]{0,30})"        # google.ALIAS_RE


def _local_only(req: Request) -> None:
    """The routes that change where every model call goes or what holds a key (the provider keys and preset, the
    custom endpoint, a Telegram bot and its service) answer only the Mac app on the Mac that runs SHAFT (loopback): a
    device paired over the tailnet could otherwise point every instance's model traffic at a server of its choosing."""
    if not req.local:
        log(f"app api refused a setup change from a tailnet device ({req.method} {req.path})")
        raise Refusal(403, "local_only", "Do this on the Mac that runs SHAFT.")


def _text(data: dict, key: str, most: int = 500) -> str:
    """A required, non-empty, single-line string field."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > most or "\n" in value or "\r" in value:
        raise Refusal(400, "bad_request", f"{key} must be one non-empty line of at most {most} characters.")
    return value


def _verb_said(rows: list[dict]) -> str:
    """What a JSON verb said when it refused: its own `error`."""
    last = rows[-1] if rows else {}
    return str(last.get("error") or last or "refused")


def _setup_instance(req: Request) -> Response:
    """`new --json --paused-until-first-reply`, then `config set`, then `drive set`: the instance as the wizard set it
    up. No opening (that is /setup/hello, once access is granted). A step's refusal: 409 with its words and `done`.
    Asked again for an instance a setup created and nobody has answered yet (it exists and still has its first-reply
    marker: `new` succeeded, then a later step was refused or the app quit before it heard), `new` is skipped and the
    remaining steps run: Try again finishes it instead of being refused "already exists"."""
    import secrets as _secrets
    from . import actions, core, opconfig
    from .web_config import DRIVE_MAX               # the web Drive page's limit, the same here
    data = _body(req)
    if set(data) != {"name", "operator", "model", "drive", "autonomy"}:
        raise Refusal(400, "bad_request", "Send name, operator, model, drive and autonomy.")
    name, operator, model = _text(data, "name", 31), _text(data, "operator"), _text(data, "model")
    drive = data["drive"]
    if not isinstance(drive, str) or not drive.strip() or len(drive) > DRIVE_MAX:
        raise Refusal(400, "bad_request", f"drive must be the drive's text, at most {DRIVE_MAX} characters.")
    if not isinstance(data["autonomy"], str) or data["autonomy"] not in AUTONOMY:
        raise Refusal(400, "bad_request", "autonomy must be own or talk.")
    who = _who(req)
    log(f"{who}: operator set up instance {name}")
    done: list[str] = []

    def refused(said: str) -> Refusal:
        return Refusal(409, "refused", said, done=list(done))

    if name in core.instance_names() and (core.SUP_DIR / f"first-reply-{name}").exists():
        if (missing := _unfinished(name)):          # `new` stopped halfway: nothing to finish, the name is taken
            raise refused(f"{name} was only partly created ({missing}), so the setup cannot finish it. Choose another "
                          f"name: {name} stays paused and never runs.")
        paused = core.paused(name)                  # made by a setup, still waiting: finish it
    else:
        code, rows = actions.run_json(["new", name, "--json", "--paused-until-first-reply", AUTONOMY[data["autonomy"]]],
                                      actions.SLOW_S, who=who)
        if code != 0 or not rows[-1].get("ok"):
            raise refused(_verb_said(rows))
        paused = bool(rows[-1].get("paused"))
    done.append("new")
    ok, said = actions.run_now(["config", name, "set", f"OPERATOR_NAME={operator}", f"LLM_MODEL={model}"], who=who)
    if not ok:
        raise refused(said)
    done.append("config")
    text = drive.replace("\r\n", "\n")
    now, sha = opconfig.drive(name)                 # what `shaft new` left (the template's drive.md): its sha, not none
    if (now or "").rstrip("\n") != text.rstrip("\n"):
        tmp = core.SUP_DIR / "web" / f"drive-{_secrets.token_hex(6)}.md"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text)
        try:
            ok, said = actions.run_now(["drive", name, "set", "--file", str(tmp), "--expect-sha", sha], who=who)
        finally:
            tmp.unlink(missing_ok=True)
        if not ok:
            raise refused(said)
    done.append("drive")
    return _json(200, {"instance": name, "done": done, "paused": paused})


def _unfinished(name: str) -> str | None:
    """None when `shaft new` finished `name`, else what is missing. Its last step is the mind store's first commit,
    with the creation's own message, made after the instance branch's creation commit and the store itself: a store
    whose history starts with that commit proves all three. A worktree with a first-reply marker alone is a `new` that
    failed halfway (its commit, a hook, the store), not an instance waiting for its remaining setup steps."""
    from . import mind
    if mind.head(name) is None:
        return "its memory store was never made" if not mind.exists(name) else "its memory store has no first commit"
    first = mind.git(name, "log", "--reverse", "--format=%s", mind.BRANCH, check=False).stdout.splitlines()
    return None if first and first[0].startswith(f"{name}: created ") else "its memory store has no creation commit"


def _setup_drives(req: Request) -> Response:
    from . import drive_presets
    try:
        return _json(200, {"drives": drive_presets.presets()})
    except drive_presets.Missing as e:
        raise Refusal(409, "refused", str(e)) from None


def _setup_secret(req: Request) -> Response:
    """`secrets set NAME --json`, the value on its stdin: only the provider and tool keys (SETUP_SECRETS) — the
    server's own auth, Google clients and Telegram tokens have their own verbs."""
    _local_only(req)
    from . import actions
    data = _body(req)
    if set(data) != {"name", "value"} or data.get("name") not in SETUP_SECRETS:
        raise Refusal(400, "bad_request", f"name must be one of {', '.join(SETUP_SECRETS)}, with its value.")
    value = data["value"]
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise Refusal(400, "bad_request", "value must be the key, one non-empty line.")
    code, rows = actions.run_json(["secrets", "set", data["name"], "--json"], who=_who(req), input=value + "\n")
    if code != 0 or not rows[-1].get("ok"):
        raise Refusal(409, "refused", _verb_said(rows))
    return _said(f"{data['name']} saved")


def _setup_secrets(req: Request) -> Response:
    """`secrets list --json`: which SETUP_SECRETS keys are set, in that order (Settings → Server keys). Names only;
    the server's own credentials, Google clients and Telegram tokens are left out."""
    _local_only(req)
    from . import actions
    code, rows = actions.run_json(["secrets", "list", "--json"], who=_who(req))
    if code != 0 or not rows[-1].get("ok"):
        raise Refusal(409, "refused", _verb_said(rows))
    return _json(200, {"names": [n for n in SETUP_SECRETS if n in rows[-1]["names"]]})


def _provider(data: dict) -> str:
    provider = data.get("provider")
    if provider not in PROVIDERS:
        raise Refusal(400, "bad_request", f"provider must be one of {', '.join(PROVIDERS)}.")
    return provider


def _setup_provider(req: Request) -> Response:
    """`provider use PROVIDER --json` with the given flags: the answer's coverage, `off` as a list."""
    _local_only(req)
    from . import actions
    data = _body(req)
    provider = _provider(data)
    args = ["provider", "use", provider, "--json"]
    for key, value in data.items():
        if key == "provider":
            continue
        kind = PROVIDER_FLAGS.get(key)
        flag = "--" + key.replace("_", "-")
        if kind is bool and type(value) is bool:
            args += [flag] if value else []
        elif kind is float and type(value) in (int, float) and math.isfinite(value) and value >= 0:
            args += [flag, str(value)]
        elif kind is str and isinstance(value, str) and value.strip() and "\n" not in value:
            args += [flag, value]
        else:
            raise Refusal(400, "bad_request", f"{key} is not a provider field of its type "
                                              f"({', '.join(PROVIDER_FLAGS)}).")
    code, rows = actions.run_json(args, actions.SLOW_S, who=_who(req))
    if code != 0 or not rows[-1].get("ok"):
        raise Refusal(409, "refused", _verb_said(rows))
    r = rows[-1]
    # The running services that still hold another value of the preset: the wizard restarts them before Test.
    late = sorted({svc for svcs in r["not_live"].values() for svc in svcs})
    return _json(200, {"provider": r["provider"], "model": r["model"], "changed": r["changed"], "covers": r["covers"],
                       "off": [{"function": f, "why": why} for f, why in r["off"].items()], "not_live": late})


def _setup_check(req: Request) -> Response:
    """`provider check PROVIDER --json`: one live call per configured model; a failed model is a row, not an error."""
    _local_only(req)
    from . import actions
    data = _body(req)
    if set(data) != {"provider"}:
        raise Refusal(400, "bad_request", "Send the provider alone.")
    provider = _provider(data)
    code, rows = actions.run_json(["provider", "check", provider, "--json"], actions.SLOW_S * 2, who=_who(req))
    if not all("model" in r for r in rows):          # the verb could not check at all
        raise Refusal(409, "refused", _verb_said(rows))
    return _json(200, {"provider": provider, "ok": code == 0 and all(r.get("ok") for r in rows),
                       "models": [{"ok": bool(r.get("ok")), "model": r["model"], "error": r.get("error")} for r in rows]})


def _setup_telegram(req: Request) -> Response:
    """`telegram token NAME`, the token on its stdin."""
    _local_only(req)
    from . import actions
    data = _body(req)
    if set(data) != {"name", "token"}:
        raise Refusal(400, "bad_request", "Send name and token.")
    name, token = _text(data, "name", 31), _text(data, "token")
    if name not in web._names():
        raise Refusal(404, "not_found", f"No instance named {name}.")
    ok, said = actions.run_now(["telegram", "token", name], who=_who(req), input=token + "\n")
    if not ok:
        raise Refusal(409, "refused", said)
    return _said(said)


def _telegram_named(n: str) -> dict:
    from . import setup
    if n not in web._names():
        raise Refusal(404, "not_found", f"No instance named {n}.")
    return setup.telegram_status(n)


def _setup_telegram_status(req: Request, n: str) -> Response:
    """Where n's Telegram stands: its bot, the paired account, the service (setup.telegram_status). The wizard polls it
    while the operator sends the pairing code."""
    return _json(200, _telegram_named(n))


def _setup_telegram_pair(req: Request, n: str) -> Response:
    """`telegram pair --json`: a one-time code the operator sends to n's bot as `/pair <code>`. Already paired: no code
    (one account talks to every bot). No bot yet: refused."""
    from . import actions
    if _body(req):
        raise Refusal(400, "bad_request", "Send no fields.")
    status = _telegram_named(n)
    if not status["token"]:
        raise Refusal(409, "refused", f"{n} has no Telegram bot yet: save its token first.")
    if status["paired"]:
        return _json(200, {**status, "code": None, "minutes": None})
    code, rows = actions.run_json(["telegram", "pair", "--json"], who=_who(req))
    if code != 0 or not rows[-1].get("ok"):
        if (now := _telegram_named(n))["paired"]:   # paired meanwhile: as the check above, no code to send
            return _json(200, {**now, "code": None, "minutes": None})
        raise Refusal(409, "refused", _verb_said(rows))
    return _json(200, {**_telegram_named(n), "code": rows[-1]["code"], "minutes": rows[-1]["minutes"]})


def _setup_telegram_service(req: Request) -> Response:
    """`telegram on`: installs (or restarts, to pick up a new bot) the Telegram service, through the verb."""
    _local_only(req)
    from . import actions
    if _body(req):
        raise Refusal(400, "bad_request", "Send no fields.")
    ok, said = actions.run_now(["telegram", "on"], actions.SLOW_S, who=_who(req))
    if not ok:
        raise Refusal(409, "refused", said)
    return _said(said)


def _setup_autonomy(req: Request, n: str) -> Response:
    """`first-reply <n> resume|stay --json`: the wizard's "How it works alone" choice, after the instance was created
    (with "own", i.e. resume). Refused once the first reply has happened (no marker left)."""
    from . import actions
    data = _body(req)
    if set(data) != {"then"} or data["then"] not in AUTONOMY.values():
        raise Refusal(400, "bad_request", "Send then: resume or stay.")
    code, rows = actions.run_json(["first-reply", n, data["then"], "--json"], who=_who(req))
    if code != 0 or not rows[-1].get("ok"):
        raise Refusal(409, "refused", _verb_said(rows))
    return _json(200, {"instance": n, "then": data["then"]})


def _setup_hello(req: Request, n: str) -> Response:
    """The setup's last step: the hidden opening (setup.opening_text) to Main, which the instance answers with its
    welcome. Asked again once it was sent, it answers that chat (the app's Try again completes) until the operator has
    written in it, then 409 already_answered (the wizard treats it as done); refused when Main has a conversation that
    did not start with the opening."""
    from . import setup, web_chat
    log(f"{_who(req)}: operator asked {n} for its welcome")
    try:
        chat = web_chat.opening(n, _login(req), setup.opening_text(n))
    except web_chat.AlreadyAnswered as e:            # the operator answered the welcome: nothing left to ask for
        raise Refusal(409, "already_answered", f"The welcome is already in your chat with {n}.",
                      chat={"id": e.chat["id"], "title": web_chat.title(e.chat)}) from None
    except web_chat.AlreadyOpened as e:              # Try again after the answer was lost: done already, not refused
        return _said(str(e), chat={"id": e.chat["id"], "title": web_chat.title(e.chat)})
    except web_chat.Refused as e:
        raise Refusal(409, "refused", str(e)) from None
    return _said(f"{n} is writing its welcome", chat={"id": chat["id"], "title": web_chat.title(chat)})


# -- access (§18.6) ---------------------------------------------------------------------------------------------------
GRANT_FIELDS = {"folder": {"what"}, "browser": set(), "mail": {"alias", "what", "writable"},
                "calendar": {"alias", "what", "writable"}, "drive": {"alias", "all"}, "chat": {"alias"},
                "meetings": {"alias"}}
GRANT_VERBS = {"mail": "mail", "calendar": "calendar", "drive": "gdrive", "chat": "gchat", "meetings": "meetings"}


def _get_access(req: Request, n: str) -> Response:
    from . import setup
    return _json(200, setup.access(n))


def _access_args(n: str, data: dict) -> list[list[str]]:
    """The verbs one access change runs, in order (the browser's and the calendar's write upgrade: _set_access)."""
    from . import google
    grant, action = data.get("grant"), data.get("action")
    if not isinstance(grant, str) or grant not in GRANT_FIELDS or action not in ("grant", "revoke"):
        raise Refusal(400, "bad_request", f"grant must be one of {', '.join(GRANT_FIELDS)}; action grant or revoke.")
    fields = GRANT_FIELDS[grant]
    if set(data) - fields - {"grant", "action"} or any(type(data[k]) is not bool for k in ("writable", "all")
                                                        if k in data):
        raise Refusal(400, "bad_request", f"{grant} takes {', '.join(sorted(fields)) or 'no other field'} "
                                          "(writable and all are true or false).")
    if grant == "folder":
        return [["share", n, "off" if action == "revoke" else _text(data, "what", 4096)]]
    if grant == "browser":
        return []
    alias = _text(data, "alias", 31)
    if not google.ALIAS_RE.fullmatch(alias):
        raise Refusal(400, "bad_request", "alias is a Google account alias (lowercase letters, digits, -).")
    verb = GRANT_VERBS[grant]
    if grant == "meetings":
        return [[verb, action, n, alias]]
    if grant in ("drive", "chat"):
        return [[verb, n, action, alias] + (["--all"] if action == "grant" and data.get("all") else [])]
    op = action
    if data.get("writable") and not (grant == "calendar" and action == "grant"):   # granted first, then upgraded
        op += "-draft" if grant == "mail" else "-write"
    return [[verb, n, op, alias, _text(data, "what")]]


def _set_access(req: Request, n: str) -> Response:
    """One grant or revocation through its CLI verb (§18.6), then the fresh access."""
    from . import actions, browser, calendar, core, google, setup
    data = _body(req)
    runs = _access_args(n, data)
    who = _who(req)
    log(f"{who}: operator {data['action']} {data['grant']} for {n}")
    if data["grant"] == "calendar" and data["action"] == "grant" and data.get("writable"):
        # the calendar this request names, resolved to its ID once: the read grant and the write upgrade both name that
        # ID — never one inferred from the stored grants, which another request's grant may reorder meanwhile
        try:
            cid = calendar.grant_id(data["alias"], data["what"])
        except (google.AccountError, google.ApiError) as e:
            raise Refusal(409, "refused", f"not granted: {e}") from None
        runs = [["calendar", n, "grant", data["alias"], cid], ["calendar", n, "grant-write", data["alias"], cid]]
    if data["grant"] == "browser":
        if data["action"] == "revoke":
            raise Refusal(409, "refused", "The browser is shared by every instance and reached from inside run: there "
                                          f"is none to take back from {n} alone (only turning off its run tool would, "
                                          "which also stops it running its own code).")
        if not browser.version():
            ok, said = actions.run_now(["browser", "install"], BROWSER_INSTALL_S, who=who)
            if not ok:
                raise Refusal(409, "refused", said)
        if "run" in core.disabled_tools(n):
            runs = [["tools", n, "on", "run"]]
    for args in runs:
        ok, said = actions.run_now(args, who=who)
        if not ok:
            raise Refusal(409, "refused", said)
    return _json(200, setup.access(n))


def _google_account(alias: str, scope: str):
    from . import core, google
    try:
        return google.Account(alias, core.secrets_env()).require(scope)
    except google.AccountError as e:
        raise Refusal(409, "refused", str(e)) from None


def _google_labels(req: Request, alias: str) -> Response:
    """The account's Gmail labels, by name: what a mail grant may name."""
    from . import google, mail
    acct = _google_account(alias, google.GMAIL)
    try:
        names = sorted(mail.labels(acct), key=str.casefold)
    except (google.AccountError, google.ApiError) as e:
        raise Refusal(409, "refused", str(e)) from None
    return _json(200, {"alias": alias, "labels": names})


def _google_calendars(req: Request, alias: str) -> Response:
    """The account's calendars, primary first: what a calendar grant may name (by id)."""
    from . import calendar, google
    acct = _google_account(alias, google.CALENDAR)
    try:
        cals = calendar.calendars(acct)
    except (google.AccountError, google.ApiError) as e:
        raise Refusal(409, "refused", str(e)) from None
    return _json(200, {"alias": alias, "calendars": [{k: c[k] for k in ("id", "title", "primary", "access")}
                                                     for c in cals]})


# -- meetings (§20) ---------------------------------------------------------------------------------------------------
MEETING_ID = rf"(?P<meeting_id>{meetings.ID})"
MEETING_ERRORS = {
    meetings.Invalid: (400, "bad_request"),
    meetings.Missing: (404, "not_found"),
    meetings.Conflict: (409, "refused"),
    meetings.TooLarge: (413, "too_large"),
}


def _meeting_call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except meetings.Error as e:
        status, code = MEETING_ERRORS[type(e)]
        raise Refusal(status, code, str(e)) from None


def _meeting_metadata(row: dict) -> dict:
    public = {key: value for key, value in row.items() if key not in ("notified", "ready_revision")}
    return {**public, **{key: stamp(row[key]) for key in ("created", "started", "ended")}}


def _create_meeting(req: Request) -> Response:
    row = _meeting_call(meetings.create, _body(req))
    return _json(201, {"id": row["id"], **meetings.upload_status(row)})


def _meeting_chunk(req: Request, meeting_id: str, index: str) -> Response:
    content_type = req.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/octet-stream":
        raise Refusal(400, "bad_request", "Send each chunk as application/octet-stream.")
    digits = index.lstrip("0") or "0"
    if len(digits) > 255:
        raise Refusal(400, "bad_request", "Chunk index is too large.")
    source, length = req.body_stream or (io.BytesIO(req.body), len(req.body))
    received = _meeting_call(meetings.put_chunk, meeting_id, int(digits), source, length)
    return _json(200, {"received": received})


def _meeting_upload(req: Request, meeting_id: str) -> Response:
    return _json(200, meetings.upload_status(_meeting_call(meetings.get, meeting_id)))


def _finish_meeting(req: Request, meeting_id: str) -> Response:
    data = _body(req)
    fields = {key: data.get(key) for key in ("size", "sha256", "chunks")}
    return _json(200, _meeting_metadata(_meeting_call(meetings.finish, meeting_id, **fields)))


def _list_meetings(req: Request) -> Response:
    return _json(200, {"meetings": [_meeting_metadata(row) for row in _meeting_call(meetings.list_meetings)]})


def _show_meeting(req: Request, meeting_id: str) -> Response:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.target).query, keep_blank_values=True)
    try:
        page = int(query.get("page", ["1"])[0])
        if page < 1:
            raise ValueError
    except ValueError:
        raise Refusal(400, "bad_request", "page must be a positive integer.") from None
    return _json(200, _meeting_metadata(_meeting_call(meetings.detail, meeting_id, page)))


def _update_meeting(req: Request, meeting_id: str) -> Response:
    return _json(200, _meeting_metadata(_meeting_call(meetings.update, meeting_id, _body(req))))


def _meeting_proposals(req: Request) -> Response:
    from . import meeting_proposals
    return _json(200, {"proposals": meeting_proposals.pending()})


def _decide_meeting_proposal(req: Request) -> Response:
    from . import meeting_proposals
    data = _body(req)
    return _json(200, _meeting_call(meeting_proposals.decide, data.get("id"), data.get("approve")))


def _reprocess_meeting(req: Request, meeting_id: str) -> Response:
    return _json(200, _meeting_metadata(_meeting_call(meetings.reprocess, meeting_id)))


def _delete_meeting(req: Request, meeting_id: str) -> Response:
    delete = speakers.delete_meeting if _meeting_flag(req, "forget_voices") else meetings.delete
    _meeting_call(delete, meeting_id)
    return _json(200, {"id": meeting_id, "deleted": True})


def _meeting_flag(req: Request, key: str) -> bool:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.target).query, keep_blank_values=True)
    value = query.get(key, ["0"])
    if value not in (["0"], ["1"]):
        raise Refusal(400, "bad_request", f"{key} must be 0 or 1.")
    return value == ["1"]


def _meeting_clip(req: Request, meeting_id: str) -> Response:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.target).query, keep_blank_values=True)
    try:
        index = int(query.get("n", ["0"])[0])
        if index < 0:
            raise ValueError
    except ValueError:
        raise Refusal(400, "bad_request", "n must be a non-negative integer.") from None
    try:
        audio = _meeting_call(speakers.clip, meeting_id, query.get("speaker", [""])[0], index)
    except OSError:
        raise Refusal(500, "internal", "Could not read speaker clip.") from None
    return Response(200, audio, {"Content-Type": "audio/wav", "Cache-Control": "no-store"})


def _meeting_speaker(req: Request, meeting_id: str) -> Response:
    body = _body(req)
    if ("name" in body) == ("merge_into" in body):
        raise Refusal(400, "bad_request", "Supply either name or merge_into.")
    operation, value = ((speakers.name_label, body["name"]) if "name" in body
                        else (speakers.merge_labels, body["merge_into"]))
    return _json(200, _meeting_call(operation, meeting_id, body.get("label"), value))


def _list_speakers(req: Request) -> Response:
    people = _meeting_call(speakers.list_people)
    return _json(200, {"people": [{**p, "created": stamp(p["created"])} for p in people]})


def _rename_speaker(req: Request, pid: str) -> Response:
    person = _meeting_call(speakers.rename, pid, _body(req).get("name"))
    return _json(200, {**person, "created": stamp(person["created"])})


def _forget_speaker(req: Request, pid: str) -> Response:
    _meeting_call(speakers.forget, pid, anonymise=_meeting_flag(req, "anonymise"))
    return _json(200, {"id": pid, "deleted": True})


ROUTES: list[tuple[str, dict]] = [
    (r"/meetings", {"GET": _list_meetings, "POST": _create_meeting}),
    (r"/meetings/proposals", {"GET": _meeting_proposals, "POST": _decide_meeting_proposal}),
    (rf"/meetings/{MEETING_ID}", {"GET": _show_meeting, "POST": _update_meeting, "DELETE": _delete_meeting}),
    (meetings.CHUNK_ROUTE, {"PUT": _meeting_chunk}),
    (rf"/meetings/{MEETING_ID}/upload", {"GET": _meeting_upload}),
    (rf"/meetings/{MEETING_ID}/finish", {"POST": _finish_meeting}),
    (rf"/meetings/{MEETING_ID}/reprocess", {"POST": _reprocess_meeting}),
    (rf"/meetings/{MEETING_ID}/clip", {"GET": _meeting_clip}),
    (rf"/meetings/{MEETING_ID}/speakers", {"POST": _meeting_speaker}),
    (r"/speakers", {"GET": _list_speakers}),
    (rf"/speakers/(?P<pid>{speakers.ID})/rename", {"POST": _rename_speaker}),
    (rf"/speakers/(?P<pid>{speakers.ID})", {"DELETE": _forget_speaker}),
    (r"/unpair", {"POST": _unpair}),
    (r"/instances", {"GET": _instances}),
    (r"/instances/move", {"POST": _move}),
    (rf"/plan/{N}", {"GET": _plan}),
    (rf"/cycles/{N}", {"GET": _cycles}),
    (rf"/cycles/{N}/(?P<ts>\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}})", {"GET": _cycle}),
    (rf"/settings/{N}", {"GET": _get_settings, "POST": _set_settings}),
    (rf"/settings/{N}/(?P<act>pause|resume)", {"POST": _pause_resume}),
    (r"/voices/report", {"POST": _report_voices}),            # before /voices/<n>: "report" is not an instance
    (rf"/voices/{N}", {"GET": _voices, "POST": _set_voice}),
    (rf"/voices/{N}/proposals/(?P<pid>[A-Za-z0-9_-]{{1,64}})", {"POST": _decide_voice}),
    (r"/version", {"GET": _version}),
    (r"/services", {"GET": _get_services}),
    (r"/services/(?P<service>api|scheduler)/(?P<act>restart)", {"POST": _service_action}),
    (r"/services/(?P<service>scheduler)/(?P<act>cancel)", {"POST": _service_action}),
    (rf"/chat/{N}", {"GET": _feed}),
    (rf"/chat/{N}/answer", {"POST": _answer}),
    (rf"/chat/{N}/send", {"POST": _send}),
    (rf"/chat/{N}/stream", {"GET": _stream}),
    (rf"/chat/{N}/(?P<act>new|rename|unqueue|uncycle|stop|end|clear|seen|away)", {"POST": _action}),
    (rf"/chat/{N}/{CID}", {"GET": _feed}),
    (rf"/chats/{N}", {"GET": _chats}),
    (rf"/threads/{N}", {"GET": _threads}),
    (rf"/notes/{N}", {"GET": _notes}),
    (rf"/threads/{N}/move", {"POST": _move}),
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
    (r"/setup/instance", {"POST": _setup_instance}),
    (r"/setup/drives", {"GET": _setup_drives}),
    (r"/setup/secret", {"POST": _setup_secret}),
    (r"/setup/secrets", {"GET": _setup_secrets}),
    (r"/setup/provider", {"POST": _setup_provider}),
    (r"/setup/provider/check", {"POST": _setup_check}),
    (r"/setup/telegram", {"POST": _setup_telegram}),
    (r"/setup/telegram/service", {"POST": _setup_telegram_service}),
    (rf"/setup/telegram/pair/{N}", {"POST": _setup_telegram_pair}),
    (rf"/setup/telegram/status/{N}", {"GET": _setup_telegram_status}),
    (rf"/setup/hello/{N}", {"POST": _setup_hello}),
    (rf"/setup/autonomy/{N}", {"POST": _setup_autonomy}),
    (rf"/access/{N}", {"GET": _get_access, "POST": _set_access}),
    (rf"/google/{ALIAS}/labels", {"GET": _google_labels}),
    (rf"/google/{ALIAS}/calendars", {"GET": _google_calendars}),
]
