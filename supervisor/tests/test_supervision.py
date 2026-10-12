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


if __name__ == "__main__":
    unittest.main()
