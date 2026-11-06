"""The operator's Google accounts, shared by every Google service an instance may be granted (docs/plans/PLAN-calendar.md).

One login (`shaft google login`) covers Gmail reads and Calendar reads/writes (docs/plans/PLAN-calendar-write.md).
The supervisor keeps each account's refresh token in .supervisor/google/<alias>.json (0600, outside every sandbox) with the OAuth client that
issued it and the scopes Google actually granted; instances never hold a credential. mail.py and calendar.py enforce
their own grants on top of an `Account`; nothing here knows about labels or calendars.

An instance's own Google account (docs/plans/PLAN-own-mail.md: Thelos's Gmail; docs/plans/PLAN-own-drive.md: its Drive, Sheets, Docs and
Slides) is not the operator's: each login is a `google` key in the instance's key store (`instance_key_login`, one
bundle from KEY_SCOPES), refreshed by the broker (`refresh`) and added to its http_request calls like any key. It
may never be one of the operator's accounts (`operator_addresses`).

`urlopen` is the one door to Google; the tests put a fake Google behind it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import core

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR = "https://www.googleapis.com/auth/calendar.readonly"
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
DRIVE = "https://www.googleapis.com/auth/drive"
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
SHEETS = "https://www.googleapis.com/auth/spreadsheets"
DOCS = "https://www.googleapis.com/auth/documents"
SLIDES = "https://www.googleapis.com/auth/presentations"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"
# Operator consent; per-calendar write grants stay separate.
SCOPES = (GMAIL, CALENDAR, CALENDAR_EVENTS, DRIVE_READONLY)
SCOPE_NAMES = {GMAIL: "gmail.readonly", CALENDAR: "calendar.readonly", CALENDAR_EVENTS: "calendar.events",
               GMAIL_MODIFY: "gmail.modify", DRIVE_READONLY: "drive.readonly", DRIVE: "drive",
               SHEETS: "spreadsheets", DOCS: "documents", SLIDES: "presentations"}
# What an instance's own Google key may be granted (a bundle of scopes, all or nothing), and the hosts its token is
# good for (the key is bound to them).
# gmail.modify: read, send, label, trash; never a permanent delete, the settings or anything outside Gmail.
# drive (docs/plans/PLAN-own-drive.md): every file in its Drive and every file shared with it, Sheets, Docs and Slides; it can
# share, trash and permanently delete its own files. www.googleapis.com serves Drive v3 and its uploads (and other
# Google APIs, which refuse a token without their scope).
KEY_SCOPES = {"gmail.modify": ((GMAIL_MODIFY,), ("gmail.googleapis.com",)),
              "drive": ((DRIVE, SHEETS, DOCS, SLIDES),
                        ("www.googleapis.com", "sheets.googleapis.com", "docs.googleapis.com", "slides.googleapis.com"))}
GMAIL_PROFILE = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
DRIVE_ABOUT = "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)"
CALENDAR_PRIMARY = "https://www.googleapis.com/calendar/v3/calendars/primary"
ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
CLIENT_RE = ALIAS_RE                                 # an OAuth client's name (GMAIL_<NAME>_CLIENT_ID)
# Google's quotas are per user per minute, shared by every instance granted the account: Gmail's hit in M0 of
# docs/plans/PLAN-mail (2026-10-15). An answer over quota is waited out, Retry-After first, for this long at most.
QUOTA_WAIT_S = 60
urlopen = urllib.request.urlopen
sleep = time.sleep


class AccountError(RuntimeError):
    """Something only the operator can fix (the OAuth client, a login, a scope, a grant); the message names it."""


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Google answered HTTP {status}: {detail}")
        self.status = status


# -- accounts: one refresh token per alias, in the supervisor's folder ---------------------------------------------
def accounts_dir() -> Path:
    return core.SUP_DIR / "google"    # read at call time: tests move SUP_DIR


def old_accounts_dir() -> Path:
    """Where docs/plans/PLAN-mail kept the account files, before docs/plans/PLAN-calendar M1 (`shaft google migrate` moves them)."""
    return core.SUP_DIR / "mail"


def unmigrated() -> list[str]:
    """Account files still in the old folder: until moved, they are not read (one location, never two)."""
    d = old_accounts_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def account(alias: str) -> dict | None:
    if not ALIAS_RE.match(alias or ""):
        return None
    try:
        data = json.loads((accounts_dir() / f"{alias}.json").read_text())
        return data if isinstance(data, dict) and data.get("refresh_token") else None
    except (OSError, ValueError):
        return None


def accounts() -> dict[str, dict]:
    d = accounts_dir()
    return {p.stem: a for p in sorted(d.glob("*.json")) if (a := account(p.stem))} if d.is_dir() else {}


def scopes(data: dict) -> set[str]:
    """The scopes Google granted an account's token, as its login recorded them (space-separated, as Google says)."""
    return set(str(data.get("scope") or "").split())


