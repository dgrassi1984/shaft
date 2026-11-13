"""Anticipation is an optional observation, independent of the existing grades."""
import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

from supervisor import judge, judge_probe, metrics, modelcall, trial
from supervisor.tests import test_measurement as measurement
from supervisor.tests.test_judge2 import _reply


class AnticipatedStorageTest(measurement.InstanceDirCase):
    def test_live_verdict_kept_with_valid_missing_or_malformed_field(self):
        answers = [True, False, None, "true", 1, [], {}]
        episodes = []
        verdicts = []
        for i, value in enumerate(answers + ["missing"]):
            ep = {"ts": f"2026-10-13T12:{i:02d}:00", "kind": "live",
                  "intent": {"goal": "output"}, "finish": {"criterion_met": "yes"}, "tools": []}
            self.episode(ep)
            episodes.append(ep)
            verdict = dict(measurement.JudgingTest.VERDICT)
            if value != "missing":
                verdict["anticipated"] = value
            verdicts.append(verdict)
        with mock.patch.object(judge, "available", return_value=True), \
             mock.patch.object(judge, "ask", side_effect=verdicts), \
             mock.patch.object(metrics, "second_sampled", return_value=False), \
             mock.patch.object(metrics, "write_stats"), \
             mock.patch.object(metrics, "_written_files", return_value=""), \
             mock.patch.object(metrics, "_provenance_evidence", return_value=""):
            self.assertEqual(metrics.judge_pending("m"), len(episodes))
        rows = metrics.judgments("m")
        self.assertEqual([r["anticipated"] for r in rows[:2]], [True, False])
        for row in rows[2:]:
            self.assertNotIn("anticipated", row)
            self.assertEqual(row["criterion_met"], "yes")

    def test_probe_keeps_verdict_and_omits_malformed_field(self):
        now = datetime.now()
        ep = {"ts": (now - timedelta(hours=1)).isoformat(), "kind": "live",
              "finish": {"delivered_via": "message"},
              "tools": [{"tool": "send_message", "args": '{"to": "operator"}', "result": "sent"}]}
        with mock.patch.object(judge, "available", return_value=True), \
             mock.patch.object(judge, "ask", return_value={**measurement.JudgingTest.VERDICT, "anticipated": "true"}), \
             mock.patch.object(metrics, "live_episodes", return_value=[ep]), \
             mock.patch.object(metrics, "judgments", return_value=[{"episode_ts": ep["ts"], "delivery_supported": "yes"}]), \
             mock.patch.object(metrics, "_written_files", return_value=""), \
             mock.patch.object(metrics, "_provenance_evidence", return_value=""):
            self.assertEqual(judge_probe.run("m"), 1)
        row, = judge_probe.records("m")
        self.assertIn("caught", row)
        self.assertNotIn("anticipated", row["verdict"])
        self.assertEqual(row["verdict"]["criterion_met"], "yes")

    def test_backfill_keeps_optional_field_without_making_old_rows_incomplete(self):
        ts = "2026-10-13T12:00:00"
        self.episode({"ts": ts, "kind": "live", "intent": {"goal": "output"}, "tools": []})
        path = metrics._judgments_path("m")
        path.parent.mkdir(parents=True)
        old = {"episode_ts": ts, "judge": judge.JUDGE_MODELS[0], "criterion_met": "yes"}
        path.write_text(json.dumps(old) + "\n")
        with mock.patch.object(judge, "ask", return_value={**measurement.JudgingTest.VERDICT, "anticipated": True}), \
             mock.patch.object(metrics, "write_stats"), \
             mock.patch.object(metrics, "_written_files", return_value=""), \
             mock.patch.object(metrics, "_provenance_evidence", return_value=""):
            self.assertEqual(metrics.backfill("m"), 1)
        row, = metrics.judgments("m")
        self.assertIs(row["anticipated"], True)
        row.pop("anticipated")
        self.assertEqual(metrics._missing(row), [])


