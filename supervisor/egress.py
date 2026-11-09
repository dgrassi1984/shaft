"""An instance's way to the internet: an HTTP proxy the supervisor runs for the length of one run.

Instances may reach the network freely, as long as nothing of the operator's is reachable. macOS's
sandbox can filter connections only as "localhost" or "anywhere", so open sockets would also reach their own
services — dev servers, databases, the LAN and the tailnet. Instead the sandbox allows one local port, this proxy,
and the proxy connects only to public addresses: it resolves the name itself and connects to the address it
checked (no second lookup an attacker's DNS could answer differently). uv, pip, urllib, requests, httpx, curl and
git all follow HTTPS_PROXY / HTTP_PROXY; anything that opens its own sockets is refused by the sandbox.

Every connection is logged, one JSON line in .supervisor/egress/<name>.jsonl: when, where, bytes each way, or why it
was refused. A trial replay's proxy refuses everything (a replay never sends: TLS hides whether a request writes).
"""
from __future__ import annotations

import base64
import ipaddress
import json
import secrets
import select
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .jsonl import jsonl_lines

CONNECT_TIMEOUT_S = 20
IDLE_TIMEOUT_S = 300     # a tunnel with no traffic either way this long is closed
CHUNK = 65536


class Refused(Exception):
    pass


def log_path(name: str) -> Path:
    """Where an instance's connections are logged (read by record(kind='egress') and the panel)."""
    from .core import SUP_DIR
    return SUP_DIR / "egress" / f"{name}.jsonl"


