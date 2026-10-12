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

from supervisor import broker, metrics, runner


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

    def test_messages_delivered_and_operator_limited(self):
        self.assertTrue(self.post("send_message", {"to": "b", "text": "hi b"})["ok"])
        self.assertIn("hi b", next((self.project / "instances" / "b" / "inbox").glob("*.md")).read_text())
        self.assertFalse(self.post("send_message", {"to": "nobody", "text": "x"})["ok"])
        with mock.patch.object(broker, "notify"):
            results = [self.post("send_message", {"to": "operator", "text": f"q{i}", "kind": "question"})["ok"]
                       for i in range(broker.OPERATOR_DAILY_LIMIT + 1)]
        self.assertEqual(results, [True] * broker.OPERATOR_DAILY_LIMIT + [False])


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
               "reasoning": "r", "judge": "j"}
              for i, (c, cl, m) in enumerate([(90, "yes", "no"), (90, "yes", "yes"), (20, "no", "no")])]
        with mock.patch.object(metrics, "judgments", return_value=js):
            text = metrics.stats_text("x")
        self.assertIn("Judged success rate: **33%**", text)        # 1 of 3
        self.assertIn("My own claim matched the judge: 2/3", text)
        brier = ((0.9 - 0) ** 2 + (0.9 - 1) ** 2 + (0.2 - 0) ** 2) / 3
        self.assertIn(f"{brier:.2f}", text)
        self.assertIn("overconfident", text)


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
            cloudllm.complete("x", {"model": "claude-opus-5-5", "messages": []}, "key", {})
        self.assertEqual(ctx.exception.status, 403)
        with mock.patch.object(cloudllm, "spent_today", return_value=cloudllm.DAILY_USD):
            with self.assertRaises(cloudllm.CloudError) as ctx:
                cloudllm.complete("x", {"model": "claude-haiku-5-5", "messages": []}, "key", {})
        self.assertEqual(ctx.exception.status, 429)


class ProbationTest(unittest.TestCase):
    def setUp(self):
        import subprocess
        self.sp = subprocess
        self.project = Path(tempfile.mkdtemp(prefix="shaft-prob-")).resolve()
        self.git(self.project, "init", "-q")
        self.git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "p"
        self.git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/p", str(self.root))
        (self.root / "memory").mkdir()
        (self.root / "memory" / "journal.md").write_text("# j\n")
        (self.root / "prompts.py").write_text("GOOD = True\n")
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-q", "-m", "good")
        (self.root / "prompts.py").write_text("GOOD = False\n")
        self.git(self.root, "commit", "-q", "-am", "self-change")
        self.change = self.git(self.root, "rev-parse", "HEAD").strip()

    def tearDown(self):
        shutil.rmtree(self.project, ignore_errors=True)

    def git(self, cwd, *args):
        return self.sp.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                           check=True, capture_output=True, text=True).stdout

    def run_probation(self, after: list[str]):
        from supervisor import core
        js = [{"criterion_met": "yes"}] * 3 + [{"criterion_met": m} for m in after]
        wt = lambda root, *a, **k: core.git_wt(root, *a, **{**k, "project": self.project})  # noqa: E731
        with mock.patch.object(metrics, "INSTANCES", self.project / "instances"), \
             mock.patch.object(metrics, "judgments", return_value=js[:3]), mock.patch.object(metrics, "git_wt", wt):
            metrics.start_probation("p", self.change, ["prompts.py"])
        with mock.patch.object(metrics, "INSTANCES", self.project / "instances"), \
             mock.patch.object(metrics, "judgments", return_value=js), mock.patch.object(metrics, "git_wt", wt):
            metrics.check_probation("p")

    def test_worse_results_revert_the_change(self):
        self.run_probation(["no"] * metrics.PROBATION_CYCLES)
        self.assertEqual((self.root / "prompts.py").read_text(), "GOOD = True\n")
        self.assertIn("failed probation", (self.root / "memory" / "journal.md").read_text())

    def test_equal_results_keep_the_change(self):
        self.run_probation(["yes"] * metrics.PROBATION_CYCLES)
        self.assertEqual((self.root / "prompts.py").read_text(), "GOOD = False\n")


if __name__ == "__main__":
    unittest.main()
