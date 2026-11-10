"""`shaft web`: the web UI for managing the instances — the control panel of `shaft top` in a
browser, for the tailnet only. Stdlib only, run by the system Python like every launchd service (core.PYTHON).

  shaft web on       install the service (com.shaft.web) and expose it with `tailscale serve` (never Funnel)
  shaft web off      stop it and remove it from the tailnet
  shaft web status   the service, the URL, the paired browsers
  shaft web pair [--name NAME]     a one-time link that pairs the browser you open it in
  shaft web unpair DEVICE | --all  revoke a paired browser

Who may use it: a request needs BOTH
  - a Tailscale-User-Login in SHAFT_API_LOGINS (set by `tailscale serve`; the server listens on 127.0.0.1 only), and
  - a paired browser's cookie. `shaft web pair` prints a link valid for PAIR_MINUTES, usable once; opening it shows a
    button, and pressing it (a POST: a link preview cannot spend it) sets an HttpOnly, Secure, SameSite=Lax
    cookie: Lax, not Strict, so it rides on a top-level GET from another site — Google's redirect back to
    /google/callback (web_google.py) — while another site's POST still carries none. Only a hash of the cookie's secret is kept (.supervisor/web/devices.json).
Any process on the Mac could forge the login header to 127.0.0.1; none can forge the cookie. Instances cannot reach
the port at all: a run's sandbox allows only the LLM, embed, broker and egress ports, and serve() refuses to listen
on a port a run may reach.

Every request must also name this host (Host is the tailnet name: no DNS rebinding), and every POST must come from
this page — Sec-Fetch-Site same-origin, or an Origin naming this host — and carry the CSRF token derived from the
browser's own cookie. Every response carries a Content-Security-Policy without inline script.

The web UI adds no authority: what it shows comes from the supervisor's own readers (dashboard, panel), as in
`shaft top`, and (from M2) every action runs the same CLI subcommand.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import apns, web_icons, web_pages
from .web_views import TEXT_TYPES                  # files shown as text: Files, messages, the documents panel
from .web_pages import Html
from .core import EMBED_PORT, LLM_PORTS, SUP_DIR, UnsafePath, knob, port, log, read_beneath

PORT, _ = knob(   # not 8782: that is the embed port, which runs reach
    "SHAFT_WEB_PORT", "8785", port, "the web UI's local port (tailscale serve forwards to it)", group="Ports and paths",
    services=("web",), web=False)
HTTPS_PORT, _ = knob(
    "SHAFT_WEB_HTTPS_PORT", "8444", port, "the web UI's https port on the tailnet (tailscale serve)",
    group="Ports and paths", services=("web",), web=False)
WEB_HOSTS, _ = knob(
    "SHAFT_WEB_HOSTS", "", lambda s: {h.strip().lower() for h in s.split(",") if h.strip()},
    "the host names the web UI answers to, comma-separated (empty: the tailnet name `shaft web on` recorded)",
    group="Ports and paths", services=("web",), web=False)
PAIR_MINUTES = 10
COOKIE = "shaft_device"
COOKIE_DAYS = 400                                           # the longest a browser keeps a cookie
STATIC = Path(__file__).resolve().parent / "web_static"
# blob: images are the composer's previews of files picked in this page (URL.createObjectURL), nothing fetched
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def allowed_logins() -> set[str]:
    from .api import allowed_logins as logins             # one list for the API and the web UI
    return logins()


_hosts: dict = {"at": 0.0, "hosts": set()}


def tailnet_hosts() -> set[str]:
    """This Mac's tailnet name with the https port, asked of tailscale now. Works from a terminal; under launchd the
    Mac app's CLI refuses to run outside the GUI session ("Tailscale.CLIError error 3"), so the service never asks
    it: `shaft web on` records the answer (record_hosts) and the service reads that (live, 2026-10-19)."""
    from .api import _tailscale
    try:
        st = json.loads(_tailscale("status", "--json").stdout or "{}")
    except (OSError, ValueError):
        st = {}
    name = (st.get("Self", {}).get("DNSName") or "").rstrip(".").lower()
    return ({f"{name}:{HTTPS_PORT}"} | ({name} if HTTPS_PORT == 443 else set())) if name else set()


def record_hosts() -> set[str]:
    """Ask tailscale for this Mac's tailnet name and keep it for the service (.supervisor/web/hosts.json)."""
    hosts = tailnet_hosts()
    if hosts:
        _write("hosts.json", {"hosts": sorted(hosts), "recorded": datetime.now().isoformat(timespec="seconds")})
    return hosts


def allowed_hosts() -> set[str]:
    """The names this UI answers to: SHAFT_WEB_HOSTS if set, else the tailnet name `shaft web on` recorded, else (no
    record yet: a terminal) tailscale's answer now."""
    if WEB_HOSTS:
        return WEB_HOSTS
    if time.time() - _hosts["at"] > 60:
        recorded = _read("hosts.json").get("hosts")
        hosts = {str(h).lower() for h in recorded} if isinstance(recorded, list) and recorded else tailnet_hosts()
        _hosts.update(at=time.time(), hosts=hosts)
    return _hosts["hosts"]


def url() -> str:
    hosts = sorted(allowed_hosts())
    return f"https://{hosts[0]}" if hosts else "(tailscale is not running)"


# -- paired browsers -------------------------------------------------------------------------------------------------
_lock = threading.Lock()


def _store(name: str) -> Path:
    return SUP_DIR / "web" / name


