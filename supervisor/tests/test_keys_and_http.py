"""An instance's own keys and http_request (the operator, 2026-10-13: "they should have their own key and be able
to manage them"). The instance stores and names its keys; only the supervisor ever handles a value."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import broker as broker_mod
from supervisor import core, sandbox
from supervisor.broker import Broker

TOKEN = "ghs_test_only"


class FakeAPI:
    def __init__(self):
        self.seen = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                  "body": self.rfile.read(n).decode() if n else ""})
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://elsewhere.example/steal")
                    self.end_headers()
                    return
                code = 404 if self.path == "/missing" else 200
                body = json.dumps({"you_sent": self.headers.get("Authorization"), "path": self.path}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-RateLimit-Remaining", "59")
                self.send_header("Set-Cookie", "session=secret")
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class KeysAndHttpTest(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-proj-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.root = self.project / "instances" / "wren"
        self.root.mkdir(parents=True)
        for p in (mock.patch.object(core, "SUP_DIR", self.project / ".supervisor"),
                  mock.patch.object(broker_mod, "resolve_public", lambda host, port: ["127.0.0.1"])):   # a local fake API
            p.start()
            self.addCleanup(p.stop)
        self.api = FakeAPI()
        self.addCleanup(self.api.close)
        self.b = Broker("wren", self.project, {}, self.project / "broker.log")

    def call(self, tool, **args):
        return self.b.call(tool, args)

    def test_get_shows_status_useful_headers_and_body_and_api_errors_are_data(self):
        out = self.call("http_request", url=self.api.base + "/repos/x")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["result"].startswith("HTTP 200"))
        self.assertIn("X-RateLimit-Remaining: 59", out["result"])
        self.assertNotIn("Set-Cookie", out["result"])
        missing = self.call("http_request", url=self.api.base + "/missing")
        self.assertTrue(missing["ok"])
        self.assertTrue(missing["result"].startswith("HTTP 404"))

    def test_post_goes_out_without_an_operator_allowlist(self):
        # the operator, 2026-10-14: writes are not gated per host any more (wren needed POST to api.github.com to mint
        # a GitHub App token). A POST to a host nobody allowed must be sent, not refused.
        out = self.call("http_request", url=self.api.base + "/issues", method="POST", body={"title": "hi"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(json.loads(self.api.seen[-1]["body"]), {"title": "hi"})
        self.assertEqual(self.api.seen[-1]["headers"]["Content-Type"], "application/json")

    def test_own_key_is_added_by_the_supervisor_and_never_shown(self):
        out = self.call("secret_set", name="github", value=TOKEN, hosts=["127.0.0.1"])
        self.assertTrue(out["ok"], out)
        self.assertNotIn(TOKEN, out["result"])
        listing = self.call("secret_list")["result"]
        self.assertIn("github: hosts 127.0.0.1", listing)
        self.assertNotIn(TOKEN, listing)
        got = self.call("http_request", url=self.api.base + "/user", auth="github")
        self.assertEqual(self.api.seen[-1]["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, got["result"])                                # the API echoed it: masked
        self.assertIn("[your key]", got["result"])
        self.assertNotIn(TOKEN, (self.project / "broker.log").read_text())   # never logged
        mode = stat.S_IMODE(os.stat(self.root / "secrets" / "secrets.json").st_mode)
        self.assertEqual(mode, 0o600)
        self.assertTrue(self.call("secret_delete", name="github")["ok"])
        self.assertEqual(self.call("secret_list")["result"], "no keys yet")

    def test_a_key_goes_only_to_its_hosts_and_never_through_a_redirect(self):
        self.call("secret_set", name="github", value=TOKEN, hosts=["api.github.com"])
        out = self.call("http_request", url=self.api.base + "/user", auth="github")
        self.assertFalse(out["ok"])
        self.assertIn("may only be sent to api.github.com", out["error"])
        self.assertEqual(self.api.seen, [])
        self.call("secret_set", name="github", value=TOKEN, hosts=["127.0.0.1"])
        out = self.call("http_request", url=self.api.base + "/redirect", auth="github")
        self.assertTrue(out["result"].startswith("HTTP 302"), out)
        self.assertIn("Location: http://elsewhere.example/steal", out["result"])
        self.assertEqual(len(self.api.seen), 1)                               # not followed

    def test_no_symlinked_key_store(self):
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "secrets.json").symlink_to(self.project / "elsewhere.json")
        out = self.call("secret_set", name="github", value=TOKEN, hosts=["api.github.com"])
        self.assertFalse(out["ok"])
        self.assertFalse((self.project / "elsewhere.json").exists())


class TailnetGrantTest(unittest.TestCase):
    """SHAFT_TAILNET_GRANTS (the operator, 2026-11-01: thelos needs a granted service on box, a tailnet host): a granted
    host:port opens for that instance's keyed http_request only. Before it, every tailnet address was refused
    (BlockedAddress 100.100.1.2) and nothing else may have changed: no other instance, port, tool, unkeyed request,
    non-tailnet address or sandbox socket gets through."""
    HOST, TAILNET_IP = "box.tailnet-name.ts.net", "100.100.1.2"

    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-proj-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        (self.project / "instances" / "wren").mkdir(parents=True)
        self.api = FakeAPI()
        self.addCleanup(self.api.close)
        self.port = self.api.server.server_address[1]
        self.url = f"http://{self.HOST}:{self.port}/mcp"
        self.answer = self.TAILNET_IP   # what the tailnet's DNS gives for HOST
        real_resolve, real_connect = broker_mod.socket.getaddrinfo, broker_mod.socket.create_connection

        def resolve(host, port, *a, **k):
            if host == self.HOST:
                answers = self.answer if isinstance(self.answer, list) else [self.answer]
                return [(2, 1, 6, "", (ip, port)) for ip in answers]
            return real_resolve(host, port, *a, **k)

        def connect(address, *a, **k):   # the tailnet address is the fake API on this Mac
            return real_connect(("127.0.0.1", address[1]) if address[0] == self.TAILNET_IP else address, *a, **k)

        for p in (mock.patch.object(core, "SUP_DIR", self.project / ".supervisor"),
                  mock.patch.object(broker_mod.socket, "getaddrinfo", resolve),
                  mock.patch.object(broker_mod.socket, "create_connection", connect),
                  mock.patch.object(broker_mod, "TAILNET_GRANTS",
                                    broker_mod.tailnet_grants(json.dumps({"wren": [f"{self.HOST}:{self.port}"]})))):
            p.start()
            self.addCleanup(p.stop)
        self.b = Broker("wren", self.project, {}, self.project / "broker.log")
        self.assertTrue(self.b.call("secret_set", {"name": "service", "value": TOKEN, "hosts": [self.HOST]})["ok"])

    def request(self, url=None, **args):
        return self.b.call("http_request", {"url": url or self.url, "method": "POST", "body": {"x": 1}, **args})

    def test_granted_host_with_a_key_is_reached_and_the_key_is_masked(self):
        out = self.request(auth="service")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["result"].startswith("HTTP 200"), out)
        self.assertEqual(self.api.seen[-1]["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(self.api.seen[-1]["headers"]["Host"], f"{self.HOST}:{self.port}")
        self.assertNotIn(TOKEN, out["result"])
        self.assertEqual(broker_mod._granted.get(), frozenset())   # closed again after the request

    def test_without_a_key_the_grant_is_refused_and_says_why(self):
        out = self.request()
        self.assertFalse(out["ok"])
        self.assertIn("give auth", out["error"])
        self.assertEqual(self.api.seen, [])

    def test_the_grant_is_closed_again_after_a_failing_request(self):
        (self.project / "instances" / "wren" / "body.txt").write_text("hello")
        out = self.request(auth="service", body_file="body.txt")
        self.assertFalse(out["ok"])
        self.assertIn("give body or body_file, not both", out["error"])
        self.assertEqual(broker_mod._granted.get(), frozenset())
        out = self.b.call("fetch_page", {"url": self.url})
        self.assertFalse(out["ok"])
        self.assertIn("non-public address", out["error"])

    def test_another_instance_is_still_refused(self):
        other = Broker("thelos", self.project, {}, self.project / "broker.log")
        (self.project / "instances" / "thelos").mkdir()
        other.call("secret_set", {"name": "service", "value": TOKEN, "hosts": [self.HOST]})
        out = other.call("http_request", {"url": self.url, "auth": "service"})
        self.assertFalse(out["ok"])
        self.assertIn(f"non-public address {self.TAILNET_IP}", out["error"])
        self.assertEqual(self.api.seen, [])

    def test_another_port_on_the_granted_host_is_still_refused(self):
        out = self.request(url=f"http://{self.HOST}:{self.port + 1}/mcp", auth="service")
        self.assertFalse(out["ok"])
        self.assertIn("non-public address", out["error"])

    def test_a_granted_name_that_resolves_off_the_tailnet_is_still_refused(self):
        self.answer = "127.0.0.1"   # a grant opens the tailnet, never this Mac or the LAN
        out = self.request(auth="service")
        self.assertFalse(out["ok"])
        self.assertIn("non-public address 127.0.0.1", out["error"])
        self.assertEqual(self.api.seen, [])

    def test_fetch_page_never_uses_the_grant(self):
        out = self.b.call("fetch_page", {"url": self.url})
        self.assertFalse(out["ok"])
        self.assertIn("non-public address", out["error"])
        self.assertEqual(self.api.seen, [])

    def test_a_granted_name_with_a_tailnet_and_a_loopback_answer_is_refused(self):
        for answers in ([self.TAILNET_IP, "127.0.0.1"], ["127.0.0.1", self.TAILNET_IP]):
            with self.subTest(answers=answers):
                self.answer = answers
                out = self.request(auth="service")
                self.assertFalse(out["ok"])
                self.assertIn("non-public address 127.0.0.1", out["error"])
                self.assertEqual(self.api.seen, [])

    def test_the_sandbox_egress_proxy_still_refuses_the_granted_host(self):
        from supervisor import egress
        with self.assertRaisesRegex(egress.Refused, "tailnet are not reachable"):
            egress.public_address(self.HOST, self.port)

    def test_bad_grants_are_refused_when_set(self):
        for bad in ('["x"]', '{"Bad Name": ["a:1"]}', '{"wren": "a:443"}', '{"wren": ["a"]}', '{"wren": ["a:0"]}',
                    '{"wren": [":443"]}', '{"wren": ["box..:443"]}', '{"wren": ["[fd7a::1]:443"]}',
                    '{"wren": ["a b:443"]}', '{"wren": ["-x.ts.net:443"]}', '{"wren": [443]}'):
            with self.assertRaises(ValueError, msg=bad):
                broker_mod.tailnet_grants(bad)
        self.assertEqual(broker_mod.tailnet_grants('{"wren": ["Box.TS.net.:443"]}'), {"wren": {("box.ts.net", 443)}})


class KeyStoreDurabilityTest(unittest.TestCase):
    """B10 (review 2026-10-24): read_secrets read an unparseable store as {}, _write_secrets truncated the file and
    wrote it in place, and store/delete were unlocked read-modify-writes: a torn write, then any `shaft secret set`,
    left one key out of all of them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-keys-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "wren"
        self.root.mkdir()
        for p in (mock.patch.object(core, "SUP_DIR", self.tmp / ".supervisor"), mock.patch.object(core, "log")):
            p.start()
            self.addCleanup(p.stop)
        core.store_secret(self.root, "github", TOKEN, ["api.github.com"])
        self.store = self.root / "secrets" / "secrets.json"

    def test_an_unreadable_store_is_never_written_over(self):
        torn = self.store.read_text()[:25]                    # what a crash mid-write used to leave
        self.store.write_text(torn)
        with self.assertRaises(ValueError) as ctx:            # core.KeyStoreUnreadable
            core.store_secret(self.root, "mail", "other-value", ["api.example.com"])
        self.assertEqual(type(ctx.exception).__name__, "KeyStoreUnreadable")
        self.assertIn(str(self.store), str(ctx.exception))
        self.assertIn("JSONDecodeError", str(ctx.exception))
        with self.assertRaises(ValueError):
            core.delete_secret(self.root, "github")
        self.assertEqual(self.store.read_text(), torn)        # nothing written over it
        self.assertEqual(core.read_secrets(self.root), {})    # a use of a key: tolerant, but logged loudly
        self.assertIn("KEY STORE UNREADABLE", core.log.call_args.args[0])

    def test_a_crash_while_writing_leaves_the_old_store_whole(self):
        real = json.dumps

        def power_cut(obj, *a, **k):
            if "mail" in obj:
                raise OSError("power cut")
            return real(obj, *a, **k)
        with mock.patch.object(core.json, "dumps", side_effect=power_cut), self.assertRaises(OSError):
            core.store_secret(self.root, "mail", "other-value", ["api.example.com"])
        self.assertEqual(sorted(core.read_secrets(self.root)), ["github"])
        self.assertEqual(sorted(p.name for p in self.store.parent.iterdir()), ["secrets.json"])   # no temp file left
        core.store_secret(self.root, "mail", "other-value", ["api.example.com"])
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o600)

    def test_two_changes_at_once_both_survive(self):
        real = core.read_secrets

        def slow(root, *a, **k):                               # both read before either writes, without the lock
            got = real(root, *a, **k)
            threading.Event().wait(0.3)
            return got
        with mock.patch.object(core, "read_secrets", side_effect=slow):
            ts = [threading.Thread(target=core.store_secret, args=(self.root, k, "v-" + k, ["h.example.com"]))
                  for k in ("one", "two")]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(sorted(core.read_secrets(self.root)), ["github", "one", "two"])