def scope_text(data: dict) -> str:
    got = scopes(data)
    return ", ".join(SCOPE_NAMES[s] for s in SCOPES if s in got) or "none"


def login_command(alias: str, data: dict | None = None) -> str:
    data = data or account(alias) or {}
    client = (data.get("client") or "").removesuffix(":web")
    return (f"shaft google login {alias} {data.get('address') or '<address>'}"
            + (f" --client {client}" if client and client != alias else ""))


def missing_scope_message(alias: str, data: dict, scope: str) -> str:
    """Consent cannot grant a scope the project's Data Access list omits. Name both operator steps, for the CLI
    refusal and the Access page alike, using the account's project/client rather than a guessed default."""
    client = (data.get("client") or alias).removesuffix(":web")
    return (f"Google account {alias!r} lacks {SCOPE_NAMES.get(scope, scope)}. Add {scope} in Google Cloud → "
            f"Google Auth Platform → Data Access for client {client}'s project "
            "(https://console.cloud.google.com/auth/scopes), then Log in again on Access or run "
            f"`{login_command(alias, data)}` again and tick it.")


def _save_account(alias: str, data: dict) -> Path:
    d = accounts_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / f"{alias}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=1))
    os.chmod(path, 0o600)
    return path


def migrate() -> list[str]:
    """Move the account files from .supervisor/mail/ to .supervisor/google/ (0600 kept, the folder 0700), and remove
    the old folder once empty. An alias present in both with different contents is refused, both left as they are.
    Idempotent: a second run finds nothing to move. Returns the lines to print."""
    old, new = old_accounts_dir(), accounts_dir()
    if not old.is_dir():
        return [f"nothing to migrate: {old} does not exist"]
    out, clashes = [], []
    for src in sorted(old.glob("*.json")):
        dst = new / src.name
        if dst.exists():
            if dst.read_bytes() != src.read_bytes():
                clashes.append(src.stem)
                continue
            src.unlink()
            out.append(f"{src.stem}: already in {new}, the old copy removed")
            continue
        new.mkdir(parents=True, exist_ok=True)
        os.chmod(new, 0o700)
        os.replace(src, dst)
        os.chmod(dst, 0o600)
        out.append(f"{src.stem}: moved to {dst}")
    if clashes:
        raise AccountError(f"{', '.join(clashes)} exist in both {old} and {new} with different contents: nothing done "
                           f"for them; keep the right one (normally the newer login in {new}) and delete the other")
    try:
        old.rmdir()
        out.append(f"removed the empty {old}")
    except OSError:
        out.append(f"{old} is not empty (left as it is): {', '.join(p.name for p in old.iterdir())}")
    return out


def client_keys(client: str) -> tuple[str, str]:
    """The .env names of an OAuth client's ID and secret: GMAIL_<CLIENT>_CLIENT_ID / _CLIENT_SECRET (named in
    docs/plans/PLAN-mail, kept for every Google service: the .env is the operator's). Internal client names ending
    in :web select the separately configured Web application credentials, including when refreshing a saved login."""
    web = client.endswith(":web")
    client = client.removesuffix(":web")
    stem = f"GMAIL_{client.upper().replace('-', '_')}" + ("_WEB" if web else "")
    return f"{stem}_CLIENT_ID", f"{stem}_CLIENT_SECRET"


def _client(client: str, secrets_env: dict[str, str]) -> tuple[str, str]:
    """An account's OAuth client, by name. One per Google organisation at least: an Internal app (work.example) only
    signs in its own organisation's users, so a personal Google account needs its own client (an External app in
    production). A refresh token works only with the client that issued it, so each account records its own."""
    if not CLIENT_RE.fullmatch((client or "").removesuffix(":web")):
        raise AccountError(f"{client!r} is not an OAuth client name (lowercase letters, digits, -)")
    keys = client_keys(client)
    missing = [k for k in keys if not secrets_env.get(k)]
    if missing:
        raise AccountError(f"Google client {client!r} needs {' and '.join(missing)} in {core.PROJECT / '.env'} (the "
                           f"{'Web application' if client.endswith(':web') else 'Desktop'} OAuth client of its Google Cloud app: "
                           "README, Google accounts)")
    return secrets_env[keys[0]], secrets_env[keys[1]]


