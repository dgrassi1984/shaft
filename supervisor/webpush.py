"""Web Push for the web UI: "tundra replied" on a phone whose app is closed or locked.

An iPhone stops a page's scripts while its app is in the background, so nothing in the page can say a reply landed.
Only a push can: the browser's push service (Apple's for an iPhone, iOS 16.4+, Home Screen apps only) wakes the app's
service worker, which shows the notification.

What goes out, and how:
  - A push carries no content. The service worker, woken by it, fetches what to show (/push/latest) from this server
    over the tailnet, with the device's own cookie. So Apple's (or Google's, Mozilla's) server sees only that a push
    was sent, never the text. What to show is queued per device when the push is sent (.supervisor/web/push-queue.json)
    and /push/latest hands the device what is waiting for it (take): two pushes in quick succession show two
    notifications, each its own (it showed "the newest reply" for both: issue #23), and a wake that failed is shown with
    the next one. Only a same-origin request takes them (the service worker's fetch). Chat replies (web_chat) and the instances' messages routed
    to the web (channel.py, forwarded by the web service: web_alerts.py) both go through notify(). (Encrypting a payload — RFC 8291 — needs AES-GCM and ECDH, which neither the stdlib nor
    the system's LibreSSL command line offers; a push with no payload needs neither.)
  - Each push is signed for its push service (VAPID, RFC 8292): an ES256 JSON Web Token. The key pair is made once with
    /usr/bin/openssl (LibreSSL, on every Mac) and kept in .supervisor/web/vapid.pem (0600); signing also runs openssl.
    APNs also signs with openssl and sends through curl for HTTP/2 (apns.py).
  - Subscriptions are per paired device (.supervisor/web/push.json), only to the known push services' hosts; one a
    push service says is gone (404, 410) or refuses (401, 403: made with a key this server no longer signs with) is
    dropped, and so is a device's when it is unpaired. Each records the key it was made for (fingerprint): /push/status
    hands the page its endpoint and the key now, and the page subscribes again when either differs.
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
    try:
        key = fingerprint()
    except PushError:                             # no key to name: the next status asks for a fresh subscription
        key = ""
    with _lock:
        subs = subscriptions()
        subs[device] = {"endpoint": endpoint, "since": time.strftime("%Y-%m-%dT%H:%M:%S"), "key": key}
        _save(subs)
    log(f"web: device {device} subscribed to reply notifications ({urllib.parse.urlsplit(endpoint).hostname})")


def holds(device: str) -> bool:
    """SHAFT has a subscription for this paired device: what the web UI's "Notifications on" means (#23)."""
    return device in subscriptions()


def status(device: str) -> dict:
    """What /push/status tells the page: whether SHAFT holds this device, the endpoint it holds, the key pushes are
    signed with now, and whether the subscription was made for another key (`stale`). The page compares them with its
    browser's subscription and subscribes again on a difference: after the key changed, or the browser rotated its
    endpoint, every push failed while the button still said on (W5, review 2026-10-24)."""
    sub = subscriptions().get(device) or {}
    try:
        key, now = public_key(), fingerprint()
    except PushError as e:
        return {"subscribed": bool(sub), "endpoint": sub.get("endpoint", ""), "key": "", "stale": False,
                "error": str(e)}
    # No key recorded (a subscription from before keys were): unknown, not stale — else every device renewed at once
    # after the update. The page still compares its browser's own applicationServerKey with `key`.
    return {"subscribed": bool(sub), "endpoint": sub.get("endpoint", ""), "key": key,
            "stale": bool(sub.get("key")) and sub["key"] != now}


def unsubscribe(device: str, endpoint: str | None = None) -> bool:
    """Drop this device's subscription and what waits for it. With `endpoint`: only if that is still the one SHAFT
    holds — a wake retrying an old endpoint for two minutes must not drop the subscription the page renewed meanwhile
    (Grok review of 8735c62)."""
    with _lock:
        subs = subscriptions()
        if endpoint is not None and (subs.get(device) or {}).get("endpoint") != endpoint:
            return False
        gone = subs.pop(device, None) is not None
        if gone:
            _save(subs)
        queue = _queue()
        if queue.pop(device, None) is not None:
            _save_queue(queue)
    return gone


# -- what each push shows -------------------------------------------------------------------------------------------
QUEUE_MAX = 20                               # per device: older ones are dropped (a phone off for a day)
NOTHING = {"title": "SHAFT", "body": "Open SHAFT to see what is new", "url": "/", "tag": "shaft", "cid": None}


def _queue_path() -> Path:
    return _dir() / "push-queue.json"


