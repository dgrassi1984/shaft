"""Reply prediction feedback reaches the prompt and the sleep that updates people files."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import broker, metrics, pushback, pushes, surprise, tom
from supervisor.tests.test_measurement import judgment

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import agent, broker as agent_broker, config, prompts, sleep
from shaft.state import State


class PredictionsTest(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(tmp)
        self.enterContext(mock.patch.object(tom, "SUP_DIR", self.root))
        self.b = object.__new__(broker.Broker)
        self.b.name, self.b.as_of = "wren", None

    def rows(self, rows):
        path = self.root / "expectations" / "wren-scored.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def row(self, scored_at, **extra):
        return {"sent_at": "2026-10-30T12:09:00", "to": "operator", "will_reply": 1,
                "within_hours": 24, "gist": "Agrees", "reply": None, "outcome": 0, "brier": .0001,
                "scored_at": scored_at, **extra}

    def test_record_since_replay_and_order(self):
        self.rows([self.row("2026-10-31T12:00:00", gist="before"),
                   self.row("2026-10-31T14:00:00", gist="at replay cut"),
                   self.row("2026-10-31T14:01:00", gist="future"),
                   self.row("2026-10-31T13:00:00", gist="equal"),
                   self.row("2026-10-31T13:01:00", gist="Came", outcome=1,
                            reply={"at": "2026-10-31T12:55:00", "file": "reply.md"},
                            match="partial", reason="Asked a question"),
                   self.row("2026-10-31T13:02:00")])
        expected = ('2026-10-30 12:09 to operator — said will_reply 1% within 24h; gist: "Came". '
                    'Outcome: replied 2026-10-31 12:55; gist match: partial — Asked a question.\n'
                    '2026-10-30 12:09 to operator — said will_reply 1% within 24h; gist: "Agrees". '
                    'Outcome: no reply in the window.')
        replay = object.__new__(broker.ReplayBroker)
        replay.name, replay.as_of = "wren", datetime(2026, 10, 31, 14)
        self.assertEqual(replay._record("expectations", since="2026-10-31T13:00"), expected)
        live = self.b._record("expectations", since="2026-10-31 13:00:00")
        self.assertTrue(live.startswith(expected + "\n"))
        self.assertIn('gist: "at replay cut"', live)
        self.assertIn('gist: "future"', live)
        self.assertNotIn('gist: "before"', live)
        self.assertNotIn('gist: "equal"', live)
        self.assertEqual(self.b._record("expectations", since="2026-10-31 13:00", limit=1), live.splitlines()[-1])

    def test_record_empty_invalid_since_and_own_rows_only(self):
        self.assertEqual(self.b._record("expectations"), "(nothing recorded yet)")
        self.rows([self.row("2026-10-31T13:00:00")])
        self.b.name = "thelos"
        self.assertEqual(self.b._record("expectations"), "(nothing recorded yet)")
        for bad in ("yesterday", "2026-02-30 13:00", 42):
            with self.subTest(since=bad), self.assertRaises(ValueError):
                self.b._record("expectations", since=bad)

    def test_record_bounds_gist_and_reason(self):
        self.rows([self.row("2026-10-31T13:00:00", gist="g" * 250, reason="r" * 250,
                            reply={"at": "2026-10-31T12:55:00"}, match="no")])
        text = self.b._record("expectations")
        self.assertIn('gist: "' + "g" * 200 + '".', text)
        self.assertIn(" — " + "r" * 200 + ".", text)
        self.assertNotIn("g" * 201, text)
        self.assertNotIn("r" * 201, text)

    def test_stats_prediction_section_is_visible_once(self):
        """tom section fell past the 5,000-char prompt head (10K in on all three instances, 2026-11-01)."""
        self.rows([self.row("2026-10-31T13:00:00")])
        for module in (metrics, surprise):
            self.enterContext(mock.patch.object(module, "SUP_DIR", self.root))
            self.enterContext(mock.patch.object(module, "INSTANCES", self.root / "roots"))
        for module in (pushes, pushback):
            self.enterContext(mock.patch.object(module, "lines", return_value=[]))
        for method in ("_sample_note", "_battery_section", "_code_section", "_traits_section", "_trials_section"):
            self.enterContext(mock.patch.object(metrics, method, return_value=[]))
        # Keep the real section dispatcher and tom renderer; unrelated modules have no bearing on this placement.
        sections = metrics._sections
        self.enterContext(mock.patch.object(metrics, "_sections", side_effect=lambda name, which:
                          sections(name, tuple(m for m in which if m == "tom"))))
        self.enterContext(mock.patch.object(metrics, "judgments", return_value=[
            judgment("2026-10-31T12:00:00", "yes", 3, "self")]))
        episode_dir = self.root / "roots" / "wren" / "memory" / "episodes"
        episode_dir.mkdir(parents=True)
        (episode_dir / "2026-10-31.jsonl").write_text(json.dumps(
            {"kind": "live", "ts": "2026-10-31T12:00:00", "tools": []}) + "\n")
        text = metrics.stats_text("wren")
        title = "## What I expected others to answer"
        at = text.index(title)
        self.assertLess(text.index("## My expectations vs what happened"), at)
        self.assertLess(at, text.index("## What my cycles were worth"))
        self.assertIn("Per peer: operator:", text[:5500])
        self.assertEqual(text.count(title), 1)
        self.assertEqual(len(tom.section("wren")), 5)


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

    def run_sleep(self, text="Scored prediction: wren replied.", error=None, last_sleep=""):
        def record(tool, **args):
            if args["kind"] == "expectations":
                if error:
                    raise agent_broker.BrokerError(error)
                return text
            return json.dumps({"text": "", "verdicts": [], "left_out": [], "next_since": None})
        with mock.patch.object(agent_broker, "call", side_effect=record) as calls, \
                mock.patch.object(sleep, "_last_sleep", return_value=last_sleep):
            sleep.run(self.cfg, self.state, mock.Mock())
        user = self.run_agent.call_args.args[1][1]["content"]
        return user, calls

    def test_sleep_includes_predictions_and_last_sleep_cursor(self):
        user, calls = self.run_sleep(last_sleep="2026-10-30T12:34:56")
        self.assertIn("## What I expected others to answer, and what happened (step 2)\n\n"
                      "Scored prediction: wren replied.", user)
        self.assertLess(user.index("Scored prediction:"), user.index("## My operational envelope"))
        calls.assert_any_call("record", kind="expectations", limit=50, since="2026-10-30 12:34:56")
        self.assertIn("a reply you put under 50% that came", user)
        self.assertIn("memory/people/<instance name>.md", user)
        self.assertIn("not mail correspondents", user)

    def test_first_sleep_omits_since_and_empty_is_explained(self):
        user, calls = self.run_sleep(text="")
        calls.assert_any_call("record", kind="expectations", limit=50)
        self.assertIn("(none scored since my last sleep)", user)

    def test_sleep_broker_failure_is_a_single_line(self):
        user, _ = self.run_sleep(error="offline\ntry later")
        self.assertIn("Reply predictions could not be read: offline try later", user)

    def test_live_stats_head_is_5500_characters(self):
        (self.root / "self" / "stats.md").write_text("x" * 5200 + "VISIBLE" + "y" * 293 + "HIDDEN")
        text = prompts.system_prompt(self.state, self.cfg, prompts.LIVE_MODE)
        self.assertIn("VISIBLE", text)
        self.assertNotIn("HIDDEN", text)