class AnticipatedTest(unittest.TestCase):
    def test_model_validation_does_not_reject_or_retry_optional_field(self):
        for value in (True, False, None, "true", 1, [], {}):
            with self.subTest(value=value):
                verdict = {**measurement.JudgingTest.VERDICT, "anticipated": value}
                with mock.patch.object(modelcall, "secrets_env", return_value={"OPENROUTER_API_KEY": "k"}), \
                     mock.patch.object(judge, "_log"), \
                     mock.patch("supervisor.cloudllm._send", return_value=_reply(json.dumps(verdict))) as send:
                    out = judge.ask_second(metrics.JUDGE_SYSTEM, "case", metrics.JUDGE_SCHEMA, "test")
                self.assertEqual(send.call_count, 1)
                self.assertEqual(out, verdict if isinstance(value, bool) else measurement.JudgingTest.VERDICT)
                second = dict(verdict)
                with mock.patch.object(judge, "ask_second", return_value=second):
                    stored = metrics.second_opinion("m", "case")
                self.assertNotIn("error", stored)
                self.assertEqual("anticipated" in stored, isinstance(value, bool))
        self.assertEqual(judge._valid(dict(measurement.JudgingTest.VERDICT), metrics.JUDGE_SCHEMA), "")

    def test_trial_arm_field_is_optional_and_normalized(self):
        self.assertEqual(trial._ARM["properties"]["anticipated"], {"type": "boolean"})
        for value in (True, False, None, "true", 1):
            verdict = {"first": {"criterion_met": "yes", "claim_supported": True, "anticipated": value},
                       "second": {"criterion_met": "no", "claim_supported": False}, "winner": "first"}
            with mock.patch.object(trial, "judge_case", return_value="case"), \
                 mock.patch.object(judge, "ask", return_value=verdict):
                out = trial.judge_pair("", {}, {}, "test")
            self.assertEqual(out["winner"], "first")
            self.assertEqual("anticipated" in out["first"], isinstance(value, bool))
            self.assertNotIn("anticipated", out["second"])
            self.assertEqual(judge._valid(out["first"], trial._ARM), "")

    def test_existing_aggregates_and_probation_inputs_are_unchanged(self):
        now = datetime(2026, 11, 6, 12)
        rows = [measurement.valued_j((now - timedelta(days=i + 1)).isoformat(), i % 6,
                         confidence_of=metrics.CONFIDENCE_V2) for i in range(6)]
        marked = [{**r, "anticipated": i % 2 == 0} for i, r in enumerate(rows)]
        with mock.patch.object(metrics, "datetime") as clock:
            clock.now.return_value = now
            for aggregate in (metrics.calibration, metrics.base_rates, metrics.mean_value,
                              metrics.value_shares, metrics.delivery_counts, metrics._summary, metrics._values):
                self.assertEqual(aggregate(rows), aggregate(marked), aggregate.__name__)
        self.assertEqual([metrics.cycle_value(r) for r in rows], [metrics.cycle_value(r) for r in marked])

    def test_stats_and_review_count_only_boolean_fields_in_the_last_14_days(self):
        now = datetime(2026, 11, 6, 12)
        def row(days, **fields):
            return measurement.valued_j((now - timedelta(days=days)).isoformat(), 3, **fields)
        rows = [row(15, anticipated=True), row(14, anticipated=True), row(3),
                row(2, anticipated=False), row(1, anticipated="false"), row(-1, anticipated=True)]
        line = "Anticipated a need (last 14 days): 1/2 judged cycles with the field"
        with mock.patch.object(metrics, "datetime") as clock:
            clock.now.return_value = now
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.assertIn("- " + line, metrics._value_section(rows))
            self.assertNotIn("Anticipated", "\n".join(metrics._value_section([row(1)])))
            self.assertNotIn("Anticipated", "\n".join(metrics._value_section([row(15, anticipated=True)])))
        with mock.patch.object(metrics, "judgments_for_stats", return_value=rows), \
             mock.patch("supervisor.judge_probe.line", return_value="probe"), \
             mock.patch("supervisor.pushes.lines", return_value=[]), \
             mock.patch("supervisor.pushback.lines", return_value=[]), \
             mock.patch("supervisor.consultstats.review_line", return_value=""):
            self.assertIn(line, metrics.review_text(["m"], now=now))
            with mock.patch.object(metrics, "judgments_for_stats", return_value=[row(1)]):
                self.assertNotIn("Anticipated", metrics.review_text(["m"], now=now))