def _read(name: str) -> dict:
    try:
        data = json.loads(_store(name).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(name: str, data: dict) -> None:
    path = _store(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.chmod(0o600)
    os.replace(tmp, path)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_pairing(device: str) -> str:
    """A one-time link that pairs the browser it is opened in as `device`."""
    token = secrets.token_urlsafe(32)
    with _lock:
        pending = {k: v for k, v in _read("pairing.json").items() if v.get("expires", 0) > time.time()}
        pending[_digest(token)] = {"device": device, "expires": time.time() + PAIR_MINUTES * 60}
        _write("pairing.json", pending)
    log(f"web: pairing link made for {device!r} (valid {PAIR_MINUTES} min, once)")
    return f"{url()}/pair?t={token}"


def _pairing(token: str) -> dict | None:
    p = _read("pairing.json").get(_digest(token))
    return p if p and p.get("expires", 0) > time.time() else None


def _use_pairing(token: str, login: str, platform: str = "") -> tuple[str, str] | None:
    """Spend the link: (device id, secret) for the cookie — or the native app's bearer (app_api, `platform` "ios" or
    "macos") — or None if it is unknown, used or expired."""
    with _lock:
        pending = _read("pairing.json")
        p = pending.pop(_digest(token), None)
        _write("pairing.json", pending)
        if not p or p.get("expires", 0) <= time.time():
            return None
        dev, secret = secrets.token_hex(8), secrets.token_urlsafe(32)
        devices = _read("devices.json")
        now = datetime.now().isoformat(timespec="seconds")
        devices[dev] = {"name": p["device"], "login": login, "created": now, "last_seen": now,
                        "hash": _digest(secret), **({"platform": platform} if platform else {})}
        _write("devices.json", devices)
    log(f"web: {f'{platform} app' if platform else 'browser'} paired as {p['device']!r} ({dev}, tailnet user {login})")
    return dev, secret


def devices() -> dict:
    return _read("devices.json")


def unpair(device: str | None = None) -> int:
    """Revoke one paired browser or app (by id or name), or all with None. Returns how many were revoked. Its push
    subscription goes with it — a revoked phone kept getting a lock-screen push and a badge for every reply (W1) — and
    so do an app's APNs token and its seen marks (web_seen)."""
    from . import web_seen, webpush
    with _lock:
        ds = _read("devices.json")
        gone = [k for k, v in ds.items() if device is None or device in (k, v.get("name"))]
        for k in gone:
            del ds[k]
        _write("devices.json", ds)
    for k in gone:
        webpush.unsubscribe(k)
        apns.unregister(k)
        web_seen.forget(k)
    if gone:
        log(f"web: unpaired {', '.join(gone)}")
    return len(gone)


def device_from_cookie(cookie_header: str) -> tuple[str, str] | None:
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE and "." in v:
            dev, _, secret = v.partition(".")
            return dev, secret
    return None


def _paired(cookie_header: str) -> tuple[str, str] | None:
    """(device id, secret) when the cookie is a paired browser's; its last_seen is refreshed every few minutes."""
    got = device_from_cookie(cookie_header)
    return got if got and paired_device(*got) else None


def paired_device(dev: str, secret: str) -> bool:
    """`dev`.`secret` is a paired device's — a browser's cookie or an app's bearer (app_api.device): the secret's hash,
    compared in constant time. Its last_seen is refreshed every few minutes, so `shaft web status` shows it in use."""
    rec = devices().get(dev)
    if not rec or not hmac.compare_digest(rec.get("hash", ""), _digest(secret)):
        return False
    if time.time() - _stamp(rec.get("last_seen", "")) > 300:
        with _lock:
            ds = _read("devices.json")
            if dev in ds:
                ds[dev]["last_seen"] = datetime.now().isoformat(timespec="seconds")
                _write("devices.json", ds)
    return True


def _stamp(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def csrf_token(secret: str) -> str:
    return hmac.new(secret.encode(), b"shaft-web-csrf", hashlib.sha256).hexdigest()[:32]


# -- requests ---------------------------------------------------------------------------------------------------------
@dataclass
class Request:
    method: str
    target: str                     # path and query, as sent
    headers: dict                   # lower-case names
    body: bytes = b""
    path: str = ""
    query: dict = field(default_factory=dict)
    set_cookies: list = field(default_factory=list)     # cookies to send with whatever the route answers
    device: str = ""                # the paired browser's id, once _route has checked its cookie

    def __post_init__(self):
        split = urllib.parse.urlsplit(self.target)
        self.path = split.path
        self.query = {k: v[0] for k, v in urllib.parse.parse_qs(split.query).items()}
        self.headers = {k.lower(): v for k, v in self.headers.items()}

    def form(self) -> dict:
        """The posted fields, blank ones included: a blank "saw" means "the page showed it unset", and dropping it
        dropped the stale-edit check (M3). A field named "…[]" (ticked boxes) keeps every value, as a list."""
        return {k: v if k.endswith("[]") else v[0]
                for k, v in urllib.parse.parse_qs(self.body.decode(errors="replace"), keep_blank_values=True).items()}


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)
    # A response written as it is made (the app's SSE stream, app_api._stream): Handler writes each piece at once, with
    # no Content-Length, then closes the connection; a client that left is noticed at the next piece (Handler._pour).
    stream: Iterator[bytes] | None = None


def _html(status: int, text: str, headers: dict | None = None) -> Response:
    return Response(status, text.encode(), {"Content-Type": "text/html; charset=utf-8", **(headers or {})})


def _refused(text: str, detail: str = "") -> Response:
    return _html(403, web_pages.message_page("Not allowed", text, detail))


def _same_origin(req: Request) -> bool:
    site = req.headers.get("sec-fetch-site")
    if site is not None:
        return site == "same-origin"
    origin = req.headers.get("origin")
    return bool(origin) and urllib.parse.urlsplit(origin).netloc.lower() == req.headers.get("host", "").lower()


_snap: dict = {"at": 0.0, "snap": None}
_snap_lock = threading.Lock()


def overview() -> dict:
    """The TUI's overview data (dashboard.snapshot + panel.extras), shared by every open page for a second."""
    from . import dashboard, panel
    with _snap_lock:
        if _snap["snap"] is None or time.monotonic() - _snap["at"] >= 1.0:
            _snap.update(snap=panel.extras(dashboard.snapshot()), at=time.monotonic())
        return _snap["snap"]


def _static(name: str, versioned: bool = False) -> Response:
    path = (STATIC / name).resolve()
    if not name or path.parent != STATIC or not path.is_file():
        return _html(404, web_pages.message_page("Not found", "No such file."))
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # ?v= is the files' own hash (web_pages.STATIC_VERSION): that URL never changes content, so it is kept for good
    cache = "public, max-age=31536000, immutable" if versioned else "max-age=3600"
    return Response(200, path.read_bytes(), {"Content-Type": ctype, "Cache-Control": cache})


# Bounded by LAUNCH_SIZES and the two schemes. Serialize first renders so simultaneous installs draw each once.
_launch_images: dict = {}
_launch_lock = threading.Lock()


def _launch(name: str, versioned: bool) -> Response:
    match = re.fullmatch(r"([1-9][0-9]{2,3})x([1-9][0-9]{2,3})-(light|dark)\.png", name)
    if match:
        w, h, scheme = int(match[1]), int(match[2]), match[3]
        if (w, h) in web_icons.LAUNCH_SIZES:
            with _launch_lock:
                key = (w, h, scheme)
                if key not in _launch_images:
                    _launch_images[key] = web_icons.launch_png(w, h, web_pages.THEME[scheme])
                body = _launch_images[key]
            cache = "public, max-age=31536000, immutable" if versioned else "max-age=3600"
            return Response(200, body, {"Content-Type": "image/png", "Cache-Control": cache})
    return _html(404, web_pages.message_page("Not found", "No such launch image."))


def _secured(r: Response) -> Response:
    r.headers.update({"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                      "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                      # a page open since before an update compares this with its own scripts' version
                      "X-Shaft-Static": web_pages.STATIC_VERSION})
    r.headers.setdefault("Cache-Control", "no-store")
    return r


def respond(req: Request) -> Response:
    r = _secured(_route(req))
    if req.method == "GET" and r.status == 200 and req.path.startswith("/fragment/"):
        # A refresh that changed nothing costs a header, not the whole feed again (~100 a minute)
        tag = web_pages.etag(r.body)
        r.headers["ETag"] = tag
        if req.headers.get("if-none-match") == tag:
            r.status, r.body = 304, b""
    if req.set_cookies:                             # a list: one Set-Cookie header each
        own = r.headers.get("Set-Cookie")
        r.headers["Set-Cookie"] = ([own] if isinstance(own, str) else own or []) + req.set_cookies
    return r


def _route(req: Request) -> Response:
    login = (req.headers.get("tailscale-user-login") or "").lower()
    app = req.path == "/app/v1" or req.path.startswith("/app/v1/")     # the native apps' JSON API (app_api.py)
    if not login or login not in allowed_logins():
        log(f"web refused tailnet user {login or '(none)'} ({req.method} {req.path})")
        if app:
            from . import app_api
            return app_api.error(403, "login", "This API is for the operator's tailnet login only.")
        return _refused("This page is for the operator's tailnet login only.")
    if req.headers.get("host", "").lower() not in allowed_hosts():
        log(f"web refused host {req.headers.get('host')!r} ({req.method} {req.path})")
        if app:
            from . import app_api
            return app_api.error(403, "host", f"Use this Mac's tailnet address: {url()}")
        return _refused("Open this page at its tailnet address.", url())
    if app:                                         # its own credential, the bearer: never the cookie (app_api.device)
        from . import app_api
        return app_api.route(req)
    if req.path.startswith("/static/"):
        return _static(urllib.parse.unquote(req.path.removeprefix("/static/")),
                       versioned=req.query.get("v") == web_pages.STATIC_VERSION)
    if req.path.startswith("/launch/"):               # WebKit 259328: startup fetches carry no paired cookie
        return _launch(req.path.removeprefix("/launch/"), req.query.get("v") == web_pages.STATIC_VERSION)
    if req.path == "/favicon.ico":                  # asked by browsers on their own: the icon, not a refusal page
        return _static("icon-180.png")
    # The app's shell: fetched by the browser before it is paired, with nothing private in it.
    if req.path == "/manifest.webmanifest":
        return Response(200, web_pages.manifest().encode(), {"Content-Type": "application/manifest+json",
                                                             "Cache-Control": "no-cache"})
    if req.path == "/sw.js":                        # at the root: its scope is the whole app
        return Response(200, web_pages.service_worker().encode(), {"Content-Type": "text/javascript; charset=utf-8",
                                                                   "Cache-Control": "no-cache"})
    if req.path == "/offline":
        return _html(200, web_pages.offline_page())
    if req.path == "/pair":
        return _pair(req, login)
    paired = _paired(req.headers.get("cookie", ""))
    if not paired:
        # A Home Screen app has cookies of its own and no address bar: it pairs by pasting the link here.
        return _html(403, web_pages.unpaired_page(), {"X-Shaft-Pair": "required"})
    dev, secret = paired
    req.device = dev
    if _renew_due(dev):                             # the cookie's 400 days start again: a used app never lapses
        req.set_cookies.append(_device_cookie(dev, secret))
    if req.method == "POST":
        if not _same_origin(req):
            log(f"web refused a cross-site POST {req.path} (device {dev})")
            return _refused("This request did not come from this page.")
        if not hmac.compare_digest(req.headers.get("x-csrf-token", ""), csrf_token(secret)):
            log(f"web refused a POST without its token {req.path} (device {dev})")
            return _refused("This request did not come from this page.")
        return _post(req, dev)
    if req.method != "GET":
        return _html(405, web_pages.message_page("Not allowed", f"{req.method} is not supported."))
    if req.path in ("/google/login", "/google/callback"):
        from . import web_google
        return web_google.route(req)
    if req.path == "/push/latest":                  # what this device's push shows: what waits for it (webpush.take)
        from . import webpush
        if req.headers.get("sec-fetch-site") != "same-origin":   # it takes them: never a link from another site
            return _refused("This request did not come from this page.")
        return Response(200, json.dumps({"events": webpush.take(dev)}).encode(),
                        {"Content-Type": "application/json", "Cache-Control": "no-store"})
    if req.path == "/push/status":                  # #23: "on" is SHAFT holding this device, not only the browser —
        from . import webpush                       # with the endpoint and key it holds, which the page compares (W5)
        return Response(200, json.dumps(webpush.status(dev)).encode(),
                        {"Content-Type": "application/json", "Cache-Control": "no-store"})
    if req.path == "/push/token":
        # The service worker's pushsubscriptionchange hands SHAFT the browser's renewed subscription (W5): a POST like
        # any other, so it needs the token, and it has no page to read it from. Every page carries it already; this
        # gives it to a same-origin request only, as /push/latest does.
        if req.headers.get("sec-fetch-site") != "same-origin":
            return _refused("This request did not come from this page.")
        return Response(200, json.dumps({"csrf": csrf_token(secret)}).encode(),
                        {"Content-Type": "application/json", "Cache-Control": "no-store"})
    try:
        r = _get(req, csrf_token(secret))
    except (Exception, SystemExit) as e:    # say what broke: an empty page reads as "nothing to show" (SystemExit:
        #                                     core.instance_root's way to say an instance is gone)
        log(f"web: {req.path} failed: {type(e).__name__}: {e}")
        why = f"{type(e).__name__}: {e}"
        if req.path.startswith("/fragment/"):
            return _html(500, web_pages.h('<p class="bad">{p} failed: {e}</p>', p=req.path, e=why))
        return _html(500, web_pages.message_page("This page failed", f"{req.path} failed: {why}",
                                                 "The error is in supervisor.log too."))
    return r or _not_found(req.path)


def _not_found(what: str) -> Response:
    return _html(404, web_pages.message_page("Not found", f"Nothing at {what}."))


def _pair(req: Request, login: str) -> Response:
    if req.method == "GET":
        p = _pairing(req.query.get("t", ""))
        if not p:
            return _html(403, web_pages.unpaired_page(expired=True))
        return _html(200, web_pages.pair_page(req.query["t"], p["device"]))
    if req.method != "POST" or not _same_origin(req):
        return _refused("This request did not come from this page.")
    form = req.form()
    token = form.get("t") or _token_from_link(form.get("link", ""))
    got = _use_pairing(token, login)
    if not got:
        return _html(403, web_pages.unpaired_page(expired=True))
    dev, secret = got
    return Response(303, b"", {"Location": "/", "Set-Cookie": _device_cookie(dev, secret)})


def _token_from_link(link: str) -> str:
    """The token of a pasted pairing link (…/pair?t=TOKEN), or the token pasted alone."""
    link = link.strip()
    if "t=" in link:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("t", [""])[0]
    return link


def _device_cookie(dev: str, secret: str) -> str:
    """Lax, not Strict: a link to the UI opened from another app (Telegram, Messages) is a cross-site navigation, and a
    Strict cookie was not sent — the page said "not paired". Every POST still needs the page's token and same-origin."""
    return (f"{COOKIE}={dev}.{secret}; Path=/; Max-Age={COOKIE_DAYS * 86400}; HttpOnly; Secure; SameSite=Lax")


RENEW_S = 86400                                     # the device cookie is sent again at most once a day


def _renew_due(dev: str) -> bool:
    """True once a day per paired device (recorded in devices.json): its cookie is then sent again with a fresh
    Max-Age, so a browser or app in use never reaches the 400 days and finds itself unpaired."""
    rec = devices().get(dev) or {}
    if time.time() - _stamp(rec.get("renewed") or rec.get("created", "")) < RENEW_S:
        return False
    with _lock:
        ds = _read("devices.json")
        if dev in ds:
            ds[dev]["renewed"] = datetime.now().isoformat(timespec="seconds")
            _write("devices.json", ds)
    return True


def feedback_action(name: str, login: str, form: dict) -> tuple[str, dict | None]:
    """The thumbs on a chat reply or a logged message (POST /feedback/<n>, and the apps' /app/v1/feedback/<n>): `op`
    rate, clear, send or unpend. (what to say, the rating it acted on); ValueError, in words, when it is refused."""
    from . import feedback, messages, web_chat
    with web_chat._inst_lock(name):
        op = form.get("op")
        item = None
        said = "Feedback kept."
        if op in ("clear", "send", "unpend"):
            item = next((r for r in feedback.items(name) if r["id"] == form.get("id")), None)
            if not item:
                raise ValueError("That rating is no longer current for this instance.")
            target = item["target"]
            form.update({"kind": target["kind"], **(
                {"cid": target["chat"], "k": str(target["k"])} if target["kind"] == "chat"
                else {"file": target["file"]})})
        if op not in ("rate", "clear", "send", "unpend"):
            raise ValueError("Choose rate, clear, send or unpend.")
        if form.get("kind") == "chat":
            chat = web_chat.load(form.get("cid", ""))
            try:
                k = int(form.get("k", ""))
            except ValueError:
                raise ValueError("Choose an assistant reply in this chat.") from None
            if not chat or chat.get("instance") != name or not 0 <= k < len(chat.get("turns", [])):
                raise ValueError("That chat reply does not belong to this instance.")
            reply = chat["turns"][k]
            if reply.get("role") != "assistant":
                raise ValueError("Only an assistant reply can be rated.")
            target = {"kind": "chat", "chat": chat["id"], "k": k}
            text = reply.get("content", "")
        elif form.get("kind") == "message":
            reply = next((r for r in messages.history() if r.get("file") == form.get("file")
                          and r.get("from") == name and r.get("to") == "operator"), None)
            if not reply:
                raise ValueError("That logged message is not from this instance to the operator.")
            target = {"kind": "message", "file": reply["file"]}
            text = reply["text"]
        else:
            raise ValueError("Choose a chat reply or a logged message.")
        # Serialize ratings/clears with the relay taking pending feedback.
        lock = web_chat._lock(target["chat"]) if target["kind"] == "chat" else web_chat._inst_lock(name)
        with lock:
            if item:
                item = next((r for r in feedback.items(name) if r["id"] == item["id"]), None)
                if not item:
                    raise ValueError("That rating is no longer current for this instance.")
            if item and target["kind"] == "chat" and not item["sent"]:
                chat = web_chat.load(target["chat"])
                if any(item["id"] in t.get("feedback", []) for t in chat["turns"]):
                    feedback.mark_sent(item["id"], "chat", chat["id"])
                    item = next(r for r in feedback.items(name) if r["id"] == item["id"])
            if op == "clear":
                feedback.clear(item["id"], login)
                said = ("Feedback cleared. The message was already delivered." if item["sent"]
                        else "Feedback cleared.")
            elif op == "rate":
                item = feedback.rate(name, login, target, form.get("thumb", ""), form.get("reason", ""),
                                     reply["ts"], text)
            elif op == "send":
                if item["sent"]:
                    raise ValueError("That feedback was already delivered.")
                chat = web_chat.load(target["chat"]) if target["kind"] == "chat" else None
                if chat and any(p["id"] == item["id"] for p in web_chat.pending_feedback(chat)):
                    raise ValueError("That feedback is already waiting for your next message.")
                reason = form.get("reason", item["reason"])[:2000]
                if reason != item["reason"]:
                    item = feedback.rate(name, login, target, item["thumb"], reason, reply["ts"], text)
        if op == "send":
            said = web_chat.send_feedback(name, item, feedback.note(item, text))
        elif op == "unpend":
            web_chat.unpend_feedback(name, item)
            said = "Feedback kept; removed from your next message."
    return said, item


def _post(req: Request, dev: str) -> Response:
    if req.path == "/unpair":
        unpair(dev)
        return _html(200, web_pages.h('<p>This browser is unpaired.</p>'))
    if m := re.fullmatch(r"/feedback/([A-Za-z0-9_-]+)", req.path):
        name = m[1]
        if name not in _names():
            return _not_found(req.path)
        try:
            said, item = feedback_action(name, (req.headers.get("tailscale-user-login") or "").lower(), req.form())
        except ValueError as e:
            return _html(409, web_pages.h("<p>{t}</p>", t=str(e)))
        return _html(200, web_pages.h("<p>{t}</p>", t=said),
                     {"X-Feedback-Id": item["id"]} if item else {})
    if m := re.fullmatch(r"/bakeoffs/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/judge", req.path):
        from . import bakeoff
        b = bakeoff.load(m[1], m[2])
        if not b:
            return _not_found(req.path)
        f = req.form()
        try:
            x, y = int(f.get("x", "")), int(f.get("y", ""))
            if not (0 <= x < len(b["arms"]) and 0 <= y < len(b["arms"])) or x == y:
                raise ValueError
            bakeoff.operator_record(b, int(f.get("state", "")), (b["arms"][x]["label"], b["arms"][y]["label"]),
                                    f.get("choice", ""), f.get("note", ""),
                                    (req.headers.get("tailscale-user-login") or "").lower(),
                                    runs=f.get("runs", "") or "missing")
        except (ValueError, bakeoff.BakeoffError) as e:
            return _html(409, web_pages.h("<p>{t}</p>", t=str(e) or "Not a pair of this bake-off."))
        return _html(200, web_pages.h("<p>Saved.</p>"))
    if m := re.match(r"^/chat/([A-Za-z0-9_-]+)/send$", req.path):
        from . import web_chat, reply_links
        if m[1] not in _names():
            return _not_found(req.path)
        try:
            fields, files = web_chat.parse_multipart(req.headers.get("content-type", ""), req.body)
            # Next cycle (background=1): to the inbox, read in its next cycle
            send = (web_chat.background if fields.get("background") == "1" else
                    web_chat.queue if fields.get("queue") == "1" else web_chat.send)
            extra = {"retry_of": fields.get("retry_of", "")} if send is web_chat.send else {}
            if send is not web_chat.background:          # spoken: a turn of the chat, not the inbox
                extra["voice"] = fields.get("voice") == "1"
                if fields.get("cid"):
                    extra["cid"] = fields["cid"]
            _, said = send(m[1], (req.headers.get("tailscale-user-login") or "").lower(),
                                    fields.get("text", ""), files, mid=fields.get("mid", ""), **extra)
            reply_links.record(m[1], fields.get("in_reply_to"), f"send:{m[1]}:{fields.get('mid') or secrets.token_hex(16)}")
        except web_chat.Refused as e:
            return _html(409, web_pages.h("<p>{t}</p>", t=str(e)))
        return _html(200, web_pages.h("<p>{t}</p>", t=said))
    if m := re.fullmatch(r"/chat/([A-Za-z0-9_-]+)/unqueue", req.path):
        from . import web_chat
        if m[1] not in _names():
            return _not_found(req.path)
        form = req.form()
        try:
            said = web_chat.unqueue(m[1], form.get("mid", ""), cid=form.get("cid", ""))
        except web_chat.Refused as e:
            return _html(409, web_pages.h("<p>{t}</p>", t=str(e)))
        return _html(200, web_pages.h("<p>{t}</p>", t=said))
    if m := re.fullmatch(r"/chat/([A-Za-z0-9_-]+)/(new|rename)", req.path):
        from . import web_chat
        if m[1] not in _names():
            return _not_found(req.path)
        form = req.form()
        try:
            if m[2] == "new":
                chat = web_chat.new_thread(m[1], (req.headers.get("tailscale-user-login") or "").lower(),
                                           title=form.get("title", ""))
                return _html(200, web_pages.h('<p data-cid="{cid}"><a href="/i/{n}/chat?id={cid}">New thread</a></p>',
                                              cid=chat["id"], n=m[1]))
            said = web_chat.rename(m[1], form.get("cid", ""), form.get("title", ""))
        except web_chat.Refused as e:
            return _html(409, web_pages.h("<p>{t}</p>", t=str(e)))
        return _html(200, web_pages.h("<p>{t}</p>", t=said))
    if m := re.match(r"^/docs/([A-Za-z0-9_-]+)/save$", req.path):
        if m[1] not in _names():
            return _not_found(req.path)
        return _save_doc(m[1], (req.headers.get("tailscale-user-login") or "").lower(), req.form())
    if m := re.match(r"^/docs/([A-Za-z0-9_-]+)/delete$", req.path):
        if m[1] not in _names():
            return _not_found(req.path)
        return _delete_doc(m[1], (req.headers.get("tailscale-user-login") or "").lower(), req.form())
    if m := re.fullmatch(r"/docs/([A-Za-z0-9_-]+)/comment(?:/(edit|delete|send|state))?", req.path):
        if m[1] not in _names():
            return _not_found(req.path)
        return _comment(m[1], (req.headers.get("tailscale-user-login") or "").lower(), m[2], req.form())
    if req.path in ("/push/subscribe", "/push/unsubscribe"):
        from . import webpush
        try:
            if req.path.endswith("unsubscribe"):
                webpush.unsubscribe(dev)
                return _html(200, web_pages.h("<p>Notifications off on this device</p>"))
            webpush.subscribe(dev, json.loads(req.body or b"{}"))
        except (webpush.PushError, ValueError) as e:
            return _html(409, web_pages.h("<p>refused: {e}</p>", e=str(e)))
        return _html(200, web_pages.h("<p>Notifications on: a reply that lands while SHAFT is closed, and a message "
                                      "an instance sends you here, show on this device</p>"))
    if m := re.match(r"^/chat/([A-Za-z0-9_-]+)/clear$", req.path):
        from . import web_chat
        if m[1] not in _names():
            return _not_found(req.path)
        try:
            said = web_chat.clear(m[1], req.form().get("cid", ""))
        except web_chat.Refused as e:
            return _html(409, web_pages.h("<p>{t}</p>", t=str(e)))
        return _html(200, web_pages.h("<p>{t}</p>", t=said))
    if req.path in ("/action/chat-stop", "/action/chat-end"):
        from . import web_chat
        cid = req.form().get("cid", "")
        try:
            said = (web_chat.stop if req.path.endswith("stop") else web_chat.end)(cid)
        except web_chat.Refused as e:
            return _html(409, web_pages.h("<p>{t}</p>", t=str(e)))
        return _html(200, web_pages.h("<p>{t}</p>", t=said))
    if m := re.match(r"^/preview/drive/([A-Za-z0-9_-]+)$", req.path):
        from . import opconfig, web_config
        if m[1] not in _names():
            return _not_found(req.path)
        old, _ = opconfig.drive(m[1])
        return _html(200, web_pages.h("{d}", d=web_config.drive_diff(old, req.form().get("text", ""))))
    if m := re.match(r"^/action/([a-z-]+)$", req.path):
        from . import dashboard, web_actions
        with _snap_lock:
            _snap["snap"] = None                     # the guards read the state as it is now, not a second ago
        try:
            status, text = web_actions.run(m[1], req.form(), overview(), _names(), list(dashboard.SERVICES))
        except (Exception, SystemExit) as e:         # say what broke, never a silent nothing
            log(f"web: action {m[1]} failed: {type(e).__name__}: {e}")
            status, text = 500, f"{m[1]} failed: {type(e).__name__}: {e}"
        with _snap_lock:
            _snap["snap"] = None                     # the next refresh shows what the action changed
        if status == 200 and req.headers.get("x-row") == "1":
            try:
                row = web_actions.saved_row(m[1], req.form())
            except (Exception, SystemExit) as e:
                log(f"web: saved row {m[1]} failed: {type(e).__name__}: {e}")
                return _html(200, web_pages.h("<p>{t}</p><p>Saved, but the row could not refresh. "
                                             "Reload before saving it again.</p>", t=text))
            return _html(200, web_pages.h("<p>{t}</p><template data-saved-row>{row}</template>", t=text, row=row),
                         {"X-Row": "1"})
        return _html(status, web_pages.h("<p>{t}</p>", t=text))
    return _html(404, web_pages.message_page("Not found", f"No action at {req.path}."))


# -- read-only pages (M1): each from the same panel reader as its TUI tab --------------------------------------------
INSTANCE_PAGE = re.compile(r"^/i/(?P<name>[A-Za-z0-9_-]+)(?:/(?P<tab>[a-z]+))?$")
TEXT_MAX = 200_000                                          # an attachment shown inline as text, at most
FILE_EXCLUSIONS = frozenset({"secrets", "config.env", ".git", "mail", "gdrive", "gchat", ".browser"})
FILES_PER_PAGE = 200
FILE_MAX = 100 * 2**20            # a download is read whole into the web service: past this, a terminal copies it
IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF87a", "image/gif"),
               (b"GIF89a", "image/gif"))


def _names() -> list[str]:
    from .core import instance_names
    return instance_names()


def _get(req: Request, csrf: str) -> Response | None:
    from . import dashboard, panel, web_views as v
    path, qs = req.path, req.query
    names = _names()
    if path == "/":                                  # chat first: the instance last talked to
        last = _last_chat(req)
        to = f"/i/{last}/chat" if last in names else f"/i/{names[0]}/chat" if names else "/overview"
        return Response(302, b"", {"Location": to})
    if path == "/fragment/calendar-proposals":
        from . import calendar_proposals
        return _html(200, v.calendar_proposals_body(calendar_proposals.listing()))
    if path == "/calendar/proposals":
        from . import calendar_proposals
        return _html(200, v.calendar_proposals_page(calendar_proposals.listing(), names, csrf))
    if path == "/overview":
        return _html(200, web_pages.overview_page(overview(), csrf))
    if path == "/fragment/overview":
        return _html(200, web_pages.overview(overview()))
    if path == "/fragment/changelog":
        from . import version
        return _html(200, web_pages.changelog_body(version.changelog()))
    if path == "/fragment/rail":
        from . import messages, web_chat
        history = messages.history()
        return _html(200, web_pages.rail(names, qs.get("c", ""), overview(),
                                         {n: web_chat.summary(n, history) for n in names}))
    if m := re.match(r"^/fragment/i/([A-Za-z0-9_-]+)/cycles-next$", path):
        if m[1] not in names:
            return None
        from . import cycles
        return _html(200, v.cycles_next(cycles.plan(m[1], _live(m[1]))))
    if m := re.match(r"^/fragment/i/([A-Za-z0-9_-]+)/live$", path):
        if m[1] not in names:
            return None
        return _html(200, v.instance_live(_live(m[1]), panel.instance_detail(m[1])))
    if m := INSTANCE_PAGE.match(path):
        name, tab = m["name"], m["tab"] or ""
        if name not in names:
            return None
        return _instance_page(name, tab, qs, names, csrf, doc_sort=_doc_sort(req))
    if m := re.match(r"^/fragment/chat/([A-Za-z0-9_-]+)/header$", path):
        if m[1] not in names:
            return None
        from . import web_chat
        chat = web_chat.load(qs["id"]) if qs.get("id") else web_chat.shown(m[1])
        if chat and chat.get("instance") != m[1]:
            return None
        past = [c for c in web_chat.chats(m[1]) if c.get("state") == "ended"]
        read_only = bool(qs.get("id")) and bool(chat) and chat.get("state") == "ended"
        return _html(200, v.chat_header(m[1], _live(m[1]), chat, past, read_only=read_only))
    if m := re.match(r"^/fragment/chat/([A-Za-z0-9_-]+)$", path):
        if m[1] not in names:
            return None
        from . import web_chat
        if qs.get("away"):                          # its page went hidden on this device: push to it again (W9)
            web_chat.unwatch(m[1], req.device, _page_ms(qs.get("at")), cid=qs.get("id"))
            return Response(204, b"")
        chat = web_chat.load(qs["id"]) if qs.get("id") else web_chat.shown(m[1])
        if chat and chat.get("instance") != m[1]:
            return None
        if not chat or chat.get("state") != "ended":
            # A covering sheet still polls the feed, but must not silence push. Stamp both paths so a late
            # poll sent before the sheet opened cannot restore watching (as with the page's away signal).
            at = _page_ms(req.headers.get("x-shaft-at"))
            if req.headers.get("x-shaft-watching") == "0":
                web_chat.unwatch(m[1], req.device, at, cid=(chat or {}).get("id"))
            else:
                web_chat.watching(m[1], req.device, at, cid=(chat or {}).get("id"))
        archived = bool(qs.get("id")) and bool(chat) and chat.get("state") == "ended"
        return _html(200, v.chat_feed(m[1], chat, _sent_during(m[1], chat, archived),
                                      archived=archived, whole=qs.get("all") == "1"))
    if m := re.match(r"^/fragment/chat/([A-Za-z0-9_-]+)/live$", path):
        # The running reply's activity, polled on its own (W12): it changes every second, and inside the feed it
        # changed the feed's ETag on every poll — the whole conversation rendered and sent again each 1.5 s.
        if m[1] not in names:
            return None
        from . import web_chat
        chat = web_chat.load(qs["id"]) if qs.get("id") else web_chat.shown(m[1])
        return _html(200, v.live_activity(chat if chat and chat.get("instance") == m[1] else None))
    if path == "/push/key":
        from . import webpush
        try:
            data = {"key": webpush.public_key()}
        except webpush.PushError as e:
            return Response(503, json.dumps({"error": str(e)}).encode(), {"Content-Type": "application/json"})
        return Response(200, json.dumps(data).encode(), {"Content-Type": "application/json"})
    if m := re.match(r"^/fragment/docs/([A-Za-z0-9_-]+)$", path):
        if m[1] not in names:
            return None
        from . import web_chat
        chat = web_chat.load(qs["id"]) if qs.get("id") else web_chat.shown(m[1])
        if chat and chat.get("instance") != m[1]:
            return None
        return _html(200, _docs_panel(m[1], chat, qs.get("doc", ""), qs.get("thread", ""),
                                      listing_only=qs.get("list") == "1",
                                      archived=bool(qs.get("id")) and bool(chat) and chat.get("state") == "ended",
                                      att=qs.get("att", ""), folder=qs.get("dir", ""), sort=_doc_sort(req))[1])
    if m := re.match(r"^/chatfiles/(web-[a-f0-9]+)/(\d+)/(\d+)$", path):
        return _chat_file(m[1], int(m[2]), int(m[3]))
    if path in ("/experiments", "/fragment/experiments"):
        from . import web_experiments
        name = qs.get("i", "")
        if name not in names:
            name = next((n for n in names if web_experiments.has_results(n)), names[0] if names else "")
        live = v.experiments_live(web_experiments.cards(name) if name else [], name, names)
        if path == "/fragment/experiments":
            return _html(200, live)
        return _html(200, v.experiments_page(live, name, names, csrf))
    if path == "/bakeoffs":
        from . import bakeoff
        return _html(200, v.bakeoffs_page(bakeoff.saved(), names, csrf,
                                          {n: bakeoff.ranking(None, n) for n in bakeoff.judged_instances()}))
    if m := re.fullmatch(r"/bakeoffs/([A-Za-z0-9_-]+)/judge-all", path):   # every bake-off of it already judged
        from . import bakeoff
        mine = [s for s in bakeoff.saved() if s["name"] == m[1]]
        if not mine:
            return None
        bs = [b for s in mine if bakeoff.operator_path(s).exists() and (b := bakeoff.load(m[1], s["id"]))]
        nxt = bakeoff.operator_next(bs)
        if not nxt:
            return _html(200, v.bakeoff_judge_page(bs[0] if bs else {"name": m[1], "id": ""}, None, 0, [], names, csrf,
                                                   back="/bakeoffs"))
        b, i, pair, left = nxt
        return _html(200, v.bakeoff_judge_page(b, bakeoff.operator_case(b, i, pair), left, [], names, csrf,
                                               back="/bakeoffs"))
    if m := re.fullmatch(r"/bakeoffs/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)(/judge)?", path):
        from . import bakeoff
        b = bakeoff.load(m[1], m[2])
        if not b:
            return None
        if not m[3]:
            return _html(200, v.bakeoff_page(b, bakeoff.operator_summary(b), names, csrf, bakeoff.ranking(b)))
        raw = urllib.parse.parse_qs(urllib.parse.urlsplit(req.target).query).get("vs", [])
        vs = sorted({int(x) for x in raw if re.fullmatch(r"[0-9]{1,3}", x) and 0 <= int(x) < len(b["arms"])}) \
            or list(range(len(b["arms"])))   # none ticked: every model
        nxt = bakeoff.operator_next([b], lambda _: [b["arms"][k]["label"] for k in vs])
        case = bakeoff.operator_case(b, nxt[1], nxt[2]) if nxt else None
        return _html(200, v.bakeoff_judge_page(b, case, nxt[3] if nxt else 0, vs, names, csrf))
    if path in ("/trials", "/fragment/trials"):
        only = qs.get("i", "")
        only = only if only in names else ""
        live = v.trials_live(panel.trials(only or None), panel.costs(days=1), only, panel.trial_progress, names)
        if path == "/fragment/trials":
            return _html(200, live)
        return _html(200, v.trials_page(live, only, names, csrf))
    if m := re.match(r"^/(fragment/)?trials/([A-Za-z0-9_-]+)/([^/]+)$", path):
        from . import trial
        if m[2] not in names:
            return None
        t = next((t for t in panel.trials(m[2]) if t.get("id") == urllib.parse.unquote(m[3])), None)
        if not t:
            return None
        text = trial.report(t)
        if m[1]:
            return _html(200, v.trial_report(text))
        return _html(200, v.trial_page(t, text, names, csrf))
    if path == "/messages":
        from . import messages
        ms = messages.history()
        people = sorted({p for x in ms for p in (x["from"], x["to"])})
        who = qs.get("who", "")
        return _html(200, v.messages_page(ms, who if who in people else "", people, names, csrf))
    if m := re.match(r"^/messages/([^/]+)$", path):
        msg = _message(urllib.parse.unquote(m[1]))
        if not msg:
            return None
        return _html(200, v.message_page(msg, _attachment_texts(msg), names, csrf))
    if m := re.match(r"^/files/([^/]+)/(\d+)$", path):
        return _file(urllib.parse.unquote(m[1]), int(m[2]))
    if path == "/costs":
        return _html(200, v.costs_page(panel.costs(days=7), names, csrf))
    if path in ("/settings", "/fragment/settings"):
        from . import actions, web_settings
        data = web_settings.listing()
        runs, busy = panel.scheduled_runs(), actions.busy_instances()
        if path == "/fragment/settings":
            return _html(200, web_settings.status(data["settings"], runs, busy))
        return _html(200, v.frame("Settings", web_settings.page_body(data, runs, busy), names, csrf,
                                  current="/settings"))
    if path in ("/services", "/fragment/services", "/fragment/services/log"):
        order = list(dashboard.SERVICES)
        from . import actions
        # The log fragment is its own poll: log_tail reads the last 8 KB, so a few seconds between refreshes is cheap.
        sel = qs.get("log") if qs.get("log") in order else order[0]
        if path == "/fragment/services/log":
            return _html(200, v.services_log(sel, panel.SERVICE_LOGS[sel], panel.log_tail(sel, 40)))
        live = v.services_live(panel.services_detail(), order, panel.scheduled_runs(), actions.busy_instances())
        if path == "/fragment/services":
            return _html(200, live)
        return _html(200, v.services_page(live, sel, panel.SERVICE_LOGS[sel], panel.log_tail(sel, 40), names, csrf))
    return None


def _page_ms(value: str | None) -> float | None:
    """A time the page stamped on a request (Date.now(), its own clock), or None."""
    try:
        return float(value) if value else None
    except ValueError:
        return None


class DocStale(Exception):
    """A document's save refused: it changed since the page read it (`changed`; on creating, it exists already), or
    the CLI refused the text. `sha`: what it has now; `diff`: what it has that the text sent has not ("" when there is
    nothing to compare: creating, or it is gone)."""

    def __init__(self, message: str, sha: str, diff: str, changed: bool):
        super().__init__(message)
        self.sha, self.diff, self.changed = sha, diff, changed


class DocRefused(Exception):
    """The CLI refused a document's deletion: its own words."""


def save_doc(name: str, login: str, form: dict) -> tuple[str, str, str]:
    """The operator's edit of a document (or a new one): through `shaft docs set` and its compare-and-set; then the
    instance hears of it (web_chat.doc_saved). (what to say, its path, its new sha). docs.Refused for a bad path or
    folder; DocStale with what the document has now that their text has not."""
    import difflib
    import secrets as _secrets
    from . import actions, docs, web_chat
    path = form.get("path", "").strip()
    if path and not path.endswith(".md"):
        path += ".md"
    folder = docs.folder(form.get("dir", ""))     # New document in a folder: it names the file inside that folder
    if folder and path:
        path = f"{folder}/{path}"
    text, sha = form.get("text", "").replace("\r\n", "\n"), form.get("sha", "")

    def refused(last: str) -> DocStale:
        now, now_sha = docs.read(name, path)
        diff = "\n".join(difflib.unified_diff(text.splitlines(), (now or "").splitlines(), "yours", "now", lineterm="",
                                              n=1)) if now is not None and sha != "none" else ""
        return DocStale(last if last.startswith("refused") else f"refused: {last}", now_sha, diff, now_sha != sha)
    old, old_sha = docs.read(name, path)
    if old_sha != sha:          # stale already: and from here, the CLI's --expect-sha makes `old` what it replaces
        raise refused(f"refused: notes/docs/{path} changed since you looked" if sha != "none" else
                      f"refused: notes/docs/{path} exists already")
    tmp = SUP_DIR / "web" / f"doc-{_secrets.token_hex(6)}.md"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    try:
        ok, last = actions.run_now(["docs", name, "set", path, "--file", str(tmp), "--expect-sha", sha], who="web")
    finally:
        tmp.unlink(missing_ok=True)
    if not ok:
        raise refused(last)
    new_sha = docs.sha(text.encode())             # what was written — never read again: the instance may write next
    rel = docs._rel(path)
    try:
        said = web_chat.doc_saved(name, login, rel, old, text, new_sha, **({"cid": form["cid"]} if form.get("cid") else {}))
    except web_chat.Refused as e:                  # written: only telling the instance failed — say both
        said = f"saved, but {name} was not told yet: {e}"
    return said, rel, new_sha


def _save_doc(name: str, login: str, form: dict) -> Response:
    """save_doc for the documents panel. Refused: what the document has now that their text has not, with the sha it
    has now, so Save again replaces it with theirs, knowingly (Cancel takes it)."""
    from . import docs
    try:
        said, rel, new_sha = save_doc(name, login, form)
    except docs.Refused as e:
        return _html(409, web_pages.h("<p>refused: {e}</p>", e=str(e)))
    except DocStale as e:
        return _html(409, web_pages.h('<p>{l}</p>{d}', l=str(e),
                                      d=web_pages.h('<p>What it has now that your text has not. Your text is kept in '
                                                    'the box: Save again to replace it with yours, or Cancel to take '
                                                    'its version.</p><pre class="diff">{x}</pre>', x=e.diff) if e.diff else ""),
                     {"X-Doc-Sha": e.sha} if e.diff else {})
    return _html(200, web_pages.h("<p>{t}</p>", t=said), {"X-Doc": rel, "X-Doc-Sha": new_sha})


def delete_doc(name: str, login: str, form: dict) -> str:
    """Delete a document: the CLI owns the deletion and its compare-and-set, just as it owns a save. (what to say);
    docs.Refused when it is not there (any more), DocStale when it changed since the page read it, DocRefused when the
    CLI refused."""
    from . import actions, docs, web_chat
    path, sha = docs._rel(form.get("path", "")), form.get("sha", "")
    old, now = docs.read(name, path)
    if old is None:
        raise docs.Refused(f"notes/docs/{path} is not there (any more)")
    if sha != now:
        raise DocStale(f"notes/docs/{path} changed since you looked", now, "", True)
    ok, last = actions.run_now(["docs", name, "rm", path, "--expect-sha", sha], who="web")
    if not ok:
        raise DocRefused(last)
    try:
        return web_chat.doc_saved(name, login, path, old, None, "none", **({"cid": form["cid"]} if form.get("cid") else {}))
    except web_chat.Refused as e:
        return f"deleted, but {name} was not told yet: {e}"


def _delete_doc(name: str, login: str, form: dict) -> Response:
    from . import docs
    try:
        said = delete_doc(name, login, form)
    except DocRefused as e:
        return _html(409, web_pages.h("<p>{t}</p>", t=str(e)))
    except (docs.Refused, DocStale) as e:
        return _html(409, web_pages.h("<p>refused: {e}</p>", e=str(e)))
    return _html(200, web_pages.h("<p>{t}</p>", t=said))


def comment_action(name: str, login: str, action: str | None, form: dict) -> tuple[str, str]:
    """A document comment: a new thread or a reply (`action` None), or edit, delete, send
    or state. (what to say, the thread's id); doc_comments.Refused or web_chat.Refused, in words."""
    from . import doc_comments, web_chat
    pid, text = form.get("pid", ""), form.get("text", "")
    if action is None:
        if form.get("thread"):
            tid = doc_comments.reply(name, pid, form["thread"], text, "operator")
        else:
            try:
                anchor = json.loads(form["anchor"]) if form.get("anchor", "").strip() else None
            except ValueError:
                raise doc_comments.Refused("invalid anchor JSON") from None
            tid = doc_comments.add_thread(name, pid, form.get("doc", ""), anchor,
                                           form.get("sha", ""), text)
        said = f"kept, not sent: {name} sees it in notes/comments/ and answers it when you ask"
        if form.get("send") in ("1", "cycle"):     # cycle: for its next cycle
            try:
                said = (web_chat.send_comment_background(name, login, pid) if form["send"] == "cycle"
                        else web_chat.send_comment(name, login, pid, **({"cid": form["cid"]} if form.get("cid") else {}))[1])
            except (doc_comments.Refused, web_chat.Refused) as e:
                said = f"saved, but not sent: {e}"
        else:                                 # the instance's copy holds unsent comments too (sending syncs it)
            web_chat._sync_copies(name, [doc_comments.get_post(name, pid)["doc"]])
    elif action == "state":
        tid = form.get("thread", "")
        doc_comments.set_state(name, tid, form.get("state", ""))
        thread = next(t for t in doc_comments.threads(name) if t["id"] == tid)
        web_chat._sync_copies(name, [thread["doc"]])
        said = f"{tid}: {form['state']}"
    else:
        post = doc_comments.get_post(name, pid)
        tid = post["thread"]
        if action == "edit":
            doc_comments.edit(name, pid, text)
            web_chat._sync_copies(name, [post["doc"]])
            said = "comment edited"
        elif action == "delete":
            doc_comments.delete(name, pid)
            web_chat._sync_copies(name, [post["doc"]])
            said = "comment deleted"
        elif form.get("when") == "cycle":
            said = web_chat.send_comment_background(name, login, pid)
        else:
            _, said = web_chat.send_comment(name, login, pid, **({"cid": form["cid"]} if form.get("cid") else {}))
    return said, tid


def _comment(name: str, login: str, action: str | None, form: dict) -> Response:
    from . import doc_comments, web_chat
    try:
        said, tid = comment_action(name, login, action, form)
    except (doc_comments.Refused, web_chat.Refused) as e:
        return _html(409, web_pages.h("<p>refused: {e}</p>", e=str(e)))
    return _html(200, web_pages.h("<p>{t}</p>", t=said), {"X-Thread": tid})


def _docs_panel(name: str, chat: dict | None, doc: str, thread: str = "", listing_only: bool = False,
                archived: bool = False, att: str = "", folder: str = "", sort: str = ""):
    """(the open document, the panel): the one asked for, else the last this chat changed (what it is writing shows as it
    writes it), else the list; listing_only (← Documents, ?list=1): the list, whatever this chat changed. With its
    comment threads, which are in the fragment so a comment alone changes its ETag.
    att (?att=<message file>/<index>): a text file the instance sent you instead, read-only (_attachment_preview).
    folder (?dir=, a subfolder of notes/docs/): the list shows that folder; asking for one is asking for the list."""
    from . import doc_comments, docs, web_views as v
    if att:
        return "", v.attachment_panel(name, *_attachment_preview(name, att))
    marked = v.in_chat(chat)
    problem, listing_only = "", listing_only or bool(folder)
    try:
        folder = docs.folder(folder)
    except docs.Refused as e:                     # a hand-made ?dir=: the top folder, saying why
        folder, problem = "", str(e)
    try:
        listing = docs.listing(name)
    except (Exception, SystemExit) as e:          # the documents never take the chat down with them: say what failed
        log(f"web: {name}'s documents: {type(e).__name__}: {e}")
        return doc, v.docs_panel(name, [], doc, None, "none", marked, f"documents not readable: {e}", archived=archived)
    if not doc and not listing_only:
        doc = next((d["path"] for d in listing if d["path"] in marked), "")
    text, sha = None, "none"
    if doc:
        try:
            text, sha = docs.read(name, doc)
        except docs.Refused as e:
            problem = str(e)
    try:
        threads = doc_comments.threads(name, doc) if doc and text is not None else []
        counts = doc_comments.open_counts(name)
    except (Exception, SystemExit) as e:          # comments never take the documents down with them
        log(f"web: {name}'s comments: {type(e).__name__}: {e}")
        threads, counts = [], {}
    return doc, v.docs_panel(name, listing, doc, text, sha, marked, problem,
                             ((chat or {}).get("saved_docs") or {}).get(doc), threads=threads, counts=counts,
                             thread=thread, archived=archived, folder=folder, sort=sort,
                             chat_id=(chat or {}).get("id", "") if archived else "")


LAST_CHAT = "shaft_last"                                     # the instance whose chat / opens


DOC_SORT = "shaft_docsort"                                  # the documents list's order (web_views.DOC_SORTS), set by web.js


def _cookie(req: Request, key: str) -> str:
    for part in (req.headers.get("cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == key:
            return v
    return ""


def _last_chat(req: Request) -> str:
    return _cookie(req, LAST_CHAT)


def _doc_sort(req: Request) -> str:
    """The documents list's order: ?sort= (a link followed without the script), else this browser's choice (the cookie
    web.js sets, so every tab and chat lists them the same way), else newest first."""
    from .web_views import doc_sort
    return doc_sort(req.query.get("sort") or _cookie(req, DOC_SORT))


def _next_cycle(name: str) -> str:
    """When the instance reads a message left for its next cycle; never fails the chat page."""
    from . import autonomy
    try:
        return autonomy.next_cycle(name)
    except (Exception, SystemExit) as e:          # noqa: BLE001 — a schedule it cannot read: the button still works
        log(f"web: could not tell {name}'s next cycle: {type(e).__name__}: {e}")
        return ""


def _live(name: str) -> dict:
    return next((i for i in overview()["instances"] if i["name"] == name), {})


def _instance_page(name: str, tab: str, qs: dict, names: list[str], csrf: str,
                   doc_sort: str = "") -> Response | None:
    from . import panel, web_views as v
    from .core import instance_root, journal_entries
    if tab in ("files", "file"):
        return _instance_files(name, tab, qs, names, csrf)
    if tab == "":
        return _html(200, v.instance_summary(name, panel.instance_detail(name), _live(name), names, csrf))
    if tab == "judge":
        js = panel.judgments(name)
        if ts := qs.get("ts"):
            j = next((j for j in js if j.get("episode_ts") == ts), None)
            return _html(200, v.judge_detail_page(name, j, names, csrf)) if j else None
        return _html(200, v.judge_page(name, js, panel.traits(name), names, csrf))
    if tab == "changes":
        return _html(200, v.changes_page(name, panel.instance_detail(name), panel.probation_events(name),
                                         panel.template_merges(name), names, csrf))
    if tab == "benchmarks":
        return _html(200, v.benchmarks_page(name, panel.battery_status(name), panel.evals(name),
                                            panel.surprise_summary(name), names, csrf))
    if tab == "journal":
        try:
            at = max(0, int(qs.get("at", "0")))
        except ValueError:
            at = 0
        return _html(200, v.journal_page(name, journal_entries(instance_root(name)), at, names, csrf))
    if tab == "activity":
        return _html(200, v.activity_page(name, activity(name), names, csrf))
    if tab == "chats":
        from . import web_chat
        past = [c for c in web_chat.chats(name) if c.get("state") == "ended"]
        body = v.join([v.link(f"/i/{v.q(name)}/chat", "Back to the current chat"),
                       v.section("Earlier chats", v.earlier_chats(name, past))])
        return _html(200, v.instance_frame(name, "chat", "Earlier chats", body, names, csrf))
    if tab == "chat":
        from . import web_chat
        chat = web_chat.load(qs["id"]) if qs.get("id") else web_chat.shown(name)
        if chat and chat.get("instance") != name:
            return None
        past = [c for c in web_chat.chats(name) if c.get("state") == "ended"]
        read_only = bool(qs.get("id")) and bool(chat) and chat.get("state") == "ended"
        doc, panel = _docs_panel(name, chat, qs.get("doc", ""), qs.get("thread", ""), archived=read_only,
                                 att=qs.get("att", ""), folder=qs.get("dir", ""), sort=doc_sort,
                                 listing_only=qs.get("list") == "1")
        return _html(200, v.chat_page(name, chat, _sent_during(name, chat, read_only), past, _live(name), names, csrf,
                                      read_only=read_only, selected=bool(qs.get("id")),
                                      panel=panel, doc=doc, att=qs.get("att", ""), folder=qs.get("dir", ""),
                                      sort=v.doc_sort(qs["sort"]) if qs.get("sort") else "",
                                      listing_only=qs.get("list") == "1" and not qs.get("doc"),
                                      whole=qs.get("all") == "1",
                                      later="" if read_only else _next_cycle(name)),
                     {"Set-Cookie": f"{LAST_CHAT}={name}; Path=/; Max-Age={COOKIE_DAYS * 86400}; Secure; SameSite=Strict"})
    if tab in ("config", "drive", "access"):
        from . import opconfig, web_config
        if tab == "config":
            body = web_pages.join([web_config.orders_panel(name),
                                   web_config.config_page(name, opconfig.fields(name), _config_history(name))])
        elif tab == "drive":
            text, sha = opconfig.drive(name)
            body = web_config.drive_page(name, text, sha, v.markdown(text) if text is not None else Html(""))
        else:
            body = web_config.access_page(name, web_config.access(name))
            if qs.get("google"):
                body = web_pages.h('<p role="status">{result}</p>{body}', result=qs["google"], body=body)
        return _html(200, v.instance_frame(name, tab, tab, body, names, csrf))
    if tab == "transcripts":
        from . import cycles
        if "ts" in qs:
            cycle = cycles.cycle(name, qs["ts"])
            return _html(200, v.cycle_page(name, cycle, names, csrf)) if cycle is not None else None
        filt = qs.get("f", "") if qs.get("f", "") in v.TX_FILTERS else ""
        if filt == "conversations":
            es = [e for e in panel.transcripts(name) if e["kind"] == "conversation"]
            return _html(200, v.transcripts_page(name, es, filt, panel.transcript_summary, _live(name), names, csrf))
        before = qs.get("before", "")
        try:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", before):
                before = None
            else:
                datetime.strptime(before, "%Y-%m-%d")
        except ValueError:
            before = None
        filt = filt if filt in cycles.FILTERS else ""
        return _html(200, v.cycles_page(name, cycles.plan(name, _live(name)),
                                        cycles.days(name, before=before, limit=3, filt=filt), filt, names, csrf))
    if tab == "transcript":
        e = next((e for e in panel.transcripts(name) if e["id"] == qs.get("id")), None)
        if not e:
            return None
        thinking = qs.get("thinking") == "1"
        return _html(200, v.transcript_page(name, e, panel.transcript_lines(name, e, thinking), thinking, names, csrf))
    return None


def _sent_during(name: str, chat: dict | None, archived: bool = False) -> list[dict]:
    """The messages a chat page shows over its span: web_chat.sent_during."""
    from . import web_chat
    return web_chat.sent_during(name, chat, archived)


def _chat_file(cid: str, k: int, j: int) -> Response | None:
    from . import web_chat
    chat = web_chat.load(cid)
    turns = (chat or {}).get("turns") or []
    atts = turns[k].get("attachments") or [] if 0 <= k < len(turns) else []
    if not 0 <= j < len(atts):
        return None
    return _serve_attachment(atts[j], f"chat {cid} turn {k}")


def _config_history(name: str) -> list[str]:
    """The last commits that touched config.env: the operator's ("changed from outside") and the instance's runs."""
    from .core import git_wt, instance_root
    try:
        out = git_wt(instance_root(name), "log", "-n", "12", "--date=format:%Y-%m-%d %H:%M", "--format=%ad · %an · %s",
                     "--", "config.env", check=False)
    except (Exception, SystemExit) as e:          # the page still works without its history
        return [f"(no history: {e})"]
    return [l for l in out.stdout.splitlines() if l.strip()] if out.returncode == 0 else [f"(git log failed: {out.stderr.strip()})"]


def activity(name: str) -> dict:
    """What an instance does with its reach: its connections (egress log), the plugins it built, its scheduled jobs,
    its Python, browser and mind store — each from the reader its CLI command uses."""
    from datetime import date
    from . import browser, egress, jobs, mind, pyenv
    from .core import instance_root
    from .core import list_beneath, safe_read
    root = instance_root(name)
    plugins = []
    for plugin in list_beneath(root, "plugins"):   # read without following a link: shown on the operator's page
        try:
            text = safe_read(root, f"plugins/{plugin}/plugin.json", errors="replace")
        except (OSError, UnsafePath):
            continue
        try:
            spec = json.loads(text)
        except ValueError as e:
            spec = {"description": f"(plugin.json does not parse: {e})"}
        spec = spec if isinstance(spec, dict) else {}
        plugins.append({"name": plugin, "description": str(spec.get("description", ""))[:300],
                        "scheduled": "schedule" in spec})
    log_path = egress.log_path(name)
    return {"egress": egress.describe(egress.recent(log_path, 50)),
            "internet_today": egress.summary(egress.recent(log_path, 2000), date.today().isoformat()),
            "plugins": plugins, "jobs": jobs.describe(name), "python": pyenv.describe(root),
            "browser": browser.describe(root), "mind": mind.describe(name)}


def _message(file: str) -> dict | None:
    from . import messages
    return next((m for m in messages.history() if m["file"] == file), None)


def _instance_files(name: str, tab: str, qs: dict, names: list[str], csrf: str) -> Response:
    """Browse or open below an instance with the attachment reader's no-follow descriptor walk."""
    from .core import instance_root, open_beneath, reserved
    from . import web_views as v
    raw = qs.get("path", "")
    rel = Path(raw)
    root = instance_root(name)
    if (rel.is_absolute() or ".." in rel.parts or "\0" in raw
            or FILE_EXCLUSIONS.intersection(p.casefold() for p in rel.parts)
            or reserved(root, rel.as_posix(), FILE_EXCLUSIONS)):   # by the folder itself, whatever its spelling
        return Response(403, b"This path is not available in Files.", {"Content-Type": "text/plain; charset=utf-8"})
    try:
        if tab == "file":
            # Nonblocking prevents a named pipe from hanging before the regular-file check.
            fd = open_beneath(root, rel.parts, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode):
                    raise UnsafePath("Only regular files can be downloaded.")
                if st.st_size > FILE_MAX:
                    return Response(413, (f"{rel.name} is {st.st_size / 2**20:.0f} MB, over the {FILE_MAX // 2**20} MB "
                                          "the web UI serves: copy it in a terminal.").encode(),
                                    {"Content-Type": "text/plain; charset=utf-8"})
                data = f.read()
            inline = qs.get("open") == "1" and rel.suffix.lower() in TEXT_TYPES
            disposition = ("inline" if inline else "attachment") + "; filename*=UTF-8''"
            return Response(200, data, {
                "Content-Type": "text/plain; charset=utf-8" if inline else "application/octet-stream",
                "Content-Disposition": disposition + urllib.parse.quote(rel.name, safe="")})
        fd = (open_beneath(root, rel.parts, os.O_RDONLY | os.O_DIRECTORY) if rel.parts
              else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        entries = []
        try:
            with os.scandir(fd) as scan:
                for entry in scan:
                    if entry.name.casefold() in FILE_EXCLUSIONS or entry.is_symlink():
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:             # removed while the directory was being listed
                        continue
                    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                        continue
                    entries.append({"name": entry.name, "directory": stat.S_ISDIR(st.st_mode),
                                    "bytes": st.st_size, "mtime": st.st_mtime,
                                    "text": Path(entry.name).suffix.lower() in TEXT_TYPES})
        finally:
            os.close(fd)
    except (UnsafePath, OSError) as e:
        # in words, never the host's paths: a link, a missing file, a folder that cannot be read
        msg = str(e)
        why = ("it does not exist" if isinstance(e, FileNotFoundError) else
               "Permission denied" if isinstance(e, PermissionError) else
               "it is a link, or it leaves the instance's folder" if "is not a plain path below" in msg else
               msg if os.sep not in msg else msg.rsplit(": ", 1)[-1])    # open_beneath: "<root>: <strerror>"
        return Response(403, f"Cannot open {raw or 'this folder'}: {why}.".encode(),
                        {"Content-Type": "text/plain; charset=utf-8"})
    entries.sort(key=lambda e: (not e["directory"], e["name"].casefold(), e["name"]))
    try:
        page = max(1, int(qs.get("page", "1")))
    except ValueError:
        page = 1
    page = min(page, max(1, (len(entries) + FILES_PER_PAGE - 1) // FILES_PER_PAGE))
    start = (page - 1) * FILES_PER_PAGE
    return _html(200, v.files_page(name, rel.parts, entries[start:start + FILES_PER_PAGE], len(entries),
                                   page, FILES_PER_PAGE, names, csrf))


def _attachment_root(path: Path) -> tuple[Path, Path]:
    """(the folder an attachment_path() is below, the rest): the folder open_beneath starts from."""
    from .core import INSTANCES
    for base in (INSTANCES.resolve(), (SUP_DIR / "operator-inbox" / "attachments").resolve()):
        try:
            rest = path.relative_to(base)
        except ValueError:
            continue
        return (base / rest.parts[0], Path(*rest.parts[1:])) if base == INSTANCES.resolve() else (base, rest)
    raise UnsafePath(f"{path} is below no attachment folder")


def attachment_path(a: dict) -> Path | None:
    """Where a message's attachment is, if it is a file inside an instance's folder or the operator's inbox
    attachments, reached without following any link below that folder; None otherwise. Only paths a message names
    are ever served (a file an instance wrote never runs as a page)."""
    from .core import INSTANCES
    rel = Path(str(a.get("path", "")))
    if ".." in rel.parts:
        return None
    if rel.is_absolute():               # store_attachments' fallback when the folder is not under the project
        bases = [(b, prefix) for d, prefix in ((INSTANCES, "instances"), (SUP_DIR / "operator-inbox" / "attachments",
                                                                         ".supervisor/operator-inbox/attachments"))
                 for b in (d, d.resolve())]    # both spellings (/var and /private/var): links below are checked later
        for base, prefix in bases:
            try:
                rel = Path(prefix) / rel.relative_to(base)
                break
            except ValueError:
                continue
        else:
            return None
    if rel.parts[:1] == (".supervisor",):                    # the supervisor's folder, wherever SUP_DIR points
        base, rest = SUP_DIR / "operator-inbox" / "attachments", Path(*rel.parts[1:])
        if rest.parts[:2] != ("operator-inbox", "attachments"):
            return None
        rest = Path(*rest.parts[2:])
    elif rel.parts[:1] == ("instances",):
        base, rest = INSTANCES, Path(*rel.parts[1:])
    else:
        return None
    if not rest.parts:
        return None
    real_base = base.resolve()
    path = real_base / rest
    try:
        if path.resolve() != path or not path.is_file():   # a link anywhere below the folder: refused
            return None
    except OSError:
        return None
    return path


def _attachment_texts(m: dict) -> dict:
    out = {}
    for k, a in enumerate(m.get("attachments") or []):
        if not a.get("name", "").lower().endswith(TEXT_TYPES) or a.get("bytes", 0) > TEXT_MAX:
            continue
        path = attachment_path(a)
        if path is None:
            continue
        try:   # opened with no link followed, as the preview reads it: nothing swapped in after attachment_path
            root, rest = _attachment_root(path)
            out[k] = read_beneath(root, rest.parts, TEXT_MAX).decode("utf-8")
        except (OSError, UnsafePath, ValueError):
            continue
    return out


def _attachment_preview(name: str, att: str) -> tuple[str, str, str | None, str]:
    """(its file name, its download URL, its text or None, why there is none) for ?att=<message file>/<index>: only a
    text file one of `name`'s own messages carries, read as /files/ serves it (no link followed), at most TEXT_MAX."""
    file, _, k = att.rpartition("/")
    m = _message(file) if k.isdigit() else None
    atts = (m or {}).get("attachments") or []
    if not m or m.get("from") != name or not int(k) < len(atts):
        return "", "", None, f"{name} sent you no such attachment."
    a = atts[int(k)]
    file_name, url = a.get("name") or f"attachment {k}", f"/files/{urllib.parse.quote(file, safe='')}/{k}"
    if not file_name.lower().endswith(TEXT_TYPES):
        return file_name, url, None, f"{file_name} is not a text file: download it to open it."
    path = attachment_path(a)
    if path is None:
        log(f"web refused to preview an attachment of message {file}: {a.get('path')!r} is not a plain file in an "
            "instance's folder or the operator's inbox")
        return file_name, "", None, f"{file_name} is not there (any more)."
    root, rest = _attachment_root(path)
    try:
        data = read_beneath(root, rest.parts, TEXT_MAX)
    except ValueError as e:
        return file_name, url, None, f"{file_name} is too big to show here ({e}): download it to read it."
    except (UnsafePath, OSError) as e:
        log(f"web refused to preview an attachment of message {file}: {e}")
        return file_name, "", None, f"{file_name} cannot be read here (the reason is in supervisor.log)."
    try:
        return file_name, url, data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return file_name, url, None, f"{file_name} is not UTF-8 text: download it to open it."


def _image_type(data: bytes) -> str | None:
    for magic, ctype in IMAGE_MAGIC:
        if data.startswith(magic):
            return ctype
    return "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None


def _file(file: str, k: int) -> Response | None:
    m = _message(file)
    atts = (m or {}).get("attachments") or []
    if not 0 <= k < len(atts):
        return None
    return _serve_attachment(atts[k], f"message {file}")


def _serve_attachment(a: dict, where: str) -> Response | None:
    path = attachment_path(a)
    if path is None:
        log(f"web refused to serve an attachment of {where}: {a.get('path')!r} is not a plain file in an instance's "
            "folder or the operator's inbox")
        return None
    root, rest = _attachment_root(path)
    try:                                          # opened with no link followed: nothing swapped in after the check
        data = read_beneath(root, rest.parts)
    except (UnsafePath, OSError) as e:
        log(f"web refused to serve an attachment of {where}: {e}")
        return None
    name = a.get("name") or path.name
    disposition = "attachment; filename*=UTF-8''" + urllib.parse.quote(name, safe="")
    if ctype := _image_type(data):
        return Response(200, data, {"Content-Type": ctype, "Content-Disposition": disposition.replace(
            "attachment", "inline", 1)})
    return Response(200, data, {"Content-Type": "application/octet-stream", "Content-Disposition": disposition})


# -- the server ------------------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "shaft-web"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def _serve(self, method: str) -> None:
        from .messages import ATTACH_MAX_TOTAL
        length = int(self.headers.get("Content-Length") or 0)
        sends = self.path.startswith("/chat/") or re.match(r"/app/v1/chat/[^/?]+/send(\?|$)", self.path)
        limit = ATTACH_MAX_TOTAL + 1_000_000 if sends else 1_000_000   # files go with a chat
        if length > limit:
            # Plain text, as a refusal the page shows: send_error's own HTML page landed whole in the document view
            # (W15, review 2026-10-24); the apps get their JSON error. The body is never read, so the connection
            # closes after it.
            said = f"Too large: at most {limit // 1_000_000} MB can be sent at once."
            if self.path.startswith("/app/v1/"):
                from . import app_api
                r = _secured(app_api.error(413, "too_large", said))
            else:
                r = _secured(Response(413, said.encode(), {"Content-Type": "text/plain; charset=utf-8"}))
            r.headers["Connection"] = "close"
            self.close_connection = True
        else:
            body = self.rfile.read(length) if length else b""
            r = respond(Request(method, self.path, dict(self.headers.items()), body))
        self.send_response(r.status)
        for k, v in r.headers.items():
            for one in v if isinstance(v, list) else [v]:
                self.send_header(k, one)
        if r.stream is not None:
            self._pour(r.stream)
            return
        self.send_header("Content-Length", str(len(r.body)))
        self.end_headers()
        self.wfile.write(r.body)

    def _pour(self, stream: Iterator[bytes]) -> None:
        """Write a streamed response piece by piece, flushed each time, on this request's own thread (the server is a
        ThreadingHTTPServer: other requests go on meanwhile). It ends when the stream does or the client leaves: a
        write that fails, or a socket the client closed (readable with nothing to read) — then the stream is closed,
        so its `finally` runs at once."""
        self.close_connection = True
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.flush()
            for piece in stream:
                self.wfile.write(piece)
                self.wfile.flush()
                if _client_gone(self.connection):
                    break
        except OSError:                           # BrokenPipeError, ConnectionResetError: it left mid-write
            pass
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def do_DELETE(self):                          # the apps' DELETE /app/v1/push/register
        self._serve("DELETE")


def _client_gone(sock) -> bool:
    """The client closed its end: the socket reads as ready with nothing in it. A streamed response's client sends
    nothing after its request, so anything readable is the close (or a reset)."""
    import select
    try:
        ready, _, _ = select.select([sock], [], [], 0)
        return bool(ready) and sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def serve() -> int:
    reachable = set(LLM_PORTS) | {EMBED_PORT}
    if PORT in reachable:
        print(f"SHAFT_WEB_PORT={PORT} is a port every run may reach ({sorted(reachable)}): choose another")
        return 2
    if not allowed_logins():
        print("no SHAFT_API_LOGINS — run: shaft api on (it records your tailnet login)")
        return 2
    from . import web_chat
    from .version import record_service
    web_chat.reap()                              # replies the previous web server was relaying are over
    record_service("web", {"port": PORT, "https_port": HTTPS_PORT})
    from . import web_alerts
    web_alerts.start()                           # the instances' messages routed to the web: pushed (channel.py)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    if not allowed_hosts():                  # every request would be refused: say so where it is read
        log("web: no tailnet name recorded and tailscale cannot be asked from here: run shaft web on in a terminal")
    log(f"web listening on 127.0.0.1:{PORT} (logins {sorted(allowed_logins())}, hosts {sorted(allowed_hosts())})")
    print(f"SHAFT web UI on http://127.0.0.1:{PORT} (tailnet: {url()}) — {datetime.now():%H:%M}")
    server.serve_forever()
    return 0


def command(action: str, name: str | None = None, everything: bool = False) -> int:
    from . import autonomy
    from .api import _tailscale
    if action == "serve":
        return serve()
    if action == "pair":
        if not allowed_logins():
            print("no SHAFT_API_LOGINS — run: shaft api on (it records your tailnet login)")
            return 1
        link = new_pairing(name or f"browser {datetime.now():%Y-%m-%d %H:%M}")
        print(f"Open this link in the browser to pair (valid {PAIR_MINUTES} minutes, once):\n\n  {link}\n")
        return 0
    if action == "unpair":
        if not name and not everything:
            print("unpair what? shaft web unpair <device id or name> | --all")
            return 1
        n = unpair(None if everything else name)
        print(f"{n} browser(s) unpaired")
        return 0 if n else 1
    if action == "on":
        if not allowed_logins():
            print("no SHAFT_API_LOGINS — run: shaft api on (it records your tailnet login)")
            return 1
        if not record_hosts():              # the service cannot ask tailscale itself (launchd): it reads this
            print("cannot read this Mac's tailnet name: is Tailscale running? (tailscale status)")
            return 1
        _hosts["at"] = 0.0
        if autonomy.service("web", "on"):
            return 1
        r = _tailscale("serve", "--bg", f"--https={HTTPS_PORT}", f"http://127.0.0.1:{PORT}")
        if r.returncode != 0:
            print(f"tailscale serve failed: {(r.stderr or r.stdout).strip()}")
            return 1
        log(f"web exposed on the tailnet at {url()} (tailscale serve, tailnet only)")
        print(f"\nSHAFT web UI (tailnet only): {url()}\n  pair a browser: shaft web pair --name <laptop|phone>")
        return 0
    if action == "off":
        _tailscale("serve", f"--https={HTTPS_PORT}", "off")
        autonomy.service("web", "off")
        print("SHAFT web UI removed from the tailnet and stopped")
        return 0
    autonomy.service("web", "status")
    print(f"url: {url()}  ·  allowed: {', '.join(sorted(allowed_logins())) or '(none)'}")
    ds = devices()
    apps = {k for k, v in ds.items() if v.get("platform")}
    print(f"paired browsers: {len(ds) - len(apps)}")
    for k, v in sorted(ds.items(), key=lambda kv: kv[1].get("created", "")):
        if k not in apps:
            print(f"  {k}  {v.get('name')}  paired {v.get('created', '')[:16]}  last seen {v.get('last_seen', '')[:16]}")
    problem = apns.configured()
    print(f"native apps: {len(apps)}  ·  native push: {'ready' if not problem else f'off: {problem}'}")
    for k, v in sorted(ds.items(), key=lambda kv: kv[1].get("created", "")):
        if k in apps:
            push = apns.status(k)
            print(f"  {k}  {v.get('name')} ({v['platform']})  paired {v.get('created', '')[:16]}  last seen "
                  f"{v.get('last_seen', '')[:16]}  push {'on' if push.get('on') else 'off'}"
                  + (f" ({push['env']})" if push.get("env") else "") + (f": {push['reason']}" if push.get("reason") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(serve())
