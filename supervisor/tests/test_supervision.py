"""Supervisor-side guarantees: broker safety, journal/core enforcement, measurement."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import broker, judge, metrics, runner


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-broker-")).resolve()
        (self.project / "instances" / "b" / "shaft").mkdir(parents=True)
        self.b = broker.Broker("a", self.project, {}, self.project / "broker.log")
        self.url = self.b.start()

    def tearDown(self):
        self.b.stop()
        shutil.rmtree(self.project, ignore_errors=True)

    def post(self, tool, args, token=None):
        req = urllib.request.Request(f"{self.url}/tools/{tool}", data=json.dumps(args).encode(),
                                     headers={"X-Shaft-Token": token if token is not None else self.b.token})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def test_refuses_private_addresses(self):
        # The broker runs outside the sandbox: an instance must not reach localhost or the LAN through it.
        for url in ("http://127.0.0.1:1337/v1/models", "http://localhost/", "http://10.0.0.1/",
                    "http://169.254.169.254/latest/meta-data", "file:///etc/passwd"):
            out = self.post("fetch_page", {"url": url})
            self.assertFalse(out["ok"], url)
            self.assertIn("BlockedAddress", out["error"])

    def test_requires_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("list_instances", {}, token="wrong")
        self.assertEqual(ctx.exception.code, 403)

    def test_no_daily_limit_on_operator_messages_by_default(self):
        """the operator, 2026-10-13: "No limit" (SHAFT_OPERATOR_PER_DAY sets one; the run's own cap of 10 stays)."""
        self.assertEqual(broker.OPERATOR_DAILY_LIMIT, 0)
        with mock.patch.object(broker, "notify"):
            ok = [self.post("send_message", {"to": "operator", "text": f"q{i}"})["ok"] for i in range(6)]
        self.assertEqual(ok, [True] * 6)

    def test_messages_delivered_and_operator_limited(self):
        self.assertTrue(self.post("send_message", {"to": "b", "text": "hi b"})["ok"])
        self.assertIn("hi b", next((self.project / "instances" / "b" / "inbox").glob("*.md")).read_text())
        self.assertFalse(self.post("send_message", {"to": "nobody", "text": "x"})["ok"])
        with mock.patch.object(broker, "notify"), mock.patch.object(broker, "OPERATOR_DAILY_LIMIT", 3):
            results = [self.post("send_message", {"to": "operator", "text": f"q{i}", "kind": "question"})["ok"]
                       for i in range(4)]
        self.assertEqual(results, [True] * 3 + [False])


class DnsRebindingTest(unittest.TestCase):
    """B4 (review 2026-10-24): check_public resolved the name and threw the answer away; urlopen resolved it again. A
    name answering public for the check and 127.0.0.1 for the connection reached the operator's own services."""

    def setUp(self):
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.reached = []
        test = self

        class Local(BaseHTTPRequestHandler):           # one of the operator's services, on this Mac
            def log_message(self, *a):
                pass

            def do_GET(self):
                test.reached.append((self.path, self.headers.get("Host")))
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"the operator's private data")

        self.local = ThreadingHTTPServer(("127.0.0.1", 0), Local)
        threading.Thread(target=self.local.serve_forever, daemon=True).start()
        self.addCleanup(self.local.server_close)
        self.addCleanup(self.local.shutdown)
        self.port = self.local.server_address[1]
        self.project = Path(tempfile.mkdtemp(prefix="shaft-rebind-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        (self.project / "instances" / "a").mkdir(parents=True)
        self.b = broker.Broker("a", self.project, {}, self.project / "broker.log")
        real, answers = socket.getaddrinfo, {"n": 0}

        def rebinding(host, port, *a, **k):            # public for the first lookup, localhost for every later one
            if host != "rebind.example":
                return real(host, port, *a, **k)
            answers["n"] += 1
            ip = "93.184.216.34" if answers["n"] == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        p = mock.patch.object(socket, "getaddrinfo", side_effect=rebinding)
        p.start()
        self.addCleanup(p.stop)
        self.answers = answers

    def test_a_name_that_rebinds_to_localhost_after_the_check_reaches_nothing(self):
        for tool in ("fetch_page", "http_request"):
            self.answers["n"] = 0
            out = self.b.call(tool, {"url": f"http://rebind.example:{self.port}/secret"})
            self.assertFalse(out["ok"], (tool, out))
            self.assertIn("BlockedAddress", out["error"])
            self.assertIn("127.0.0.1", out["error"])
        self.assertEqual(self.reached, [], "the request reached a service on localhost")

    def test_https_connects_to_the_checked_address_with_the_name_for_sni(self):
        import socket
        seen = {}

        class Context:                                  # stands in for the TLS context: records the name it verifies
            def wrap_socket(self, sock, server_hostname=None):
                seen["sni"] = server_hostname
                return sock

        class Sock:
            def setsockopt(self, *a):
                pass

        def connect(address, *a, **k):
            seen["address"] = address
            return Sock()
        conn = broker._PinnedHTTPS("rebind.example", 443, context=Context())
        with mock.patch.object(socket, "create_connection", side_effect=connect):
            conn.connect()
        self.assertEqual(seen, {"address": ("93.184.216.34", 443), "sni": "rebind.example"})
        self.assertEqual(self.answers["n"], 1, "the name was resolved again after the check")

    def test_a_redirect_to_a_name_that_rebinds_reaches_nothing_and_a_public_one_is_followed(self):
        """Codex on e77d5e5: a behavioural redirect test. hop.example answers public (93.184.216.34, which this test's
        network carries to a local server playing the public site) and redirects: to rebind.example, which answers
        public for the check and 127.0.0.1 for the connection; or to a page of its own."""
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        test, hosts = self, []

        class Hop(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hosts.append(self.headers.get("Host"))
                if self.path == "/final":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"the public page")
                    return
                self.send_response(302)
                self.send_header("Location", f"http://rebind.example:{test.port}/secret" if self.path == "/start"
                                 else f"http://hop.example:{hop.server_address[1]}/final")
                self.end_headers()

        hop = ThreadingHTTPServer(("127.0.0.1", 0), Hop)
        threading.Thread(target=hop.serve_forever, daemon=True).start()
        self.addCleanup(hop.server_close)
        self.addCleanup(hop.shutdown)
        lookup, connect = socket.getaddrinfo, socket.create_connection

        def resolve(host, port, *a, **k):
            if host == "hop.example":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
            return lookup(host, port, *a, **k)

        def carry(address, *a, **k):                          # the "public" site is the local hop server
            public = address[0] in ("93.184.216.34", "hop.example")   # (by name too: unpinned code connects by name)
            return connect(("127.0.0.1", address[1]) if public else address, *a, **k)
        with mock.patch.object(socket, "getaddrinfo", side_effect=resolve), \
                mock.patch.object(socket, "create_connection", side_effect=carry):
            for tool in ("fetch_page", "http_request"):
                self.answers["n"] = 0
                out = self.b.call(tool, {"url": f"http://hop.example:{hop.server_address[1]}/start"})
                self.assertFalse(out["ok"], (tool, out))
                self.assertIn("BlockedAddress", out["error"])
            self.assertEqual(self.reached, [], "a redirect reached a service on localhost")
            out = self.b.call("fetch_page", {"url": f"http://hop.example:{hop.server_address[1]}/go"})
        self.assertTrue(out["ok"], out)
        self.assertIn("the public page", out["result"])
        self.assertEqual(set(hosts), {f"hop.example:{hop.server_address[1]}"})   # the name, not the address

    def test_redirects_and_keyed_requests_use_the_pinned_connections(self):
        for opener in (broker._opener, broker._opener_no_redirects):
            kinds = {type(h) for h in opener.handlers}
            self.assertIn(broker._PinnedHTTPHandler, kinds)
            self.assertIn(broker._PinnedHTTPSHandler, kinds)
            self.assertFalse(kinds & {urllib.request.HTTPHandler, urllib.request.HTTPSHandler,
                                      urllib.request.ProxyHandler}, "an unpinned or proxied way out")


class EnforcementTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-enf-")).resolve()
        (self.root / "memory").mkdir()
        (self.root / "self").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        (runner.SUP_DIR / "core-changed-test-enf").unlink(missing_ok=True)

    def test_rewritten_journal_is_restored(self):
        j = self.root / "memory" / "journal.md"
        j.write_text("old\n")
        j.write_text("forged history\n")
        self.assertIsNotNone(runner.enforce_journal(self.root, "old\n"))
        self.assertEqual(j.read_text(), "old\n")
        self.assertEqual(len(list((self.root / "memory").glob("journal.rejected-*.md"))), 1)
        j.write_text("old\nappended\n")
        self.assertIsNone(runner.enforce_journal(self.root, "old\n"))

    def test_core_cannot_change_twice_within_window(self):
        p = self.root / "self" / "personality.md"
        p.write_text("# P\n\n## Core\n- curious [2026-10-12]\n\n## Surface\n- x [2026-10-12]\n")
        first = runner._core(self.root)
        self.assertIsNone(runner.enforce_core("test-enf", self.root, ""))           # first core: allowed
        p.write_text("# P\n\n## Core\n- reckless [2026-10-12]\n\n## Surface\n- y [2026-10-12]\n")
        self.assertIsNotNone(runner.enforce_core("test-enf", self.root, first))     # too soon: refused
        self.assertIn("curious", p.read_text())
        self.assertIn("- y [2026-10-12]", p.read_text())                            # surface untouched


class StatsTest(unittest.TestCase):
    def test_calibration_numbers(self):
        js = [{"episode_ts": f"2026-10-12T1{i}:00:00", "confidence": c, "claimed": cl, "criterion_met": m,
               "claim_supported": cl == m, "criterion_quality": "checkable", "failure_mode": "", "goal": "g",
               "reasoning": "r", "judge": judge.JUDGE_MODELS[0]}
              for i, (c, cl, m) in enumerate([(90, "yes", "no"), (90, "yes", "yes"), (20, "no", "no")])]
        with mock.patch.object(metrics, "judgments", return_value=js):
            text = metrics.stats_text("x")
        self.assertIn("too few (3) vs none", text)        # 3 verdicts: no rate from so few
        self.assertIn("My own claim matched the judge: 2/3", text)
        brier = ((0.9 - 0) ** 2 + (0.9 - 1) ** 2 + (0.2 - 0) ** 2) / 3
        self.assertIn(f"{brier:.2f}", text)
        self.assertIn("overconfident", text)
        self.assertIn("Calibration v1, older question 'probability of success'", text)   # A4: labelled, own lines
        self.assertIn("not pooled with v2", text)


class CloudTranslationTest(unittest.TestCase):
    def test_history_is_resent_verbatim_and_tool_results_lead(self):
        from supervisor import cloudllm
        original = [{"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "tool_use", "id": "t1", "name": "remember", "input": {"fact": "x"}}]
        remembered = {cloudllm._turn_key("", ["t1"]): original}
        body = {"model": "claude-haiku-5-5", "max_tokens": 100, "messages": [
            {"role": "system", "content": "S"}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "remember", "arguments": '{"fact": "x"}'}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "stored"},
            {"role": "user", "content": "[supervisor] wrap up"}]}
        out = cloudllm.to_anthropic(body, remembered)
        self.assertEqual(out["system"], "S")
        self.assertEqual(out["messages"][1]["content"], original)            # thinking block kept, unmodified
        last = out["messages"][2]
        self.assertEqual([b["type"] for b in last["content"]], ["tool_result", "text"])
        self.assertEqual(last["role"], "user")

    def test_disallowed_model_and_budget_refused_before_any_call(self):
        from supervisor import cloudllm
        with self.assertRaises(cloudllm.CloudError) as ctx:
            cloudllm.complete("x", {"model": "claude-fable-5-1", "messages": []}, {"ANTHROPIC_API_KEY": "key"}, {})
        self.assertEqual(ctx.exception.status, 403)
        with mock.patch.object(cloudllm, "spent_today", return_value=cloudllm.DAILY_USD):
            with self.assertRaises(cloudllm.CloudError) as ctx:
                cloudllm.complete("x", {"model": "claude-haiku-5-5", "messages": []}, {"ANTHROPIC_API_KEY": "key"}, {})
        self.assertEqual(ctx.exception.status, 429)


class RunToolTest(unittest.TestCase):
    """`run`: commands in a stricter sandbox — working areas only, no mind, no network, bounded."""

    def setUp(self):
        # Inside the home folder like real instances (regression: python3 failed there because path
        # resolution could not stat the parent directories; the /var temp dir hid it).
        base = Path(__file__).resolve().parents[2] / ".supervisor"
        base.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="test-run-", dir=base)).resolve()
        self.root = self.tmp / "instances" / "r"
        for d in ("shaft", "notes", "memory", "self"):
            (self.root / d).mkdir(parents=True)
        (self.root / "memory" / "journal.md").write_text("journal\n")
        self.outside = self.tmp / "outside.txt"
        self.outside.write_text("secret\n")
        self.b = broker.Broker("r", self.tmp, {}, self.tmp / "broker.log")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, command, **kw):
        out = self.b.call("run", {"command": command, **kw})
        self.assertTrue(out["ok"], out)
        return out["result"]

    def test_works_in_the_working_areas(self):
        r = self.run_cmd("echo hi > notes/x.txt && cat notes/x.txt && python3 -c 'print(6*7)'")
        self.assertTrue(r.startswith("exit 0"), r)
        self.assertIn("hi", r)
        self.assertIn("42", r)

    def test_heredocs_work(self):
        # Regression (2026-10-13): zsh writes here-document temp files to $TMPPREFIX (default /tmp/zsh), which the
        # run sandbox may not write; every heredoc failed ("can't create temp file for here document") and
        # shaft and wisp concluded the shell itself was crippled.
        r = self.run_cmd("cat <<'EOT'\nhello from a heredoc\nEOT\npython3 - <<'EOF'\nprint(6 * 7)\nEOF")
        self.assertTrue(r.startswith("exit 0"), r)
        self.assertIn("hello from a heredoc", r)
        self.assertIn("42", r)

    def test_cannot_touch_its_mind(self):
        r = self.run_cmd("echo forged > memory/journal.md; echo x > self/self.md; echo $?")
        self.assertEqual((self.root / "memory" / "journal.md").read_text(), "journal\n")
        self.assertFalse((self.root / "self" / "self.md").exists())

    def test_no_network_and_no_reading_outside(self):
        r = self.run_cmd(f"python3 -c \"import urllib.request; urllib.request.urlopen('http://example.com', timeout=5)\"")
        self.assertNotIn("exit 0", r.splitlines()[0])
        plan = Path(__file__).resolve().parents[2] / "README.md"   # in the home folder, outside the instance
        r = self.run_cmd(f"head -c 200 {plan}")
        # The file's own text, not the word "SHAFT": an app install lives in ~/SHAFT, so the refusal itself names it
        # (on such an install this test failed every gate until 2026-11-26).
        self.assertNotIn(next(line for line in plan.read_text().splitlines() if len(line) > 40)[:40], r)
        self.assertIn("Operation not permitted", r)

    def test_runaway_is_killed(self):
        r = self.run_cmd("sleep 30 & sleep 30", timeout_s=1)
        self.assertIn("killed after 1 s", r)


