"""The egress proxy: instances reach the public internet, never anything of the operator's (the operator, 2026-10-14).

Before it, `run` had no network at all and the agent could reach only its local ports, so wren could not install
a crypto library and planned to hand-write RSA signing. Open sockets were not an option: macOS's sandbox filters
only "localhost" or "anywhere", which would have opened localhost, the LAN and the tailnet too.
"""
from __future__ import annotations

import base64
import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import broker, core, egress, sandbox
from supervisor.core import PROJECT

NAME = "wren"


class Upstream:
    """A local web server standing in for a public one (the proxy is told it resolved to it)."""

    def __init__(self):
        self.seen: list[dict] = []
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                up.seen.append({"path": self.path, "headers": dict(self.headers)})
                body = b"hello from upstream"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


REAL_PUBLIC_ADDRESS = egress.public_address


def pretend_public(host: str, port: int) -> str:
    """Any name resolves to the local stand-in; a literal local address is still refused, as in production."""
    if host in ("127.0.0.1", "localhost"):
        return REAL_PUBLIC_ADDRESS(host, port)
    return "127.0.0.1"


class PublicAddressTest(unittest.TestCase):
    def test_local_lan_tailnet_and_metadata_are_refused(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.20", "172.16.0.1", "100.101.102.103", "169.254.169.254",
                   "::1", "fd00::1"):
            with self.subTest(ip=ip), self.assertRaises(egress.Refused):
                egress.public_address(ip, 443)

    def test_a_public_name_that_resolves_to_a_private_address_is_refused(self):
        # DNS that points a public-looking name at the operator's LAN must not get through.
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 443))]
        with mock.patch.object(egress.socket, "getaddrinfo", return_value=fake), \
                self.assertRaises(egress.Refused) as e:
            egress.public_address("innocent.example", 443)
        self.assertIn("192.168.1.20", str(e.exception))

    def test_a_public_address_is_returned(self):
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]
        with mock.patch.object(egress.socket, "getaddrinfo", return_value=fake):
            self.assertEqual(egress.public_address("example.com", 443), "93.184.215.14")


class SummaryTest(unittest.TestCase):
    def test_todays_hosts_and_refusals(self):
        rows = [{"ts": "2026-10-13T23:59:00", "host": "old.example"},
                {"ts": "2026-10-14T09:00:00", "host": "pypi.org"}, {"ts": "2026-10-14T09:00:01", "host": "pypi.org"},
                {"ts": "2026-10-14T09:00:02", "host": "files.pythonhosted.org"},
                {"ts": "2026-10-14T09:01:00", "host": "192.168.1.2", "refused": "non-public"}]
        self.assertEqual(egress.summary(rows, "2026-10-14"),
                         "4 today · pypi.org ×2, files.pythonhosted.org ×1 · 1 refused")
        self.assertEqual(egress.summary(rows, "2026-10-15"), "no connections today")


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-egress-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "egress.jsonl"
        self.proxy = egress.Egress(NAME, self.log).start()
        self.addCleanup(self.proxy.stop)
        self.up = Upstream()
        self.addCleanup(self.up.close)

    def opener(self, token: str | None = None):
        url = self.proxy.url if token is None else f"http://shaft:{token}@127.0.0.1:{self.proxy.port}"
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))

    def rows(self) -> list[dict]:
        """The log, once the proxy has written a line of it: a connection is logged when it closes, which can be just
        after the client has its answer. Waiting for the file was not enough: open("a") makes it before the line is
        written, and a read in between found no row (the gate's parallel run, 2026-10-29)."""
        end = time.monotonic() + 3
        while time.monotonic() < end and not (self.log.exists() and self.log.read_text().endswith("\n")):
            time.sleep(0.02)
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_plain_http_is_relayed_and_logged_without_the_proxy_credentials(self):
        with mock.patch.object(egress, "public_address", pretend_public):
            body = self.opener().open(f"http://api.example:{self.up.port}/v1?x=1", timeout=10).read()
        self.assertEqual(body, b"hello from upstream")
        self.assertEqual(self.up.seen[0]["path"], "/v1?x=1")
        self.assertNotIn("Proxy-Authorization", self.up.seen[0]["headers"])   # the token never leaves the machine
        row = self.rows()[-1]
        self.assertEqual((row["host"], row["method"]), ("api.example", "GET"))
        self.assertGreater(row["down"], 0)

    def test_connect_tunnels_to_a_public_host(self):
        with mock.patch.object(egress, "public_address", pretend_public):
            s = socket.create_connection(("127.0.0.1", self.proxy.port), timeout=10)
            auth = base64.b64encode(f"shaft:{self.proxy.token}".encode()).decode()
            s.sendall(f"CONNECT api.example:{self.up.port} HTTP/1.1\r\nHost: api.example\r\n"
                      f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode())
            head = s.recv(4096)
            self.assertIn(b"200", head.split(b"\r\n")[0])
            s.sendall(b"GET /tunnelled HTTP/1.1\r\nHost: api.example\r\nConnection: close\r\n\r\n")
            data = b""
            while chunk := s.recv(4096):
                data += chunk
            s.close()
        self.assertIn(b"hello from upstream", data)
        self.assertEqual(self.up.seen[-1]["path"], "/tunnelled")

    def test_local_destinations_are_refused_and_never_contacted(self):
        for url in (f"http://127.0.0.1:{self.up.port}/", f"http://localhost:{self.up.port}/"):
            with self.subTest(url=url), self.assertRaises(urllib.error.HTTPError) as e:
                self.opener().open(url, timeout=10)
            self.assertEqual(e.exception.code, 403)
        self.assertEqual(self.up.seen, [])
        self.assertTrue(all("refused" in r for r in self.rows()))

    def test_wrong_or_missing_credentials_get_407(self):
        with mock.patch.object(egress, "public_address", pretend_public), \
                self.assertRaises(urllib.error.HTTPError) as e:
            self.opener(token="not-the-token").open(f"http://api.example:{self.up.port}/", timeout=10)
        self.assertEqual(e.exception.code, 407)
        self.assertEqual(self.up.seen, [])

    def test_env_keeps_the_runs_own_local_services_direct(self):
        env = self.proxy.env()
        self.assertEqual(env["HTTPS_PROXY"], self.proxy.url)
        self.assertEqual(env["https_proxy"], self.proxy.url)
        self.assertIn("127.0.0.1", env["NO_PROXY"])