def _error_text(e: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(e.read() or b"{}")
    except ValueError:
        return e.reason or str(e.code)
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message") or json.dumps(err)[:300]
    return " ".join(str(x) for x in (err, body.get("error_description")) if x) or str(e.code)


def _over_quota(status: int, detail: str) -> bool:
    return status == 429 or (status == 403 and any(k in detail.casefold() for k in
            ("quota exceeded", "rate limit exceeded", "ratelimitexceeded")))


def _retry_after(e: urllib.error.HTTPError) -> float:
    try:
        return max(0.0, float((e.headers or {}).get("Retry-After") or 0))
    except ValueError:
        return 0.0


def refresh(client: str, refresh_token: str, secrets_env: dict[str, str], who: str, again: str) -> tuple[str, float]:
    """A fresh access token from a refresh token, with the OAuth client that issued it: (token, expiry as a Unix
    time). `who` names the login in an error and `again` is the command that redoes it; a refused refresh never
    comes back as an empty answer. The one refresh path, for operator accounts and instance keys alike."""
    cid, secret = _client(client, secrets_env)
    try:
        data = _post_form(TOKEN_URL, {"client_id": cid, "client_secret": secret, "grant_type": "refresh_token",
                                      "refresh_token": refresh_token})
    except urllib.error.HTTPError as e:
        raise AccountError(f"{who}: Google refused its login ({_error_text(e)}); the operator runs `{again}` "
                           "again") from None
    return data["access_token"], time.time() + int(data.get("expires_in") or 3600)


def operator_addresses() -> set[str]:
    """The addresses of the operator's own Google accounts (casefolded): never an instance's own key, which has no
    label check, so the operator's mailbox would be read whole (docs/plans/PLAN-own-mail.md, D2)."""
    return {str(a.get("address") or "").casefold() for a in accounts().values()} - {""}


def _post_form(url: str, form: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=30) as resp:
        return json.load(resp)


class Account:
    """One account's API session, for one broker run or one command, shared by its services: its access token is
    kept and refreshed when it expires (or when Google says it no longer holds)."""

    def __init__(self, alias: str, secrets_env: dict[str, str]):
        if not ALIAS_RE.match(alias or ""):
            raise AccountError(f"{alias!r} is not a Google account alias (lowercase letters, digits, -)")
        self.alias, self.secrets = alias, secrets_env
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()

    def _saved(self) -> dict:
        saved = account(self.alias)
        if not saved and self.alias in unmigrated():
            raise AccountError(f"Google account {self.alias!r} is not read: its file is still in {old_accounts_dir()} "
                               "(docs/plans/PLAN-mail's folder); the operator runs `shaft google migrate`")
        if not saved:
            raise AccountError(f"Google account {self.alias!r} is not logged in: the operator runs "
                               f"`shaft google login {self.alias} <address>`")
        return saved

    @property
    def address(self) -> str:
        return self._saved().get("address", "?")

    def require(self, scope: str) -> "Account":
        """This account, if its token was granted `scope`; otherwise the login command that grants it. Checked
        before any call to Google, so a missing scope never comes back as an empty result."""
        saved = self._saved()
        if scope not in scopes(saved):
            raise AccountError(f"Google account {self.alias!r} ({saved.get('address', '?')}) was not granted "
                               f"{SCOPE_NAMES.get(scope, scope)} (its login granted: {scope_text(saved)}); the operator "
                               f"runs `{login_command(self.alias, saved)}` again and ticks it")
        return self

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
            saved = self._saved()
            if not saved.get("client"):
                raise AccountError(f"Google account {self.alias!r} ({saved.get('address', '?')}) names no OAuth client; "
                                   f"the operator runs `shaft google login {self.alias} "
                                   f"{saved.get('address', '<address>')} --client <name>` again")
            self._token, self._expires = refresh(
                saved["client"], saved["refresh_token"], self.secrets,
                f"Google account {self.alias!r} ({saved.get('address', '?')})",
                login_command(self.alias, saved))
            return self._token

    def get(self, url: str, **params) -> dict:
        """GET one Google API URL as this account."""
        return self._request("GET", url, None, **params)

    def post(self, url: str, body: dict, **params) -> dict:
        return self._request("POST", url, body, **params)

    def patch(self, url: str, body: dict, *, etag: str = "", **params) -> dict:
        return self._request("PATCH", url, body, headers={"If-Match": etag} if etag else {}, **params)

    def _request(self, method: str, url: str, body: dict | None, *, headers: dict | None = None, **params) -> dict:
        """One door and the same refresh/quota handling for reads and writes (PLAN-calendar-write M0).
        Transport failures are not retried: an insert may already have reached Google."""
        return self._response(method, url, body, json.load, 30, headers=headers, **params)

    def download(self, url: str, max_bytes: int, timeout: int = 120, **params) -> bytes:
        """GET bytes, reading at most one byte beyond the cap to detect an oversized response."""
        if max_bytes < 0:
            raise ValueError("max_bytes must be nonnegative")

        def read(resp):
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"download is at least {len(data)} bytes, more than {max_bytes} bytes allowed")
            return data

        return self._response("GET", url, None, read, timeout, **params)

    def _response(self, method, url, body, read, timeout, *, headers=None, **params):
        """JSON and byte reads share refresh and quota handling; transport errors propagate unchanged."""
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")}, doseq=True)
        refreshed, waited, attempt = False, 0.0, 0
        while True:
            req = urllib.request.Request(url + (f"?{query}" if query else ""),
                                         data=json.dumps(body).encode() if body is not None else None, method=method,
                                         headers={"Authorization": f"Bearer {self.token()}",
                                                  "Content-Type": "application/json", **(headers or {})})
            try:
                with urlopen(req, timeout=timeout) as resp:
                    return read(resp)
            except urllib.error.HTTPError as e:
                detail = _error_text(e)
                if e.code == 401 and not refreshed:   # the access token expired early: refresh once
                    self._token, refreshed = "", True
                    continue
                if _over_quota(e.code, detail):
                    attempt += 1
                    pause = _retry_after(e) or min(2.0 ** attempt, 30.0)
                    if waited + pause <= QUOTA_WAIT_S:
                        sleep(pause)
                        waited += pause
                        continue
                    raise ApiError(e.code, f"{detail} — Google's per-minute quota for {self.alias}, still exceeded "
                                           f"after waiting {waited:.0f} s; try again in a minute") from None
                if e.code == 403 and "insufficient authentication scopes" in detail.lower():
                    raise AccountError(f"Google account {self.alias!r}: its token lacks the scope this needs "
                                       f"({detail}); the operator runs `{login_command(self.alias)}` again") from None
                raise ApiError(e.code, detail) from None