class AgentEnvironmentTest(unittest.TestCase):
    def test_no_inherited_llm_variable_reaches_instance_code(self):
        """B5 (review 2026-10-24): ENV_ALLOW_PREFIXES held "LLM_", so an LLM_API_KEY the shell or launchd exported
        reached the agent, its plugins and its tests. Only the names the supervisor passes on purpose get through."""
        from supervisor import sandbox
        env = sandbox.agent_env({"PATH": "/usr/bin", "LC_ALL": "en_US.UTF-8", "LLM_API_KEY": "sk-leaked-canary",
                                 "LLM_BASE_URL": "http://10.0.0.1/v1", "LLM_MODEL": "inherited"},
                                LLM_MODEL="the-trial-arm")
        self.assertNotIn("LLM_API_KEY", env)
        self.assertNotIn("LLM_BASE_URL", env)
        self.assertNotIn("sk-leaked-canary", json.dumps(env))
        self.assertEqual(env["LLM_MODEL"], "the-trial-arm")        # given by name (trial.py): still passed
        self.assertEqual(env["LC_ALL"], "en_US.UTF-8")


class AgentPythonOnPathTest(unittest.TestCase):
    def test_python3_in_agent_path_is_the_python_running_shaft(self):
        """2026-11-26: on an app install the launchd PATH put the Command Line Tools' Python 3.9 first, so a plugin
        that ran `python3` got 3.9 while the plugin itself ran on 3.12."""
        from supervisor import core, sandbox
        with tempfile.TemporaryDirectory() as d:
            bindir = Path(d) / ".runtime/python/cpython-3.12.14-macos-aarch64-none/bin"
            bindir.mkdir(parents=True)
            for name in ("python3", "python3.12"):
                (bindir / name).touch()
            with mock.patch.object(core, "PYTHON", str(bindir / "python3.12")):
                env = sandbox.agent_env({"PATH": "/x/.runtime/bin:/usr/bin:/bin"})
                self.assertEqual(env["PATH"], f"{bindir}:/x/.runtime/bin:/usr/bin:/bin")
                self.assertEqual(sandbox.agent_env(env)["PATH"], env["PATH"])   # not added twice

    def test_gate_copy_reads_the_installs_runtime(self):
        """2026-11-26: a gate's copy of the project has no .runtime, so its profiles gave the app-installed Python no
        read rule and every instance test failed at interpreter start. RUNTIME follows the Python running SHAFT."""
        from supervisor import core
        with tempfile.TemporaryDirectory() as d:
            python = Path(d) / "SHAFT/.runtime/python/cpython-3.12.14-macos-aarch64-none/bin/python3.12"
            with mock.patch.object(core, "PYTHON", str(python)):
                self.assertEqual(core._runtime_root(), Path(d) / "SHAFT/.runtime")
            with mock.patch.object(core, "PYTHON", "/opt/homebrew/opt/python@3.12/bin/python3.12"):
                self.assertEqual(core._runtime_root(), core.PROJECT / ".runtime")   # a Homebrew install: unchanged


