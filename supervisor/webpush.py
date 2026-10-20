"""Web Push for the web UI (docs/plans/PLAN-pwa P4): "thelos replied" on a phone whose app is closed or locked.

An iPhone stops a page's scripts while its app is in the background, so nothing in the page can say a reply landed.
Only a push can: the browser's push service (Apple's for an iPhone, iOS 16.4+, Home Screen apps only) wakes the app's
service worker, which shows the notification.

What goes out, and how:
  - A push carries no content. The service worker, woken by it, fetches what to show (/push/latest) from this server
    over the tailnet, with the device's own cookie. So Apple's (or Google's, Mozilla's) server sees only that a push
    was sent, never the text. (Encrypting a payload — RFC 8291 — needs AES-GCM and ECDH, which neither the stdlib nor
    the system's LibreSSL command line offers; a push with no payload needs neither.)
  - Each push is signed for its push service (VAPID, RFC 8292): an ES256 JSON Web Token. The key pair is made once with
    /usr/bin/openssl (LibreSSL, on every Mac) and kept in .supervisor/web/vapid.pem (0600); signing also runs openssl.
    This is the one place SHAFT's stdlib-only rule calls a system binary.
  - Subscriptions are per paired device (.supervisor/web/push.json), only to the known push services' hosts; one a
    push service says is gone (404, 410) is dropped.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import core
from .core import log

OPENSSL = "/usr/bin/openssl"
TTL_S = 3600                                 # a push not delivered within an hour is not worth delivering
# The push services a subscription may point at: an endpoint is where this server will send a request, so it is never
# an address a page could choose freely.
PUSH_HOSTS = ("web.push.apple.com", ".push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com",
              ".notify.windows.com")
SUBJECT = "https://github.com/dgrassi1984/shaft"   # VAPID's contact: a URL, not anyone's address

_lock = threading.Lock()


class PushError(Exception):
    pass


def _dir() -> Path:
    return core.SUP_DIR / "web"                  # read at call time: a test's folder, never the real one


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# -- the key --------------------------------------------------------------------------------------------------------
def _key_path() -> Path:
    return _dir() / "vapid.pem"


def ensure_key() -> Path:
    path = _key_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".vapid.{os.getpid()}.pem")
        r = subprocess.run([OPENSSL, "ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", str(tmp)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise PushError(f"openssl could not make the push key: {r.stderr.strip()}")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        log("web: made the Web Push key (.supervisor/web/vapid.pem)")
    return path


def public_key() -> str:
    """The key's public point (65 bytes, uncompressed), base64url: the page's applicationServerKey."""
    r = subprocess.run([OPENSSL, "ec", "-in", str(ensure_key()), "-pubout", "-outform", "DER"], capture_output=True)
    if r.returncode != 0 or len(r.stdout) < 65:
        raise PushError(f"openssl could not read the push key: {r.stderr.decode(errors='replace').strip()}")
    return b64url(r.stdout[-65:])


def _der_to_raw(sig: bytes) -> bytes:
    """An ECDSA signature as DER (what openssl writes) to r || s, 32 bytes each (what JWS ES256 wants)."""
    if len(sig) < 8 or sig[0] != 0x30:
        raise PushError("not a DER signature")
    at = 2 if sig[1] < 0x80 else 2 + (sig[1] & 0x7f)
    out = b""
    for _ in range(2):
        if sig[at] != 0x02:
            raise PushError("not a DER signature")
        n = sig[at + 1]
        out += sig[at + 2:at + 2 + n].lstrip(b"\0").rjust(32, b"\0")
        at += 2 + n
    return out


def vapid_token(endpoint: str, now: float | None = None) -> str:
    """The JWT a push service checks: who it is for (the endpoint's origin), until when, and whose key signed it."""
    u = urllib.parse.urlsplit(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + 12 * 3600), "sub": SUBJECT}
    signing = (b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "."
               + b64url(json.dumps(claims, separators=(",", ":")).encode()))
    r = subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(ensure_key())], input=signing.encode(),
                       capture_output=True)
    if r.returncode != 0:
        raise PushError(f"openssl could not sign: {r.stderr.decode(errors='replace').strip()}")
    return signing + "." + b64url(_der_to_raw(r.stdout))


# -- subscriptions --------------------------------------------------------------------------------------------------
def _subs_path() -> Path:
    return _dir() / "push.json"


def subscriptions() -> dict:
    try:
        return json.loads(_subs_path().read_text())
    except (OSError, ValueError):
        return {}


def _save(subs: dict) -> None:
    path = _subs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".push.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(subs, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def allowed_endpoint(endpoint: str) -> bool:
    u = urllib.parse.urlsplit(endpoint)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h.lstrip(".") or (h.startswith(".") and host.endswith(h))
                                       for h in PUSH_HOSTS)


def subscribe(device: str, sub: dict) -> None:
    """Record this device's push subscription (the browser's PushSubscription as JSON)."""
    endpoint = str(sub.get("endpoint") or "")
    if not allowed_endpoint(endpoint):
        raise PushError(f"not a known push service: {urllib.parse.urlsplit(endpoint).hostname or endpoint[:60]!r}")
    with _lock:
        subs = subscriptions()
        subs[device] = {"endpoint": endpoint, "since": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _save(subs)
    log(f"web: device {device} subscribed to reply notifications ({urllib.parse.urlsplit(endpoint).hostname})")


def holds(device: str) -> bool:
    """SHAFT has a subscription for this paired device: what the web UI's "Notifications on" means (#23)."""
    return device in subscriptions()


def unsubscribe(device: str) -> bool:
    with _lock:
        subs = subscriptions()
        gone = subs.pop(device, None) is not None
        if gone:
            _save(subs)
    return gone


# -- sending --------------------------------------------------------------------------------------------------------
def _send_one(device: str, endpoint: str) -> str:
    req = urllib.request.Request(endpoint, data=b"", method="POST", headers={
        "TTL": str(TTL_S), "Urgency": "high", "Content-Length": "0",
        "Authorization": f"vapid t={vapid_token(endpoint)}, k={public_key()}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"{r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):                    # the browser dropped it: so do we
            unsubscribe(device)
            return f"{e.code}: gone, removed"
        return f"{e.code}: {e.read()[:200].decode(errors='replace')}"
    except (OSError, urllib.error.URLError) as e:
        return f"not sent: {e}"


def notify(why: str) -> None:
    """Wake every subscribed device (in a thread: a reply's end never waits on a push service)."""
    subs = subscriptions()
    if not subs:
        return

    def run():
        for device, s in subs.items():
            try:
                said = _send_one(device, s["endpoint"])
            except PushError as e:
                said = f"not sent: {e}"
            log(f"web: push to device {device} ({why}): {said}")
    threading.Thread(target=run, daemon=True, name="webpush").start()


def fingerprint() -> str:
    """A short id of the key, for the page: a subscription made with another key is renewed."""
    return hashlib.sha256(public_key().encode()).hexdigest()[:12]