class ThroughTheInstancesClientTest(unittest.TestCase):
    def test_secret_set_as_the_instance_calls_it(self):
        # Regression (wren, 2026-10-13): "ERROR: TypeError: call() got multiple values for argument 'name'" — the
        # instance's broker.call(name, **args) took secret_set's own `name` argument as the tool name. The
        # broker-level tests above called the broker directly and never went through the instance's client.
        project = Path(tempfile.mkdtemp(prefix="shaft-proj-")).resolve()
        self.addCleanup(shutil.rmtree, project, True)
        (project / "instances" / "wren").mkdir(parents=True)
        with mock.patch.object(core, "SUP_DIR", project / ".supervisor"):
            b = Broker("wren", project, {}, project / "broker.log")
            base = b.start()
            self.addCleanup(b.stop)
            template = Path(__file__).resolve().parents[2] / "template"
            sys.path.insert(0, str(template))
            from shaft.tools import Toolbox
            with mock.patch.dict(os.environ, {"SHAFT_BROKER_URL": base, "SHAFT_BROKER_TOKEN": b.token}):
                box = Toolbox(project / "instances" / "wren")
                out = box.run("secret_set", json.dumps({"name": "github", "value": TOKEN, "hosts": ["api.github.com"]}))
                listing = box.run("secret_list", "{}")
                gone = box.run("secret_delete", json.dumps({"name": "github"}))
        self.assertTrue(out.startswith("stored your key 'github'"), out)
        self.assertIn("github: hosts api.github.com", listing)
        self.assertIn("deleted", gone)
        self.assertNotIn(TOKEN, json.dumps(box.log))


