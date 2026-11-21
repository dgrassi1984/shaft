"""Sleep updates peer people files and the live prompt shows the 5,500-char stats head."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import agent, broker as agent_broker, config, prompts, sleep
from shaft.state import State


class SleepPredictionsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for folder in ("self", "memory", "inbox"):
            (self.root / folder).mkdir()
        self.state = State(self.root)
        self.cfg = config.load(self.root, environ={"LLM_BASE_URL": "fake", "LLM_MODEL": "fake"})
        self.enterContext(mock.patch.dict(os.environ, {"SHAFT_ROOT": str(self.root)}, clear=True))
        self.enterContext(mock.patch.object(agent_broker, "available", return_value=True))
        self.enterContext(mock.patch.object(agent_broker, "tools", return_value=[]))
        self.enterContext(mock.patch.object(agent_broker, "envelope", return_value=None))
        self.run_agent = self.enterContext(mock.patch.object(agent, "run", return_value=
            agent.Outcome(mock.Mock(content="Done."), "answer", 1)))

    def run_sleep(self, last_sleep=""):
        def record(tool, **args):
            return json.dumps({"text": "", "verdicts": [], "left_out": [], "next_since": None})
        with mock.patch.object(agent_broker, "call", side_effect=record) as calls, \
                mock.patch.object(sleep, "_last_sleep", return_value=last_sleep):
            sleep.run(self.cfg, self.state, mock.Mock())
        user = self.run_agent.call_args.args[1][1]["content"]
        return user, calls

    def test_sleep_includes_predictions_and_last_sleep_cursor(self):
        user, _ = self.run_sleep(last_sleep="2026-10-30T12:34:56")
        self.assertIn("memory/people/<instance name>.md", user)
        self.assertIn("not mail correspondents", user)

    def test_live_stats_head_is_5500_characters(self):
        (self.root / "self" / "stats.md").write_text("x" * 5200 + "VISIBLE" + "y" * 293 + "HIDDEN")
        text = prompts.system_prompt(self.state, self.cfg, prompts.LIVE_MODE)
        self.assertIn("VISIBLE", text)
        self.assertNotIn("HIDDEN", text)
