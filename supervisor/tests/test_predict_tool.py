"""Predictions use supervisor clocks, isolate replay writes, and reach the stats head."""
from datetime import timedelta
from unittest import mock

from supervisor import claimlint, gauges, judge_probe, metrics, predictions, stats_head, tom, unrecorded
from supervisor.tests.test_idle_exit import IsolatedCase, NOW


class PredictToolTest(IsolatedCase):
    def clock(self, at):
        return mock.patch.object(predictions, "datetime", mock.Mock(now=lambda: at))

    def make(self, box):
        return box.call("predict", {"text": "The report will pass", "due": (NOW + timedelta(hours=2)).isoformat(),
                                    "how_to_check": "Read the check output"})

    def test_made_event_uses_supervisor_clock_and_caller_cannot_override_it(self):
        b = self.box()
        with self.clock(NOW):
            result = self.make(b)
            self.assertTrue(result["ok"], result)
            for field in ("at", "now"):
                args = {"text": "Another check passes", "due": (NOW + timedelta(hours=3)).isoformat(),
                        "how_to_check": "Check output", field: "2000-01-01T00:00:00"}
                reply = b.call("predict", args)
                if not reply["ok"]:
                    self.assertIn("bad arguments", reply["error"])
        rows = predictions._rows(predictions._path("ember").read_text())
        self.assertGreaterEqual(len(rows), 1)
        for row in rows:
            self.assertEqual(row["event"], "made")
            self.assertEqual(row["at"], NOW.isoformat())
        from supervisor import broker
        props = broker.SPECS["predict"]["function"]["parameters"]["properties"]
        self.assertNotIn("at", props)
        self.assertNotIn("now", props)

    def test_reporting_before_due_errors_and_at_due_appends_report(self):
        b = self.box()
        args = {"id": "p1", "outcome": "held", "evidence": "2026-11-11 14:00#1"}
        with self.clock(NOW):
            self.assertTrue(self.make(b)["ok"])
            result = b.call("predict", args)
            self.assertFalse(result["ok"], result)
            self.assertIn("not due until", result["error"])
        self.assertEqual(len(predictions._rows(predictions._path("ember").read_text())), 1)
        with self.clock(NOW + timedelta(hours=2)):
            result = b.call("predict", args)
        self.assertTrue(result["ok"], result)
        rows = predictions._rows(predictions._path("ember").read_text())
        self.assertEqual([r["event"] for r in rows], ["made", "reported"])
        self.assertEqual(rows[-1]["at"], (NOW + timedelta(hours=2)).isoformat())

    def test_replay_prediction_visible_to_record_without_real_ledger_or_score(self):
        b = self.box(replay=True)
        result = self.make(b)
        self.assertTrue(result["ok"], result)
        self.assertIn("recorded prediction p1", result["result"])
        shown = b.call("record", {"kind": "predictions"})
        self.assertTrue(shown["ok"], shown)
        self.assertIn("p1: open", shown["result"])
        self.assertIn("The report will pass", shown["result"])
        self.assertEqual(b.sim_predictions[0]["at"], NOW.isoformat())
        self.assertFalse((self.sup / "predictions").exists())
        with mock.patch("supervisor.judge.ask") as ask:
            self.assertEqual(predictions.score("ember", NOW + timedelta(days=5)), 0)
        ask.assert_not_called()
        self.assertEqual(predictions.fold("ember"), {})

    def test_after_cycle_scores_and_scoring_failure_still_writes_stats(self):
        for module, name in [(metrics, "judge_pending"), (judge_probe, "run"), (tom, "score"),
                             (claimlint, "run"), (unrecorded, "run"), (metrics, "check_probation")]:
            self.enterContext(mock.patch.object(module, name, return_value=0))
        for error in (None, ValueError("broken prediction")):
            with self.subTest(error=error), mock.patch.object(predictions, "score", side_effect=error) as score, \
                 mock.patch.object(metrics, "write_stats") as write:
                metrics.after_cycle("ember")
                score.assert_called_once_with("ember")
                write.assert_called_once_with("ember")

    def test_stats_head_shows_due_unreported_prediction_then_removes_reported(self):
        with self.clock(NOW):
            self.assertTrue(self.make(self.box())["ok"])
        for module, name, value in [(claimlint, "head_lines", []),
                                    (stats_head, "_last_sleep", None), (stats_head, "verdicts", []),
                                    (stats_head, "since_sleep", []), (stats_head, "model_change", [])]:
            self.enterContext(mock.patch.object(module, name, return_value=value))
        self.enterContext(mock.patch.object(gauges, "head_lines", side_effect=lambda *a: []))
        self.enterContext(mock.patch("supervisor.battery.self_miss_lines", return_value=[]))
        due = NOW + timedelta(hours=2)
        self.assertIn("Prediction p1 was due", "\n".join(stats_head.head("ember", due)))
        with self.clock(due):
            result = self.box().call("predict", {"id": "p1", "outcome": "held", "evidence": "2026-11-11 14:00#1"})
        self.assertTrue(result["ok"], result)
        self.assertNotIn("Prediction p1 was due", "\n".join(stats_head.head("ember", due)))