class KeysOutOfReachTest(unittest.TestCase):
    """The instance's own process, its plugins and `run` cannot read the key file; nor can the coder."""

    def setUp(self):
        home = Path(os.path.expanduser("~")).resolve()
        project = Path(__file__).resolve().parents[2]
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-keys-", dir=project / ".supervisor")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root, self.home = self.tmp / "inst", home
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "notes").mkdir()
        core._write_secrets(self.root, {"github": {"value": TOKEN, "hosts": ["api.github.com"]}})

    def sandboxed(self, prof: str, script: str) -> subprocess.CompletedProcess:
        path = self.tmp / "p.sb"
        path.write_text(prof)
        return subprocess.run(sandbox.wrap(path, [core.PYTHON, "-E", "-s", "-c", script]), cwd=self.root,
                              capture_output=True, text=True)

    def test_agent_and_plugins_cannot_read_it_but_can_list_their_folder(self):
        r = self.sandboxed(sandbox.profile(self.root, self.home, [8780]),
                           "import pathlib,sys\n"
                           "try:\n print(open('secrets/secrets.json').read())\n"
                           "except PermissionError: print('DENIED')\n"
                           "print(sorted(p.name for p in pathlib.Path('.').rglob('*') if 'secrets' not in p.parts))")
        self.assertIn("DENIED", r.stdout, r.stderr)
        self.assertNotIn(TOKEN, r.stdout)
        self.assertIn("notes", r.stdout)           # walking its own folder still works

    def test_run_cannot_read_it(self):
        r = self.sandboxed(sandbox.run_profile(self.root, self.home),
                           "import subprocess; print(subprocess.run(['cat','secrets/secrets.json'],"
                           "capture_output=True,text=True))")
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        self.assertIn("Operation not permitted", r.stdout)

    def test_the_coder_never_sees_it(self):
        from supervisor import coder
        self.assertIn("not readable", coder._read_only_tool(self.root, "read_file", {"path": "secrets/secrets.json"}))
        self.assertNotIn("secrets", coder._read_only_tool(self.root, "list_files", {"path": "."}))