# -- login: the operator's consent, once per account, for every scope ---------------------------------------------
def _signed_in_as(access_token: str, granted: set[str]) -> str:
    """The account the token belongs to, read with a scope it was granted: Gmail's profile, else the primary
    calendar (whose ID is the account's address), else Drive's `about`."""
    if granted & {GMAIL, GMAIL_MODIFY}:
        url, read = GMAIL_PROFILE, lambda d: d.get("emailAddress")
    elif CALENDAR in granted:
        url, read = CALENDAR_PRIMARY, lambda d: d.get("id")
    elif granted & {DRIVE, DRIVE_READONLY}:
        url, read = DRIVE_ABOUT, lambda d: (d.get("user") or {}).get("emailAddress")
    else:
        raise AccountError("Google granted no Gmail, Calendar or Drive scope: nothing saved")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urlopen(req, timeout=30) as resp:
        return str(read(json.load(resp)) or "")


def login(alias: str, address: str, secrets_env: dict[str, str], client: str = "", open_url=None,
          timeout_s: int = 300) -> dict:
    """Run Google's consent flow for every scope in SCOPES with OAuth client `client` (default: the one named like
    the alias) on a loopback redirect, check the account is `address`, and keep its refresh token, the client that
    issued it and the scopes Google granted (the consent page lets the operator untick one). Nothing is saved unless
    every step succeeds."""
    client = client or alias
    if not ALIAS_RE.match(alias or ""):
        raise AccountError(f"{alias!r} is not an alias: lowercase letters, digits, -, starting with a letter (e.g. work)")
    data = consent(address, client, SCOPES, secrets_env, open_url=open_url, timeout_s=timeout_s)
    _save_account(alias, data)
    return data


