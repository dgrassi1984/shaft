"""The talk client's status line (the operator: "add a status bar with context information, tokens/sec average")."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from supervisor.talk_stats import Stats

TEMPLATE = Path(__file__).resolve().parents[2] / "template"


class StatusLineTest(unittest.TestCase):
    def test_context_speed_and_totals(self):
        st = Stats(window=262144)
        self.assertEqual(st.line("shaft", "ternary-bonsai-2-27b-mlx", False),
                         "shaft · ternary-bonsai-2-27b-mlx · thinking hidden")   # before any call
        st.add({"prompt_tokens": 18000, "completion_tokens": 1000, "tokens_per_s": 20.0})
        st.add({"prompt_tokens": 19000, "completion_tokens": 300, "tokens_per_s": 25.0})
        line = st.line("shaft", "ternary-bonsai-2-27b-mlx", False)
        self.assertIn("ctx 19.3k/262k (7%)", line)
        self.assertIn("25.0 tok/s (avg 21.0)", line)   # weighted by tokens: 1300 / (50 + 12) s
        self.assertIn("2 calls · 1.3k out", line)
        self.assertNotIn("$", line)

    def test_cloud_cost_and_unknown_window(self):
        st = Stats()
        st.add({"prompt_tokens": 11000, "completion_tokens": 400, "tokens_per_s": 60.0, "cost": 0.0051})
        line = st.line("wren", "openrouter/xiaomi/mimo-v2.6-pro", True)
        self.assertIn("wren · mimo-v2.6-pro · ctx 11.4k ·", line)
        self.assertIn("$0.0051", line)


class UsageEventTest(unittest.TestCase):
    def test_talk_reports_each_model_call(self):
        from supervisor.tests.fakellm import FakeLLM
        fake = FakeLLM([{"tool_calls": [("list_files", {"path": "."})]}, {"content": "done"}])
        self.addCleanup(fake.close)
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        (root / "config.env").write_text(f'LLM_BASE_URL="{fake.url}"\nLLM_MODEL="m"\n')
        r = subprocess.run([sys.executable, "-E", "-s", "-m", "shaft", "talk", "--json"], cwd=TEMPLATE,
                           env={"PATH": "/usr/bin:/bin", "SHAFT_ROOT": str(root)}, capture_output=True, text=True,
                           input=json.dumps({"type": "user", "text": "hi"}) + "\n" + json.dumps({"type": "exit"}) + "\n",
                           timeout=60)
        events = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
        ready = next(e for e in events if e["type"] == "ready")
        self.assertEqual(ready["context_tokens"], 262144)
        usage = [e for e in events if e["type"] == "usage"]
        self.assertEqual(len(usage), 2, r.stderr)   # one per model call: the tool round and the answer
        self.assertEqual((usage[-1]["prompt_tokens"], usage[-1]["completion_tokens"]), (10, 5))


if __name__ == "__main__":
    unittest.main()