class SymlinkEscapeTest(unittest.TestCase):
    """An instance may create symlinks in its own folder; the unsandboxed supervisor must never
    follow them when it writes (journal notes, inbox deliveries, stats, coder changes)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-link-")).resolve()
        self.project = self.tmp / "project"
        self.root = self.project / "instances" / "evil"
        (self.root / "shaft").mkdir(parents=True)
        (self.root / "memory").mkdir()
        self.victim = self.tmp / "victim.txt"
        self.victim.write_text("untouched\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_journal_note_does_not_follow_a_symlinked_journal(self):
        from supervisor import core
        (self.root / "memory" / "journal.md").symlink_to(self.victim)
        with mock.patch.object(core, "log") as log:   # logged, not raised: a run's settlement goes on (B1, 2026-10-24)
            core.journal_note(self.root, "x", "y")
        self.assertEqual(self.victim.read_text(), "untouched\n")
        self.assertIn("NOT written", log.call_args.args[0])

    def test_message_delivery_does_not_follow_a_symlinked_inbox(self):
        (self.root / "inbox").symlink_to(self.tmp, target_is_directory=True)
        b = broker.Broker("other", self.project, {}, self.tmp / "broker.log")
        out = b.call("send_message", {"to": "evil", "text": "hello"})
        self.assertFalse(out["ok"])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["broker.log", "project", "victim.txt"])

    def test_coder_changes_cannot_write_through_links(self):
        from supervisor import coder, core
        (self.root / "shaft" / "x.py").symlink_to(self.victim)
        with self.assertRaises(core.UnsafePath):
            core.safe_write(self.root, "shaft/x.py", "pwned")
        self.assertEqual(coder.check_targets([{"path": "self/self.md", "content": ""},
                                              {"path": "../x.py", "content": ""},
                                              {"path": "shaft/ok.py", "content": ""}]), ["self/self.md", "../x.py"])
        self.assertEqual(self.victim.read_text(), "untouched\n")


class FinetuneGuardTest(unittest.TestCase):
    def test_memorising_adapter_is_rejected_before_judging(self):
        # Regression: the first Bonsai adapter was "kept" by a noisy single-sample judgment although its
        # validation loss rose from 2.93 to 3.68 — it had memorised 5 conversations.
        from supervisor import finetune
        d = Path(tempfile.mkdtemp(prefix="shaft-ft-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "adapter_config.json").write_text(json.dumps({"history": {"val": [[0, 2.93], [60, 2.65], [120, 3.68]]}}))
        self.assertIn("2.93 to 3.68", finetune.overfit(d))
        (d / "adapter_config.json").write_text(json.dumps({"history": {"val": [[0, 2.93], [120, 2.40]]}}))
        self.assertIsNone(finetune.overfit(d))


class ProbationTest(unittest.TestCase):
    """Probation undoes a failed self-change by restoring exactly its files from git — on a real instance worktree,
    shaped like the three changes of 2026-10-13 whose `git revert` of the whole run commit failed: the run commit
    also appended to memory/episodes/*.jsonl, which every later run appends to again."""

    FILES = ["plugins/weather/main.py", "plugins/weather/plugin.json", "tests/test_weather.py"]

    def setUp(self):
        import subprocess
        self.sp = subprocess
        self.project = Path(tempfile.mkdtemp(prefix="shaft-prob-")).resolve()
        self.git(self.project, "init", "-q")
        self.git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "p"
        self.git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/p", str(self.root))
        for rel, text in {"memory/journal.md": "# j\n", "self/self.md": "me\n", "shaft/prompts.py": "GOOD = True\n",
                          "plugins/clock/main.py": "def run(): return 'now'\n",
                          "plugins/clock/plugin.json": '{"name": "clock"}\n'}.items():
            self.write(rel, text)
        self.cycles = 0
        self.episode("before")
        self.commit("good")
        # the run that made the change: plugin files, its test, and the run's own memory in one commit
        self.write("plugins/weather/main.py", "def run(**a): return 'sunny'\n")
        self.write("plugins/weather/plugin.json", '{"name": "weather"}\n')
        self.write("tests/test_weather.py", "# weather test\n")
        self.episode("the talk that made the change")
        with (self.root / "memory" / "journal.md").open("a") as f:
            f.write("made a weather plugin\n")
        self.commit("p: talk")
        self.change = self.git(self.root, "rev-parse", "HEAD").strip()
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-prob-sup-")).resolve()
        self.judged: list[dict] = [{"episode_ts": f"b{i}", "criterion_met": "yes", "difficulty": 3,
                                    "judge": judge.JUDGE_MODELS[0]} for i in range(3)]
        self.tests_pass = True

    def tearDown(self):
        shutil.rmtree(self.project, ignore_errors=True)
        shutil.rmtree(self.sup, ignore_errors=True)

    def git(self, cwd, *args):
        return self.sp.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                           check=True, capture_output=True, text=True).stdout

    def write(self, rel, text):
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / rel).write_text(text)

    def commit(self, msg):
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-q", "-m", msg)

    def episode(self, what, tools=(), day="2026-10-13"):
        self.cycles += 1
        ts = f"{day}T{self.cycles:02d}:00:00"
        (self.root / "memory" / "episodes").mkdir(parents=True, exist_ok=True)
        with (self.root / "memory" / "episodes" / f"{day}.jsonl").open("a") as f:
            f.write(json.dumps({"ts": ts, "kind": "live", "what": what, "tools": [{"tool": t} for t in tools]}) + "\n")
        return ts

    def later_cycles(self, results, tools=("weather",), day="2026-10-13"):
        """Cycles after the change, each committed as its own run (as the real supervisor does)."""
        for met in results:
            ts = self.episode(f"cycle {met}", tools, day)
            self.judged.append({"episode_ts": ts, "criterion_met": met, "difficulty": 3, "judge": judge.JUDGE_MODELS[0]})
            self.commit(f"p: live {ts}")

    def patched(self):
        from contextlib import ExitStack
        from supervisor import core, gate
        wt = lambda root, *a, **k: core.git_wt(root, *a, **{**k, "project": self.project})  # noqa: E731
        stack = ExitStack()
        for target, attr, value in ((metrics, "INSTANCES", self.project / "instances"), (metrics, "SUP_DIR", self.sup),
                                    (metrics, "git_wt", wt), (core, "PROJECT", self.project),
                                    (core, "INSTANCES", self.project / "instances"), (core, "SUP_DIR", self.sup),
                                    (gate, "run_tests", lambda *a, **k: self.gate_run())):
            stack.enter_context(mock.patch.object(target, attr, value))
        return stack

    def gate_run(self):
        return self.tests_pass, "Ran 3 tests\nFAILED"

    def start(self, files=None):
        with self.patched(), mock.patch.object(metrics, "judgments", return_value=list(self.judged)):
            metrics.start_probation("p", self.change, files or self.FILES)

    def check(self):
        with self.patched(), mock.patch.object(metrics, "judgments", return_value=list(self.judged)):
            metrics.check_probation("p")

    def open_entries(self):
        p = self.sup / "probation" / "p.json"
        return json.loads(p.read_text()) if p.exists() else []

    def episode_lines(self):
        return sum(p.read_text().count("\n") for p in (self.root / "memory" / "episodes").glob("*.jsonl"))

    def undo_keeps_memory(self, day):
        self.start()
        self.later_cycles(["no"] * metrics.PROBATION_CYCLES, day=day)
        lines, journal = self.episode_lines(), (self.root / "memory" / "journal.md").read_text()
        self.check()
        self.assertEqual(self.episode_lines(), lines, "the episode log lost or gained lines")
        self.assertFalse((self.root / "plugins" / "weather" / "main.py").exists(), "the change was not undone")
        after = (self.root / "memory" / "journal.md").read_text()
        self.assertTrue(after.startswith(journal), "the journal was rewritten, not appended to")
        self.assertIn("made a weather plugin", after)
        self.assertEqual((self.root / "self" / "self.md").read_text(), "me\n")
        self.assertEqual(self.open_entries(), [])

    def test_probation_revert_leaves_memory_episodes_untouched(self):
        # Bug 2026-10-13: probation ran `git revert` on the whole run commit, which also holds the episode line
        # the run appended; every later run appends again, so all three reverts failed with a conflict.
        self.undo_keeps_memory("2026-10-13")

    def test_probation_revert_leaves_memory_episodes_untouched_across_days(self):
        # The same bug when the later cycles wrote the next day's file: `git revert` went through and deleted the
        # run's episode record and its journal lines.
        self.undo_keeps_memory("2026-10-14")

    def test_the_revert_restores_exactly_the_listed_files(self):
        self.start()
        self.later_cycles(["no"] * metrics.PROBATION_CYCLES)
        self.check()
        for rel in self.FILES:   # none of them existed before the change
            self.assertFalse((self.root / rel).exists(), rel)
        self.assertEqual((self.root / "shaft" / "prompts.py").read_text(), "GOOD = True\n")
        self.assertTrue((self.root / "plugins" / "clock" / "main.py").exists())
        head = self.git(self.root, "show", "--name-only", "--format=%an%n%s", "HEAD").split("\n")
        self.assertEqual(head[0], "SHAFT supervisor")
        self.assertIn(f"before {self.change[:8]}", head[1])
        self.assertEqual(sorted(l for l in head[2:] if l.strip()), sorted(self.FILES))
        self.assertEqual(self.git(self.root, "status", "--porcelain", "--", *self.FILES), "")
        note = (self.root / "memory" / "journal.md").read_text().split("self-change undone after probation")[1]
        for rel in self.FILES:
            self.assertIn(rel, note)
        for raw in ("hint:", "error:", "FAILED", "revert --continue"):
            self.assertNotIn(raw, note)

    def test_a_modified_file_gets_its_content_from_before_the_change(self):
        self.write("shaft/prompts.py", "GOOD = False\n")
        self.episode("the talk that changed prompts")
        self.commit("p: talk 2")
        self.change = self.git(self.root, "rev-parse", "HEAD").strip()
        self.start(["shaft/prompts.py"])
        self.later_cycles(["no"] * metrics.PROBATION_CYCLES, tools=())
        self.check()
        self.assertEqual((self.root / "shaft" / "prompts.py").read_text(), "GOOD = True\n")
        self.assertTrue((self.root / "plugins" / "weather" / "main.py").exists(), "an unlisted file was restored")

    def test_a_file_rewritten_while_the_restore_is_tested_is_committed_as_tested_and_put_back(self):
        """The coordinator on 2306a15: restore_change committed its restored files with `git add` from the working
        tree, after its tests: a write while they ran was committed, and committed code is approved code."""
        self.write("shaft/prompts.py", "GOOD = False\n")
        self.episode("the talk that changed prompts")
        self.commit("p: talk 2")
        self.change = self.git(self.root, "rev-parse", "HEAD").strip()
        self.start(["shaft/prompts.py"])
        self.later_cycles(["no"] * metrics.PROBATION_CYCLES, tools=())

        def tamper():
            (self.root / "shaft" / "prompts.py").write_text("GOOD = 'slipped in'\n")
            return True, "Ran 3 tests\nOK"
        self.gate_run = tamper
        with mock.patch.object(metrics, "log") as logged:
            self.check()
        self.assertEqual(self.git(self.root, "show", "HEAD:shaft/prompts.py"), "GOOD = True\n",
                         "the commit took the working tree, not what was tested")
        self.assertEqual((self.root / "shaft" / "prompts.py").read_text(), "GOOD = True\n")
        self.assertIn("written after", (self.root / "memory" / "journal.md").read_text())
        self.assertTrue(any("FAILED" in str(c.args[0]) for c in logged.call_args_list))

    def test_a_commit_that_git_refuses_is_said_not_raised(self):
        """Codex on 9bd175e: `.decode()` on a CalledProcessError's stderr, a str from git_wt's text mode, raised
        AttributeError instead of saying why the restore was not committed."""
        self.write("shaft/prompts.py", "GOOD = False\n")
        self.episode("the talk that changed prompts")
        self.commit("p: talk 2")
        self.change = self.git(self.root, "rev-parse", "HEAD").strip()
        self.start(["shaft/prompts.py"])
        self.later_cycles(["no"] * metrics.PROBATION_CYCLES, tools=())
        from supervisor import core
        refuse = self.sp.CalledProcessError(128, "git", stderr="fatal: unable to write new index file")
        with mock.patch.object(core, "_commit_gated", side_effect=refuse), mock.patch.object(metrics, "log"):
            self.check()
        [entry] = self.open_entries()
        self.assertIn("unable to write new index file", entry["restore_failed"])
        self.assertEqual((self.root / "shaft" / "prompts.py").read_text(), "GOOD = False\n")   # put back

    def test_a_failed_restore_keeps_the_entry(self):
        # Bug 2026-10-13: after the failed revert the entry was dropped from probation, silently.
        self.start()
        self.later_cycles(["no"] * metrics.PROBATION_CYCLES)
        self.write("plugins/weather/main.py", "def run(**a): return 'rainy'\n")   # changed again later
        self.commit("p: live later change")
        journal = (self.root / "memory" / "journal.md").read_text()
        self.check()
        [entry] = self.open_entries()
        self.assertEqual(entry["commit"], self.change)
        self.assertIn("changed again", entry["restore_failed"])
        self.assertEqual((self.root / "plugins" / "weather" / "main.py").read_text(), "def run(**a): return 'rainy'\n")
        self.assertEqual((self.root / "memory" / "journal.md").read_text(), journal)
        self.check()   # retried, still kept
        self.assertEqual(len(self.open_entries()), 1)

    def test_a_restore_whose_tests_fail_is_put_back_and_kept(self):
        self.start()
        self.later_cycles(["no"] * metrics.PROBATION_CYCLES)
        self.tests_pass = False
        self.check()
        [entry] = self.open_entries()
        self.assertIn("tests fail", entry["restore_failed"])
        self.assertEqual((self.root / "plugins" / "weather" / "main.py").read_text(), "def run(**a): return 'sunny'\n")
        self.assertEqual(self.git(self.root, "status", "--porcelain"), "")
        self.tests_pass = True
        self.check()   # the verdict stands; the retry restores it
        self.assertEqual(self.open_entries(), [])
        self.assertFalse((self.root / "plugins" / "weather" / "main.py").exists())

    def test_equal_results_keep_the_change(self):
        self.start()
        self.later_cycles(["yes"] * metrics.PROBATION_CYCLES)
        self.check()
        self.assertTrue((self.root / "plugins" / "weather" / "main.py").exists())
        self.assertEqual(self.open_entries(), [])

    def test_a_plugin_never_called_is_not_blamed(self):
        # Bug 2026-10-13: shaft's weather plugin "failed probation" over 6 cycles that never called it.
        self.start()
        self.later_cycles(["no"] * metrics.PROBATION_CYCLES, tools=("clock", "read_file"))
        self.check()
        self.assertTrue((self.root / "plugins" / "weather" / "main.py").exists())
        self.assertEqual(len(self.open_entries()), 1, "still waiting for cycles that use it")
        self.later_cycles(["no"] * (metrics.PROBATION_EXPIRY - 1) * metrics.PROBATION_CYCLES, tools=("clock",))
        self.check()
        self.assertTrue((self.root / "plugins" / "weather" / "main.py").exists())
        self.assertEqual(self.open_entries(), [], "too little use to judge: kept and closed")

    def test_a_test_only_change_is_not_put_on_probation(self):
        self.start(["tests/test_weather.py"])
        self.assertEqual(self.open_entries(), [])


if __name__ == "__main__":
    unittest.main()