def instance_key_login(address: str, client: str, scope: str, secrets_env: dict[str, str], open_url=None,
                       timeout_s: int = 300) -> dict:
    """The consent flow for an instance's own Google account (docs/plans/PLAN-own-mail.md, docs/plans/PLAN-own-drive.md): one bundle from
    KEY_SCOPES, every scope in it granted or nothing saved (the consent page lets the operator untick one); never an
    operator account (checked before the browser opens and again on the address Google signed in). Returns the
    key's fields for core.store_google_key; nothing is stored here."""
    if scope not in KEY_SCOPES:
        raise AccountError(f"an instance's Google key may be granted {', '.join(KEY_SCOPES)}, not {scope!r}")
    if (address or "").casefold() in operator_addresses():
        raise AccountError(f"{address} is one of your own Google accounts (shaft google): an instance never holds it "
                           "as a key; grant it labels or calendars instead (shaft mail, shaft calendar)")
    urls, hosts = KEY_SCOPES[scope]
    data = consent(address, client, urls, secrets_env, open_url=open_url, timeout_s=timeout_s, incremental=False)
    if data["address"].casefold() in operator_addresses():
        raise AccountError(f"signed in as {data['address']}, one of your own Google accounts: nothing saved")
    missing = [SCOPE_NAMES.get(u, u) for u in urls if u not in scopes(data)]
    if missing:
        bundle = f"; {scope} needs all of {', '.join(SCOPE_NAMES[u] for u in urls)}" if len(urls) > 1 else ""
        raise AccountError(f"Google did not grant {', '.join(missing)} (it granted {data['scope'] or 'nothing'}"
                           f"{bundle}): nothing saved; log in again and leave every box ticked")
    return {"kind": "google", **data, "hosts": list(hosts)}


def consent(address: str, client: str, wanted: tuple[str, ...], secrets_env: dict[str, str], open_url=None,
            timeout_s: int = 300, incremental: bool = True) -> dict:
    """Google's consent flow with OAuth client `client` for `wanted` scopes, on a loopback redirect with PKCE and a
    state check, signed in as `address`: {address, client, refresh_token, scope, created}. `incremental` keeps
    scopes this client was granted before (an operator login); an instance key asks only for what it needs."""
    import webbrowser
    if "@" not in (address or ""):
        raise AccountError(f"{address!r} is not an email address")
    cid, secret = _client(client, secrets_env)
    got: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if "code" in q or "error" in q:
                got.update(q)
                done.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("SHAFT: the login finished; you can close this tab.".encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(wanted),
        "include_granted_scopes": "true" if incremental else "false", "access_type": "offline", "prompt": "consent", "login_hint": address,
        "state": state, "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()})
    try:
        print(f"Sign in as {address} in the browser (it opens now; otherwise open this):\n{url}", flush=True)
        (open_url or webbrowser.open)(url)
        if not done.wait(timeout_s):
            raise AccountError(f"no answer from the browser in {timeout_s} s: nothing saved")
    finally:
        server.shutdown()
        server.server_close()
    if got.get("state") != state:
        raise AccountError("the browser's answer does not match this login (state): nothing saved")
    if got.get("error"):
        raise AccountError(f"Google refused the login: {got['error']}: nothing saved")
    try:
        tok = _post_form(TOKEN_URL, {"client_id": cid, "client_secret": secret, "code": got["code"],
                                     "code_verifier": verifier, "grant_type": "authorization_code",
                                     "redirect_uri": redirect})
    except urllib.error.HTTPError as e:
        raise AccountError(f"Google refused the login code ({_error_text(e)}): nothing saved") from None
    if not tok.get("refresh_token"):
        raise AccountError("Google gave no refresh token (remove SHAFT's access at myaccount.google.com/permissions "
                           "and log in again): nothing saved")
    if not tok.get("scope"):
        raise AccountError("Google did not say which scopes it granted: nothing saved")
    granted = set(str(tok["scope"]).split())
    me = _signed_in_as(tok["access_token"], granted)
    if me.casefold() != address.casefold():
        raise AccountError(f"signed in as {me or 'an unknown account'}, not {address}: nothing saved")
    return {"address": me, "client": client, "refresh_token": tok["refresh_token"],
            "scope": " ".join(s for s in sorted(granted)), "created": datetime.now().isoformat(timespec="seconds")}
