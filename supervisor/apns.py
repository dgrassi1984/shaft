"""Native push: a title through APNs, the message fetched over the tailnet (/app/v1/push/latest).
Retries stop after unpair or token replacement. Provider-token rejection clears the JWT cache; dead device tokens,
including DeviceTokenNotForTopic, remove only the matching registration.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from . import core, webpush
from .core import knob, log

KEY_ID, _ = knob("SHAFT_APNS_KEY_ID", "", str.strip, "APNs signing key id; empty disables native push",
                 group="Ports and paths", services=("web", "runs"))
TEAM_ID, _ = knob("SHAFT_APNS_TEAM_ID", "", str.strip, "Apple developer team id for native push",
                  group="Ports and paths", services=("web", "runs"))
TOPIC, _ = knob("SHAFT_APNS_TOPIC", "", str.strip, "Native app bundle id used as the APNs topic",
                group="Ports and paths", services=("web", "runs"))
CURL = "/usr/bin/curl"
_lock = threading.Lock()
_token_lock = threading.Lock()
_cached: tuple = ()


def _key_path() -> Path:
    return core.SUP_DIR / "web" / "apns" / f"AuthKey_{KEY_ID}.p8"


def _config() -> str:
    return f"key id={KEY_ID!r}, team={TEAM_ID!r}, topic={TOPIC!r}, key path={str(_key_path())!r}"


def configured() -> str | None:
    missing = [name for name, value in (("SHAFT_APNS_KEY_ID", KEY_ID), ("SHAFT_APNS_TEAM_ID", TEAM_ID),
                                       ("SHAFT_APNS_TOPIC", TOPIC)) if not value]
    if not _key_path().is_file() or not os.access(_key_path(), os.R_OK):
        missing.append("readable APNs key file")
    return f"native push off: missing {', '.join(missing)} ({_config()})" if missing else None


def _path() -> Path:
    return core.SUP_DIR / "web" / "apns.json"


def devices() -> dict:
    try:
        data = json.loads(_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".apns.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def register(device: str, token: str, env: str, topic: str, platform: str) -> None:
    if not isinstance(token, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", token):
        raise ValueError("APNs token must be nonempty hex bytes")
    if env not in {"sandbox", "production"}:
        raise ValueError("APNs env must be sandbox or production")
    if topic != TOPIC:
        raise ValueError(f"APNs topic must equal SHAFT_APNS_TOPIC={TOPIC!r}")
    if platform not in {"ios", "macos"}:
        raise ValueError("APNs platform must be ios or macos")
    with _lock:
        data = devices()
        data[device] = {"token": token.lower(), "env": env, "topic": topic, "platform": platform,
                        "registered": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _save(data)


def unregister(device: str) -> bool:
    with _lock:
        data = devices()
        gone = data.pop(device, None) is not None
        if gone:
            _save(data)
    return gone


def status(device: str) -> dict:
    sub = devices().get(device) or {}
    reason = configured() or ("APNs topic changed: " + _config() if sub and sub["topic"] != TOPIC else "")
    reason = reason or ("" if sub else "device has no APNs token")
    return {"on": bool(sub) and not reason, "env": sub.get("env"), "reason": reason}


def provider_token(now: float | None = None) -> str:
    global _cached
    now = time.time() if now is None else now
    reason = configured()
    if reason:
        raise webpush.PushError(reason)
    st = _key_path().stat()
    identity = (KEY_ID, TEAM_ID, TOPIC, str(_key_path()), st.st_mtime_ns, st.st_size)
    with _token_lock:
        if _cached and _cached[0] == identity and 0 <= now - _cached[1] < 50 * 60:
            return _cached[2]
        head = {"alg": "ES256", "kid": KEY_ID}
        claims = {"iss": TEAM_ID, "iat": int(now)}
        signing = ".".join(webpush.b64url(json.dumps(v, separators=(",", ":")).encode()) for v in (head, claims))
        r = subprocess.run([webpush.OPENSSL, "dgst", "-sha256", "-sign", str(_key_path())],
                           input=signing.encode(), capture_output=True, timeout=15)
        if r.returncode != 0:
            raise webpush.PushError(f"openssl could not sign APNs token: {r.stderr.decode(errors='replace').strip()}")
        token = signing + "." + webpush.b64url(webpush._der_to_raw(r.stdout))
        _cached = (identity, now, token)
        return token


def _send_one(device: str, sub: dict, event: dict) -> str:
    global _cached
    try:
        reason = configured()
        if reason:
            raise webpush.PushError(reason)
        if sub["topic"] != TOPIC:
            raise webpush.PushError(f"registered topic={sub['topic']!r} differs from SHAFT_APNS_TOPIC")
        from .web_seen import badge                  # the API owns seen state; read the badge at each attempt
        parts = urllib.parse.urlsplit(event["url"]).path.split("/")
        instance = parts[2] if len(parts) > 2 and parts[1] == "i" else ""
        payload = {"aps": {"alert": {"title": event["title"]}, "badge": badge(device), "sound": "default",
                           "thread-id": instance, "mutable-content": 1}, "instance": instance}
        host = "api.sandbox.push.apple.com" if sub["env"] == "sandbox" else "api.push.apple.com"
        r = subprocess.run([CURL, "--http2", "--silent", "--show-error", "--max-time", "15", "--request", "POST",
                            "--header", f"authorization: bearer {provider_token()}",
                            "--header", f"apns-topic: {TOPIC}", "--header", "apns-push-type: alert",
                            "--header", "apns-priority: 10", "--header",
                            f"apns-expiration: {int(time.time()) + webpush.TTL_S}",
                            "--header", "content-type: application/json", "--data-binary", "@-",
                            "--write-out", "\n%{http_code}", f"https://{host}/3/device/{sub['token']}"],
                           input=json.dumps(payload).encode(), capture_output=True, timeout=20)
        if r.returncode != 0:
            raise webpush.PushError(f"curl: {r.stderr.decode(errors='replace').strip()}")
        body, _, code = r.stdout.decode(errors="replace").rpartition("\n")
        try:
            reason = json.loads(body).get("reason", "") if body else ""
        except ValueError:
            reason = body[:200]
        said = f"{code}: {reason}"
        if reason in {"ExpiredProviderToken", "InvalidProviderToken"}:
            with _token_lock:
                _cached = ()                         # keep the device, mint a new provider JWT on the next attempt
        if code == "410" or reason in {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}:
            with _lock:
                data = devices()
                if data.get(device) != sub:           # a failure for an old token must not drop its replacement
                    return said + ", an old token: stopped"
                del data[device]
                _save(data)
            return said + ", removed"
        if code.startswith("2"):
            return said
    except (OSError, subprocess.SubprocessError, webpush.PushError) as e:
        said = f"not sent: {e}"
    log(f"web: APNs to device {device}: {said} ({_config()})")
    return said


def _wake(subs: dict, event: dict) -> None:
    def run(device: str, sub: dict):
        for wait in (0, *webpush.RETRY_S):
            time.sleep(wait)
            from . import web
            if devices().get(device) != sub or device not in web.devices():
                return
            said = _send_one(device, sub, event)
            log(f"web: APNs to device {device} ({event['title']}): {said}")
            if said.startswith("2") or "removed" in said or "stopped" in said:
                return
    for device, sub in subs.items():
        threading.Thread(target=run, args=(device, sub), daemon=True, name="apns").start()
