"""`shaft web`: the web UI for managing the instances (PLAN-webui.md) — the control panel of `shaft top` in a
browser, for the tailnet only. Stdlib only, run by the system Python like every launchd service (core.PYTHON).

  shaft web on       install the service (com.shaft.web) and expose it with `tailscale serve` (never Funnel)
  shaft web off      stop it and remove it from the tailnet
  shaft web status   the service, the URL, the paired browsers
  shaft web pair [--name NAME]     a one-time link that pairs the browser you open it in
  shaft web unpair DEVICE | --all  revoke a paired browser

Who may use it (Daniele, 2026-10-19): a request needs BOTH
  - a Tailscale-User-Login in SHAFT_API_LOGINS (set by `tailscale serve`; the server listens on 127.0.0.1 only), and
  - a paired browser's cookie. `shaft web pair` prints a link valid for PAIR_MINUTES, usable once; opening it shows a
    button, and pressing it (a POST: a link preview cannot spend it) sets an HttpOnly, Secure, SameSite=Strict
    cookie. Only a hash of the cookie's secret is kept (.supervisor/web/devices.json).
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
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import web_pages
from .web_pages import Html
from .core import EMBED_PORT, LLM_PORTS, SUP_DIR, knob, log

PORT, _ = knob("SHAFT_WEB_PORT", "8785", int)               # not 8782: that is the embed port, which runs reach
HTTPS_PORT, _ = knob("SHAFT_WEB_HTTPS_PORT", "8444", int)   # the tailnet's https port (`tailscale serve`)
PAIR_MINUTES = 10
COOKIE = "shaft_device"
COOKIE_DAYS = 400                                           # the longest a browser keeps a cookie
STATIC = Path(__file__).resolve().parent / "web_static"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def allowed_logins() -> set[str]:
    from .api import allowed_logins as logins             # one list for the API and the web UI
    return logins()


_hosts: dict = {"at": 0.0, "hosts": set()}


def allowed_hosts() -> set[str]:
    """The names this UI answers to: this Mac's tailnet name with the https port (SHAFT_WEB_HOSTS overrides)."""
    if raw := os.environ.get("SHAFT_WEB_HOSTS"):
        return {h.strip().lower() for h in raw.split(",") if h.strip()}
    if time.time() - _hosts["at"] > 60:
        from .api import _tailscale
        try:
            st = json.loads(_tailscale("status", "--json").stdout or "{}")
        except (OSError, ValueError):
            st = {}
        name = (st.get("Self", {}).get("DNSName") or "").rstrip(".").lower()
        _hosts.update(at=time.time(), hosts={f"{name}:{HTTPS_PORT}"} | ({name} if HTTPS_PORT == 443 else set())
                      if name else set())
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


def _use_pairing(token: str, login: str) -> tuple[str, str] | None:
    """Spend the link: (device id, secret) for the cookie, or None if it is unknown, used or expired."""
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
                        "hash": _digest(secret)}
        _write("devices.json", devices)
    log(f"web: browser paired as {p['device']!r} ({dev}, tailnet user {login})")
    return dev, secret


def devices() -> dict:
    return _read("devices.json")


def unpair(device: str | None = None) -> int:
    """Revoke one paired browser (by id or name), or all with None. Returns how many were revoked."""
    with _lock:
        ds = _read("devices.json")
        gone = [k for k, v in ds.items() if device is None or device in (k, v.get("name"))]
        for k in gone:
            del ds[k]
        _write("devices.json", ds)
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
    if not got:
        return None
    dev, secret = got
    rec = devices().get(dev)
    if not rec or not hmac.compare_digest(rec.get("hash", ""), _digest(secret)):
        return None
    if time.time() - _stamp(rec.get("last_seen", "")) > 300:
        with _lock:
            ds = _read("devices.json")
            if dev in ds:
                ds[dev]["last_seen"] = datetime.now().isoformat(timespec="seconds")
                _write("devices.json", ds)
    return dev, secret


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

    def __post_init__(self):
        split = urllib.parse.urlsplit(self.target)
        self.path = split.path
        self.query = {k: v[0] for k, v in urllib.parse.parse_qs(split.query).items()}
        self.headers = {k.lower(): v for k, v in self.headers.items()}

    def form(self) -> dict:
        """The posted fields, blank ones included: a blank "saw" means "the page showed it unset", and dropping it
        dropped the stale-edit check (M3)."""
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.body.decode(errors="replace"),
                                                          keep_blank_values=True).items()}


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)


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