@unittest.skipUnless(Path(sandbox.SANDBOX_EXEC).exists(), "macOS sandbox")
class SandboxedRunTest(unittest.TestCase):
    """Real sandboxed `run` commands: the internet through the proxy works, sockets of their own do not."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-egress-", dir=PROJECT / ".supervisor")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        self.root = self.project / "instances" / NAME
        for d in ("notes", ".tmp"):
            (self.root / d).mkdir(parents=True)
        p = mock.patch.object(core, "SUP_DIR", self.tmp / "sup")
        p.start()
        self.addCleanup(p.stop)
        self.b = broker.Broker(NAME, self.project, {}, self.tmp / "broker.log")
        self.b.start()
        self.addCleanup(self.b.stop)
        self.up = Upstream()
        self.addCleanup(self.up.close)

    def run_py(self, code: str) -> str:
        return self.b.call("run", {"command": f"python3 - <<'PY'\n{code}\nPY"})["result"]

    def test_http_through_the_proxy_reaches_a_public_host(self):
        with mock.patch.object(egress, "public_address", pretend_public):
            out = self.run_py(f"import urllib.request; print(urllib.request.urlopen("
                              f"'http://api.example:{self.up.port}/from-run', timeout=10).read().decode())")
        self.assertIn("hello from upstream", out)
        self.assertIn("api.example", egress.describe(egress.recent(egress.log_path(NAME))))
        record = self.b.call("record", {"kind": "egress"})["result"]
        self.assertIn(f"api.example:{self.up.port}", record)

    def test_direct_sockets_are_denied_by_the_sandbox(self):
        # Localhost (the operator's services) and a public address alike: only the proxy port is open.
        out = self.run_py(
            "import socket\n"
            f"for where in (('127.0.0.1', {self.up.port}), ('1.1.1.1', 80)):\n"
            "    try: socket.create_connection(where, timeout=3); print('OPEN', where)\n"
            "    except OSError as e: print('DENIED', where, e.errno)\n")
        self.assertNotIn("OPEN", out)
        self.assertEqual(out.count("DENIED"), 2, out)
        self.assertEqual(self.up.seen, [])

    def test_the_proxy_url_carries_the_egress_token_not_the_broker_token(self):
        out = self.b.call("run", {"command": "echo $HTTPS_PROXY"})["result"]
        self.assertIn(self.b.egress.token, out)
        self.assertNotIn(self.b.token, out)


class ReplayEgressTest(unittest.TestCase):
    """A trial replay never sends: its proxy refuses everything, and each refusal is traced for the judge."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-egress-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(core, "SUP_DIR", self.tmp / "sup")
        p.start()
        self.addCleanup(p.stop)
        state = self.tmp / "copy"
        state.mkdir()
        self.b = broker.ReplayBroker(NAME, self.tmp, {}, self.tmp / "broker.log", state_root=state, start_commit="0",
                                     as_of=datetime.now(), cache={}, cache_lock=threading.Lock(), billed="x@trial",
                                     tools_log=self.tmp / "tools.log")
        self.b.start()
        self.addCleanup(self.b.stop)
        self.up = Upstream()
        self.addCleanup(self.up.close)

    def test_every_connection_is_refused_and_traced(self):
        url = self.b.egress.url
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url}))
        with mock.patch.object(egress, "public_address", pretend_public), \
                self.assertRaises(urllib.error.HTTPError) as e:
            op.open(f"http://api.example:{self.up.port}/write", timeout=10)
        self.assertEqual(e.exception.code, 403)
        self.assertEqual(self.up.seen, [])
        net = [c for c in self.b.trace if c["tool"] == "network"]
        self.assertEqual(len(net), 1)
        self.assertIn("simulated: not sent", net[0]["tags"])
        self.assertFalse(egress.log_path(NAME).exists())   # the instance's own record is not the replay's


if __name__ == "__main__":
    unittest.main()
