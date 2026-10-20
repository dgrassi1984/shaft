"""`shaft top`'s snapshot: state and what each instance is doing right now (the operator, 2026-10-13)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import dashboard


class InstanceStateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".tmp").mkdir()
        (self.root / "config.env").write_text('LLM_BASE_URL="broker"\nLLM_MODEL="openrouter/xiaomi/mimo-v2.6-pro"\n'
                                              "WAKE_INTERVAL_MINUTES=30\n")
        self.patches = [mock.patch.object(dashboard, "instance_root", return_value=self.root),
                        mock.patch("supervisor.core.instance_root", return_value=self.root),
                        mock.patch("supervisor.runner.busy_with", return_value="live since 13:47"),
                        mock.patch("supervisor.runner.open_conversations", return_value={}),
                        mock.patch.object(dashboard, "paused", return_value=False),
                        mock.patch.object(dashboard, "_usd_today", return_value=0.01),
                        mock.patch.object(dashboard, "_judged", return_value=(3, 1.0, 2.0))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def write_activity(self, pid: int, **fields):
        (self.root / ".tmp" / "activity.json").write_text(json.dumps({"pid": pid, "ts": time.time() - 4, **fields}))

    def test_what_it_is_doing_now_and_its_goal(self):
        self.write_activity(os.getpid(), phase="tool", tool="fetch_page", args='{"url": "https://example.org"}',
                            goal="Run the rogue-AI watch")
        s = dashboard.instance_state("wren")
        self.assertEqual(s["state"], "in a cycle")
        self.assertTrue(s["doing"].startswith('fetch_page {"url": "https://example.org"} · '))
        self.assertEqual(s["goal"], "Run the rogue-AI watch")
        self.assertEqual((s["model"], s["every"]), ("cloud mimo-v2.6-pro", "30 min"))

    def test_a_dead_process_activity_is_not_shown(self):
        self.write_activity(2 ** 22 + 4321, phase="thinking", step=3, goal="old")
        s = dashboard.instance_state("wren")
        self.assertEqual((s["doing"], s["goal"]), ("", ""))

    def test_a_cycle_paused_for_a_conversation(self):
        self.write_activity(os.getpid(), phase="paused")
        (self.root / ".tmp" / f"paused-{os.getpid()}").write_text("1")
        self.assertEqual(dashboard.instance_state("wren")["state"], "live paused for a conversation")


class EventsTest(unittest.TestCase):
    def test_noise_is_left_out_and_times_are_short(self):
        sup = Path(tempfile.mkdtemp(prefix="shaft-sup-"))
        self.addCleanup(shutil.rmtree, sup, True)
        (sup / "supervisor.log").write_text(
            "[2026-10-13 13:00:00] first line is cut\n[2026-10-13 13:00:01] api \"GET /v1/models HTTP/1.1\" 200 -\n"
            "[2026-10-13 13:00:02] shaft: live paused — the operator opened a conversation\n")
        with mock.patch.object(dashboard, "SUP_DIR", sup):
            self.assertEqual(dashboard.events(), ["13:00:02 shaft: live paused — the operator opened a conversation"])


if __name__ == "__main__":
    unittest.main()