def public_address(host: str, port: int) -> list[str]:
    """The addresses to connect to for host:port — refused unless every address the name resolves to is public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise Refused(f"cannot resolve {host}: {e}") from e
    if not infos:
        raise Refused(f"cannot resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise Refused(f"{host} is a non-public address ({ip}): localhost, your LAN and the tailnet are not "
                          f"reachable from your sandbox")
    return list(dict.fromkeys(info[4][0] for info in infos))


class Egress:
    """One run's proxy. `refuse(host, port)` may return a reason to refuse a destination (trial replays)."""

    def __init__(self, name: str, log_path: Path, refuse: Callable[[str, int], str | None] | None = None,
                 on_refused: Callable[[str, int, str], None] | None = None):
        self.name, self.log_path = name, Path(log_path)
        self.refuse = refuse or (lambda host, port: None)
        self.on_refused = on_refused or (lambda host, port, why: None)
        self.token = secrets.token_hex(16)
        self.server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self.server.server_address[1] if self.server else 0

    @property
    def url(self) -> str:
        return f"http://shaft:{self.token}@127.0.0.1:{self.port}"

    def env(self) -> dict[str, str]:
        """What a sandboxed process needs to use it. The run's own local services (broker, model) stay direct."""
        out = {}
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            out[k] = out[k.lower()] = self.url
        out["NO_PROXY"] = out["no_proxy"] = "127.0.0.1,localhost"
        return out

    def start(self) -> "Egress":
        egress = self

        class Handler(BaseHTTPRequestHandler):
            rbufsize = 0                    # nothing read ahead: after the headers, the socket is the client's
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _fail(self, code: int, why: str, extra: dict | None = None) -> None:
                body = (why + "\n").encode()
                self.send_response(code)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def _authorised(self) -> bool:
                given = self.headers.get("Proxy-Authorization", "")
                want = "Basic " + base64.b64encode(f"shaft:{egress.token}".encode()).decode()
                if secrets.compare_digest(given.strip(), want):
                    return True
                self._fail(407, "proxy credentials missing or wrong (use HTTPS_PROXY as given)",
                           {"Proxy-Authenticate": 'Basic realm="shaft"'})
                return False

            def _open(self, host: str, port: int) -> socket.socket | None:
                try:
                    why = egress.refuse(host, port)
                    if why:
                        raise Refused(why)
                    addresses = public_address(host, port)
                except Refused as e:
                    egress._record(host, port, refused=str(e))
                    egress.on_refused(host, port, str(e))
                    self._fail(403, f"refused: {e}")
                    return None
                last_error = None
                for address in addresses:
                    try:
                        return socket.create_connection((address, port), timeout=CONNECT_TIMEOUT_S)
                    except OSError as e:
                        last_error = e
                egress._record(host, port, refused=f"connection failed: {last_error}")
                self._fail(502, f"cannot connect to {host}:{port}: {last_error}")
                return None

            def do_CONNECT(self):
                if not self._authorised():
                    return
                host, _, port = self.path.rpartition(":")
                host = host.strip("[]")
                if not host or not port.isdigit():
                    return self._fail(400, f"CONNECT needs host:port, not {self.path!r}")
                upstream = self._open(host, int(port))
                if upstream is None:
                    return
                self.send_response(200, "Connection established")
                self.end_headers()
                self.wfile.flush()
                up, down = egress._pump(self.connection, upstream)
                egress._record(host, int(port), up=up, down=down)
                self.close_connection = True

            def _forward(self):
                """Plain http:// through the proxy: the request is sent on with its path only, then relayed."""
                if not self._authorised():
                    return
                parts = urlsplit(self.path)
                if parts.scheme != "http" or not parts.hostname:
                    return self._fail(400, "only absolute http:// URLs here; https goes through CONNECT")
                port = parts.port or 80
                upstream = self._open(parts.hostname, port)
                if upstream is None:
                    return
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                head = [f"{self.command} {path} HTTP/1.1"]
                for k, v in self.headers.items():
                    if k.lower() not in ("proxy-authorization", "proxy-connection", "connection", "keep-alive"):
                        head.append(f"{k}: {v}")
                head.append("Connection: close")
                upstream.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
                up, down = egress._pump(self.connection, upstream)
                egress._record(parts.hostname, port, up=up, down=down, method=self.command)
                self.close_connection = True

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _forward

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    # -- traffic -------------------------------------------------------------------------------------------
    @staticmethod
    def _pump(client: socket.socket, upstream: socket.socket) -> tuple[int, int]:
        """Relay both ways until either side closes or the link is idle. Returns (bytes up, bytes down)."""
        counts = {client: 0, upstream: 0}
        other = {client: upstream, upstream: client}
        try:
            while True:
                ready, _, broken = select.select([client, upstream], [], [client, upstream], IDLE_TIMEOUT_S)
                if broken or not ready:
                    break
                for s in ready:
                    data = s.recv(CHUNK)
                    if not data:
                        return counts[client], counts[upstream]
                    other[s].sendall(data)
                    counts[s] += len(data)
        except OSError:
            pass
        finally:
            upstream.close()
        return counts[client], counts[upstream]

    def _record(self, host: str, port: int, **fields) -> None:
        row = {"ts": datetime.now().isoformat(timespec="seconds"), "host": host, "port": port, **fields}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.log_path.open("a") as f:
            f.write(json.dumps(row) + "\n")


def recent(log_path: Path, limit: int = 20, before: str | None = None) -> list[dict]:
    """The last connections logged (optionally only those before an ISO time)."""
    try:
        lines = jsonl_lines(log_path.read_text(errors="replace"))[-20000:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if before is None or str(r.get("ts", "")) < before:
            rows.append(r)
    return rows[-limit:]


def describe(rows: list[dict]) -> str:
    out = []
    for r in rows:
        where = f"{r.get('host')}:{r.get('port')}"
        if r.get("refused"):
            out.append(f"{r.get('ts', '?')[:16]} REFUSED {where} — {r['refused']}")
        else:
            m = f"{r['method']} " if r.get("method") else ""
            out.append(f"{r.get('ts', '?')[:16]} {m}{where} · up {r.get('up', 0)} B · down {r.get('down', 0)} B")
    return "\n".join(out)



def summary(rows: list[dict], day: str) -> str:
    """One line for the operator's panel: the day's connections, the busiest hosts, the refusals."""
    from collections import Counter
    rows = [r for r in rows if str(r.get("ts", "")).startswith(day)]
    if not rows:
        return "no connections today"
    hosts = Counter(r.get("host") for r in rows if not r.get("refused"))
    refused = sum(1 for r in rows if r.get("refused"))
    busiest = ", ".join(f"{h} ×{n}" for h, n in hosts.most_common(4))
    return f"{len(rows)} today" + (f" · {busiest}" if busiest else "") + (f" · {refused} refused" if refused else "")
