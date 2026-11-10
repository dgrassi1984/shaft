"""#140: reverting loses measured push budgets, timing judgments and recorded operator reactions."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, broker, cloudllm, consult, core, judge, messages, metrics, panel, pushes
from supervisor.tests.test_surprise_judge import VERDICT

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import prompts

NOW = datetime(2026, 10, 28, 12)


class PushTest(unittest.TestCase):
    """#140: use fake supervisor records, never inbox files as push-count evidence."""

    def setUp(self):
        self.project = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="shaft-pushes-")))
        self.sup = self.project / ".supervisor"
        self.sup.mkdir()
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(metrics, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(panel, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(pushes, "PUSH_BUDGET", 6))
        for module in (broker, pushes, metrics):
            self.enterContext(mock.patch.object(module, "datetime", wraps=datetime)).now.return_value = NOW
        self.enterContext(mock.patch.object(broker, "OPERATOR_DAILY_LIMIT", 0))
        self.enterContext(mock.patch.object(broker, "notify"))
        self.enterContext(mock.patch.object(broker.Broker, "_answer_comments"))
        self.enterContext(mock.patch("supervisor.channel.route", return_value=("web", "test")))
        self.enterContext(mock.patch.object(cloudllm, "spent_today", return_value=0))
        self.enterContext(mock.patch.object(cloudllm, "daily_cap", return_value=8))
        self.enterContext(mock.patch.object(consult, "envelope", return_value=None))
        self.enterContext(mock.patch.object(metrics, "probation_status", return_value=[]))
        self.enterContext(mock.patch.object(autonomy, "next_sleep", return_value=None))
        self.enterContext(mock.patch.object(autonomy, "next_wake", return_value=None))
        self.b = object.__new__(broker.Broker)
        self.b.name = self.b.usage_name = "m"
        self.b.project, self.b.root = self.project, self.project / "m"
        self.b.calls, self.b.max_calls, self.b.counts = 0, 60, Counter()
        self.b.disabled, self.b.unavailable = {"trial", "delegate_coding"}, {}
        self.b.model_allowed, self.b.as_of = True, None
        self.b.talk_id = None

    def message(self, at=NOW, sender="m", to="operator", alert=True):
        path = self.sup / f"{at:%Y%m%d-%H%M%S-%f}-from-{sender}.md"
        messages.record(self.sup, path, sender, to, "note", at, "check the calendar",
                        alert=("web", "test") if alert else None)
        return path.name

    def test_envelope_and_send_result_count_archived_pushes_and_still_deliver_over_budget(self):
        """#140: reverting hides N of B or mistakes the measured budget for a send limit."""
        for minute in range(6):
            self.message(NOW - timedelta(minutes=minute + 1))
        self.message(NOW - timedelta(days=1))
        self.message(alert=False)
        self.message(to="other")
        self.message(sender="other")
        self.message(NOW + timedelta(hours=1))
        env = self.b.envelope()
        self.assertEqual(env["pushes"], {"today": 6, "budget": 6})
        self.assertIn("pushes to the operator today: 6 of budget 6", prompts.envelope_text(env, None))
        result = self.b._send_message("operator", "Deadline today, please act now")
        self.assertIn("delivered to the operator", result)
        self.assertIn("pushes to the operator today: 7 of budget 6", result)
        self.assertEqual(self.b.envelope()["pushes"], {"today": 7, "budget": 6})
        row = pushes.records(self.sup)[-1]
        self.assertEqual((row["to"], row["alert"], row["text"]),
                         ("operator", "web", "Deadline today, please act now"))
        self.assertTrue((self.sup / "operator-inbox" / row["file"]).is_file())

    def test_zero_budget_hides_count_in_envelope_text_and_send_result(self):
        """#140: reverting ignores the operator's choice to hide the budget with 0."""
        with mock.patch.object(pushes, "PUSH_BUDGET", 0):
            env = self.b.envelope()
            self.assertIsNone(env["pushes"])
            self.assertNotIn("pushes", prompts.envelope_text(env, None))
            self.assertNotIn("pushes", self.b._send_message("operator", "a cancellation"))
            self.assertEqual(len(pushes.records(self.sup)), 1)

    def test_replay_uses_historical_pushes_and_simulated_sends(self):
        """#140: reverting leaks later pushes into replay or fails to count its simulated send."""
        self.message(NOW - timedelta(minutes=1))
        self.message(NOW + timedelta(minutes=1))
        replay = object.__new__(broker.ReplayBroker)
        replay.name, replay.project, replay.as_of, replay.operator_sent = "m", self.project, NOW, 2
        self.assertEqual(replay._push_usage(), {"today": 3, "budget": 6})
        self.assertEqual(len(pushes.records(self.sup)), 2)

    def test_unavailable_count_does_not_fail_a_delivered_send(self):
        """#140: reverting makes a failed measurement look like a failed delivery and invites duplicate pushes."""
        with mock.patch.object(pushes, "usage", side_effect=OSError("cannot read the count")):
            self.assertIsNone(self.b.envelope()["pushes"])
            self.assertIn("delivered to the operator", self.b._send_message("operator", "a deadline today"))
        self.assertEqual(pushes.records(self.sup)[0]["text"], "a deadline today")

    def test_reactions_are_next_recorded_message_or_conversation_not_tone_or_read_flag(self):
        """#140: reverting loses 1 h/24 h/none outcomes or invents a timed dismissal from read state."""
        sent = NOW - timedelta(days=3)
        first = self.message(sent)
        self.message(sent + timedelta(minutes=30), sender="operator", to="m", alert=False)
        second = self.message(sent + timedelta(days=1))
        self.message(sent + timedelta(days=1, hours=2), sender="operator", to="m", alert=False)
        third = self.message(sent + timedelta(days=2) - timedelta(hours=1))
        fourth = self.message(NOW - timedelta(minutes=15))
        self.message(NOW + timedelta(minutes=1), sender="operator", to="m", alert=False)
        self.message(NOW - timedelta(minutes=14), sender="operator", to="other", alert=False)
        (self.sup / "supervisor.log").write_text(
            "[2026-10-28 11:50:00] other: start talk talk-1\n"
            "[2026-10-28 11:55:00] m: start api cycle_timeout_s=2400\n")
        rows = [{**r, "read": True} for r in pushes.records(self.sup)]
        outcomes = {r["file"]: r["outcome"] for r in pushes.reactions("m", rows, self.sup, NOW)}
        self.assertEqual(outcomes, {first: "within_1h", second: "within_24h",
                                    third: "no_response", fourth: "within_1h"})

    def test_stats_and_review_keep_budget_judged_timing_and_reactions_separate(self):
        """#140: reverting drops zero-count days, conflates scores or treats old ungraded rows as no."""
        self.message(NOW - timedelta(days=1, hours=2))
        self.message(NOW - timedelta(hours=1))
        self.message(NOW - timedelta(minutes=30), sender="operator", to="m", alert=False)
        rows = [{"episode_ts": NOW.isoformat(), "should_have_waited": v} for v in ("yes", "no", "na")]
        rows.append({"episode_ts": NOW.isoformat()})
        text = "\n".join(pushes.lines("m", rows, NOW))
        self.assertIn("pushes to the operator today: 1 of budget 6", text)
        self.assertIn("10-22: 0, 10-23: 0, 10-24: 0, 10-25: 0, 10-26: 0, 10-27: 1, 10-28: 1", text)
        self.assertIn("judged push cycles): 1/2 (50%); na 1, ungraded 1", text)
        self.assertIn("within 1 h 1, 1–24 h 0, no response within 24 h recorded yet 1", text)
        with mock.patch.object(metrics, "judgments", return_value=[]):
            for key in ("_battery_section", "_code_section", "_traits_section", "_trials_section"):
                self.enterContext(mock.patch.object(metrics, key, return_value=[]))
            self.assertIn("pushes to the operator today: 1 of budget 6", metrics.stats_text("m"))
            self.assertIn("pushes to the operator today: 1 of budget 6", metrics.review_text(["m"], now=NOW))

    def test_judge_requires_timing_including_control_and_refuses_missing_fresh_verdict(self):
        """#140: reverting accepts fresh verdicts with no timing judgment or makes the control inconsistent."""
        self.assertEqual(metrics.PUSH_PROPS["should_have_waited"]["enum"], ["yes", "no", "na"])
        for schema in (metrics.JUDGE_SCHEMA, metrics.CONTROL_SCHEMA):
            self.assertIn("should_have_waited", schema["required"])
        self.assertIn("should_have_waited", metrics.CONTROL_SYSTEM)
        self.assertIn("Never judge this from", metrics.CONTROL_SYSTEM)
        self.assertIn("calendar clash within hours", metrics.CONTROL_SYSTEM)
        self.assertEqual(set(metrics.CONTROL_SCHEMA["properties"]) | set(metrics.SURPRISES),
                         set(metrics.JUDGE_SCHEMA["properties"]))
        metrics._check_ratings(dict(VERDICT))
        for value in ("yes", "no", "na"):
            metrics._check_ratings({**VERDICT, "should_have_waited": value})
        for value in (None, "maybe", True):
            with self.subTest(value=value), self.assertRaises(judge.JudgeError):
                metrics._check_ratings({**VERDICT, "should_have_waited": value})
        old = {k: v for k, v in VERDICT.items() if k != "should_have_waited"}
        with self.assertRaisesRegex(judge.JudgeError, "should_have_waited"):
            metrics._check_ratings(old)
        ep = {"ts": NOW.isoformat(), "intent": {"goal": "check calendar"}, "tools": []}
        for key, value in (("judgments", []), ("unsampled", set()), ("live_episodes", [ep]),
                           ("judge_sampled", True), ("second_sampled", False), ("_written_files", ""),
                           ("_provenance_evidence", ""), ("_prior_goals", ""), ("_followed_surprise", ""),
                           ("own_models", [])):
            self.enterContext(mock.patch.object(metrics, key, return_value=value))
        self.enterContext(mock.patch.object(judge, "available", return_value=True))
        self.enterContext(mock.patch.object(judge, "ask", return_value=old))
        self.enterContext(mock.patch.object(metrics, "log"))
        self.assertEqual(metrics.judge_pending("m"), 0)
        self.assertFalse((self.sup / "judgments/m.jsonl").exists())

    def test_old_verdict_loads_in_stats_panel_and_broker(self):
        """#140: reverting old-row fallbacks loses historical stats, panels and sleep's broker reads."""
        old = {k: v for k, v in VERDICT.items() if k != "should_have_waited"}
        old.update(episode_ts=NOW.isoformat(), judged_at=NOW.isoformat(), goal="check calendar", confidence=70,
                   claimed="yes", judge=judge.JUDGE_MODELS[0], model="test")
        folder = self.sup / "judgments"
        folder.mkdir()
        (folder / "m.jsonl").write_text(json.dumps(old) + "\n")
        for key in ("_battery_section", "_code_section", "_traits_section", "_trials_section", "_surprise_section",
                    "_sample_note", "_sections", "_base_rate_lines"):
            self.enterContext(mock.patch.object(metrics, key, return_value=[]))
        self.enterContext(mock.patch.object(metrics, "_QUESTIONS", {}))
        self.assertIn("na 0, ungraded 1", metrics.stats_text("m"))
        shown = panel.judgments("m")
        self.assertEqual(shown[0]["criterion_met"], "yes")
        self.assertIn("criterion met **yes**", broker.judgments_text(shown))
        with mock.patch.object(broker.datetime, "now", return_value=NOW + timedelta(minutes=1)):
            page = json.loads(self.b._record("judgments", since="2026-10-27 00:00", format="json"))
        self.assertIsNone(page["verdicts"][0]["should_have_waited"])


if __name__ == "__main__":
    unittest.main()