def _queue() -> dict:
    try:
        data = json.loads(_queue_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_queue(queue: dict) -> None:
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".push-queue.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(queue, indent=1, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _fields(event: dict) -> dict:
    return {**{k: event[k] for k in ("title", "body", "url", "tag")}, "cid": event.get("cid")}


def take(device: str, instance: str | None = None, now: float | None = None) -> list[dict]:
    """What this device shows for the push that woke it: every event still queued for it that its push service could
    still deliver (TTL_S), oldest first, taken off the queue. All of them, not one: a wake that failed (the network, a
    push service's error) left its event behind, and the next wake shows it too, beside its own (Codex review,
    2026-10-20). With nothing queued — an earlier wake showed it all — the last one shown again under its own tag
    (it replaces itself), since iOS shows something for every push; with nothing ever, a plain one. With an instance,
    take its thread and From URLs and fall back only to its last event; other instances stay queued."""
    now = time.time() if now is None else now
    with _lock:
        queue = _queue()
        mine = queue.get(device) or {}
        pending = [e for e in mine.get("pending") or [] if now - e.get("at", 0) <= TTL_S]
        def matches(event):
            path = urllib.parse.urlsplit(event["url"]).path
            return instance is None or path in (f"/i/{instance}/chat", f"/i/{instance}/from")
        fresh = [e for e in pending if matches(e)]
        lasts = mine.get("last_by_url") or {}
        if previous := mine.get("last"):
            lasts.setdefault(previous["url"], previous)
        for event in fresh:
            lasts[event["url"]] = event
        fallback = (max((e for e in reversed(list(lasts.values())) if matches(e)),
                        key=lambda e: e.get("at", 0), default=None) if instance is not None else mine.get("last"))
        last = fresh[-1] if fresh else fallback
        if last:
            queue[device] = {"pending": [e for e in pending if not matches(e)],
                             "last": fresh[-1] if fresh else mine.get("last"), "last_by_url": lasts}
            _save_queue(queue)
    return [_fields(e) for e in fresh] if fresh else [_fields(last)] if last else [dict(NOTHING)]


# -- sending --------------------------------------------------------------------------------------------------------
def _send_one(device: str, endpoint: str) -> str:
    req = urllib.request.Request(endpoint, data=b"", method="POST", headers={
        "TTL": str(TTL_S), "Urgency": "high", "Content-Length": "0",
        "Authorization": f"vapid t={vapid_token(endpoint)}, k={public_key()}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"{r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (404, 410, 401, 403):
            # 404/410: the browser dropped it, so do we. 401/403: made for a key we no longer sign with, dead too (W5):
            # the page subscribes again. Either way only that endpoint: one renewed since is the device's now.
            why = "gone" if e.code in (404, 410) else f"refused ({e.read()[:200].decode(errors='replace')})"
            if unsubscribe(device, endpoint):
                return f"{e.code}: {why}, removed"
            return f"{e.code}: {why}, an old endpoint (the device has renewed it): stopped"
        return f"{e.code}: {e.read()[:200].decode(errors='replace')}"
    except (OSError, urllib.error.URLError) as e:
        return f"not sent: {e}"


def _paired() -> set[str] | None:
    """The paired browsers' ids (web.py's devices.json, read at call time like every file here); None when it cannot
    be read — then nothing is dropped for it: a read that failed must never unsubscribe every phone."""
    try:
        data = json.loads((_dir() / "devices.json").read_text())
    except (OSError, ValueError) as e:
        log(f"web: cannot read the paired devices ({type(e).__name__}: {e}): pushing to every subscription")
        return None
    return set(data) if isinstance(data, dict) else None


def notify(event: dict, skip=frozenset()) -> list[str]:
    """Queue `event` ({title, body, url, tag, cid}: what the notification shows) for every Web Push or configured APNs device
    still paired and not in `skip` (the devices that have the chat on screen: web_chat.watchers), and wake each (in a thread:
    a reply's end never waits on a push service). A subscription of a device no longer paired is dropped: a revoked
    phone kept getting a push for every reply (W1, review 2026-10-24). Returns the devices it woke."""
    from . import apns
    subs, native, paired = subscriptions(), apns.devices(), _paired()
    for device in [d for d in subs if paired is not None and d not in paired]:
        unsubscribe(device)
        log(f"web: dropped the push subscription of device {device}: it is no longer paired")
    for device in [d for d in native if paired is not None and d not in paired]:
        apns.unregister(device)
        log(f"web: dropped the APNs token of device {device}: it is no longer paired")
    subs = {d: s for d, s in subs.items() if (paired is None or d in paired) and d not in skip}
    native = {d: s for d, s in native.items() if (paired is None or d in paired) and d not in skip}
    if native:
        reason = apns.configured()
        if reason is not None:
            log(f"web: {reason}")
            native = {}
    recipients = subs.keys() | native.keys()
    if not recipients:
        return []
    why = event["title"]
    with _lock:
        queue = _queue()
        for device in recipients:
            mine = queue.get(device) or {}
            mine["pending"] = [*(mine.get("pending") or []), {**event, "at": time.time()}][-QUEUE_MAX:]
            queue[device] = mine
        _save_queue(queue)
    _wake(subs, why)
    apns._wake(native, event)
    return sorted(recipients)


RETRY_S = (20, 120)                           # a wake that failed is tried again after these


def _wake(subs: dict, why: str) -> None:
    """One empty push to each device, in a thread; one that fails is tried again (RETRY_S) — its event waits in the
    queue either way, and shows with the next wake that gets through."""
    def run(device: str, endpoint: str):
        for wait in (0, *RETRY_S):
            time.sleep(wait)
            try:
                said = _send_one(device, endpoint)
            except PushError as e:
                said = f"not sent: {e}"
            log(f"web: push to device {device} ({why}): {said}")
            if said.startswith("2") or "removed" in said or "stopped" in said:   # sent, or dead (dropped or replaced)
                return
    for device, s in subs.items():                # one thread each: a device's retries never hold up another's
        threading.Thread(target=run, args=(device, s["endpoint"]), daemon=True, name="webpush").start()


def fingerprint() -> str:
    """A short id of the key, kept with each subscription: one made with another key is renewed (status)."""
    return hashlib.sha256(public_key().encode()).hexdigest()[:12]