def _static(name: str) -> Response:
    path = (STATIC / name).resolve()
    if not name or path.parent != STATIC or not path.is_file():
        return _html(404, web_pages.message_page("Not found", "No such file."))
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return Response(200, path.read_bytes(), {"Content-Type": ctype, "Cache-Control": "max-age=3600"})


def respond(req: Request) -> Response:
    r = _route(req)
    r.headers.update({"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                      "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})
    r.headers.setdefault("Cache-Control", "no-store")
    return r


def _route(req: Request) -> Response:
    login = (req.headers.get("tailscale-user-login") or "").lower()
    if not login or login not in allowed_logins():
        log(f"web refused tailnet user {login or '(none)'} ({req.method} {req.path})")
        return _refused("This page is for the operator's tailnet login only.")
    if req.headers.get("host", "").lower() not in allowed_hosts():
        log(f"web refused host {req.headers.get('host')!r} ({req.method} {req.path})")
        return _refused("Open this page at its tailnet address.", url())
    if req.path.startswith("/static/"):
        return _static(urllib.parse.unquote(req.path.removeprefix("/static/")))
    if req.path == "/pair":
        return _pair(req, login)
    paired = _paired(req.headers.get("cookie", ""))
    if not paired:
        return _refused("This browser is not paired.",
                        "Run `shaft web pair` in your terminal and open the link it prints in this browser.")
    dev, secret = paired
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
            return _refused("This pairing link is unknown, used or expired.", "Run `shaft web pair` for a new one.")
        return _html(200, web_pages.pair_page(req.query["t"], p["device"]))
    if req.method != "POST" or not _same_origin(req):
        return _refused("This request did not come from this page.")
    got = _use_pairing(req.form().get("t", ""), login)
    if not got:
        return _refused("This pairing link is unknown, used or expired.", "Run `shaft web pair` for a new one.")
    dev, secret = got
    cookie = (f"{COOKIE}={dev}.{secret}; Path=/; Max-Age={COOKIE_DAYS * 86400}; HttpOnly; Secure; "
              "SameSite=Strict")
    return Response(303, b"", {"Location": "/", "Set-Cookie": cookie})


def _post(req: Request, dev: str) -> Response:
    if req.path == "/unpair":
        unpair(dev)
        return _html(200, web_pages.h('<p>This browser is unpaired.</p>'))
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
        return _html(status, web_pages.h("<p>{t}</p>", t=text))
    return _html(404, web_pages.message_page("Not found", f"No action at {req.path}."))


# -- read-only pages (M1): each from the same panel reader as its TUI tab --------------------------------------------
INSTANCE_PAGE = re.compile(r"^/i/(?P<name>[A-Za-z0-9_-]+)(?:/(?P<tab>[a-z]+))?$")
TEXT_TYPES = (".md", ".txt", ".csv", ".json", ".log", ".py", ".yaml", ".yml", ".toml", ".html", ".xml")
TEXT_MAX = 200_000                                          # an attachment shown inline as text, at most
IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF87a", "image/gif"),
               (b"GIF89a", "image/gif"))


def _names() -> list[str]:
    from .core import instance_names
    return instance_names()


def _get(req: Request, csrf: str) -> Response | None:
    from . import dashboard, panel, web_views as v
    path, qs = req.path, req.query
    if path == "/":
        return _html(200, web_pages.overview_page(overview(), csrf))
    if path == "/fragment/overview":
        return _html(200, web_pages.overview(overview()))
    names = _names()
    if m := re.match(r"^/fragment/i/([A-Za-z0-9_-]+)/live$", path):
        if m[1] not in names:
            return None
        return _html(200, v.instance_live(_live(m[1]), panel.instance_detail(m[1])))
    if m := INSTANCE_PAGE.match(path):
        name, tab = m["name"], m["tab"] or ""
        if name not in names:
            return None
        return _instance_page(name, tab, qs, names, csrf)
    if path == "/trials":
        only = qs.get("i", "")
        only = only if only in names else ""
        return _html(200, v.trials_page(panel.trials(only or None), panel.costs(days=1), only, panel.trial_progress,
                                        names, csrf))
    if m := re.match(r"^/trials/([A-Za-z0-9_-]+)/([^/]+)$", path):
        from . import trial
        if m[1] not in names:
            return None
        t = next((t for t in panel.trials(m[1]) if t.get("id") == urllib.parse.unquote(m[2])), None)
        return _html(200, v.trial_page(t, trial.report(t), names, csrf)) if t else None
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
    if path in ("/services", "/fragment/services"):
        order = list(dashboard.SERVICES)
        from . import actions
        live = v.services_live(panel.services_detail(), order, panel.scheduled_runs(), actions.busy_instances())
        if path == "/fragment/services":
            return _html(200, live)
        sel = qs.get("log") if qs.get("log") in order else order[0]
        return _html(200, v.services_page(live, sel, panel.SERVICE_LOGS[sel], panel.log_tail(sel, 40), names, csrf))
    return None