class InstanceSideTest(unittest.TestCase):
    TEMPLATE = Path(__file__).resolve().parents[2] / "template"

    def test_key_values_stay_out_of_the_tool_log_and_git(self):
        sys.path.insert(0, str(self.TEMPLATE))
        from shaft.tools import _loggable, Toolbox
        logged = _loggable("secret_set", json.dumps({"name": "github", "value": TOKEN, "hosts": ["api.github.com"]}))
        self.assertNotIn(TOKEN, logged)
        self.assertIn("github", logged)
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        (root / "secrets").mkdir()
        self.assertIn("handled by the supervisor", Toolbox(root).run("read_file", '{"path": "secrets/secrets.json"}'))
        self.assertIn("secrets/", (self.TEMPLATE / ".gitignore").read_text().split())


class OperatorCommandTest(unittest.TestCase):
    def test_shaft_secret_set_from_stdin_never_prints_the_value(self):
        tmp = Path(tempfile.mkdtemp(prefix="shaft-sec-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "wren"
        root.mkdir()
        with mock.patch("supervisor.__main__.instance_root", return_value=root), \
             mock.patch("sys.stdin", new=__import__("io").StringIO(TOKEN + "\n")), \
             mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out, \
             mock.patch("supervisor.__main__.log"):
            from supervisor import __main__ as cli
            code = cli.main(["secret", "wren", "set", "github", "--hosts", "api.github.com"])
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertEqual(core.read_secrets(root)["github"]["value"], TOKEN)


if __name__ == "__main__":
    unittest.main()
