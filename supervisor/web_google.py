"""Phone consent uses a Web application client, never the desktop loopback client. Pending consent lives only in
this server process for ten minutes; restarting cancels it. Tokens record client '<name>:web', which google._client
resolves to GMAIL_<NAME>_WEB_CLIENT_ID/_SECRET on refresh without changing the shared refresh API.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
from datetime import datetime

from . import core, google

_pending: dict[str, dict] = {}
_lock = threading.Lock()
CODE = re.compile(r"[a-z_]{1,64}")   # an OAuth error code (access_denied, redirect_uri_mismatch): safe to show


def _google_code(e: urllib.error.HTTPError) -> str:
    """The `error` field of Google's JSON answer, when it is a bare code; never its description or anything else:
    a remote answer may echo what it was sent."""
    try:
        code = json.loads(e.read() or b"{}").get("error", "")
    except (ValueError, AttributeError, OSError):
        return ""
    return code if isinstance(code, str) and CODE.fullmatch(code) else ""


def setup(client: str, redirect: str) -> str:
    keys = google.client_keys(client + ":web")
    return (f"Create an OAuth client of type Web application in client {client}'s same Google Cloud project. "
            f"Register the authorized redirect URI {redirect}. Set {keys[0]} and {keys[1]} in .env.")


def route(req):
    """Called only after the normal tailnet and paired-cookie gates. Google cannot carry the page's CSRF token:
    callback state bound to that paired browser session is its CSRF protection. Pop before any validation or network
    call so concurrent callbacks, errors and mismatched browsers cannot replay it. Never print Google's payloads.
    GET start is a same-origin navigation from Access; callback permits Google's cross-site navigation.
    """
    from . import web
    if req.path not in ("/google/login", "/google/callback") or req.method != "GET":
        return None
    redirect = web.url() + "/google/callback"
    if req.path == "/google/login":
        if req.headers.get("sec-fetch-site") != "same-origin":
            return web._refused("Start Google login from the Access page.")
        alias, name = req.query.get("alias", ""), req.query.get("name", "")
        saved = google.account(alias)
        if not saved or name not in web._names():
            return web._refused("Unknown Google account or instance.")
        client = (saved.get("client") or alias).removesuffix(":web")
        env = core.secrets_env()
        if not all(env.get(k) for k in google.client_keys(client + ":web")):
            return web._refused(setup(client, redirect))
        cid, _ = google._client(client + ":web", env)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        with _lock:
            now = time.time()
            for key in list(_pending):
                if _pending[key]["expires"] <= now:
                    del _pending[key]
            _pending[state] = {"alias": alias, "name": name, "device": req.device, "verifier": verifier,
                               "expires": now + 600, "client": client, "address": saved["address"], "redirect": redirect}
        query = {"client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(google.SCOPES),
                 "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
                 "login_hint": saved["address"], "state": state, "code_challenge_method": "S256",
                 "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()}
        return web.Response(303, b"", {"Location": google.AUTH_URL + "?" + urllib.parse.urlencode(query)})
    with _lock:
        pending = _pending.pop(req.query.get("state", ""), None)
    if not pending or pending["expires"] <= time.time() or pending["device"] != req.device:
        return web._refused("Invalid, expired or already used Google login state. Nothing saved.")
    try:
        if req.query.get("error") or not req.query.get("code"):
            code = req.query.get("error", "")
            raise google.AccountError("consent refused" + (f" ({code})" if CODE.fullmatch(code) else ""))
        client = pending["client"] + ":web"
        cid, secret = google._client(client, core.secrets_env())
        tok = google._post_form(google.TOKEN_URL, {"client_id": cid, "client_secret": secret,
                               "code": req.query["code"], "code_verifier": pending["verifier"],
                               "grant_type": "authorization_code", "redirect_uri": pending["redirect"]})
        if not tok.get("refresh_token") or not tok.get("scope"):
            raise google.AccountError("No refresh token or scopes")
        granted = set(str(tok["scope"]).split())
        address = google._signed_in_as(tok["access_token"], granted)
        if address.casefold() != pending["address"].casefold():
            raise google.AccountError(f"Different account: signed in as {address}, not {pending['address']}")
        # A deleted/replaced account must not be resurrected by an older pending login.
        current = google.account(pending["alias"])
        if not current or current.get("address", "").casefold() != pending["address"].casefold():
            raise google.AccountError("Account changed")
        saved = {"address": address, "client": client, "refresh_token": tok["refresh_token"],
                 "scope": " ".join(sorted(granted)), "created": datetime.now().isoformat(timespec="seconds")}
        google._save_account(pending["alias"], saved)
        web.log(f"Google account {pending['alias']} logged in as {address} (client {client}, granted "
                f"{google.scope_text(saved)}) by the operator")
        result = "Google login saved. Granted: " + google.scope_text(saved)
    except Exception as e:
        # Never silent (review): a wrong redirect URI or an unadded scope must be diagnosable. Only our own refusals
        # (AccountError, written here) and Google's bare error code are shown; a remote answer may echo a code or a
        # token, so its text reaches neither the page nor supervisor.log.
        if isinstance(e, google.AccountError):
            why = str(e)
        elif isinstance(e, urllib.error.HTTPError):
            code = _google_code(e)
            why = f"Google refused the token exchange (HTTP {e.code}{': ' + code if code else ''})"
        else:
            why = type(e).__name__
        web.log(f"Google login for {pending['alias']} failed: {why}; nothing saved")
        result = f"Google login failed: {why}. Nothing saved; start again from Access."
    return web.Response(303, b"", {"Location": f"/i/{pending['name']}/access?" + urllib.parse.urlencode({"google": result})})