def _live(name: str) -> dict:
    return next((i for i in overview()["instances"] if i["name"] == name), {})


def _instance_page(name: str, tab: str, qs: dict, names: list[str], csrf: str) -> Response | None:
    from . import panel, web_views as v
    from .core import instance_root, journal_entries
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
    if tab in ("config", "drive", "access"):
        from . import opconfig, web_config
        if tab == "config":
            body = web_config.config_page(name, opconfig.fields(name), _config_history(name))
        elif tab == "drive":
            text, sha = opconfig.drive(name)
            body = web_config.drive_page(name, text, sha, v.markdown(text) if text is not None else Html(""))
        else:
            body = web_config.access_page(name, web_config.access(name))
        return _html(200, v.instance_frame(name, tab, tab, body, names, csrf))
    if tab == "transcripts":
        filt = qs.get("f", "") if qs.get("f", "") in v.TX_FILTERS else ""
        es = panel.transcripts(name)
        if filt == "cycles":
            es = [e for e in es if e["kind"] != "conversation"]
        elif filt == "conversations":
            es = [e for e in es if e["kind"] == "conversation"]
        return _html(200, v.transcripts_page(name, es, filt, panel.transcript_summary, _live(name), names, csrf))
    if tab == "transcript":
        e = next((e for e in panel.transcripts(name) if e["id"] == qs.get("id")), None)
        if not e:
            return None
        thinking = qs.get("thinking") == "1"
        return _html(200, v.transcript_page(name, e, panel.transcript_lines(name, e, thinking), thinking, names, csrf))
    return None


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
    root = instance_root(name)
    plugins = []
    for manifest in sorted((root / "plugins").glob("*/plugin.json")) if (root / "plugins").is_dir() else []:
        try:
            spec = json.loads(manifest.read_text(errors="replace"))
        except (OSError, ValueError) as e:
            spec = {"description": f"(plugin.json does not parse: {e})"}
        spec = spec if isinstance(spec, dict) else {}
        plugins.append({"name": manifest.parent.name, "description": str(spec.get("description", ""))[:300],
                        "scheduled": "schedule" in spec})
    log_path = egress.log_path(name)
    return {"egress": egress.describe(egress.recent(log_path, 50)),
            "internet_today": egress.summary(egress.recent(log_path, 2000), date.today().isoformat()),
            "plugins": plugins, "jobs": jobs.describe(name), "python": pyenv.describe(root),
            "browser": browser.describe(root), "mind": mind.describe(name)}


def _message(file: str) -> dict | None:
    from . import messages
    return next((m for m in messages.history() if m["file"] == file), None)


def attachment_path(a: dict) -> Path | None:
    """Where a message's attachment is, if it is a file inside an instance's folder or the operator's inbox
    attachments, reached without following any link below that folder; None otherwise. Only paths a message names
    are ever served (PLAN-webui.md: a file an instance wrote never runs as a page)."""
    from .core import INSTANCES, PROJECT
    rel = Path(str(a.get("path", "")))
    if rel.is_absolute() or ".." in rel.parts:
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
        try:
            out[k] = path.read_bytes()[:TEXT_MAX].decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return out


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
    path = attachment_path(atts[k])
    if path is None:
        log(f"web refused to serve attachment {k} of {file}: {atts[k].get('path')!r} is not a plain file in an "
            "instance's folder or the operator's inbox")
        return None
    data = path.read_bytes()
    name = atts[k].get("name") or path.name
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
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length else b""
        r = respond(Request(method, self.path, dict(self.headers.items()), body))
        self.send_response(r.status)
        for k, v in r.headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(r.body)))
        self.end_headers()
        self.wfile.write(r.body)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")


def serve() -> int:
    reachable = set(LLM_PORTS) | {EMBED_PORT}
    if PORT in reachable:
        print(f"SHAFT_WEB_PORT={PORT} is a port every run may reach ({sorted(reachable)}): choose another")
        return 2
    if not allowed_logins():
        print("no SHAFT_API_LOGINS — run: shaft api on (it records your tailnet login)")
        return 2
    from .version import record_service
    record_service("web", {"port": PORT, "https_port": HTTPS_PORT})
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
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
    print(f"paired browsers: {len(ds)}")
    for k, v in sorted(ds.items(), key=lambda kv: kv[1].get("created", "")):
        print(f"  {k}  {v.get('name')}  paired {v.get('created', '')[:16]}  last seen {v.get('last_seen', '')[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(serve())
